"""Concern 68: recovery for terminal settlement after the attempt budget is spent.

Discovered by the live concern 67 recovery of ``RUN-20260928-000005``, and by
nothing going wrong in it. The sequence, in order:

1. Concern 66 fixed the transaction lifetime, so a fix-loop turn no longer holds
   a database transaction across model inference.
2. Concern 67 added safe execution ownership and an operator recovery that takes
   the next ``execution_generation`` atomically.
3. ``RUN-20260928-000005`` was recovered, correctly, as generation 1.
4. The reconstruction correctly selected attempt 3 -- not 1, which would have
   overwritten the evidence of attempt 1, and not 2, whose ``model_runs`` row the
   pre-concern-66 defect destroyed but whose artifact directory survived to prove
   the provider had been asked.
5. Attempt 3 reached the provider's 600-second timeout.
6. Concern 66 correctly persisted that timeout after inference: the ``model_runs``
   row is durably ``FAILED``.
7. Concern 67 correctly released ownership on the way out.
8. The durable accounting therefore correctly says attempts 1, 2 and 3 are spent,
   against ``max_attempts = 3``.

That is eight steps of the system working. **The attempt-3 timeout is a provider
timeout, not a supervisor defect.** The ninth step is the defect:

9. Recoverability refused re-entry -- "there is no attempt left to execute, so
   recovering would only exhaust the budget again" -- even though deterministic
   exhausted-budget settlement was still pending.

So the run was left permanently stranded: ``run.status = RUNNING``,
``task.status = CODING``, no owner, no candidate, and no supported operation that
could finish it. Abandonment would have been a different, lossier answer: it
writes ``ABANDONED`` rather than the ``ESCALATED`` / ``RETRY_EXHAUSTED`` ending
the run had actually earned, and it produces no escalation for a person to read.

**The conflation.** One check answered two questions -- "is there a coder attempt
left?" and "is there any useful workflow action left?" -- and they are not the
same question. ``agents.fix_loop.run_fix_loop`` has always known the difference:
when ``first > ceiling`` it logs ``fix_loop_attempts_already_spent``, iterates an
*empty* range, and falls through to its closing
``settle(ESCALATED, RETRY_EXHAUSTED)``. No provider is called, no verification
command is run, no reviewer is asked, no candidate is built, and
``attempt_number`` never moves.

**What concern 68 therefore is.** Not a settlement implementation -- the fix loop
already owns that policy and a second copy in the recovery service would
eventually disagree with the first. It is one distinction made observable:
``recovery_mode`` is ``continue`` when an attempt remains and
``settlement_only`` when none does but the run is non-terminal, and the second
is recoverable. Ownership is still acquired atomically, old generations are
still fenced, and the audit event is still exactly one.

**How these tests prove the absence of model work.** Not from the event log,
which would only show that nothing was *recorded*. The coder and reviewer
providers handed to the recovered ``WorkflowRunner`` record every call and then
raise, and the project's verification command writes a sentinel file. The tests
assert the call lists are empty and the sentinel does not exist, so a settlement
that quietly asked a model anything fails here rather than being inferred about.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.agents.fix_loop import durable_checkpoint
from apps.orchestrator.agents.loop_recovery import RecoveredLoopState
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import TaskRow, TaskRunRow
from apps.orchestrator.db.session import create_db_engine, reset_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    EscalationStatus,
    FailureReason,
    ModelPurpose,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.errors import RunOwnershipLostError
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import (
    Model,
    ModelRun,
    Project,
    RunEvent,
    Task,
    TaskLimits,
)
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.main import create_app
from apps.orchestrator.providers.base import ConnectionReport, ProviderConfig
from apps.orchestrator.repositories import (
    EscalationRepository,
    ModelRepository,
    ModelRunRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services import artifact_store
from apps.orchestrator.services import run_recovery as recovery_service
from apps.orchestrator.services.errors import EntityConflict
from apps.orchestrator.services.run_recovery import (
    RecoveryMode,
    assess_recoverability,
    recover_run,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import workspace_path
from apps.orchestrator.workflow import WorkflowRunner
from apps.orchestrator.workflow.checkpoints import SqlAlchemyCheckpointSaver
from tests.conftest import run_git
from tests.db_safety import drop_test_database

# =============================================================================
# 0. Providers that fail the test if they are asked anything.
# =============================================================================
#
# The negative evidence this file rests on. A settlement-only recovery must
# perform no inference at all, and "no ``model_runs`` row appeared" is not the
# same claim: a call can be made and lost, which is precisely the concern 66
# defect this whole sequence began with. So the provider itself is the witness.
#
# Each spy records the request *before* raising, and the tests assert on the
# recorded list rather than on whether an exception escaped. A settlement that
# called a model and swallowed the error still fails.


class NeverCalledCoder:
    """A ``ModelProvider`` whose being called is the failure."""

    def __init__(self) -> None:
        self.config = ProviderConfig(
            provider_id="concern68-never-called-coder",
            base_url="http://stub/v1",
            model_name="coder-must-not-run",
            role=ModelRole.CODER,
            context_window=32768,
        )
        self.calls: list[object] = []

    async def generate(self, request):  # noqa: ANN001 - protocol signature
        self.calls.append(request)
        raise AssertionError(
            "the coder provider was asked for inference during a settlement-only "
            "recovery"
        )

    async def check_connection(self) -> ConnectionReport:  # pragma: no cover
        self.calls.append("check_connection")
        raise AssertionError("the coder endpoint was probed")

    async def aclose(self) -> None:
        return None


class NeverCalledReviewer:
    """A ``ReviewProvider`` whose being called is the failure."""

    def __init__(self) -> None:
        self.config = ProviderConfig(
            provider_id="concern68-never-called-reviewer",
            base_url="http://stub/v1",
            model_name="reviewer-must-not-run",
            role=ModelRole.REVIEWER,
            context_window=32768,
        )
        self.calls: list[object] = []

    async def review(self, request):  # noqa: ANN001 - protocol signature
        self.calls.append(request)
        raise AssertionError(
            "the reviewer was asked for a verdict during a settlement-only recovery"
        )

    async def check_connection(self) -> ConnectionReport:  # pragma: no cover
        self.calls.append("check_connection")
        raise AssertionError("the reviewer endpoint was probed")

    async def aclose(self) -> None:
        return None


# =============================================================================
# 1. The post-concern-67 signature of RUN-20260928-000005.
# =============================================================================
#
# Rebuilt from the live evidence rather than from the concern 67 fixture's
# starting point, because the run has moved: it was recovered once, it made
# attempt 3, and attempt 3 timed out. The differences that matter are all
# durable, and all of them are what make this the *settlement* case:
#
#   attempt 1  CODE model_run SUCCEEDED, then BUILD_FAILED
#   attempt 2  artifact directory only -- the row the concern 66 defect ate
#   attempt 3  CODE model_run FAILED, ModelTimeout, ~600s, no output at all
#   run        RUNNING, attempt_number 3, review_cycle 0, candidate NULL,
#              execution_generation 1, execution_owner NULL
#   task       CODING, max_attempts 3
#
# so attempts_started is 3 and the next durable coder execution would be 4.


@dataclass
class Spent:
    """A private database, a real repository, and the run whose budget is spent."""

    path: Path
    repository: Path
    settings: Settings
    factory: sessionmaker
    project_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    model_id: uuid.UUID
    #: Written by the project's verification command if it is ever run.
    sentinel: Path

    def rebuilt_factory(self) -> sessionmaker:
        return sessionmaker(
            bind=create_db_engine(f"sqlite:///{self.path}"), expire_on_commit=False
        )

    def run(self, session: Session):
        return TaskRunRepository(session).get(self.run_id)

    def run_directory(self) -> Path:
        with self.factory() as session:
            external = self.run(session).external_run_id
        return artifact_store.run_directory(external, settings=self.settings)

    def integration_sha(self) -> str:
        return run_git(self.repository, "rev-parse", INTEGRATION_BRANCH).strip()


def _build_repository(root: Path, sentinel: Path) -> Path:
    """A real repository whose verification command leaves a trace if it runs."""
    repo = root / "tracestack"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "nav.py").write_text("def navigate():\n    return None\n", "utf-8")
    # Not a test that passes or fails: a test that *records having been run*.
    # Whether verification would have succeeded is irrelevant here; whether it
    # was executed at all is the entire question.
    (repo / "tools" / "test.py").write_text(
        f"open({str(sentinel)!r}, 'a', encoding='utf-8').write('verification ran\\n')\n",
        "utf-8",
    )
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    run_git(repo, "branch", INTEGRATION_BRANCH)
    return repo


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=root / "data",
        worktree_root=root / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


def _checkpoint(factory: sessionmaker, run_id: uuid.UUID) -> None:
    """Durable workflow state for the run's thread, written by the real saver.

    Through ``SqlAlchemyCheckpointSaver`` rather than as a hand-built row,
    because these tests actually *resume* the graph from this thread: an opaque
    payload is good enough for a recoverability read (concern 67's fixture uses
    one) but LangGraph has to be able to load it, and a fixture that could not
    be loaded would be testing the wrong failure.

    The checkpoint is empty, which is the honest content: the live run's graph
    cursor is at the boundary before ``execute``, and settlement walks the graph
    from the start exactly as a fresh dispatch would.
    """
    saver = SqlAlchemyCheckpointSaver(factory)
    saver.put(
        {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "loop", "step": -1, "parents": {}},
        {},
    )


def _seed(
    factory: sessionmaker,
    repository: Path,
    settings: Settings,
    *,
    max_attempts: int = 3,
    spent_attempts: int = 3,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """The signature, parameterised only where a boundary case needs it.

    ``spent_attempts`` is how many attempts the durable record says were begun,
    and it is charged the way attempts are really charged -- a model call for
    attempt 1 and 3, an artifact directory for every one of them including the
    attempt whose row was destroyed. ``max_attempts`` is the task's ceiling.
    Both are parameters rather than fixtures so the zero-budget boundary is the
    same code path as the live signature, not a separate reconstruction of it.
    """
    # ``create_run`` refuses to open a run whose first attempt already exceeds the
    # ceiling, so a zero-budget run cannot be *created* -- it can only be reached
    # by a ceiling that was lowered after the fact, which is what a task whose
    # limits an operator tightened mid-flight looks like. The task is therefore
    # seeded at the default ceiling and narrowed at the end.
    seeded_max_attempts = max(1, max_attempts)
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="TraceStack",
                repository_path=str(repository),
                default_branch="main",
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=("python3 tools/test.py",)),
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
                files_to_modify=["src/nav.py"],
                limits=TaskLimits(
                    max_attempts=seeded_max_attempts, max_review_cycles=2
                ),
            )
        )
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        # RUN-20260928-000004: the earlier, terminal run. Present because TS-109
        # really has two, and "the task's expected in-flight run" is a claim
        # about a set.
        first = create_run(session, task.id)
        TaskRunRepository(session).finish(first.id, RunStatus.FAILED)
        TaskRepository(session).transition(task.id, TaskStatus.FAILED)
        TaskRepository(session).transition(task.id, TaskStatus.READY)

    starting = run_git(repository, "rev-parse", INTEGRATION_BRANCH).strip()
    branch = "agent/TS-109-keep-only-the-entries-from-one-navigation-source-run2"

    with factory.begin() as session:
        run = create_run(session, task.id)
        external = artifact_store.ensure_run_id(session, run.id)
        TaskRunRepository(session).update_fields(
            run.id,
            status=RunStatus.RUNNING,
            # The row the concern 67 reconstruction left: attempt 3 was the one
            # being made when the provider timed out.
            attempt_number=max(1, spent_attempts),
            review_cycle=0,
            candidate_commit=None,
            starting_commit=starting,
            branch_name=branch,
            # Generation 1: the concern 67 recovery took it, and the executor
            # released the owner stamp on its way out.
            execution_generation=1,
            execution_owner=None,
        )
        TaskRepository(session).transition(task.id, TaskStatus.CODING)

        calls = ModelRunRepository(session)
        events = RunEventRepository(session)
        if spent_attempts >= 1:
            # Attempt 1: the call succeeded and the build then failed.
            calls.add(
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
            for event_type, payload in (
                (RunEventType.CODING_STARTED, {"is_fix_attempt": False}),
                (RunEventType.BUILD_FAILED, {"failure_reason": "BUILD_FAILED"}),
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
        if spent_attempts >= 3:
            # Attempt 3: the 600-second provider timeout, persisted after
            # inference by concern 66. No output, no verification, no review.
            started = datetime.now(UTC) - timedelta(seconds=600)
            calls.add(
                ModelRun(
                    task_run_id=run.id,
                    model_id=model.id,
                    purpose=ModelPurpose.CODE,
                    status=RunStatus.FAILED,
                    attempt=3,
                    review_cycle=1,
                    started_at=started,
                    completed_at=started + timedelta(seconds=600),
                    duration_ms=600_000,
                    error_detail="coder request timed out after 600s",
                )
            )
            events.append(
                RunEvent(
                    task_run_id=run.id,
                    project_id=project.id,
                    task_id=task.id,
                    event_type=RunEventType.CODING_STARTED,
                    attempt=3,
                    payload={"is_fix_attempt": True},
                )
            )
        run_id = run.id

    if max_attempts != seeded_max_attempts:
        with factory.begin() as session:
            session.get(TaskRow, task.id).max_attempts = max_attempts

    _checkpoint(factory, run_id)

    # The directories. Attempt 2's is the *only* surviving record of the call
    # whose row the concern 66 defect destroyed, and the reason attempts_started
    # is 3 rather than 2.
    root = artifact_store.run_directory(external, settings=settings)
    for attempt in range(1, spent_attempts + 1):
        directory = root / f"attempt-{attempt}-cycle-1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "prompt.txt").write_text(f"the attempt-{attempt} prompt", "utf-8")

    worktree = workspace_path(project.id, "TS-109", 2, settings=settings)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(repository, "worktree", "add", "-b", branch, str(worktree), starting)
    return project.id, task.id, run_id, model.id


def _spent(tmp_path: Path, **kwargs) -> Iterator[Spent]:
    sentinel = tmp_path / "verification-ran.txt"
    repository = _build_repository(tmp_path, sentinel)
    settings = _settings(tmp_path)
    path = tmp_path / "concern68.db"
    engine = create_db_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    project_id, task_id, run_id, model_id = _seed(
        factory, repository, settings, **kwargs
    )
    try:
        yield Spent(
            path=path,
            repository=repository,
            settings=settings,
            factory=factory,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            model_id=model_id,
            sentinel=sentinel,
        )
    finally:
        engine.dispose()


@pytest.fixture
def spent(tmp_path: Path) -> Iterator[Spent]:
    yield from _spent(tmp_path)


@pytest.fixture
def budget_free(tmp_path: Path) -> Iterator[Spent]:
    """A task whose ceiling is zero, with nothing attempted. The boundary case."""
    yield from _spent(tmp_path, max_attempts=0, spent_attempts=0)


def test_the_fixture_is_the_post_concern_67_run_000005_signature(spent: Spent):
    """Guard the fixture: every claim below is about this exact shape."""
    with spent.factory() as session:
        run = spent.run(session)
        task = TaskRepository(session).get(spent.task_id)
        calls = ModelRunRepository(session).list_for_run(run.id)
        runs_of_task = TaskRunRepository(session).list_for_task(spent.task_id)

    assert run.status is RunStatus.RUNNING
    assert run.run_number == 2
    assert run.attempt_number == 3
    assert run.review_cycle == 0
    assert run.candidate_commit is None
    assert run.execution_generation == 1
    assert run.execution_owner is None
    assert task.status is TaskStatus.CODING
    assert task.limits.max_attempts == 3
    # Attempt 1 succeeded and attempt 3 timed out; attempt 2 has no row at all.
    assert sorted((c.attempt, c.status) for c in calls) == [
        (1, RunStatus.SUCCEEDED),
        (3, RunStatus.FAILED),
    ]
    timed_out = next(c for c in calls if c.attempt == 3)
    assert timed_out.duration_ms == 600_000
    assert "timed out" in (timed_out.error_detail or "")
    # Three attempt directories, including attempt 2's, which is the only
    # surviving evidence that its provider call happened.
    assert sorted(
        p.name for p in spent.run_directory().iterdir() if p.is_dir()
    ) == ["attempt-1-cycle-1", "attempt-2-cycle-1", "attempt-3-cycle-1"]
    assert len(runs_of_task) == 2
    assert not spent.sentinel.exists()


# =============================================================================
# 2. Recoverability semantics: three answers, not two.
# =============================================================================


def test_the_spent_signature_is_recoverable_for_settlement(spent: Spent):
    """Requirement 1. The exact live reading, with the corrected consequence.

    The arithmetic is not softened anywhere: attempts 1-3 are spent, the next
    durable coder execution would be attempt 4, and 4 is more than 3. The run is
    recoverable regardless, because settling it is not an attempt.
    """
    with spent.factory() as session:
        report = assess_recoverability(
            session, spent.run_id, settings=spent.settings
        )

    assert report.attempts_started == 3
    assert report.next_attempt == 4
    assert report.max_attempts == 3
    assert report.reviews_completed == 0
    assert report.recoverable is True
    assert report.refusals == ()


def test_recoverability_reports_settlement_only_rather_than_continuation(
    spent: Spent,
):
    """Requirement 2. The distinction is observable, not implied by a boolean."""
    with spent.factory() as session:
        report = assess_recoverability(
            session, spent.run_id, settings=spent.settings
        )

    assert report.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
    assert report.settlement_only is True
    assert report.describe()["recovery_mode"] == "settlement_only"
    action = next(
        check for check in report.checks if check.name == "workflow_action_available"
    )
    assert action.passed is True
    assert "budget is spent" in action.detail
    assert "RETRY_EXHAUSTED" in action.detail
    # And the accounting question is now a separate question with its own answer.
    accounting = next(
        check
        for check in report.checks
        if check.name == "attempt_accounting_reconstructable"
    )
    assert accounting.passed is True


def test_ordinary_continuation_is_still_reported_as_continuation(tmp_path: Path):
    """Requirement 22. Concern 67's own case is untouched.

    One attempt spent of three: there is a real next attempt, the mode says so,
    and nothing about this run is a settlement.
    """
    for signature in _spent(tmp_path, spent_attempts=1):
        with signature.factory() as session:
            report = assess_recoverability(
                session, signature.run_id, settings=signature.settings
            )
        assert report.recovery_mode is RecoveryMode.CONTINUE
        assert report.settlement_only is False
        assert report.attempts_started == 1
        assert report.next_attempt == 2
        assert report.recoverable is True


def test_a_zero_attempt_budget_settles_rather_than_continues(budget_free: Spent):
    """Requirement 24. The documented policy at the boundary.

    ``max_attempts = 0`` means attempt 1 is already outside the budget, and the
    fix loop agrees: ``first`` is 1, ``ceiling`` is 0, the range is empty and the
    loop settles. Recovery therefore reports ``settlement_only`` for a run that
    has attempted nothing at all -- which is the right answer rather than a
    curiosity, because a run nobody may make an attempt for still has a
    ``RUNNING`` row that something has to close.
    """
    with budget_free.factory() as session:
        report = assess_recoverability(
            session, budget_free.run_id, settings=budget_free.settings
        )

    assert report.attempts_started == 0
    assert report.next_attempt == 1
    assert report.max_attempts == 0
    assert report.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
    assert report.recoverable is True


def test_inconsistent_attempt_accounting_remains_fail_closed(
    spent: Spent, monkeypatch: pytest.MonkeyPatch
):
    """Requirement 23, first half. A record that contradicts itself is refused.

    The reconstruction's invariant is that an attempt is charged when a model is
    asked, so the number to continue at is always strictly past every number
    already begun. Forced here, because no honest durable record can produce the
    violation -- and that is the point: if one ever could, neither mode is safe
    and the answer must be neither.
    """

    def inconsistent(session, run, *, settings, initial_feedback=None):
        return RecoveredLoopState(
            feedback=None,
            next_attempt=2,
            cycle=1,
            attempts_started=3,
            reviews_completed=0,
        )

    monkeypatch.setattr(recovery_service, "recover_loop_state", inconsistent)
    with spent.factory() as session:
        report = assess_recoverability(
            session, spent.run_id, settings=spent.settings
        )

    assert report.recovery_mode is None
    assert report.recoverable is False
    assert {check.name for check in report.refusals} == {
        "attempt_accounting_reconstructable",
        "workflow_action_available",
    }

    with spent.factory() as session, pytest.raises(
        EntityConflict, match="not self-consistent"
    ):
        recover_run(
            session,
            spent.run_id,
            reason="attempting to recover an incoherent record",
            settings=spent.settings,
        )


def test_unreadable_attempt_accounting_remains_fail_closed(
    spent: Spent, monkeypatch: pytest.MonkeyPatch
):
    """Requirement 23, second half. An exception is not a settlement signal."""

    def broken(session, run, *, settings, initial_feedback=None):
        raise RuntimeError("the artifact root is unreadable")

    monkeypatch.setattr(recovery_service, "recover_loop_state", broken)
    with spent.factory() as session:
        report = assess_recoverability(
            session, spent.run_id, settings=spent.settings
        )

    assert report.recovery_mode is None
    assert report.recoverable is False
    assert report.next_attempt is None
    assert any(
        "could not be reconstructed" in check.detail for check in report.refusals
    )


def test_the_settlement_assessment_mutates_nothing(spent: Spent):
    """The read stays a read. It is the operator's dry run for this decision."""
    before = spent.integration_sha()
    with spent.factory() as session:
        run_before = spent.run(session)
        events_before = len(RunEventRepository(session).list_for_run(spent.run_id))
        assess_recoverability(session, spent.run_id, settings=spent.settings)

    with spent.rebuilt_factory()() as session:
        run_after = TaskRunRepository(session).get(spent.run_id)
        events_after = len(RunEventRepository(session).list_for_run(spent.run_id))

    assert run_after.status is run_before.status
    assert run_after.attempt_number == run_before.attempt_number
    assert run_after.execution_generation == run_before.execution_generation
    assert run_after.execution_owner is None
    assert events_after == events_before
    assert spent.integration_sha() == before


# =============================================================================
# 3. The settlement itself, executed for real.
# =============================================================================
#
# The recovery is the live operation and the runner is the real one: the only
# things substituted are the two providers, and they are substituted with
# witnesses rather than with stubs that would answer.


@dataclass
class Settled:
    spent: Spent
    authorization: object
    state: dict
    coder: NeverCalledCoder
    reviewer: NeverCalledReviewer
    integration_before: str


@pytest_asyncio.fixture
async def settled(spent: Spent) -> Settled:
    integration_before = spent.integration_sha()
    with spent.factory.begin() as session:
        authorization = recover_run(
            session,
            spent.run_id,
            reason="attempts 1-3 are spent; settle the run deterministically",
            requested_by="operator",
            settings=spent.settings,
        )
    coder = NeverCalledCoder()
    reviewer = NeverCalledReviewer()
    runner = WorkflowRunner(
        spent.factory, coder=coder, reviewer=reviewer, settings=spent.settings
    )
    try:
        state = await runner.run(
            spent.run_id,
            acquired=(authorization.owner, authorization.generation),
        )
    finally:
        await runner.aclose()
    return Settled(
        spent=spent,
        authorization=authorization,
        state=dict(state),
        coder=coder,
        reviewer=reviewer,
        integration_before=integration_before,
    )


@pytest.mark.asyncio
async def test_the_recovery_acquires_a_new_execution_generation(settled: Settled):
    """Requirement 3. Settlement is a write, so it is fenced like any other."""
    assert settled.authorization.previous_generation == 1
    assert settled.authorization.generation == 2
    assert settled.authorization.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
    assert settled.authorization.settlement_only is True
    with settled.spent.factory() as session:
        run = settled.spent.run(session)
    assert run.execution_generation == 2
    # And given back on the way out: a settled run is not a run somebody is
    # inside.
    assert run.execution_owner is None


@pytest.mark.asyncio
async def test_the_settlement_keeps_the_same_task_run(settled: Settled):
    """Requirements 4, 5, 6. Same row, same name, same number."""
    spent = settled.spent
    with spent.factory() as session:
        run = spent.run(session)
        runs_of_task = TaskRunRepository(session).list_for_task(spent.task_id)
        total = session.scalar(select(func.count()).select_from(TaskRunRow))

    assert run.id == spent.run_id
    assert run.run_number == 2
    # The external identity the earlier run never needed and this one allocated
    # on its first artifact write. Unchanged by the settlement, which is the
    # claim: an operator who asks about RUN-... afterwards finds the same run.
    assert run.external_run_id is not None
    assert re.fullmatch(r"RUN-\d{8}-000001", run.external_run_id)
    assert len(runs_of_task) == 2
    assert total == 2


@pytest.mark.asyncio
async def test_the_settlement_does_not_move_the_attempt_accounting(settled: Settled):
    """Requirements 7, 8. Attempt 3 is where the run stopped, and where it stays."""
    spent = settled.spent
    with spent.factory() as session:
        run = spent.run(session)
        report_after = assess_recoverability(
            session, spent.run_id, settings=spent.settings
        )
        calls = ModelRunRepository(session).list_for_run(spent.run_id)

    assert run.attempt_number == 3
    assert report_after.attempts_started == 3
    # No fourth call of any purpose, successful or failed.
    assert [c.attempt for c in calls if c.attempt == 4] == []
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_the_settlement_starts_no_fourth_attempt(settled: Settled):
    """Requirements 9, 10. Nothing on disk and nothing in the rows says attempt 4."""
    spent = settled.spent
    directories = sorted(p.name for p in spent.run_directory().iterdir() if p.is_dir())
    assert directories == [
        "attempt-1-cycle-1",
        "attempt-2-cycle-1",
        "attempt-3-cycle-1",
    ]
    with spent.factory() as session:
        calls = ModelRunRepository(session).list_for_run(spent.run_id)
        events = RunEventRepository(session).list_for_run(spent.run_id)
    assert all((c.attempt or 0) <= 3 for c in calls)
    # No FIX_STARTED for a fourth attempt either: the loop advances the row
    # before an attempt, and it never advanced it.
    assert [
        e for e in events if e.event_type == RunEventType.FIX_STARTED and e.attempt == 4
    ] == []
    # The evidence of the attempts that were made is untouched.
    assert (
        spent.run_directory() / "attempt-3-cycle-1" / "prompt.txt"
    ).read_text("utf-8") == "the attempt-3 prompt"


@pytest.mark.asyncio
async def test_no_provider_and_no_verification_command_ran(settled: Settled):
    """Requirements 11, 12, 13. The negative evidence, from the witnesses.

    Asserted against the provider spies and the sentinel file rather than
    against the event log, because the event log can only say what was
    *recorded* -- and an unrecorded call is exactly the failure mode this whole
    sequence of concerns began with.
    """
    assert settled.coder.calls == []
    assert settled.reviewer.calls == []
    assert not settled.spent.sentinel.exists()
    with settled.spent.factory() as session:
        verifications = VerificationRunRepository(session).list_for_run(
            settled.spent.run_id
        )
    assert verifications == []


@pytest.mark.asyncio
async def test_the_settlement_builds_no_candidate_and_moves_no_integration_ref(
    settled: Settled,
):
    """Requirements 14, 15. Nothing was produced and nothing was accepted."""
    spent = settled.spent
    with spent.factory() as session:
        run = spent.run(session)
    assert run.candidate_commit is None
    assert spent.integration_sha() == settled.integration_before
    # The branch the run was on is where it was, too: settlement is not delivery.
    assert run.branch_name is not None


@pytest.mark.asyncio
async def test_the_fix_loop_settles_through_its_exhausted_attempt_path(
    settled: Settled,
):
    """Requirements 16, 17. The existing policy, reached and recorded.

    ``RETRY_EXHAUSTED`` on the run and an escalation whose reason is the same is
    the fix loop's own closing ``settle(ESCALATED, RETRY_EXHAUSTED)`` -- the
    branch that already existed, and that this concern only made reachable.
    """
    spent = settled.spent
    with spent.factory() as session:
        run = spent.run(session)
        escalations = EscalationRepository(session).list_for_task(spent.task_id)
        events = RunEventRepository(session).list_for_run(spent.run_id)

    assert settled.state["outcome"] == "ESCALATED"
    assert run.failure_reason == FailureReason.RETRY_EXHAUSTED.value
    assert len(escalations) == 1
    assert escalations[0].reason == FailureReason.RETRY_EXHAUSTED.value
    assert escalations[0].status is EscalationStatus.OPEN
    assert escalations[0].task_run_id == spent.run_id
    # The escalation a person reads says three attempts were made, because three
    # were -- not one, which is what deriving the count from this process's own
    # empty iteration list would have said.
    assert "3 of the task's 3 permitted attempts" in escalations[0].summary
    assert RunEventType.HUMAN_REVIEW_REQUIRED in {e.event_type for e in events}


@pytest.mark.asyncio
async def test_the_terminal_state_is_internally_consistent(settled: Settled):
    """Requirement 18. One ending, agreed on by the run, the task and the graph."""
    spent = settled.spent
    with spent.factory() as session:
        run = spent.run(session)
        task = TaskRepository(session).get(spent.task_id)

    assert run.status is RunStatus.FAILED
    assert run.completed_at is not None
    assert task.status is TaskStatus.HUMAN_REVIEW
    assert settled.state["outcome"] == "ESCALATED"
    # And the run is no longer in flight, which is what makes a second recovery
    # a refusal rather than a second settlement.
    with spent.factory() as session:
        report = assess_recoverability(
            session, spent.run_id, settings=spent.settings
        )
    assert report.recoverable is False
    assert {check.name for check in report.refusals} >= {
        "run_in_flight",
        "task_state_recoverable",
    }


@pytest.mark.asyncio
async def test_exactly_one_recovery_authorization_is_recorded(settled: Settled):
    """Requirement 19, and what the record says the recovery was for."""
    spent = settled.spent
    with spent.factory() as session:
        authorizations = [
            e
            for e in RunEventRepository(session).list_for_run(spent.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]

    assert len(authorizations) == 1
    payload = authorizations[0].payload
    assert payload["recovery_mode"] == "settlement_only"
    assert payload["previous_generation"] == 1
    assert payload["generation"] == 2
    assert payload["next_attempt"] == 4
    assert payload["attempts_started"] == 3
    assert payload["max_attempts"] == 3
    assert payload["run_number"] == 2
    assert payload["reason"].startswith("attempts 1-3 are spent")


@pytest.mark.asyncio
async def test_a_second_settlement_recovery_is_refused(settled: Settled):
    """Requirement 20, first half. A settled run is settled once.

    Not by an idempotence check written for this case, but because the first
    settlement made the run terminal -- and a terminal run has no execution to
    recover. The refusal an operator who retries the request sees is the same one
    concern 67 already gives for any finished run.
    """
    spent = settled.spent
    with spent.factory() as session, pytest.raises(EntityConflict) as conflict:
        recover_run(
            session,
            spent.run_id,
            reason="retrying the settlement request",
            settings=spent.settings,
        )
    assert "FAILED" in str(conflict.value)

    with spent.factory() as session:
        escalations = EscalationRepository(session).list_for_task(spent.task_id)
        authorizations = [
            e
            for e in RunEventRepository(session).list_for_run(spent.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
    assert len(escalations) == 1
    assert len(authorizations) == 1


def test_the_fenced_generation_cannot_persist_after_a_settlement_recovery(
    spent: Spent,
):
    """Requirement 21. The guarantee, not the policy.

    A dispatch still holding generation 1 -- the executor whose attempt-3 call
    timed out, had it somehow come back -- cannot commit anything once the
    settlement recovery has taken generation 2. Asserted while the run is still
    in flight, because that is where the *generation* is the thing doing the
    refusing: once settlement finishes, the run is terminal and concern 64's
    barrier answers first, which proves nothing about fencing.
    """
    with spent.factory.begin() as session:
        authorization = recover_run(
            session,
            spent.run_id,
            reason="settle the run; the old executor may yet return",
            settings=spent.settings,
        )
    assert authorization.generation == 2

    with spent.factory() as session:
        run = spent.run(session)
        assert run.status is RunStatus.RUNNING
        stale = durable_checkpoint(
            session, spent.run_id, session.commit, expected_generation=1
        )
        with pytest.raises(RunOwnershipLostError):
            stale()
        # The recovered generation, by contrast, is allowed to persist -- which
        # is what makes the settlement below possible at all.
        durable_checkpoint(
            session,
            spent.run_id,
            session.commit,
            expected_generation=authorization.generation,
        )()


@pytest.mark.asyncio
async def test_an_ordinary_continuation_recovery_still_executes_an_attempt(
    tmp_path: Path,
):
    """Requirement 22, the other half: the change did not make every recovery inert.

    One attempt spent of three, so there is real work to do. The coder here is a
    witness of the opposite kind -- it is *expected* to be asked -- and the proof
    that ``settlement_only`` is a genuine classification rather than the new
    behaviour of every recovery.
    """
    from apps.orchestrator.providers.errors import ModelUnavailable

    for signature in _spent(tmp_path, spent_attempts=1):
        with signature.factory.begin() as session:
            authorization = recover_run(
                session,
                signature.run_id,
                reason="one attempt remains",
                settings=signature.settings,
            )
        assert authorization.recovery_mode is RecoveryMode.CONTINUE

        coder = NeverCalledCoder()
        reviewer = NeverCalledReviewer()

        # Swap the assertion for a provider error, so the loop's *own* handling
        # of a failed attempt runs rather than the test's exception escaping.
        # The record of the call is what is being asserted on either way.
        async def generate(request, spy=coder):
            spy.calls.append(request)
            raise ModelUnavailable("the endpoint is down")

        coder.generate = generate  # type: ignore[method-assign]
        runner = WorkflowRunner(
            signature.factory,
            coder=coder,
            reviewer=reviewer,
            settings=signature.settings,
        )
        try:
            with contextlib.suppress(Exception):
                await runner.run(
                    signature.run_id,
                    acquired=(authorization.owner, authorization.generation),
                )
        finally:
            await runner.aclose()

        assert coder.calls, "a continuation recovery asked the coder for nothing"


# =============================================================================
# 4. A restarted process, which is the only process the live run will ever get.
# =============================================================================


_CHILD = """
import asyncio, json, sys, uuid
from sqlalchemy.orm import sessionmaker
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.repositories import (
    EscalationRepository,
    ModelRunRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.run_recovery import assess_recoverability, recover_run
from apps.orchestrator.workflow import WorkflowRunner
from tests.integration.test_concern68 import NeverCalledCoder, NeverCalledReviewer

db, artifacts, worktrees, run_id = sys.argv[1:5]
settings = Settings(
    _env_file=None,
    artifact_root=artifacts,
    worktree_root=worktrees,
    worker_backend=WorkerBackend.SUBPROCESS,
    worker_command_timeout_seconds=30,
)
engine = create_db_engine("sqlite:///" + db)
factory = sessionmaker(bind=engine, expire_on_commit=False)
run_uuid = uuid.UUID(run_id)

with factory() as session:
    report = assess_recoverability(session, run_uuid, settings=settings)
out = {"report": report.describe()}

with factory.begin() as session:
    authorization = recover_run(
        session,
        run_uuid,
        reason="settled from a fresh service process",
        requested_by="operator",
        settings=settings,
    )
out["generation"] = authorization.generation
out["recovery_mode"] = authorization.recovery_mode.value

coder, reviewer = NeverCalledCoder(), NeverCalledReviewer()
runner = WorkflowRunner(factory, coder=coder, reviewer=reviewer, settings=settings)


async def main():
    try:
        return dict(
            await runner.run(
                run_uuid, acquired=(authorization.owner, authorization.generation)
            )
        )
    finally:
        await runner.aclose()


state = asyncio.run(main())
out["outcome"] = state.get("outcome")
out["coder_calls"] = len(coder.calls)
out["reviewer_calls"] = len(reviewer.calls)
with factory() as session:
    run = TaskRunRepository(session).get(run_uuid)
    task = TaskRepository(session).get(run.task_id)
    out["run"] = {
        "id": str(run.id),
        "run_number": run.run_number,
        "external_run_id": run.external_run_id,
        "attempt_number": run.attempt_number,
        "status": str(run.status),
        "failure_reason": run.failure_reason,
        "execution_generation": run.execution_generation,
        "execution_owner": run.execution_owner,
        "candidate_commit": run.candidate_commit,
    }
    out["task_status"] = str(task.status)
    out["calls"] = len(ModelRunRepository(session).list_for_run(run_uuid))
    out["escalations"] = len(EscalationRepository(session).list_for_task(run.task_id))
    out["authorizations"] = len(
        [
            e
            for e in RunEventRepository(session).list_for_run(run_uuid)
            if str(e.event_type) == "RUN_RECOVERY_AUTHORIZED"
        ]
    )
engine.dispose()
print(json.dumps(out))
"""


def test_a_restarted_process_can_perform_the_settlement(spent: Spent):
    """Requirement 25. Nothing in this interpreter is what is being observed.

    The live ``RUN-20260928-000005`` will only ever be settled by a process that
    did not start it, so a settlement that depended on anything left in memory by
    whoever did would be no fix at all.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(spent.path),
            str(spent.settings.artifact_root),
            str(spent.settings.worktree_root),
            str(spent.run_id),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["report"]["recoverable"] is True
    assert result["report"]["recovery_mode"] == "settlement_only"
    assert result["report"]["next_attempt"] == 4
    assert result["recovery_mode"] == "settlement_only"
    assert result["generation"] == 2
    assert result["outcome"] == "ESCALATED"
    assert result["coder_calls"] == 0
    assert result["reviewer_calls"] == 0
    assert result["calls"] == 2
    assert result["escalations"] == 1
    assert result["authorizations"] == 1
    assert result["task_status"] == "HUMAN_REVIEW"
    assert result["run"]["id"] == str(spent.run_id)
    assert result["run"]["run_number"] == 2
    assert result["run"]["attempt_number"] == 3
    assert result["run"]["status"] == "FAILED"
    assert result["run"]["failure_reason"] == "RETRY_EXHAUSTED"
    assert result["run"]["execution_owner"] is None
    assert result["run"]["candidate_commit"] is None
    assert not spent.sentinel.exists()

    # And this process, reading through a rebuilt engine, agrees.
    with spent.rebuilt_factory()() as session:
        run = TaskRunRepository(session).get(spent.run_id)
    assert run.status is RunStatus.FAILED
    assert run.execution_generation == 2


# =============================================================================
# 5. The HTTP boundary.
# =============================================================================


@pytest.fixture
def api(spent: Spent, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{spent.path}")
    monkeypatch.setenv("ARTIFACT_ROOT", str(spent.settings.artifact_root))
    monkeypatch.setenv("WORKTREE_ROOT", str(spent.settings.worktree_root))
    from apps.orchestrator.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    reset_engine()
    app = create_app()
    with TestClient(app) as client:
        yield client
    reset_engine()
    settings_module.get_settings.cache_clear()


def test_the_eligibility_endpoint_reports_the_settlement_mode(
    api: TestClient, spent: Spent
):
    response = api.get(f"/runs/{spent.run_id}/recoverability")
    assert response.status_code == 200
    body = response.json()
    assert body["recoverable"] is True
    assert body["recovery_mode"] == "settlement_only"
    assert body["next_attempt"] == 4
    assert body["attempts_started"] == 3
    assert body["max_attempts"] == 3
    assert body["execution_generation"] == 1
    assert body["execution_owner"] is None
    assert body["candidate_commit"] is None
    assert [c for c in body["checks"] if not c["passed"]] == []
    assert not spent.sentinel.exists()


def test_the_recovery_mode_is_part_of_the_documented_contract(api: TestClient):
    schema = api.get("/openapi.json").json()["components"]["schemas"]
    assert "recovery_mode" in schema["RecoverabilityResponse"]["properties"]
    assert "recovery_mode" in schema["RunRecoveryResponse"]["properties"]
    # Concern 76 adds ``delivery_only``: a run whose review already approved a
    # committed candidate re-enters the ordinary delivery node without any model
    # work. It joins ``continue`` and ``settlement_only`` as a supported mode.
    assert set(schema["RecoveryMode"]["enum"]) == {
        "continue",
        "settlement_only",
        "delivery_only",
    }


# =============================================================================
# 6. PostgreSQL. The only authority on whether two settlements can both happen.
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
    server = _server_url()
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:  # pragma: no cover - no server available
        admin.dispose()
        pytest.skip(f"no PostgreSQL server for the race test at {server}: {exc}")
    name = f"settle_{uuid.uuid4().hex[:12]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.rsplit("/", 1)[0] + f"/{name}"
    finally:
        drop_test_database(server, name)
        admin.dispose()


@dataclass
class PgSpent:
    engine: Engine
    factory: sessionmaker
    settings: Settings
    run_id: uuid.UUID
    task_id: uuid.UUID


@pytest.fixture
def pg_spent(tmp_path: Path) -> Iterator[PgSpent]:
    with _scratch_postgres() as url:
        engine = create_db_engine(url)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        sentinel = tmp_path / "verification-ran.txt"
        repository = _build_repository(tmp_path, sentinel)
        settings = _settings(tmp_path)
        _project_id, task_id, run_id, _model_id = _seed(factory, repository, settings)
        try:
            yield PgSpent(
                engine=engine,
                factory=factory,
                settings=settings,
                run_id=run_id,
                task_id=task_id,
            )
        finally:
            engine.dispose()


@pytest.mark.integration
def test_two_simultaneous_settlement_recoveries_produce_one_owner(pg_spent: PgSpent):
    """Requirement 26, and requirement 20's other half.

    Two threads, one barrier, no sleeps. Both read the run at generation 1 and
    both correctly decide it is recoverable for settlement -- which is why the
    decision cannot be the guard. The guard is the acquisition: one ``UPDATE``
    whose predicate names the generation the request quoted, evaluated by
    PostgreSQL under the row lock. So there is one owner, one authorization, and
    therefore one settlement.
    """
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def worker(index: int) -> None:
        try:
            with pg_spent.factory() as session:
                report = assess_recoverability(
                    session, pg_spent.run_id, settings=pg_spent.settings
                )
                assert report.recoverable is True
                assert report.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
                assert report.execution_generation == 1
                barrier.wait(timeout=30)
                try:
                    authorization = recover_run(
                        session,
                        pg_spent.run_id,
                        reason=f"racing settlement {index}",
                        requested_by=f"operator-{index}",
                        settings=pg_spent.settings,
                    )
                    session.commit()
                    results[index] = authorization.generation
                except EntityConflict as conflict:
                    session.rollback()
                    results[index] = conflict
        except Exception as error:  # captured, then asserted on
            results[index] = error

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a racing thread hung"

    winners = [r for r in results if isinstance(r, int)]
    losers = [r for r in results if isinstance(r, EntityConflict)]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert winners[0] == 2

    with pg_spent.factory() as session:
        run = TaskRunRepository(session).get(pg_spent.run_id)
        authorizations = [
            e
            for e in RunEventRepository(session).list_for_run(pg_spent.run_id)
            if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED
        ]
        total_runs = session.scalar(select(func.count()).select_from(TaskRunRow))

    assert run.execution_generation == 2
    assert run.attempt_number == 3
    assert len(authorizations) == 1
    assert authorizations[0].payload["recovery_mode"] == "settlement_only"
    assert total_runs == 2


@pytest.mark.integration
def test_postgres_reports_the_settlement_reconstruction(pg_spent: PgSpent):
    """The exact RUN-20260928-000005 arithmetic, on the real database."""
    with pg_spent.factory() as session:
        report = assess_recoverability(
            session, pg_spent.run_id, settings=pg_spent.settings
        )
    assert (report.attempts_started, report.next_attempt, report.max_attempts) == (
        3,
        4,
        3,
    )
    assert report.recovery_mode is RecoveryMode.SETTLEMENT_ONLY
    assert report.recoverable is True
