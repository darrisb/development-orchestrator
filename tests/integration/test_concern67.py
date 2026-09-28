"""Concern 67: safe recovery of a stranded in-flight run.

Concern 66 fixed the defect that stranded ``RUN-20260928-000005`` and proposed
``POST /tasks/{task_id}/resume`` as the operator action. Executed against the
real run, that proposal returned::

    HTTP 409
    {"error":"EntityConflict","detail":"Task TS-109 is not paused"}

which is correct: resume continues *paused* work, and TS-109 was ``VERIFYING``
with a live ``RUNNING`` run whose executor had died. There was no supported
operation for continuing an existing in-flight run, and widening resume to
cover one would have made a single verb mean two things -- the dangerous one
silently, because "continue paused work" cannot acquire a second executor and
"continue work somebody may still be doing" absolutely can.

**What these tests claim.** An operator can take execution of a stranded run;
the run keeps its database id, its ``run_number`` and its external ``RUN-...``
identity; no new ``TaskRun`` appears; attempt 1 stays historical and is neither
repeated nor re-charged; the attempt-2 provider call that was lost before it
became durable is not counted; and exactly one ``RUN_RECOVERY_AUTHORIZED``
event is appended.

**What they refuse.** Terminal, abandoned and superseded runs; a paused task,
which keeps its own operation; a missing checkpoint; a starting commit that is
gone; an integration baseline that diverged; and a run a dispatch is currently
inside.

**One of those refusals was wrong, and concern 68 removed it.** A spent attempt
budget used to be refused here too. It is now reported as
``recovery_mode="settlement_only"`` and recovered, because the fix loop still
owes such a run a deterministic ``ESCALATED`` / ``RETRY_EXHAUSTED`` ending that
costs no provider call. The two tests below that used to assert the refusal now
assert the mode instead; ``tests/integration/test_concern68.py`` owns the rest
of that behaviour, including the proof that no model is asked anything.

**Where the real guarantee is.** Not in any of those refusals, which are
policy, but in the fencing token: recovery increments
``task_runs.execution_generation`` in one guarded ``UPDATE``, and every durable
checkpoint quotes its own generation back to the database under the row lock.
So the tests that matter most are the ones where the *old* executor comes back
-- after the transfer, and with a model answer in its hand -- and cannot
persist. That is the property that makes it unnecessary to guess whether the
old executor is dead, which is the guess every weak strandedness signal
(elapsed time, absent PID, quiet log) is really making.

**Where the races are tested for real.** SQLite serializes writers, so two
transactions contending for one row is not a thing it can demonstrate. The
concurrency section uses a scratch PostgreSQL database, two threads and a
barrier -- no sleeps -- and is skipped rather than failed when no server is
available, the same bargain ``test_db_locking.py`` and ``test_concern64.py``
strike.
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.agents.fix_loop import durable_checkpoint
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import (
    TaskRunRow,
    WorkflowCheckpointRow,
)
from apps.orchestrator.db.session import create_db_engine, reset_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    ModelPurpose,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.errors import (
    AbandonedRunError,
    RunOwnershipLostError,
)
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import (
    Model,
    ModelRun,
    PauseRequest,
    Project,
    RunEvent,
    Task,
    TaskLimits,
)
from apps.orchestrator.main import create_app
from apps.orchestrator.repositories import (
    ModelRepository,
    ModelRunRepository,
    PauseRequestRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services import artifact_store
from apps.orchestrator.services.abandon import abandon_run
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound
from apps.orchestrator.services.pauses import resume_task
from apps.orchestrator.services.run_recovery import (
    RecoveryMode,
    assess_recoverability,
    recover_run,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import workspace_path
from tests.conftest import run_git

# =============================================================================
# 0. The durable signature of RUN-20260928-000005, rebuilt honestly.
# =============================================================================
#
# Rebuilt rather than mocked, and rebuilt the way it actually happened: the run
# is opened by ``create_run``, the task is walked through the state machine to
# VERIFYING, the attempt-1 CODE call is recorded as ``record_model_call`` would
# record it, and the events are the ones the live run actually wrote. The one
# thing deliberately *not* created is an attempt-2 ``model_runs`` row, because
# that is precisely what the pre-concern-66 defect destroyed -- and a fixture
# that invented one would be testing the wrong run.


@dataclass
class Signature:
    """A private database, a real repository, and the run under test."""

    path: Path
    repository: Path
    settings: Settings
    factory: sessionmaker
    project_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    model_id: uuid.UUID

    def rebuilt_engine(self) -> Engine:
        """A second engine, as a restarted process would build."""
        return create_db_engine(f"sqlite:///{self.path}")

    def rebuilt_factory(self) -> sessionmaker:
        return sessionmaker(bind=self.rebuilt_engine(), expire_on_commit=False)

    def run(self, session: Session):
        return TaskRunRepository(session).get(self.run_id)


def _build_repository(root: Path) -> Path:
    repo = root / "tracestack"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "nav.py").write_text("def navigate():\n    return None\n", "utf-8")
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    # The accepted cumulative baseline, which is where a task worktree starts.
    run_git(repo, "branch", INTEGRATION_BRANCH)
    return repo


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=root / "data",
        worktree_root=root / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
    )


def _checkpoint(session: Session, run_id: uuid.UUID, checkpoint_id: str) -> None:
    """A workflow checkpoint row, as the saver writes one.

    The bytes are opaque here on purpose: recoverability asks whether durable
    workflow state *exists* for the thread, and inventing a decodable payload
    would test this file's idea of LangGraph's serializer rather than the
    question the service actually asks.
    """
    session.add(
        WorkflowCheckpointRow(
            thread_id=str(run_id),
            checkpoint_ns="",
            checkpoint_id=checkpoint_id,
            parent_checkpoint_id=None,
            checkpoint_type="msgpack",
            checkpoint=b"\x80",
            metadata_type="msgpack",
            checkpoint_metadata=b"\x80",
        )
    )
    session.flush()


def _seed_signature(
    factory: sessionmaker, repository: Path, settings: Settings
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """The RUN-20260928-000005 shape, seeded into whatever database it is given.

    Shared by the SQLite fixture and the PostgreSQL concurrency fixture so the
    run two threads contend over is provably the same run the rest of this file
    is about, rather than a simplified stand-in that happens to be easier to
    build.
    """
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="TraceStack",
                repository_path=str(repository),
                default_branch="main",
                worker_profile=WorkerProfile.NODE,
            )
        )
        model = ModelRepository(session).add(
            Model(
                provider="openai_compatible",
                model_name="qwen3-coder-30b",
                role=ModelRole.CODER,
                endpoint="http://localhost:11434/v1",
            )
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-109",
                title="Keep only the entries from one navigation source",
                complexity=Complexity.MEDIUM,
                limits=TaskLimits(max_attempts=3, max_review_cycles=2),
            )
        )
        TaskRepository(session).transition(task.id, TaskStatus.READY)

        # RUN-20260928-000004: the earlier run, terminal FAILED. Present because
        # the real task has two runs and "the task's expected in-flight run" is a
        # claim about a set, not about one row.
        first = create_run(session, task.id)
        TaskRunRepository(session).finish(first.id, RunStatus.FAILED)
        TaskRepository(session).transition(task.id, TaskStatus.CODING)
        TaskRepository(session).transition(task.id, TaskStatus.VERIFYING)

    starting = run_git(repository, "rev-parse", INTEGRATION_BRANCH).strip()

    with factory.begin() as session:
        task_row = TaskRepository(session).get(task.id)
        assert task_row.status is TaskStatus.VERIFYING
        # A run is created from READY, which is the only state the service
        # permits; the task is put back where the live one is afterwards.
        TaskRepository(session).transition(task.id, TaskStatus.FAILED)
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        run = create_run(session, task.id)
        artifact_store.ensure_run_id(session, run.id)
        branch = (
            "agent/TS-109-keep-only-the-entries-from-one-navigation-source-run2"
        )
        TaskRunRepository(session).update_fields(
            run.id,
            status=RunStatus.RUNNING,
            attempt_number=1,
            review_cycle=0,
            candidate_commit=None,
            starting_commit=starting,
            branch_name=branch,
        )
        TaskRepository(session).transition(task.id, TaskStatus.CODING)
        TaskRepository(session).transition(task.id, TaskStatus.VERIFYING)

        # The one durable CODE call: attempt 1, cycle 1, SUCCEEDED.
        ModelRunRepository(session).add(
            ModelRun(
                task_run_id=run.id,
                model_id=model.id,
                purpose=ModelPurpose.CODE,
                status=RunStatus.SUCCEEDED,
                attempt=1,
                review_cycle=1,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
        events = RunEventRepository(session)
        for event_type, payload in (
            (RunEventType.CODING_STARTED, {"is_fix_attempt": False}),
            (RunEventType.BUILD_FAILED, {"failure_reason": "BUILD_FAILED"}),
            (
                RunEventType.OUTCOME_RECORDED,
                {"outcome": "in_progress", "review_cycles": 0, "attempts": 1},
            ),
        ):
            events.append(
                RunEvent(
                    task_run_id=run.id,
                    project_id=project.id,
                    task_id=task.id,
                    event_type=event_type,
                    attempt=1,
                    payload=payload,
                )
            )
        _checkpoint(session, run.id, "1f0-checkpoint-4")
        run_id = run.id

    # The worktree the live run still has, at its starting commit.
    worktree = workspace_path(project.id, "TS-109", 2, settings=settings)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(repository, "worktree", "add", "-b", branch, str(worktree), starting)
    return project.id, task.id, run_id, model.id


@pytest.fixture
def signature(tmp_path: Path) -> Iterator[Signature]:
    repository = _build_repository(tmp_path)
    settings = _settings(tmp_path)
    path = tmp_path / "concern67.db"
    engine = create_db_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    project_id, task_id, run_id, model_id = _seed_signature(
        factory, repository, settings
    )
    try:
        yield Signature(
            path=path,
            repository=repository,
            settings=settings,
            factory=factory,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            model_id=model_id,
        )
    finally:
        engine.dispose()


def test_the_fixture_is_the_run_000005_signature(signature: Signature):
    """Guard the fixture itself: every claim below is about this exact shape."""
    with signature.factory() as session:
        run = signature.run(session)
        task = TaskRepository(session).get(signature.task_id)
        calls = ModelRunRepository(session).list_for_run(run.id)
        events = RunEventRepository(session).list_for_run(run.id)
        runs_of_task = TaskRunRepository(session).list_for_task(signature.task_id)

    assert run.status is RunStatus.RUNNING
    assert run.run_number == 2
    assert run.attempt_number == 1
    assert run.review_cycle == 0
    assert run.candidate_commit is None
    assert run.external_run_id is not None
    assert run.execution_generation == 0
    assert run.execution_owner is None
    assert task.status is TaskStatus.VERIFYING
    assert task.limits.max_attempts == 3
    assert [(c.purpose, c.attempt) for c in calls] == [(ModelPurpose.CODE, 1)]
    types = {e.event_type for e in events}
    assert RunEventType.BUILD_FAILED in types
    assert RunEventType.OUTCOME_RECORDED in types
    # Two historical runs, exactly as TS-109 has: one terminal FAILED and this
    # one, and nothing anywhere below is allowed to make that three.
    assert len(runs_of_task) == 2


# =============================================================================
# 1. Reconstruction: what the recovered run would actually do next.
# =============================================================================


def test_the_run_000005_signature_reconstructs_to_attempt_two(signature: Signature):
    """The whole arithmetic, stated as one number.

    Attempt 1 was made: a CODE call is on the record and a ``CODING_STARTED``
    event is stamped with it, so the number is spent and cannot be handed back.
    The attempt-2 provider call that was lost before it became durable left
    neither, so it is not spent. The next durable coder execution is therefore
    attempt 2 -- not 1 (which would repeat work and overwrite its evidence) and
    not 3 (which would charge the task for a call nothing recorded).
    """
    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )

    assert report.recoverable is True
    assert report.next_attempt == 2
    assert report.attempts_started == 1
    assert report.reviews_completed == 0
    assert report.max_attempts == 3


def test_the_lost_provider_call_is_not_counted(signature: Signature):
    """A call that left no durable trace is a call this system did not make.

    Concern 66's defect destroyed the attempt-2 turn *and* its would-be
    ``model_runs`` row, because the row was written in the transaction the
    database killed. Counting it anyway would mean guessing, from the absence
    of evidence, that something happened -- and it would spend a third of the
    task's attempt budget on the guess.
    """
    with signature.factory() as session:
        before = ModelRunRepository(session).list_for_run(signature.run_id)
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )

    assert len(before) == 1
    assert report.attempts_started == 1
    assert report.next_attempt == 2


def test_attempt_accounting_stays_inside_max_attempts(signature: Signature):
    """Recovery does not widen the budget, and says so when it is spent.

    Concern 68 changed the *consequence* of a spent budget, not the arithmetic:
    the fourth attempt of a three-attempt task is still not executable, and the
    assessment still says so. What it no longer does is call the run
    unrecoverable, because settling it is neither an attempt nor optional.
    """
    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
        assert report.next_attempt <= report.max_attempts
        assert report.recovery_mode is RecoveryMode.CONTINUE

    # Charge the remaining attempts the way they are really charged -- by
    # recording the calls that made them -- and the answer changes.
    with signature.factory.begin() as session:
        for attempt in (2, 3):
            ModelRunRepository(session).add(
                ModelRun(
                    task_run_id=signature.run_id,
                    model_id=signature.model_id,
                    purpose=ModelPurpose.FIX,
                    status=RunStatus.SUCCEEDED,
                    attempt=attempt,
                    review_cycle=1,
                )
            )

    with signature.factory() as session:
        exhausted = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
    assert exhausted.next_attempt == 4
    assert exhausted.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
    assert exhausted.recoverable is True
    assert exhausted.refusals == ()


def test_the_review_cycle_accounting_is_preserved(signature: Signature):
    """A cycle is charged when a reviewer answers, and none has."""
    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
        run_before = signature.run(session)

    assert report.reviews_completed == 0
    assert report.review_cycle == 0

    with signature.factory.begin() as session:
        recover_run(
            session,
            signature.run_id,
            reason="the executor died mid-turn",
            requested_by="operator",
            settings=signature.settings,
        )

    with signature.factory() as session:
        run_after = signature.run(session)
    assert run_after.review_cycle == run_before.review_cycle == 0


# =============================================================================
# 2. What recovery does to the run, and what it leaves alone.
# =============================================================================


@pytest.fixture
def recovered(signature: Signature):
    with signature.factory.begin() as session:
        authorization = recover_run(
            session,
            signature.run_id,
            reason="the executor died mid-turn and left the run in flight",
            requested_by="operator",
            settings=signature.settings,
        )
    return authorization


def test_recovery_operates_on_the_existing_task_run(
    signature: Signature, recovered
):
    assert recovered.run.id == signature.run_id
    with signature.factory() as session:
        run = signature.run(session)
    assert run.id == signature.run_id
    assert run.status is RunStatus.RUNNING


def test_recovery_creates_no_new_task_run(signature: Signature, recovered):
    """The load-bearing negative: a recovery is not a retry.

    Counted from a rebuilt engine, because the point is what is in the
    database and not what this process happens to remember.
    """
    rebuilt = signature.rebuilt_engine()
    try:
        with rebuilt.connect() as connection:
            total = connection.execute(
                text("SELECT count(*) FROM task_runs")
            ).scalar_one()
            for_task = connection.execute(
                text("SELECT count(*) FROM task_runs WHERE task_id = :t"),
                {"t": signature.task_id.hex},
            ).scalar_one()
    finally:
        rebuilt.dispose()
    assert total == 2
    assert for_task == 2


def test_recovery_preserves_the_run_number(signature: Signature, recovered):
    with signature.factory() as session:
        run = signature.run(session)
    assert run.run_number == 2


def test_recovery_preserves_the_external_run_identity(
    signature: Signature, recovered
):
    with signature.factory() as session:
        run = signature.run(session)
    assert run.external_run_id == recovered.report.external_run_id
    assert run.external_run_id is not None


def test_historical_attempt_one_evidence_is_unchanged(signature: Signature):
    """Every row attempt 1 left, before and after, byte for byte."""
    with signature.factory() as session:
        calls_before = [
            (c.id, c.purpose, c.attempt, c.status)
            for c in ModelRunRepository(session).list_for_run(signature.run_id)
        ]
        events_before = [
            (e.id, e.event_type, e.attempt, tuple(sorted(e.payload.items())))
            for e in RunEventRepository(session).list_for_run(signature.run_id)
        ]

    with signature.factory.begin() as session:
        recover_run(
            session,
            signature.run_id,
            reason="stranded",
            settings=signature.settings,
        )

    with signature.factory() as session:
        calls_after = [
            (c.id, c.purpose, c.attempt, c.status)
            for c in ModelRunRepository(session).list_for_run(signature.run_id)
        ]
        events_after = [
            (e.id, e.event_type, e.attempt, tuple(sorted(e.payload.items())))
            for e in RunEventRepository(session).list_for_run(signature.run_id)
        ]

    assert calls_after == calls_before
    # The only difference is the one appended event; nothing earlier moved.
    assert events_after[: len(events_before)] == events_before


def test_attempt_one_is_not_repeated_and_the_row_is_not_bumped(
    signature: Signature, recovered
):
    """``attempt_number`` is the row's floor, and recovery does not touch it.

    The number the *next* execution uses comes from the reconstruction, not
    from an increment here: incrementing the row would charge the attempt
    before anything asked a model for anything, which is the mistake the whole
    of ``loop_recovery`` exists to avoid.
    """
    with signature.factory() as session:
        run = signature.run(session)
    assert run.attempt_number == 1
    assert recovered.next_attempt == 2


def test_the_candidate_state_is_neither_duplicated_nor_integrated(
    signature: Signature, recovered
):
    with signature.factory() as session:
        run = signature.run(session)
    assert run.candidate_commit is None
    assert (
        run_git(signature.repository, "rev-parse", INTEGRATION_BRANCH).strip()
        == run.starting_commit
    )


def test_recovery_appends_exactly_one_audit_event(signature: Signature, recovered):
    with signature.factory() as session:
        events = [
            e
            for e in RunEventRepository(session).list_for_run(signature.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["reason"].startswith("the executor died")
    assert payload["requested_by"] == "operator"
    assert payload["previous_generation"] == 0
    assert payload["generation"] == 1
    assert payload["next_attempt"] == 2
    assert payload["run_number"] == 2
    assert payload["external_run_id"] is not None


def test_recovery_takes_the_next_execution_generation(
    signature: Signature, recovered
):
    with signature.factory() as session:
        run = signature.run(session)
    assert recovered.previous_generation == 0
    assert recovered.generation == 1
    assert run.execution_generation == 1
    assert run.execution_owner == recovered.owner


@pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
def test_a_reason_is_required(signature: Signature, reason: str):
    with signature.factory() as session, pytest.raises(ValueError, match="reason is required"):
        recover_run(
            session, signature.run_id, reason=reason, settings=signature.settings
        )


def test_an_unknown_run_is_not_found(signature: Signature):
    with signature.factory() as session, pytest.raises(EntityNotFound):
        recover_run(
            session,
            uuid.uuid4(),
            reason="nothing there",
            settings=signature.settings,
        )


# =============================================================================
# 3. Refusals. Every one of them is a statement about durable evidence.
# =============================================================================


def _refusal_names(signature: Signature, **kwargs) -> set[str]:
    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings, **kwargs
        )
    return {check.name for check in report.refusals}


def _expect_conflict(signature: Signature, match: str, **kwargs) -> str:
    with signature.factory() as session, pytest.raises(EntityConflict, match=match) as raised:
        recover_run(
            session,
            signature.run_id,
            reason="try anyway",
            settings=signature.settings,
            **kwargs,
        )
    return str(raised.value)


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED])
def test_a_terminal_run_refuses_recovery(signature: Signature, status: RunStatus):
    with signature.factory.begin() as session:
        TaskRunRepository(session).finish(signature.run_id, status)

    assert "run_in_flight" in _refusal_names(signature)
    _expect_conflict(signature, "not recoverable")


def test_an_abandoned_run_refuses_recovery(signature: Signature):
    """Abandonment is terminal, and recovery is not a way around it.

    Concern 64 made a person's decision to stop a run durable. A recovery that
    could pick one up again would undo that decision silently, which is the
    exact failure -- "silent resurrection" -- concern 64 was written about.
    """
    with signature.factory.begin() as session:
        abandon_run(session, signature.run_id, reason="began under a stale image")

    refusals = _refusal_names(signature)
    assert "run_not_abandoned" in refusals
    assert "run_in_flight" in refusals
    detail = _expect_conflict(signature, "not recoverable")
    assert "abandoned" in detail


def test_a_superseded_run_refuses_recovery(signature: Signature):
    """Two in-flight runs of one task is a state nobody should build on.

    The recovery would be of a run the task has already moved past, and
    continuing it would put two executors on one task rather than on one run.
    """
    with signature.factory.begin() as session:
        TaskRepository(session).transition(signature.task_id, TaskStatus.FAILED)
        TaskRepository(session).transition(signature.task_id, TaskStatus.READY)
        create_run(session, signature.task_id)
        TaskRepository(session).transition(signature.task_id, TaskStatus.CODING)
        TaskRepository(session).transition(signature.task_id, TaskStatus.VERIFYING)

    assert "not_superseded" in _refusal_names(signature)
    detail = _expect_conflict(signature, "not recoverable")
    assert "superseded" in detail


def test_a_paused_task_keeps_the_resume_operation(signature: Signature):
    """The architectural refusal, and the one this concern exists because of.

    A paused task is not a stranded run. It has an operation of its own, and
    recovery says so by name rather than quietly doing resume's job with
    ownership semantics resume never had.
    """
    with signature.factory.begin() as session:
        PauseRequestRepository(session).add(
            PauseRequest(
                project_id=signature.project_id,
                task_id=signature.task_id,
                reason="operator",
            )
        )
        TaskRepository(session).transition(signature.task_id, TaskStatus.PAUSED)

    refusals = _refusal_names(signature)
    assert "task_not_paused" in refusals
    assert "no_pause_in_force" in refusals
    detail = _expect_conflict(signature, "not recoverable")
    assert "/tasks/{task_id}/resume" in detail

    # And resume still works on it, unchanged, with the run left in flight.
    with signature.factory.begin() as session:
        task = resume_task(session, signature.task_id)
    assert task.status is TaskStatus.PAUSED or task.status in {
        TaskStatus.READY,
        TaskStatus.PENDING,
    }
    with signature.factory() as session:
        assert signature.run(session).status is RunStatus.RUNNING
        assert signature.run(session).execution_generation == 0


def test_a_pause_in_force_refuses_recovery(signature: Signature):
    with signature.factory.begin() as session:
        PauseRequestRepository(session).add(
            PauseRequest(
                project_id=signature.project_id,
                task_id=signature.task_id,
                reason="hold everything",
            )
        )

    assert "no_pause_in_force" in _refusal_names(signature)
    _expect_conflict(signature, "release it before recovering")


def test_a_missing_checkpoint_refuses_recovery(signature: Signature):
    """No durable workflow state is no execution to reconstruct.

    Recovery continues an execution; it does not invent one. A run whose graph
    thread was never written has nothing to continue from, and saying so is
    better than starting it over under the same run identity.
    """
    with signature.factory.begin() as session:
        session.execute(
            text("DELETE FROM workflow_checkpoints WHERE thread_id = :t"),
            {"t": str(signature.run_id)},
        )

    assert "checkpoint_exists" in _refusal_names(signature)
    _expect_conflict(signature, "no durable workflow checkpoint")


def test_a_missing_starting_commit_refuses_recovery(signature: Signature):
    """Git history is never silently repaired.

    The starting commit is what every diff, every scope decision and every
    delivery of this run is measured against. If it is gone, the honest answer
    is a refusal an operator has to look at.
    """
    with signature.factory.begin() as session:
        TaskRunRepository(session).update_fields(
            signature.run_id, starting_commit="0" * 40
        )

    assert "starting_commit_exists" in _refusal_names(signature)
    _expect_conflict(signature, "not readable")


def test_a_diverged_integration_baseline_refuses_recovery(signature: Signature):
    """The explicit, deterministic baseline policy -- never a silent pass.

    "Compatible" means the accepted baseline still contains the commit this run
    started from. An integration branch that was moved somewhere else is a
    divergence, and continuing would build on a tree the orchestrator never
    accepted. Note that a baseline which merely *advanced* is not a divergence:
    it still contains the starting commit, and that case is reported rather
    than refused.
    """
    run_git(signature.repository, "checkout", "--quiet", "main")
    (signature.repository / "src" / "other.py").write_text("x = 1\n", "utf-8")
    run_git(signature.repository, "add", "-A")
    run_git(signature.repository, "commit", "--quiet", "-m", "Divergent history")
    run_git(signature.repository, "checkout", "--quiet", "--orphan", "elsewhere")
    run_git(signature.repository, "commit", "--quiet", "-m", "Unrelated root")
    run_git(signature.repository, "branch", "-f", INTEGRATION_BRANCH, "HEAD")

    assert "integration_baseline_compatible" in _refusal_names(signature)
    detail = _expect_conflict(signature, "does not contain")
    assert "never accepted" in detail


def test_an_advanced_integration_baseline_is_reported_not_refused(
    signature: Signature,
):
    run_git(signature.repository, "checkout", "--quiet", INTEGRATION_BRANCH)
    (signature.repository / "src" / "later.py").write_text("y = 2\n", "utf-8")
    run_git(signature.repository, "add", "-A")
    run_git(signature.repository, "commit", "--quiet", "-m", "A later task landed")

    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
    assert report.recoverable is True
    assert report.integration_advanced_since_start is True


def test_a_missing_worktree_refuses_recovery(signature: Signature):
    """Recovery does not rebuild Git state behind an operator's back."""
    worktree = workspace_path(
        signature.project_id, "TS-109", 2, settings=signature.settings
    )
    run_git(signature.repository, "worktree", "remove", "--force", str(worktree))

    assert "worktree_usable" in _refusal_names(signature)
    _expect_conflict(signature, "worktree")


def test_a_healthy_active_owner_refuses_recovery(signature: Signature):
    """Requirement 10, and the reason ``execution_owner`` exists at all.

    A run a dispatch is inside is not a stranded run, and nothing else in the
    durable record can tell the difference -- ``RUNNING`` means "somebody
    started this", not "somebody is still doing it". The owner stamp is set on
    dispatch and cleared on the way out, so it is a held/not-held marker and
    never a liveness claim; that is why the refusal names the override rather
    than pretending to know.
    """
    with signature.factory.begin() as session:
        acquired = TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="live-dispatch"
        )
        assert acquired is not None

    assert "execution_ownership_available" in _refusal_names(signature)
    detail = _expect_conflict(signature, "has held this run")
    assert "override_active_owner" in detail

    # And the override is an explicit decision, recorded as one.
    assert "execution_ownership_available" not in _refusal_names(
        signature, override_active_owner=True
    )
    with signature.factory.begin() as session:
        authorization = recover_run(
            session,
            signature.run_id,
            reason="the holding process was killed",
            override_active_owner=True,
            settings=signature.settings,
        )
    assert authorization.generation == 2
    with signature.factory() as session:
        events = [
            e
            for e in RunEventRepository(session).list_for_run(signature.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
    assert len(events) == 1
    assert events[0].payload["override_active_owner"] is True
    assert events[0].payload["previous_execution_owner"] == "live-dispatch"


def test_the_eligibility_read_mutates_nothing(signature: Signature):
    """The dry run is a dry run, asserted rather than asserted-in-a-docstring."""
    rebuilt = signature.rebuilt_engine()

    def snapshot() -> tuple:
        with rebuilt.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT status, execution_generation, execution_owner, "
                        "attempt_number, review_cycle, candidate_commit "
                        "FROM task_runs WHERE id = :i"
                    ),
                    {"i": signature.run_id.hex},
                ).one(),
                connection.execute(
                    text("SELECT count(*) FROM run_events")
                ).scalar_one(),
                connection.execute(
                    text("SELECT count(*) FROM task_runs")
                ).scalar_one(),
            )

    try:
        before = snapshot()
        with signature.factory() as session:
            assess_recoverability(
                session, signature.run_id, settings=signature.settings
            )
            assess_recoverability(
                session, signature.run_id, settings=signature.settings
            )
        assert snapshot() == before
    finally:
        rebuilt.dispose()


# =============================================================================
# 4. Fencing. The part that is a guarantee rather than a policy.
# =============================================================================


def test_the_old_execution_owner_cannot_persist_after_recovery(
    signature: Signature,
):
    """The invariant, in the shape the concern states it.

    The old owner holds generation N and is off doing external work. Recovery
    takes N+1. The old owner comes back and tries to make its turn durable --
    and the barrier, which is a locked predicate and not a read, refuses it.
    Its transaction is never committed, so nothing it did lands on top of the
    executor that replaced it.
    """
    with signature.factory.begin() as session:
        old = TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="old-dispatch"
        )
    held = old.execution_generation
    assert held == 1

    # The old owner's session, holding its turn, about to commit.
    old_session = signature.factory()
    try:
        checkpoint = durable_checkpoint(
            old_session,
            signature.run_id,
            old_session.commit,
            expected_generation=held,
        )
        # Before the transfer it is allowed through.
        checkpoint()

        with signature.factory.begin() as session:
            recover_run(
                session,
                signature.run_id,
                reason="the old dispatch is gone",
                override_active_owner=True,
                settings=signature.settings,
            )

        with pytest.raises(RunOwnershipLostError) as raised:
            checkpoint()
    finally:
        old_session.rollback()
        old_session.close()

    assert raised.value.held == 1
    assert raised.value.current == 2

    with signature.factory() as session:
        run = signature.run(session)
    assert run.execution_generation == 2
    assert run.status is RunStatus.RUNNING


def test_an_outstanding_model_answer_is_rejected_after_the_transfer(
    signature: Signature,
):
    """Recovery while the old inference is still outstanding.

    This is the ordering that actually happens: concern 66 made the old
    executor commit *before* its provider call and hold no transaction across
    it, so the recovery lands in the middle of the wait. The answer arrives
    afterwards, the executor opens a fresh transaction to record it, and the
    post-call fence is where it is stopped -- before ``record_model_call``'s
    row can be committed, not after.
    """
    with signature.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="old-dispatch"
        )

    calls_before = _count(signature, "model_runs")

    old_session = signature.factory()
    try:
        # --- the old executor is waiting on the provider; it holds nothing ---
        with signature.factory.begin() as session:
            recover_run(
                session,
                signature.run_id,
                reason="stranded mid-inference",
                override_active_owner=True,
                settings=signature.settings,
            )

        # --- the answer comes back; the executor tries to record it ---
        ModelRunRepository(old_session).add(
            ModelRun(
                task_run_id=signature.run_id,
                model_id=signature.model_id,
                purpose=ModelPurpose.FIX,
                status=RunStatus.SUCCEEDED,
                attempt=2,
                review_cycle=1,
            )
        )
        checkpoint = durable_checkpoint(
            old_session,
            signature.run_id,
            old_session.commit,
            expected_generation=1,
        )
        with pytest.raises(RunOwnershipLostError):
            checkpoint()
    finally:
        old_session.rollback()
        old_session.close()

    # The late answer left nothing behind, so the reconstruction is unchanged.
    assert _count(signature, "model_runs") == calls_before
    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
    assert report.next_attempt == 2


def test_the_fence_distinguishes_ownership_loss_from_abandonment(
    signature: Signature,
):
    """Three different faults, three different types.

    An operator stopped the run; an operator moved it to another executor; the
    run finished on its own. Collapsing any two of these would tell whoever
    reads the log the wrong thing about what happened.
    """
    with signature.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="dispatch"
        )

    with signature.factory() as session:
        runs = TaskRunRepository(session)
        runs.require_in_flight(signature.run_id, expected_generation=1)
        with pytest.raises(RunOwnershipLostError):
            runs.require_in_flight(signature.run_id, expected_generation=0)

    with signature.factory.begin() as session:
        abandon_run(session, signature.run_id, reason="stop")

    with signature.factory() as session, pytest.raises(AbandonedRunError):
        TaskRunRepository(session).require_in_flight(
            signature.run_id, expected_generation=1
        )


def test_a_release_by_a_fenced_owner_clears_nothing(signature: Signature):
    """An old dispatch unwinding must not strip the new one's ownership."""
    with signature.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="old-dispatch"
        )
    with signature.factory.begin() as session:
        authorization = recover_run(
            session,
            signature.run_id,
            reason="taking it",
            override_active_owner=True,
            settings=signature.settings,
        )

    with signature.factory.begin() as session:
        released = TaskRunRepository(session).release_execution(
            signature.run_id, owner="old-dispatch"
        )
    assert released is False

    with signature.factory() as session:
        run = signature.run(session)
    assert run.execution_owner == authorization.owner
    assert run.execution_generation == 2


def test_duplicate_recovery_requests_cannot_create_two_executors(
    signature: Signature,
):
    """Sequentially first, where the answer must still be one owner.

    The second request is not idempotent and does not pretend to be: it finds a
    run that has moved on, and is refused on the specific ground that another
    recovery won. Two events would mean two executors were told they may
    continue, which is the thing that must never happen.
    """
    with signature.factory.begin() as session:
        first = recover_run(
            session,
            signature.run_id,
            reason="first",
            settings=signature.settings,
        )

    with signature.factory() as session, pytest.raises(
        EntityConflict, match="held by dispatch|not recoverable"
    ):
        recover_run(
            session,
            signature.run_id,
            reason="second",
            settings=signature.settings,
        )

    with signature.factory() as session:
        run = signature.run(session)
        events = [
            e
            for e in RunEventRepository(session).list_for_run(signature.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
    assert run.execution_generation == 1
    assert run.execution_owner == first.owner
    assert len(events) == 1


def test_a_stale_assessment_loses_the_acquisition(signature: Signature):
    """The generation the request quoted is the predicate it is judged on.

    Two operators can assess the same run at the same generation. Whichever
    acquisition commits second finds the number replaced and is told exactly
    that, rather than incrementing on top of the winner.
    """
    with signature.factory() as session:
        stale = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
    assert stale.execution_generation == 0

    with signature.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="somebody-else"
        )
        TaskRunRepository(session).release_execution(
            signature.run_id, owner="somebody-else"
        )

    with signature.factory() as session:
        runs = TaskRunRepository(session)
        assert (
            runs.acquire_execution(
                signature.run_id, owner="loser", expected_generation=0
            )
            is None
        )
        assert (
            runs.acquire_execution(
                signature.run_id, owner="winner", expected_generation=1
            )
            is not None
        )


def _count(signature: Signature, table: str) -> int:
    rebuilt = signature.rebuilt_engine()
    try:
        with rebuilt.connect() as connection:
            return connection.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed literals
            ).scalar_one()
    finally:
        rebuilt.dispose()


# =============================================================================
# 5. End to end: a run really strands, and is really recovered and finished.
# =============================================================================
#
# The strand is produced rather than staged: the coder's second call raises
# ModelTimeout exactly as the live provider did, the workflow unwinds, and the
# run is left RUNNING with its task mid-flight. What happens next is the whole
# of concern 67 -- an operator takes ownership, the *same* run continues, and
# the fix loop reaches a verdict. Nothing here mocks recovery; the only thing
# scripted is what the models say.


@pytest.fixture
def e2e(tmp_path: Path):
    from apps.orchestrator.domain.verification import VerificationProfile
    from apps.orchestrator.providers.errors import ModelTimeout
    from apps.orchestrator.workflow import WorkflowRunner
    from tests.integration.test_fix_loop import (
        BROKEN,
        STUB,
        WORKING,
        ScriptedModel,
        _code,
        _review,
        reviewer,
    )

    repository = tmp_path / "project"
    (repository / "src").mkdir(parents=True)
    (repository / "tools").mkdir()
    (repository / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (repository / "tools" / "test.py").write_text(
        "assert 'return target' in open('src/nav.py').read()\n", encoding="utf-8"
    )
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")

    engine = create_db_engine(f"sqlite:///{tmp_path / 'e2e.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="fixture",
                repository_path=str(repository),
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=("python3 tools/test.py",)),
            )
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-109",
                title="implement navigation",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
            )
        )
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        run = create_run(session, task.id)

    # Attempt 1 writes code the tests reject; attempt 2 times out exactly as
    # RUN-20260928-000005's did. Then the run is stranded.
    stranding = WorkflowRunner(
        factory,
        coder=ScriptedModel(
            _code(BROKEN),
            ModelTimeout(
                    "coder request timed out after 600s", timeout_seconds=600.0
                ),
        ),
        reviewer=reviewer(_review(taskId="TS-109")),
        settings=settings,
    )
    continuing = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-109")),
        settings=settings,
    )
    yield (
        factory,
        settings,
        repository,
        project.id,
        task.id,
        run.id,
        stranding,
        continuing,
    )
    engine.dispose()


@pytest.mark.asyncio
async def test_a_stranded_run_is_recovered_and_finishes_as_the_same_run(e2e):
    factory, settings, repository, project_id, task_id, run_id, stranding, continuing = e2e

    with contextlib.suppress(Exception):
        await stranding.run(run_id)
    await stranding.aclose()

    with factory() as session:
        stranded = TaskRunRepository(session).get(run_id)
        calls = ModelRunRepository(session).list_for_run(run_id)
        checkpoints = session.scalar(
            select(func.count()).select_from(WorkflowCheckpointRow)
        )
    # The live signature: in flight, ownership released by the unwind, the
    # attempt-1 call durable, and the timed-out attempt-2 call recorded as the
    # failure it was (concern 66) rather than lost.
    assert stranded.status is RunStatus.RUNNING
    assert stranded.execution_owner is None
    assert stranded.execution_generation == 1
    assert checkpoints and checkpoints > 0
    assert [c.attempt for c in calls if c.status is RunStatus.SUCCEEDED] == [1]

    with factory() as session:
        report = assess_recoverability(session, run_id, settings=settings)
    assert report.recoverable is True

    with factory.begin() as session:
        authorization = recover_run(
            session,
            run_id,
            reason="the attempt-2 provider call outlived its transaction",
            requested_by="operator",
            settings=settings,
        )
    assert authorization.generation == 2

    state = await continuing.run(
        run_id, acquired=(authorization.owner, authorization.generation)
    )
    await continuing.aclose()

    with factory() as session:
        run = TaskRunRepository(session).get(run_id)
        runs = TaskRunRepository(session).list_for_task(task_id)
        task = TaskRepository(session).get(task_id)
        events = RunEventRepository(session).list_for_run(run_id)

    # The same run, finished. Not a new one, not a new number, not a new name.
    assert len(runs) == 1
    assert run.id == run_id
    assert run.run_number == 1
    assert run.external_run_id == stranded.external_run_id
    assert state["outcome"] == "COMPLETED"
    assert task.status is TaskStatus.COMPLETE
    assert run.execution_owner is None
    assert (
        len(
            [
                e
                for e in events
                if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_a_second_dispatch_of_an_owned_run_is_refused(e2e):
    """A healthy active run never acquires a second executor.

    Dispatch is where ownership is taken, so the refusal is the acquisition's
    own predicate rather than a check somebody remembered to write at the top
    of ``run``.
    """
    factory, settings, _repo, _project_id, _task_id, run_id, stranding, continuing = e2e
    await stranding.aclose()

    with factory.begin() as session:
        TaskRunRepository(session).acquire_execution(run_id, owner="live-dispatch")

    with pytest.raises(EntityConflict, match="already held by dispatch"):
        await continuing.run(run_id)
    await continuing.aclose()


@pytest.mark.asyncio
async def test_a_dispatch_releases_ownership_on_the_way_out(e2e):
    factory, settings, _repo, _project_id, _task_id, run_id, stranding, continuing = e2e
    await continuing.aclose()

    with contextlib.suppress(Exception):
        await stranding.run(run_id)
    await stranding.aclose()

    with factory() as session:
        run = TaskRunRepository(session).get(run_id)
    # Released even though the dispatch ended in an exception: a run nobody is
    # inside must not look like a run somebody is inside.
    assert run.execution_owner is None
    assert run.execution_generation == 1


# =============================================================================
# 6. Reconstruction from a new process, not from this one's memory.
# =============================================================================


_CHILD = """
import json, sys, uuid
from sqlalchemy.orm import sessionmaker
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.repositories import RunEventRepository, TaskRunRepository
from apps.orchestrator.services.run_recovery import assess_recoverability, recover_run

db, artifacts, worktrees, run_id, act = sys.argv[1:6]
settings = Settings(
    _env_file=None,
    artifact_root=artifacts,
    worktree_root=worktrees,
    worker_backend=WorkerBackend.SUBPROCESS,
)
engine = create_db_engine("sqlite:///" + db)
factory = sessionmaker(bind=engine, expire_on_commit=False)
run_uuid = uuid.UUID(run_id)
with factory() as session:
    report = assess_recoverability(session, run_uuid, settings=settings)
out = {"report": report.describe()}
if act == "recover":
    with factory.begin() as session:
        authorization = recover_run(
            session,
            run_uuid,
            reason="recovered from a fresh service process",
            requested_by="operator",
            settings=settings,
        )
    out["generation"] = authorization.generation
    out["previous_generation"] = authorization.previous_generation
    out["next_attempt"] = authorization.next_attempt
    with factory() as session:
        run = TaskRunRepository(session).get(run_uuid)
        out["run"] = {
            "id": str(run.id),
            "run_number": run.run_number,
            "external_run_id": run.external_run_id,
            "attempt_number": run.attempt_number,
            "status": str(run.status),
            "execution_generation": run.execution_generation,
        }
        out["events"] = [
            str(e.event_type)
            for e in RunEventRepository(session).list_for_run(run_uuid)
        ]
engine.dispose()
print(json.dumps(out))
"""


def _in_a_new_process(signature: Signature, action: str) -> dict:
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(signature.path),
            str(signature.settings.artifact_root),
            str(signature.settings.worktree_root),
            str(signature.run_id),
            action,
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    return __import__("json").loads(completed.stdout.strip().splitlines()[-1])


def test_recovery_works_from_a_new_service_process(signature: Signature):
    """Nothing in this process is what is being observed.

    A separate interpreter, a separate engine, a separate identity map. If any
    part of the recoverability decision or the reconstruction depended on
    in-memory state left over from whoever started the run, this is where it
    would stop working -- which is exactly the failure mode the live
    ``RUN-20260928-000005`` has, since the process that started it is gone.
    """
    assessed = _in_a_new_process(signature, "assess")["report"]
    assert assessed["recoverable"] is True
    assert assessed["next_attempt"] == 2
    assert assessed["execution_generation"] == 0

    result = _in_a_new_process(signature, "recover")
    assert result["previous_generation"] == 0
    assert result["generation"] == 1
    assert result["next_attempt"] == 2
    assert result["run"]["id"] == str(signature.run_id)
    assert result["run"]["run_number"] == 2
    assert result["run"]["attempt_number"] == 1
    assert result["run"]["status"] == "RUNNING"
    assert result["events"].count("RUN_RECOVERY_AUTHORIZED") == 1

    # And the parent process, reading through a rebuilt engine, agrees.
    with signature.rebuilt_factory()() as session:
        run = TaskRunRepository(session).get(signature.run_id)
    assert run.execution_generation == 1
    assert run.external_run_id == result["run"]["external_run_id"]


# =============================================================================
# 7. The HTTP boundary.
# =============================================================================


@pytest.fixture
def api(signature: Signature, monkeypatch: pytest.MonkeyPatch):
    """A real app over the signature's own committed database.

    Not the suite's shared rolled-back session: the mutating endpoint has to
    commit before it dispatches, because the generation it commits is the token
    the executor quotes back. A fixture that could not commit would be testing
    a different endpoint.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{signature.path}")
    monkeypatch.setenv("ARTIFACT_ROOT", str(signature.settings.artifact_root))
    monkeypatch.setenv("WORKTREE_ROOT", str(signature.settings.worktree_root))
    from apps.orchestrator.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    reset_engine()
    app = create_app()
    with TestClient(app) as client:
        yield client
    reset_engine()
    settings_module.get_settings.cache_clear()


def test_the_routes_are_part_of_the_documented_contract(api: TestClient):
    paths = api.get("/openapi.json").json()["paths"]
    assert "post" in paths["/runs/{run_id}/recover"]
    assert "get" in paths["/runs/{run_id}/recoverability"]


def test_the_eligibility_endpoint_reports_the_reconstruction(
    api: TestClient, signature: Signature
):
    response = api.get(f"/runs/{signature.run_id}/recoverability")
    assert response.status_code == 200
    body = response.json()
    assert body["recoverable"] is True
    assert body["next_attempt"] == 2
    assert body["max_attempts"] == 3
    assert body["execution_generation"] == 0
    assert body["execution_owner"] is None
    assert body["candidate_commit"] is None
    assert {check["name"] for check in body["checks"]} >= {
        "run_in_flight",
        "not_superseded",
        "task_not_paused",
        "checkpoint_exists",
        "starting_commit_exists",
        "integration_baseline_compatible",
        "worktree_usable",
        "attempt_accounting_reconstructable",
        "execution_ownership_available",
    }


def test_the_eligibility_endpoint_takes_no_ownership(
    api: TestClient, signature: Signature
):
    api.get(f"/runs/{signature.run_id}/recoverability")
    api.get(f"/runs/{signature.run_id}/recoverability")
    run = api.get(f"/runs/{signature.run_id}").json()
    assert run["execution_generation"] == 0
    assert run["execution_owner"] is None
    assert run["status"] == "RUNNING"


def test_the_run_endpoint_now_reports_the_external_run_identity(
    api: TestClient, signature: Signature
):
    """Concern 67's one piece of adjacent API work, and why it is not adjacent.

    Mapping ``RUN-20260928-000005`` to its durable UUID meant reading a run
    artifact off disk, because no run response carried the external identity.
    That is a hand mapping standing between an operator and the most
    consequential run-scoped operation there is, and getting it wrong recovers
    a different run. The route stays unambiguous -- ``{run_id}`` is the UUID --
    and the identity an operator actually holds is now something they can look
    up.
    """
    run = api.get(f"/runs/{signature.run_id}").json()
    assert run["external_run_id"] is not None
    assert run["external_run_id"].startswith("RUN-")

    listed = api.get(f"/tasks/{signature.task_id}/runs").json()
    assert [r["external_run_id"] for r in listed] == [
        r["external_run_id"] for r in sorted(listed, key=lambda r: r["run_number"])
    ]
    assert all(r["external_run_id"] is not None for r in listed if r["run_number"] == 2)


def test_an_unknown_run_is_a_404_over_http(api: TestClient):
    missing = uuid.uuid4()
    assert api.get(f"/runs/{missing}/recoverability").status_code == 404
    response = api.post(f"/runs/{missing}/recover", json={"reason": "x"})
    assert response.status_code == 404


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}])
def test_a_blank_reason_is_a_422_over_http(
    api: TestClient, signature: Signature, body: dict
):
    response = api.post(f"/runs/{signature.run_id}/recover", json=body)
    assert response.status_code == 422


def test_a_terminal_run_is_a_409_over_http(api: TestClient, signature: Signature):
    with signature.factory.begin() as session:
        TaskRunRepository(session).finish(signature.run_id, RunStatus.SUCCEEDED)

    response = api.post(
        f"/runs/{signature.run_id}/recover", json={"reason": "please"}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "EntityConflict"
    assert "not recoverable" in response.json()["detail"]


def test_an_abandoned_run_is_a_409_over_http(api: TestClient, signature: Signature):
    with signature.factory.begin() as session:
        abandon_run(session, signature.run_id, reason="stale image")

    response = api.post(
        f"/runs/{signature.run_id}/recover", json={"reason": "please"}
    )
    assert response.status_code == 409
    assert "abandoned" in response.json()["detail"]


def test_a_held_run_is_a_409_over_http(api: TestClient, signature: Signature):
    with signature.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="live-dispatch"
        )

    response = api.post(
        f"/runs/{signature.run_id}/recover", json={"reason": "please"}
    )
    assert response.status_code == 409
    assert "override_active_owner" in response.json()["detail"]


def test_a_paused_task_is_a_409_that_names_resume(
    api: TestClient, signature: Signature
):
    with signature.factory.begin() as session:
        TaskRepository(session).transition(signature.task_id, TaskStatus.PAUSED)

    response = api.post(
        f"/runs/{signature.run_id}/recover", json={"reason": "please"}
    )
    assert response.status_code == 409
    assert "/tasks/{task_id}/resume" in response.json()["detail"]


# =============================================================================
# 8. The operations concern 67 must not have changed.
# =============================================================================


def test_abandonment_is_unchanged_and_still_wins(signature: Signature):
    with signature.factory.begin() as session:
        run = abandon_run(
            session, signature.run_id, reason="operator", requested_by="me"
        )
    assert run.status is RunStatus.ABANDONED
    with signature.factory() as session:
        task = TaskRepository(session).get(signature.task_id)
        events = [
            e
            for e in RunEventRepository(session).list_for_run(signature.run_id)
            if e.event_type == RunEventType.RUN_ABANDONED
        ]
    assert task.status is TaskStatus.FAILED
    assert len(events) == 1


def test_abandonment_still_wins_against_a_recovered_owner(signature: Signature):
    """Concern 64's guarantee survives concern 67's new ownership.

    An operator stopping a run outranks an operator continuing it: the
    recovered executor's next durable checkpoint finds the run ABANDONED and is
    refused, and that refusal is still an ``AbandonedRunError`` -- the decision
    the person actually made.
    """
    with signature.factory.begin() as session:
        authorization = recover_run(
            session, signature.run_id, reason="take it", settings=signature.settings
        )
    with signature.factory.begin() as session:
        abandon_run(session, signature.run_id, reason="no, stop")

    with signature.factory() as session, pytest.raises(AbandonedRunError):
        TaskRunRepository(session).require_in_flight(
            signature.run_id, expected_generation=authorization.generation
        )


def test_retry_is_unchanged_and_still_refuses_an_in_flight_run(
    signature: Signature,
):
    from apps.orchestrator.services.retry import retry_failed_task

    with signature.factory() as session, pytest.raises(EntityConflict):
        retry_failed_task(session, signature.task_id, reason="try again")

    with signature.factory.begin() as session:
        abandon_run(session, signature.run_id, reason="stop")
    with signature.factory.begin() as session:
        task = retry_failed_task(session, signature.task_id, reason="try again")
    assert task.status is TaskStatus.READY


def test_resume_is_unchanged_and_still_refuses_an_unpaused_task(
    signature: Signature,
):
    """The exact refusal concern 66's proposal met, preserved as a regression.

    ``EntityConflict: Task TS-109 is not paused`` is what the live
    ``POST /tasks/{task_id}/resume`` returned against the real run, and it is
    still what it returns. Concern 67 adds an operation beside resume; it does
    not widen resume, and this is the assertion that says so.
    """
    with signature.factory() as session, pytest.raises(EntityConflict, match="is not paused"):
        resume_task(session, signature.task_id)

    with signature.factory() as session:
        run = signature.run(session)
    assert run.status is RunStatus.RUNNING
    assert run.execution_generation == 0
    assert run.execution_owner is None


def test_the_concern_64_barrier_still_answers_without_a_generation(
    signature: Signature,
):
    """Callers that ask only the terminality question get only that answer."""
    with signature.factory() as session:
        TaskRunRepository(session).require_in_flight(signature.run_id)

    with signature.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(
            signature.run_id, owner="somebody"
        )

    with signature.factory() as session:
        # Still in flight, now owned by generation 1, and a generation-free
        # caller is not refused by it.
        TaskRunRepository(session).require_in_flight(signature.run_id)


# =============================================================================
# 9. PostgreSQL. The only authority on whether two requests can both win.
# =============================================================================


_DEFAULT_SERVER = (
    "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/postgres"
)


def _server_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if configured.startswith("postgresql"):
        return configured.rsplit("/", 1)[0] + "/postgres"
    return os.environ.get("TEST_POSTGRES_SERVER_URL", _DEFAULT_SERVER)


@contextlib.contextmanager
def _scratch_postgres() -> Iterator[str]:
    """A scratch database on the configured server, dropped afterwards.

    Nothing here touches a managed project's data: the name is unique per call
    and the database is dropped on the way out, including when a test fails.
    """
    server = _server_url()
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:  # pragma: no cover - no server available
        admin.dispose()
        pytest.skip(f"no PostgreSQL server for the race test at {server}: {exc}")
    name = f"recover_{uuid.uuid4().hex[:12]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.rsplit("/", 1)[0] + f"/{name}"
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@dataclass
class PgSignature:
    engine: Engine
    factory: sessionmaker
    settings: Settings
    repository: Path
    run_id: uuid.UUID
    task_id: uuid.UUID


@pytest.fixture
def pg_signature(tmp_path: Path) -> Iterator[PgSignature]:
    with _scratch_postgres() as url:
        engine = create_db_engine(url)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        repository = _build_repository(tmp_path)
        settings = _settings(tmp_path)
        _project_id, task_id, run_id, _model_id = _seed_signature(
            factory, repository, settings
        )
        try:
            yield PgSignature(
                engine=engine,
                factory=factory,
                settings=settings,
                repository=repository,
                run_id=run_id,
                task_id=task_id,
            )
        finally:
            engine.dispose()


def _race(pg: PgSignature, worker) -> list:
    """Two threads, one barrier, no sleeps.

    The barrier is what makes this deterministic rather than probabilistic:
    both threads have their session open and their assessment made before
    either is allowed to attempt the write, so the contention is real and does
    not depend on how the scheduler happened to interleave them.
    """
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def run(index: int) -> None:
        try:
            results[index] = worker(barrier, index)
        except Exception as error:  # captured, then asserted on
            results[index] = error

    threads = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a racing thread hung"
    return results


@pytest.mark.integration
def test_two_simultaneous_recoveries_produce_exactly_one_owner(
    pg_signature: PgSignature,
):
    """Requirement 24, against the database that can actually answer it.

    Both requests read the run at generation 0 and both decide it is
    recoverable -- which is correct, and is why the decision cannot be the
    guard. The guard is the acquisition: one guarded ``UPDATE`` whose predicate
    includes the generation the request quoted, evaluated by PostgreSQL while
    it holds the row lock. The loser's predicate is falsified by the winner's
    commit, so it matches no row and is told so.
    """

    def worker(barrier: threading.Barrier, index: int):
        with pg_signature.factory() as session:
            report = assess_recoverability(
                session, pg_signature.run_id, settings=pg_signature.settings
            )
            assert report.recoverable is True
            assert report.execution_generation == 0
            barrier.wait(timeout=30)
            try:
                authorization = recover_run(
                    session,
                    pg_signature.run_id,
                    reason=f"racing operator {index}",
                    requested_by=f"operator-{index}",
                    settings=pg_signature.settings,
                )
                session.commit()
                return authorization.generation
            except EntityConflict as conflict:
                session.rollback()
                return conflict

    results = _race(pg_signature, worker)
    winners = [r for r in results if isinstance(r, int)]
    losers = [r for r in results if isinstance(r, EntityConflict)]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert winners[0] == 1

    with pg_signature.factory() as session:
        run = TaskRunRepository(session).get(pg_signature.run_id)
        events = [
            e
            for e in RunEventRepository(session).list_for_run(pg_signature.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
        total_runs = session.scalar(select(func.count()).select_from(TaskRunRow))

    # One owner, one generation, one event, one run. The whole invariant.
    assert run.execution_generation == 1
    assert run.execution_owner is not None
    assert len(events) == 1
    assert total_runs == 2


@pytest.mark.integration
def test_two_simultaneous_acquisitions_produce_exactly_one_owner(
    pg_signature: PgSignature,
):
    """The primitive on its own, so the guarantee is not read through policy."""

    def worker(barrier: threading.Barrier, index: int):
        with pg_signature.factory() as session:
            runs = TaskRunRepository(session)
            assert runs.get(pg_signature.run_id).execution_generation == 0
            barrier.wait(timeout=30)
            acquired = runs.acquire_execution(
                pg_signature.run_id,
                owner=f"dispatch-{index}",
                expected_generation=0,
            )
            session.commit()
            return None if acquired is None else acquired.execution_generation

    results = _race(pg_signature, worker)
    assert sorted(r is None for r in results) == [False, True], results

    with pg_signature.factory() as session:
        run = TaskRunRepository(session).get(pg_signature.run_id)
    assert run.execution_generation == 1


@pytest.mark.integration
def test_the_loser_of_a_postgres_race_is_fenced_from_persisting(
    pg_signature: PgSignature,
):
    """Losing the race is not merely being told no; it is being unable to write."""

    def worker(barrier: threading.Barrier, index: int):
        with pg_signature.factory() as session:
            runs = TaskRunRepository(session)
            barrier.wait(timeout=30)
            acquired = runs.acquire_execution(
                pg_signature.run_id,
                owner=f"dispatch-{index}",
                expected_generation=0,
            )
            session.commit()
            return acquired.execution_generation if acquired else None

    results = _race(pg_signature, worker)
    held = next(r for r in results if r is not None)

    with pg_signature.factory() as session:
        runs = TaskRunRepository(session)
        runs.require_in_flight(pg_signature.run_id, expected_generation=held)
        with pytest.raises(RunOwnershipLostError):
            runs.require_in_flight(pg_signature.run_id, expected_generation=held - 1)


@pytest.mark.integration
def test_the_postgres_recovery_reconstructs_to_attempt_two(
    pg_signature: PgSignature,
):
    """The same arithmetic, on the database the campaign actually runs on."""
    with pg_signature.factory() as session:
        report = assess_recoverability(
            session, pg_signature.run_id, settings=pg_signature.settings
        )
    assert report.recoverable is True
    assert report.next_attempt == 2
    assert report.attempts_started == 1
    assert report.max_attempts == 3


# =============================================================================
# 10. The artifact directory is evidence too, and it changes the answer.
# =============================================================================
#
# Discovered by running the read-only eligibility endpoint against the real
# RUN-20260928-000005 after deployment. The durable *rows* say attempt 1 was
# the last one begun, and the fixture above reproduces exactly those rows. The
# live run also has ``runs/RUN-20260928-000005/attempt-2-cycle-1/`` on disk,
# holding the rendered prompt -- written before the provider was called, which
# is the whole reason it is written there. So attempt 2 was begun: the model
# was asked, the call cost provider time, and the only thing that did not
# survive was its ``model_runs`` row.
#
# ``loop_recovery`` already says what to do about that, and these tests pin it:
# an attempt is charged when a model is *asked*, and the directory is the
# record that survives a rollback. Re-running as attempt 2 would overwrite the
# evidence of the call that was made, which is precisely what a resume must not
# do. The next durable coder execution is therefore attempt 3.


def _begin_attempt_directory(signature: Signature, attempt: int, cycle: int) -> Path:
    """The directory a turn writes before it calls a model."""
    with signature.factory() as session:
        run = signature.run(session)
    root = artifact_store.run_directory(
        run.external_run_id, settings=signature.settings
    )
    directory = root / f"attempt-{attempt}-cycle-{cycle}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "prompt.txt").write_text("the prompt that was sent", "utf-8")
    return directory


def test_a_begun_attempt_with_no_row_is_still_charged(signature: Signature):
    """The live RUN-20260928-000005 reading, reproduced.

    Same rows as every test above; one more piece of durable evidence. The
    answer moves from 2 to 3, and that is the correct answer rather than a
    regression: a provider call that happened is a provider call that happened,
    and the number it used is not handed back because the transaction that
    would have recorded it died.
    """
    _begin_attempt_directory(signature, 2, 1)

    with signature.factory() as session:
        report = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )

    assert report.next_attempt == 3
    assert report.attempts_started == 2
    assert report.reviews_completed == 0
    assert report.max_attempts == 3
    # Still recoverable: attempt 3 of at most 3 is the last one, not one too
    # many, and recovery does not refuse work a task is still entitled to.
    assert report.recoverable is True


def test_the_last_attempt_is_recoverable_and_the_one_after_only_settles(
    signature: Signature,
):
    """The boundary, from both sides, with nothing else changed.

    Concern 68: past the boundary the run is still recoverable, but for a
    different purpose. The two sides are distinguished by ``recovery_mode``,
    which is the whole point of having one.
    """
    _begin_attempt_directory(signature, 2, 1)
    with signature.factory() as session:
        last = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
    assert (last.next_attempt, last.recoverable) == (3, True)
    assert last.recovery_mode is RecoveryMode.CONTINUE

    _begin_attempt_directory(signature, 3, 1)
    with signature.factory() as session:
        spent = assess_recoverability(
            session, signature.run_id, settings=signature.settings
        )
    assert spent.next_attempt == 4
    assert spent.recoverable is True
    assert spent.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
    assert spent.refusals == ()


def test_a_recovered_run_does_not_overwrite_a_begun_attempt_directory(
    signature: Signature,
):
    """The consequence the accounting rule exists for.

    If the reconstruction handed attempt 2 back, the next turn's artifacts
    would land in a directory that already holds the prompt of the call that
    was actually made -- destroying the only surviving evidence of why the run
    stranded.
    """
    directory = _begin_attempt_directory(signature, 2, 1)
    contents = (directory / "prompt.txt").read_text("utf-8")

    with signature.factory.begin() as session:
        authorization = recover_run(
            session,
            signature.run_id,
            reason="stranded after the attempt-2 provider call",
            settings=signature.settings,
        )

    assert authorization.next_attempt == 3
    assert (directory / "prompt.txt").read_text("utf-8") == contents
    with signature.factory() as session:
        events = [
            e
            for e in RunEventRepository(session).list_for_run(signature.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
    assert events[0].payload["next_attempt"] == 3
    assert events[0].payload["attempts_started"] == 2
