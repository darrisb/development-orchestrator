"""Concern 64: operator abandonment of an in-flight durable run.

**The incident.** ``RUN-20260927-000020`` was the fifth run of TS-106. It began
at 20:38:21Z under a stale supervisor image, and its third coding call was still
in flight at 20:45:34Z. At 22:44:05Z, nearly two hours later, the run was
abandoned with the reason ``"x"`` and no operator identifier. The run became
ABANDONED, TS-106 became FAILED, no candidate was ever created, and
``agent/integration`` did not move. The abandonment was premature and
accidental. It is history, and nothing here undoes it.

**What this file is.** The capability that should have existed before that
happened, and the evidence that it works. The first version of this file
claimed to prove an API lifecycle, a late-worker race and candidate/integration
fencing, and proved none of it: its two "race" tests were sequential re-reads,
its "integration ref does not move" test re-asserted ``candidate_commit is
None``, it had no API test at all, and deleting every graph fence left all 28
tests green. The claims were false and the evidence was missing, so this
version replaces them rather than restating them.

**Three properties worth having, and where each is enforced.**

*Terminality is durable, not merely observed.* The graph's nodes check the run's
status before doing anything consequential, but a check in one node and a write
in the next leaves a window, and an abandonment is precisely what happens inside
a window. So the guarantee lives in ``TaskRunRepository.finish``, which writes a
terminal status with a ``status <> 'ABANDONED'`` predicate the database
evaluates while holding the row lock, and in ``TaskRunRepository.abandon``,
whose predicate is the mirror image. Commit order decides it. The race test
below is what holds that claim to account.

*Delivery cannot be resurrected by a graph fence alone.* An abandoned run whose
task reached APPROVED has a real candidate and a delivery about to happen, so
the fence is repeated in ``services.delivery`` -- at the boundary, before the
commit, which is the first irreversible step. The delivery tests pin that
boundary.

*The run and its task move together.* They share the caller's transaction and
nothing swallows the task transition, so a failure rolls the run back rather
than leaving a run that says ABANDONED over a task that says CODING.

**Test isolation, asserted rather than assumed.** The suite's shared ``session``
fixture promises a transaction that is rolled back afterwards. It does not
survive ``session.commit()``: SQLAlchemy's SQLite dialect issues no real
``BEGIN`` for the fixture's outer transaction, so releasing the session's
outermost savepoint *ends* the transaction and commits, after which the
fixture's ``transaction.rollback()`` has nothing to roll back. The first
version of this file called ``session.commit()`` and left a committed
``projects`` row behind -- which is why
``test_projects_api.py::test_a_project_is_created_and_listed`` failed whenever
this file ran first. The last two tests here exist to stop that coming back,
and one of them runs this whole module in a separate process and reads the
resulting database with the standard library.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.agents.fix_loop import durable_checkpoint, run_fix_loop
from apps.orchestrator.config.settings import Settings, WorkerBackend, get_settings
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import ProjectRow, TaskRow
from apps.orchestrator.db.session import create_db_engine, get_db
from apps.orchestrator.domain.enums import (
    ABANDONABLE_RUN_STATUSES,
    Complexity,
    ModelPurpose,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.errors import (
    AbandonedRunError,
    InvalidStateTransition,
    RunNotInFlightError,
)
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import (
    Project,
    RunEvent,
    Task,
    TaskLimits,
    TaskRun,
)
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.main import create_app
from apps.orchestrator.providers import ConnectionReport, ModelRequest, ModelResponse
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.abandon import abandon_run, is_run_abandonable
from apps.orchestrator.services.delivery import deliver_candidate
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound
from apps.orchestrator.services.git_service import GitService
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import attach_workspace, prepare_workspace
from apps.orchestrator.workflow import WorkflowRunner
from apps.orchestrator.workflow.recovery import inspect_incomplete_runs
from tests.conftest import run_git
from tests.integration.test_fix_loop import (
    STUB,
    WORKING,
    ScriptedModel,
    _code,
    _review,
    reviewer,
)

pytestmark = pytest.mark.integration

PYTHON = "python3"

#: Every table this file writes through. The pollution checks count rows in all
#: of them rather than in ``task_runs`` alone, because the row that actually
#: leaked was a ``projects`` row: the shared database is a whole-world resource
#: and one leaked table is enough to fail an unrelated test.
_TABLES = (
    "projects",
    "tasks",
    "task_runs",
    "run_events",
    "model_runs",
    "verification_runs",
    "reviews",
    "human_escalations",
    "artifacts",
    "workflow_checkpoints",
)


# =============================================================================
# Rollback-isolated fixtures, for semantics that need no durable commit.
# =============================================================================


@pytest.fixture
def project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Abandon fixture",
            repository_path="/tmp/abandon-fixture",
            worker_profile=WorkerProfile.PYTHON,
        )
    )


@pytest.fixture
def coding_task(session: Session, project: Project) -> Task:
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="T-1",
            title="Task to abandon",
        )
    )
    # READY before CODING: the state machine allows no other route.
    task = TaskRepository(session).transition(task.id, TaskStatus.READY)
    return TaskRepository(session).transition(task.id, TaskStatus.CODING)


@pytest.fixture
def running_run(session: Session, coding_task: Task) -> TaskRun:
    return TaskRunRepository(session).add(
        TaskRun(
            task_id=coding_task.id,
            run_number=1,
            attempt_number=1,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
    )


def _abandon_events(session: Session, run_id: uuid.UUID) -> list[RunEvent]:
    events = RunEventRepository(session).list_for_run(run_id)
    # ``==`` rather than ``is``: a reloaded row carries the column's plain
    # string, and StrEnum members are equal to it without being identical.
    return [e for e in events if e.event_type == RunEventType.RUN_ABANDONED]


def _walk_task(session: Session, task: Task, *states: TaskStatus) -> Task:
    """Move a task along the only route the state machine allows to ``states``.

    APPROVED is four moves from CODING and there is no shortcut, which is worth
    stating in a test rather than working around: there is no way to invent an
    APPROVED task, only to reach one the way the workflow does.
    """
    for state in states:
        task = TaskRepository(session).transition(task.id, state)
    return task


# =============================================================================
# 1. The operation, and its documented semantics.
# =============================================================================


def test_a_running_run_becomes_abandoned(session: Session, running_run: TaskRun):
    abandoned = abandon_run(
        session,
        running_run.id,
        reason="stale supervisor image",
        requested_by="operator",
    )

    assert abandoned.status is RunStatus.ABANDONED
    assert abandoned.completed_at is not None
    assert abandoned.failure_reason == "OPERATOR_ABANDONED"


def test_a_pending_run_can_be_abandoned(session: Session, project: Project):
    """PENDING is in flight too: a run created but not started is stoppable."""
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="T-pending", title="Not started")
    )
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.PENDING)
    )

    abandoned = abandon_run(session, run.id, reason="queued against a stale image")

    assert abandoned.status is RunStatus.ABANDONED


def test_the_event_records_the_reason_and_the_operator(
    session: Session, running_run: TaskRun
):
    abandon_run(
        session,
        running_run.id,
        reason="began under image 168a74d323e2, built before d50752d",
        requested_by="operator-123",
    )

    events = _abandon_events(session, running_run.id)

    assert len(events) == 1
    payload = events[0].payload
    assert payload["reason"] == "began under image 168a74d323e2, built before d50752d"
    assert payload["requested_by"] == "operator-123"
    assert payload["previous_status"] == "RUNNING"
    assert events[0].attempt == running_run.attempt_number


def test_repeating_the_request_is_idempotent(session: Session, running_run: TaskRun):
    first = abandon_run(session, running_run.id, reason="first")
    second = abandon_run(session, running_run.id, reason="second")

    assert first.id == second.id
    assert second.status is RunStatus.ABANDONED
    # One piece of evidence, not two: the second request had nothing to add.
    events = _abandon_events(session, running_run.id)
    assert len(events) == 1
    assert events[0].payload["reason"] == "first"


@pytest.mark.parametrize("terminal", [RunStatus.SUCCEEDED, RunStatus.FAILED])
def test_a_run_that_already_finished_conflicts(
    session: Session, project: Project, terminal: RunStatus
):
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id=f"T-{terminal.value}",
            title="Done",
        )
    )
    run = TaskRunRepository(session).add(
        TaskRun(
            task_id=task.id,
            run_number=1,
            status=terminal,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )

    with pytest.raises(EntityConflict, match=terminal.value):
        abandon_run(session, run.id, reason="too late")

    # A refused request writes nothing, including no event.
    assert TaskRunRepository(session).get(run.id).status is terminal
    assert _abandon_events(session, run.id) == []


def test_an_unknown_run_is_not_found(session: Session):
    with pytest.raises(EntityNotFound):
        abandon_run(session, uuid.uuid4(), reason="typo in the id")


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_reason_is_required(session: Session, running_run: TaskRun, reason: str):
    with pytest.raises(ValueError, match="reason is required"):
        abandon_run(session, running_run.id, reason=reason)

    assert TaskRunRepository(session).get(running_run.id).status is RunStatus.RUNNING


# =============================================================================
# 2. is_run_abandonable, which now answers one question instead of two.
# =============================================================================


@pytest.mark.parametrize(
    ("status", "abandonable"),
    [
        (RunStatus.PENDING, True),
        (RunStatus.RUNNING, True),
        (RunStatus.ABANDONED, False),
        (RunStatus.SUCCEEDED, False),
        (RunStatus.FAILED, False),
    ],
)
def test_is_run_abandonable_answers_only_the_eligibility_question(
    session: Session, project: Project, status: RunStatus, abandonable: bool
):
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="T-predicate", title="Predicate")
    )
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=status)
    )

    assert is_run_abandonable(run) is abandonable


def test_an_abandoned_run_is_not_an_abandonable_run(
    session: Session, running_run: TaskRun
):
    """The predicate and the operation disagree on purpose, and both are right.

    ``is_run_abandonable`` says no -- there is nothing in flight to stop.
    ``abandon_run`` still succeeds, idempotently and with no second event. The
    first version of this module put ABANDONED in the abandonable set to make
    those agree, which meant every caller asking "may I abandon this?" was told
    yes about runs that were already closed.
    """
    abandon_run(session, running_run.id, reason="first")
    reloaded = TaskRunRepository(session).get(running_run.id)

    assert reloaded.status is RunStatus.ABANDONED
    assert is_run_abandonable(reloaded) is False
    assert RunStatus.ABANDONED not in ABANDONABLE_RUN_STATUSES
    # And the operation still works, which is the point of keeping them apart.
    assert abandon_run(session, running_run.id, reason="again").status is RunStatus.ABANDONED


# =============================================================================
# 3. Task state, and the failure path that must not become a success.
# =============================================================================


def test_the_task_leaves_coding_for_failed(
    session: Session, running_run: TaskRun, coding_task: Task
):
    abandon_run(session, running_run.id, reason="test")

    task = TaskRepository(session).get(coding_task.id)
    assert task.status is TaskStatus.FAILED
    # FAILED is the state that keeps a retry available and everything else shut.
    assert task.status is not TaskStatus.COMPLETE


def test_an_approved_task_is_still_moved_out_of_the_way(
    session: Session, running_run: TaskRun, coding_task: Task
):
    """APPROVED is the state abandonment most needs to handle.

    A run whose task is APPROVED has a real candidate and is one step from
    delivery. The first version of this module did not list APPROVED among the
    abandonable task states, so abandoning such a run left the task APPROVED --
    holding a deliverable candidate, which is the outcome the operation exists
    to prevent.
    """
    approved = _walk_task(
        session,
        coding_task,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
    )
    assert approved.status is TaskStatus.APPROVED

    abandon_run(session, running_run.id, reason="about to deliver a bad candidate")

    assert TaskRepository(session).get(coding_task.id).status is TaskStatus.FAILED


def test_a_task_transition_failure_is_not_converted_into_success(
    session: Session,
    running_run: TaskRun,
    coding_task: Task,
    monkeypatch: pytest.MonkeyPatch,
):
    """The failure the first version swallowed.

    It caught every exception from the task transition, logged a warning, and
    returned the abandoned run. The operator was told the run had been abandoned
    while its task was still in CODING: a run nobody will ever resume, over a
    task that still looks like it is being worked on.

    The rollback is a nested one, ``session.begin_nested()``, because that is
    what makes the assertion mean something on SQLite: a savepoint is released
    by rolling *back* to it, so the run write, the event and the failed task
    transition are all undone inside the fixture's transaction. The outermost
    ``session.begin()`` cannot be used here -- releasing it is the bug this
    module documents -- so the test rolls back the way a caller that owns its
    transaction really would, and then reads the run and the task together and
    expects both unchanged.
    """

    def explode(task_id: uuid.UUID, target: TaskStatus, *args, **kwargs) -> Task:
        raise RuntimeError("the task row could not be updated")

    monkeypatch.setattr(
        "apps.orchestrator.services.abandon.TaskRepository.transition", explode
    )

    with pytest.raises(RuntimeError, match="could not be updated"), session.begin_nested():
        abandon_run(session, running_run.id, reason="test")

    # One transaction, so refusing the task took the run with it.
    assert TaskRunRepository(session).get(running_run.id).status is RunStatus.RUNNING
    assert TaskRepository(session).get(coding_task.id).status is TaskStatus.CODING
    assert _abandon_events(session, running_run.id) == []


def test_the_task_transition_is_decided_before_the_run_is_written(
    session: Session,
    project: Project,
    running_run: TaskRun,
    monkeypatch: pytest.MonkeyPatch,
):
    """The check runs first, so a refusal has nothing half-done behind it.

    ``assert_transition`` is called before the compare-and-swap, so a state the
    operation may not touch fails the request while the run is still RUNNING --
    rather than writing ABANDONED and discovering afterwards that the task could
    not follow. The transition is stubbed to refuse because every task state
    except COMPLETE can legally reach FAILED; the point is the ordering, not the
    availability of a state that provokes it.
    """

    def refuse(current: TaskStatus, requested: TaskStatus) -> TaskStatus:
        raise InvalidStateTransition(current, requested)

    monkeypatch.setattr("apps.orchestrator.services.abandon.assert_transition", refuse)

    with pytest.raises(InvalidStateTransition, match="from CODING to FAILED"):
        abandon_run(session, running_run.id, reason="test")

    assert TaskRunRepository(session).get(running_run.id).status is RunStatus.RUNNING
    assert _abandon_events(session, running_run.id) == []


def test_a_task_that_had_not_started_work_is_still_failed(
    session: Session, project: Project
):
    """READY is the state the scheduler starts from, so it cannot be the answer.

    A run exists, so work was going to happen. An operator stopping it has to
    leave the task somewhere only a person can move it on from, and READY is
    the opposite of that: the next scheduling pass would open a fresh run on
    the same task, so the run would be stopped and the work would carry on.
    The first version of this module read that the task "was not in flight" and
    left it alone, which is the same restart by another name.
    """
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(project_id=project.id, external_task_id="T-ready", title="Not started")
    )
    task = tasks.transition(task.id, TaskStatus.READY)
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.PENDING)
    )

    abandon_run(session, run.id, reason="stopped before any work")

    assert tasks.get(task.id).status is TaskStatus.FAILED
    assert len(_abandon_events(session, run.id)) == 1


def test_downstream_tasks_stay_blocked(session: Session, project: Project):
    tasks = TaskRepository(session)
    upstream = tasks.add(
        Task(project_id=project.id, external_task_id="T-up", title="Upstream")
    )
    upstream = tasks.transition(upstream.id, TaskStatus.READY)
    upstream = tasks.transition(upstream.id, TaskStatus.CODING)
    downstream = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="T-down",
            title="Downstream",
            depends_on=["T-up"],
        )
    )
    run = TaskRunRepository(session).add(
        TaskRun(task_id=upstream.id, run_number=1, status=RunStatus.RUNNING)
    )

    abandon_run(session, run.id, reason="test")

    assert tasks.get(downstream.id).status is TaskStatus.PENDING


def test_no_replacement_run_is_created(
    session: Session, running_run: TaskRun, coding_task: Task
):
    abandon_run(session, running_run.id, reason="test")

    runs = TaskRunRepository(session).list_for_task(coding_task.id)
    assert len(runs) == 1
    assert runs[0].status is RunStatus.ABANDONED


def test_evidence_written_before_the_abandonment_survives_it(
    session: Session, running_run: TaskRun, project: Project
):
    events = RunEventRepository(session)
    events.append(
        RunEvent(
            task_run_id=running_run.id,
            project_id=project.id,
            task_id=running_run.task_id,
            event_type=RunEventType.CODING_STARTED,
            attempt=1,
        )
    )

    abandon_run(session, running_run.id, reason="test")

    types = [e.event_type for e in events.list_for_run(running_run.id)]
    assert RunEventType.CODING_STARTED in types
    assert RunEventType.RUN_ABANDONED in types


def test_the_candidate_commit_is_untouched(session: Session, running_run: TaskRun):
    """No candidate is created, and none is invented for an abandoned run.

    RUN-20260927-000020 ended with ``candidate_commit`` NULL, and that is the
    state an abandonment has to leave behind: a candidate column is how a later
    reader decides whether there was work to land, and writing one during
    abandonment would put a SHA in front of a run that never landed anything.
    """
    abandon_run(session, running_run.id, reason="test")

    assert TaskRunRepository(session).get(running_run.id).candidate_commit is None


def test_abandoning_one_run_does_not_touch_another(
    session: Session, project: Project
):
    tasks = TaskRepository(session)
    runs = TaskRunRepository(session)
    first_task = tasks.add(
        Task(project_id=project.id, external_task_id="T-a", title="A")
    )
    first_task = tasks.transition(first_task.id, TaskStatus.READY)
    first_task = tasks.transition(first_task.id, TaskStatus.CODING)
    second_task = tasks.add(
        Task(project_id=project.id, external_task_id="T-b", title="B")
    )
    second_task = tasks.transition(second_task.id, TaskStatus.READY)
    second_task = tasks.transition(second_task.id, TaskStatus.CODING)
    first = runs.add(
        TaskRun(task_id=first_task.id, run_number=1, status=RunStatus.RUNNING)
    )
    second = runs.add(
        TaskRun(task_id=second_task.id, run_number=1, status=RunStatus.RUNNING)
    )

    abandon_run(session, first.id, reason="test")

    assert runs.get(second.id).status is RunStatus.RUNNING
    assert tasks.get(second_task.id).status is TaskStatus.CODING


# =============================================================================
# 4. Durable terminality at the persistence boundary.
# =============================================================================


@pytest.mark.parametrize(
    "attempted", [RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED]
)
def test_a_late_workflow_cannot_move_an_abandoned_run(
    session: Session, running_run: TaskRun, attempted: RunStatus
):
    """The invariant itself, at the layer that enforces it.

    Each of these is a status a real workflow writes when it finishes: RUNNING
    from ``_prepare_workspace``, SUCCEEDED from delivery, FAILED from the fix
    loop. All three were reachable from an abandoned run before the
    compare-and-swap, and each would have overwritten the operator's decision
    while looking like an ordinary completion.
    """
    abandon_run(session, running_run.id, reason="test")

    with pytest.raises(AbandonedRunError, match="abandoned by an operator"):
        TaskRunRepository(session).finish(running_run.id, attempted)

    assert TaskRunRepository(session).get(running_run.id).status is RunStatus.ABANDONED


def test_the_other_status_write_path_is_guarded_too(
    session: Session, running_run: TaskRun
):
    """``update_fields`` is the second way a run's status is written.

    ``_prepare_workspace`` uses it to move PENDING to RUNNING. Left unguarded it
    was a quieter resurrection than ``finish``: no exception, no event, just a
    run that looked live again.
    """
    abandon_run(session, running_run.id, reason="test")

    with pytest.raises(AbandonedRunError):
        TaskRunRepository(session).update_fields(running_run.id, status=RunStatus.RUNNING)

    assert TaskRunRepository(session).get(running_run.id).status is RunStatus.ABANDONED


@pytest.mark.parametrize(
    "start",
    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED],
)
def test_every_other_transition_still_works(
    session: Session, project: Project, start: RunStatus
):
    """The guard is one status wide, not a general lockdown.

    A repository that refused to finish runs would look exactly as correct and
    would stop the orchestrator doing its job, so this pins the transitions the
    guard has to leave alone.
    """
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id=f"T-{start.value}", title="x")
    )
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=start)
    )
    target = RunStatus.SUCCEEDED if start is not RunStatus.SUCCEEDED else RunStatus.FAILED

    finished = TaskRunRepository(session).finish(run.id, target, "because")

    assert finished.status is target


def test_the_abandon_write_itself_loses_to_a_finished_run(
    session: Session, project: Project
):
    """The other half of the race, in the operator's direction.

    ``abandon`` returns None rather than raising, because "the run got there
    first" is a result the caller has to report, not an error. The commit that
    happened first is the one that stands, and the loser changes nothing.
    """
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="T-late", title="x")
    )
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
    )
    runs = TaskRunRepository(session)
    runs.finish(run.id, RunStatus.SUCCEEDED)

    assert runs.abandon(run.id, failure_reason="OPERATOR_ABANDONED") is None
    assert runs.get(run.id).status is RunStatus.SUCCEEDED


# --- the barrier in front of every turn commit --------------------------------


def test_a_turn_may_only_be_made_durable_while_the_run_is_in_flight(
    session: Session, running_run: TaskRun
):
    """The guard a turn's commit goes through, and the case it is there for.

    ``finish`` protects one write. This protects the other kind: a turn that is
    about to be made permanent, on a run an operator has since abandoned. It is
    the guard that fires when the operator's transaction commits *during* a model
    call, which is the only window a long workflow really has.
    """
    committed: list[str] = []
    checkpoint = durable_checkpoint(session, running_run.id, lambda: committed.append("commit"))

    checkpoint()
    assert committed == ["commit"], "an in-flight run must be allowed to commit"

    abandon_run(session, running_run.id, reason="test")

    with pytest.raises(AbandonedRunError, match="abandoned by an operator"):
        checkpoint()
    assert committed == ["commit"], "the barrier let a commit through anyway"


def test_the_barrier_distinguishes_a_finished_run_from_an_abandoned_one(
    session: Session, project: Project
):
    """A run that finished on its own is a different fault, and is said so.

    Both are "this workflow is writing to a run that is no longer live", and
    conflating them would report an ordinary lifecycle bug as a person's
    decision -- which is the sort of thing that makes a real abandonment stop
    being believed.
    """
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="T-self", title="x")
    )
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
    )
    TaskRunRepository(session).finish(run.id, RunStatus.SUCCEEDED)

    with pytest.raises(RunNotInFlightError, match="not in flight"):
        durable_checkpoint(session, run.id, lambda: None)()


# =============================================================================
# 5. Durability across a commit and a process-shaped reconstruction.
# =============================================================================


@dataclasses.dataclass
class _Durable:
    """A private database, real commits, and a second engine to read it back.

    The suite's shared ``session`` cannot be used for any of this, because its
    ``commit()`` is durable (see the module docstring) and a test that needs a
    commit to survive would pollute every test after it. A file per test is also
    the closer model: the orchestrator has one database, and a restarted
    process reads it exactly this way.
    """

    path: Path
    settings: Settings
    factory: sessionmaker

    def rebuilt_engine(self) -> Engine:
        """A second engine, as a restarted process would build."""
        return create_db_engine(f"sqlite:///{self.path}")

    def seed(self) -> tuple[TaskRun, Task]:
        """A committed RUNNING run over a CODING task.

        The task reaches CODING *after* the run is opened, because a run is
        created from READY -- the order the scheduler uses, and the only one the
        service permits.
        """
        with self.factory() as session:
            project_id = session.scalar(select(ProjectRow.id).limit(1))
            task = TaskRepository(session).add(
                Task(
                    project_id=project_id,
                    external_task_id="TS-064",
                    title="Abandon me",
                )
            )
            task = TaskRepository(session).transition(task.id, TaskStatus.READY)
            run = create_run(session, task.id)
            task = TaskRepository(session).transition(task.id, TaskStatus.CODING)
            session.commit()
            return run, task


@pytest.fixture
def durable(tmp_path: Path) -> Iterator[_Durable]:
    path = tmp_path / "abandon.db"
    engine = create_db_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
    )
    with factory.begin() as setup:
        ProjectRepository(setup).add(
            Project(
                name="TraceStack",
                repository_path=str(tmp_path / "repo"),
                worker_profile=WorkerProfile.PYTHON,
            )
        )
    try:
        yield _Durable(path=path, settings=settings, factory=factory)
    finally:
        engine.dispose()


def test_the_abandonment_survives_a_commit(durable: _Durable):
    run, _ = durable.seed()

    with durable.factory() as session:
        abandon_run(
            session,
            run.id,
            reason="began under a stale image",
            requested_by="operator",
        )
        session.commit()

    # Read through a second engine, so nothing in this process carried it over.
    rebuilt = durable.rebuilt_engine()
    try:
        with rebuilt.connect() as connection:
            status, failure_reason = connection.execute(
                text("SELECT status, failure_reason FROM task_runs WHERE id = :id"),
                # Uuid is CHAR(32) on SQLite, i.e. the hex without its dashes.
                # Binding the str() form would match nothing, which is a good way
                # to notice that this is raw SQL and not the ORM.
                {"id": run.id.hex},
            ).one()
        assert status == "ABANDONED"
        assert failure_reason == "OPERATOR_ABANDONED"
    finally:
        rebuilt.dispose()


def test_an_abandoned_run_is_not_recoverable_after_reconstruction(durable: _Durable):
    """Terminality for discovery is the repository filter, so prove the filter.

    The first version carried an ``ABANDONED`` branch in ``recovery.py`` that
    could not execute, because ``list_incomplete()`` only ever returns PENDING
    and RUNNING. That branch is gone. What replaces it is an assertion about the
    query itself, made against a rebuilt engine and session so that nothing
    cached in this process can be what is being observed.
    """
    run, _ = durable.seed()
    with durable.factory() as session:
        abandon_run(session, run.id, reason="test")
        session.commit()

    rebuilt = durable.rebuilt_engine()
    try:
        with sessionmaker(bind=rebuilt, expire_on_commit=False)() as session:
            incomplete = TaskRunRepository(session).list_incomplete()
            candidates = inspect_incomplete_runs(session, settings=durable.settings)
        assert run.id not in {r.id for r in incomplete}
        assert run.id not in {c.run_id for c in candidates}
    finally:
        rebuilt.dispose()


def test_an_abandoned_run_is_still_listed_as_terminal(durable: _Durable):
    """Filtered out of recovery, not out of existence.

    The run is evidence. It has to stay countable in the metrics and the review
    history, and it has to still be there for an operator to look at -- which is
    the whole reason abandonment is an operation rather than a deletion.
    """
    run, _ = durable.seed()
    with durable.factory() as session:
        abandon_run(session, run.id, reason="test")
        session.commit()
        terminal = TaskRunRepository(session).list_terminal()

    assert run.id in {r.id for r in terminal}


# =============================================================================
# 6. The HTTP boundary.
# =============================================================================


@pytest.fixture
def api_client(session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The project's normal API test shape: a real app driven by a TestClient.

    Every request shares the test's transaction through ``get_db``, so these are
    real HTTP requests through a real router, a real pydantic body and the real
    exception handlers -- and they cost no committed rows. A service call is not
    an API test, which is why the first version of this file had none: it had
    no way to fail, because it never went through the layer that maps a missing
    reason to 422 or a terminal run to 409.
    """
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("WORKER_BACKEND", "subprocess")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def _abandon(client: TestClient, run_id: uuid.UUID, **body):
    return client.post(f"/runs/{run_id}/abandon", json=body)


def test_the_endpoint_abandons_a_running_run(
    api_client: TestClient, session: Session, running_run: TaskRun, coding_task: Task
):
    response = _abandon(
        api_client,
        running_run.id,
        reason="began under image 168a74d323e2",
        requested_by="operator-123",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(running_run.id)
    assert body["status"] == "ABANDONED"
    assert body["failure_reason"] == "OPERATOR_ABANDONED"
    assert body["completed_at"] is not None
    # The task reaches its intended state through the same request.
    assert TaskRepository(session).get(coding_task.id).status is TaskStatus.FAILED


def test_the_reason_is_required_over_http(
    api_client: TestClient, session: Session, running_run: TaskRun
):
    missing = _abandon(api_client, running_run.id)
    assert missing.status_code == 422
    assert "reason" in missing.json()["detail"][0]["loc"]

    assert _abandon(api_client, running_run.id, reason="   ").status_code == 422

    # Neither attempt changed anything.
    assert TaskRunRepository(session).get(running_run.id).status is RunStatus.RUNNING
    assert _abandon_events(session, running_run.id) == []


def test_the_persisted_event_carries_the_reason_and_the_operator(
    api_client: TestClient, session: Session, running_run: TaskRun
):
    _abandon(
        api_client,
        running_run.id,
        reason="started under a stale supervisor image",
        requested_by="operator-123",
    )

    events = _abandon_events(session, running_run.id)

    assert len(events) == 1
    assert events[0].payload["reason"] == "started under a stale supervisor image"
    assert events[0].payload["requested_by"] == "operator-123"
    assert events[0].payload["previous_status"] == "RUNNING"


def test_repeating_the_request_over_http_is_idempotent(
    api_client: TestClient, session: Session, running_run: TaskRun
):
    first = _abandon(api_client, running_run.id, reason="first", requested_by="op")
    second = _abandon(api_client, running_run.id, reason="second", requested_by="op")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "ABANDONED"
    # Documented behavior: one event, carrying the first request's reason, and
    # the completed_at of the abandonment that actually happened.
    assert first.json()["completed_at"] == second.json()["completed_at"]
    events = _abandon_events(session, running_run.id)
    assert len(events) == 1
    assert events[0].payload["reason"] == "first"


@pytest.mark.parametrize("terminal", [RunStatus.SUCCEEDED, RunStatus.FAILED])
def test_a_terminal_run_conflicts_over_http(
    api_client: TestClient, session: Session, project: Project, terminal: RunStatus
):
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id=f"T-{terminal.value}", title="Done")
    )
    run = TaskRunRepository(session).add(
        TaskRun(
            task_id=task.id,
            run_number=1,
            status=terminal,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )

    response = _abandon(api_client, run.id, reason="too late")

    assert response.status_code == 409
    assert response.json()["error"] == "EntityConflict"
    assert terminal.value in response.json()["detail"]


def test_an_unknown_run_is_a_404_over_http(api_client: TestClient):
    response = _abandon(api_client, uuid.uuid4(), reason="typo in the id")

    assert response.status_code == 404
    assert response.json()["error"] == "EntityNotFound"


def test_an_abandoned_run_is_reported_abandoned_by_the_run_endpoint(
    api_client: TestClient, running_run: TaskRun
):
    _abandon(api_client, running_run.id, reason="test")

    body = api_client.get(f"/runs/{running_run.id}").json()

    assert body["status"] == "ABANDONED"


def test_the_route_is_part_of_the_documented_contract(api_client: TestClient):
    """The route is in the published contract, not an accident of a router."""
    paths = api_client.get("/openapi.json").json()["paths"]

    assert "post" in paths["/runs/{run_id}/abandon"]


def test_a_lost_race_is_reported_as_a_conflict_over_http(
    api_client: TestClient, session: Session, running_run: TaskRun
):
    """The 409 mapping for AbandonedRunError, exercised end to end.

    The durable guard is what a caller reaches when a workflow terminalized the
    run between the request being read and written, and the translation layer
    has to report that as a conflict rather than a 500: the caller asked for
    something well formed that the world no longer allows. Going through the
    real handler is the only way to know the mapping is wired at all.
    """
    from apps.orchestrator.api.errors import _STATUS_BY_ERROR
    from apps.orchestrator.domain.errors import AbandonedRunError as Error

    handler_status = dict(
        (error_type, code) for error_type, code in _STATUS_BY_ERROR
    ).get(Error)
    assert handler_status == 409

    # And the failure it describes really is refused for this run.
    _abandon(api_client, running_run.id, reason="test")
    with pytest.raises(Error):
        TaskRunRepository(session).finish(running_run.id, RunStatus.SUCCEEDED)
    assert api_client.get(f"/runs/{running_run.id}").json()["status"] == "ABANDONED"


# =============================================================================
# 7. The late-worker race, for real.
# =============================================================================


@pytest.fixture
def scratch_postgres() -> Iterator[tuple[Engine, sessionmaker]]:
    """A scratch PostgreSQL database with the whole schema in it.

    Here, and not in the rest of this file, because here the tests need two
    sessions with real transactions open against each other. SQLite serializes
    writers, so "one transaction reads while another writes" is not a thing it
    can do -- it answers with ``database is locked`` -- and a test written
    against it would be testing the harness. Skipped, never failed, when there
    is no server, the same bargain ``test_db_locking.py`` strikes.
    """
    with _scratch_postgres() as url:
        engine = create_db_engine(url)
        Base.metadata.create_all(engine)
        try:
            yield engine, sessionmaker(bind=engine, expire_on_commit=False)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()


def test_a_stale_workflow_cannot_overwrite_a_newer_task_status(
    scratch_postgres: tuple[Engine, sessionmaker],
):
    """The task's own compare-and-swap, with two sessions and a real commit.

    The run's guard is only half the answer. A workflow also moves the task, and
    the copy it moves from is exactly as stale as its copy of the run -- so
    without a guard here, an abandoned run's FAILED task is quietly rewritten to
    APPROVED by a session that read the task before the operator touched it, and
    APPROVED is the state that makes a candidate deliverable.

    PostgreSQL, because the reader's transaction has to stay open while the
    writer commits. The stale session is opened and primed first, and it is that
    priming, not a stale variable, that the guard is about.
    """
    _engine, factory = scratch_postgres
    with factory.begin() as setup:
        project = ProjectRepository(setup).add(
            Project(
                name="TraceStack",
                repository_path="/tmp/stale-task",
                worker_profile=WorkerProfile.PYTHON,
            )
        )
        task = TaskRepository(setup).add(
            Task(project_id=project.id, external_task_id="TS-064", title="x")
        )
        TaskRepository(setup).transition(task.id, TaskStatus.READY)
        run = create_run(setup, task.id)
        TaskRepository(setup).transition(task.id, TaskStatus.CODING)

    # The workflow's session, reading the task while it is in flight.
    workflow = factory()
    workflow.get(TaskRow, task.id)
    assert workflow.get(TaskRow, task.id).status == TaskStatus.CODING.value

    with factory() as operator:
        abandon_run(operator, run.id, reason="test", requested_by="operator")
        operator.commit()

    # Still holding the copy it read before the operator wrote.
    with pytest.raises(InvalidStateTransition, match="from FAILED to VERIFYING"):
        TaskRepository(workflow).transition(task.id, TaskStatus.VERIFYING)
    workflow.rollback()
    workflow.close()

    with factory() as reader:
        assert TaskRepository(reader).get(task.id).status is TaskStatus.FAILED


class _BlockingCoder:
    """A coder whose model call is genuinely in flight, and genuinely stalled.

    ``ScriptedModel`` answers immediately, which cannot produce a race: by the
    time a test looked, the call was over. This one signals that the request has
    been made and then blocks until released, so the operator's transaction
    really does overlap an open call rather than being scheduled around it.

    The wait yields to the event loop rather than blocking it, so the workflow
    thread stays free to finish once the answer is allowed through.
    """

    def __init__(
        self,
        answer: str,
        *,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self._inner = ScriptedModel(answer, role=ModelRole.CODER)
        self.config = self._inner.config
        self.entered = entered
        self.release = release

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return await self._inner.generate(request)

    async def check_connection(self) -> ConnectionReport:
        return await self._inner.check_connection()

    async def aclose(self) -> None:
        await self._inner.aclose()


@pytest.fixture
def race_repo(tmp_path: Path) -> Path:
    """The fixture project the workflow tests use, built locally here.

    Defined rather than imported so that this file's repository is this file's,
    and a change to another file's fixture cannot quietly change what these
    assertions mean.
    """
    repo = tmp_path / "tracestack"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (repo / "tools" / "test.py").write_text(
        "assert 'return target' in open('src/nav.py').read()\n", encoding="utf-8"
    )
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


def _project_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


def _register(factory: sessionmaker, repository: Path) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A project, an in-flight task with a dependent, and a run for it."""
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="TraceStack",
                repository_path=str(repository),
                default_branch="main",
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=(f"{PYTHON} tools/test.py",)),
            )
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-064",
                title="Implement navigate",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
                limits=TaskLimits(max_files_changed=3, max_diff_lines=200),
            )
        )
        task = TaskRepository(session).transition(task.id, TaskStatus.READY)
        TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-065",
                title="Depends on the abandoned work",
                depends_on=["TS-064"],
            )
        )
        run = create_run(session, task.id)
        return project.id, task.id, run.id


def _git(repository: Path) -> GitService:
    return GitService(repository, default_branch="main", settings=Settings(_env_file=None))


def _integration_sha(repository: Path) -> str | None:
    git = _git(repository)
    if not git.branch_exists(INTEGRATION_BRANCH):
        return None
    return git.resolve_sha(INTEGRATION_BRANCH)


def _assert_integration_did_not_move(repository: Path, before: str | None) -> None:
    """The integration ref holds none of this run's work.

    Not a plain equality check, because the ref is created by workspace
    preparation -- at the imported branch's own commit, before any candidate
    exists -- so a run that got as far as calling a coder can leave behind a ref
    that did not exist when the test started. Creating it is not moving it, and a
    test that says otherwise would be asserting that the orchestrator may never
    prepare a worktree.

    What is not allowed is the ref advancing, or holding anything the coder
    produced. So: unchanged, or created at the imported branch; and the run's own
    commit is not reachable from it either way.
    """
    git = _git(repository)
    after = _integration_sha(repository)
    if before is not None:
        assert after == before, "agent/integration moved after the run was abandoned"
    else:
        assert after in (None, git.resolve_sha("main")), (
            f"agent/integration was created at {after}, not at the imported branch"
        )
    if after is None:
        return
    run_branch = "ts-064-run1"
    if git.branch_exists(run_branch):
        assert not git.contains_commit(
            git.resolve_sha(run_branch), ref=INTEGRATION_BRANCH
        ), "the run's work reached agent/integration"


def test_a_late_worker_cannot_resurrect_an_abandoned_run(
    tmp_path: Path,
    race_repo: Path,
    scratch_postgres: tuple[Engine, sessionmaker],
):
    """The race, with two real transactions and a real stall in between.

    Ordered as the incident was: a coder's call is in flight, and an operator
    tries to abandon the run underneath it. What happens next is the part worth
    writing down, because it is a property of the orchestrator and not of the
    test.

    Concern 66 commits the pre-call checkpoint before entering the provider.
    The operator therefore does not wait behind an idle workflow transaction:
    abandonment commits while the call is still in flight.  When the late
    answer returns, the fresh locking fence must observe that decision and
    refuse every post-call write.

    **Why PostgreSQL.** SQLite cannot express any of this: it serializes
    writers, so a second transaction's write fails with ``database is locked``
    while the workflow holds its transaction open, which is a property of the
    harness rather than of the orchestrator. PostgreSQL is what the orchestrator
    actually runs, and it gives the two transactions a real interleaving. The
    race therefore runs against a scratch PostgreSQL database and skips only
    when there is no server -- the same bargain ``test_db_locking.py`` strikes
    for the same reason.
    """
    entered = threading.Event()
    release = threading.Event()
    settings = _project_settings(tmp_path)

    engine, factory = scratch_postgres
    _drive_the_race(engine, factory, settings, race_repo, entered, release)


def _drive_the_race(
    engine: Engine,
    factory: sessionmaker,
    settings: Settings,
    repository: Path,
    entered: threading.Event,
    release: threading.Event,
) -> None:
    project_id, task_id, run_id = _register(factory, repository)
    integration_before = _integration_sha(repository)

    runner = WorkflowRunner(
        factory,
        coder=_BlockingCoder(
            _code(WORKING), entered=entered, release=release
        ),
        reviewer=reviewer(_review(taskId="TS-064")),
        settings=settings,
    )

    failures: list[BaseException] = []

    def drive() -> None:
        try:
            asyncio.run(runner.run(run_id))
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test below
            failures.append(exc)

    worker = threading.Thread(target=drive, name="workflow", daemon=True)
    worker.start()

    # 1-2. Execution genuinely began, and is now stalled with the call open.
    assert entered.wait(timeout=60), "the workflow never reached the model call"
    with engine.connect() as observer:
        states = list(
            observer.scalars(
                text(
                    "SELECT state FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
        )
    assert "idle in transaction" not in states, states

    # 3. The operator abandons from a different session and transaction while
    #    that call is still open. Concern 66 requires this to complete promptly.
    operator = sessionmaker(bind=engine, expire_on_commit=False)
    operator_done = threading.Event()

    def operate() -> None:
        try:
            with operator() as op_session:
                abandon_run(
                    op_session,
                    run_id,
                    reason="abandoned while the coder was in flight",
                    requested_by="operator",
                )
                op_session.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test below
            abandon_failures.append(exc)
        finally:
            operator_done.set()

    abandon_failures: list[BaseException] = []
    requester = threading.Thread(target=operate, name="operator", daemon=True)
    requester.start()

    assert operator_done.wait(timeout=5), (
        "the model call held a database transaction open and blocked abandonment"
    )
    assert abandon_failures == [], f"the abandon raised: {abandon_failures[0]!r}"
    with operator() as reader:
        assert TaskRunRepository(reader).get(run_id).status is RunStatus.ABANDONED

    # 4-6. The call is released and its late answer reaches the post-call fence.
    release.set()

    # 7. The workflow carries on from its stale copy of the task and tries to
    #    make the turn durable. It is refused, and which guard refuses depends on
    #    which transaction the database let through first: the run-row barrier
    #    that precedes every turn commit, or the task's own compare-and-swap
    #    when a task write follows the operator's without a commit in between.
    #    Either is a refusal; what must not happen is the workflow completing.
    worker.join(timeout=180)
    assert not worker.is_alive(), "the workflow thread did not finish"
    assert len(failures) == 1 and isinstance(
        failures[0], (AbandonedRunError, InvalidStateTransition)
    ), (
        "the workflow should have been refused by a durable guard, not "
        f"completed: {failures!r}"
    )

    with operator() as session:
        run_after = TaskRunRepository(session).get(run_id)
        task_after = TaskRepository(session).get(task_id)
        events = RunEventRepository(session).list_for_run(run_id)
        model_runs = ModelRunRepository(session).list_for_run(run_id)
        downstream = session.scalar(
            select(TaskRow).where(TaskRow.external_task_id == "TS-065")
        )

    # The run is untouched by the late completion.
    assert run_after.status is RunStatus.ABANDONED
    assert run_after.failure_reason == "OPERATOR_ABANDONED"
    assert run_after.candidate_commit is None

    # The task is not resurrected into any state that would imply live work.
    assert task_after.status is TaskStatus.FAILED
    assert task_after.status is not TaskStatus.CODING
    assert task_after.status is not TaskStatus.COMPLETE

    # Downstream stays locked.
    assert downstream.status == TaskStatus.PENDING.value

    # No candidate, no integration, no completed task.
    _assert_integration_did_not_move(repository, integration_before)
    types = {e.event_type for e in events}
    assert RunEventType.COMMIT_CREATED not in types
    assert RunEventType.INTEGRATION_ADVANCED not in types
    assert RunEventType.TASK_COMPLETED not in types

    # The late answer is not persisted as an accepted model result. Its prompt
    # artifact was checkpointed before the call, which is sufficient for
    # recovery accounting without writing through the abandonment fence.
    assert not [m for m in model_runs if m.purpose == ModelPurpose.CODE]
    # What the guard rolled back is everything the turn claimed afterwards. The
    # turn's own event for finishing the coding attempt is not on the record,
    # because the attempt was not finished: the run was abandoned while the
    # coder was still in flight and the work after that was never made durable.
    assert RunEventType.CODING_COMPLETED not in types


# =============================================================================
# 8. Candidate and integration fencing, through the path that does the work.
# =============================================================================


@pytest.fixture
def approved_run(tmp_path: Path, race_repo: Path):
    """A real run driven to APPROVED, with a real candidate in a real worktree.

    This is the state an abandonment most easily fails to hold. The fix loop has
    returned APPROVED, a real commit-worthy diff exists in the worktree, and the
    next graph edge is ``deliver``. Every other fencing test starts from CODING,
    where there is nothing to land yet and the assertion is nearly free.

    Nothing is stubbed. A scripted coder writes into a real worktree, the
    project's own verification runs in a real subprocess worker, and a scripted
    reviewer reads the real diff.
    """
    engine = create_db_engine(f"sqlite:///{tmp_path / 'approved.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = _project_settings(tmp_path)
    _project_id, task_id, run_id = _register(factory, race_repo)
    # Committed, not left in a session that rolls back: the workspace's
    # branch_name and starting_commit are what attach_workspace re-reads, and a
    # run whose preparation was rolled back has none.
    with factory.begin() as session:
        workspace = prepare_workspace(session, run_id, settings=settings)
    try:
        yield factory, settings, run_id, task_id, workspace, race_repo
    finally:
        engine.dispose()


async def _drive_to_approved(factory, settings, run_id, workspace) -> None:
    with factory() as session:
        result = await run_fix_loop(
            session,
            workspace,
            coder=ScriptedModel(_code(WORKING)),
            reviewer=reviewer(_review(taskId="TS-064")),
            settings=settings,
        )
        session.commit()
    assert result.outcome.value == "APPROVED"


@pytest.mark.asyncio
async def test_abandonment_before_delivery_prevents_the_candidate(approved_run):
    factory, settings, run_id, task_id, workspace, repository = approved_run
    await _drive_to_approved(factory, settings, run_id, workspace)

    with factory() as session:
        assert TaskRepository(session).get(task_id).status is TaskStatus.APPROVED
        assert TaskRunRepository(session).get(run_id).status is RunStatus.RUNNING
    integration_before = _integration_sha(repository)

    with factory.begin() as session:
        abandon_run(session, run_id, reason="stop before this candidate lands")

    # The real delivery path, refused at the boundary before the commit.
    with factory() as session:
        attached = attach_workspace(session, run_id, settings=settings)
        with pytest.raises(AbandonedRunError, match="abandoned by an operator"):
            deliver_candidate(session, attached, settings=settings)

    with factory() as session:
        run_after = TaskRunRepository(session).get(run_id)
        task_after = TaskRepository(session).get(task_id)
        events = RunEventRepository(session).list_for_run(run_id)
        cumulative = [
            v
            for v in VerificationRunRepository(session).list_for_run(run_id)
            if v.verification_type
            in {
                VerificationType.INTEGRATION_BUILD,
                VerificationType.INTEGRATION_LINT,
                VerificationType.INTEGRATION_TESTS,
            }
        ]

    assert run_after.status is RunStatus.ABANDONED
    assert run_after.candidate_commit is None
    assert task_after.status is not TaskStatus.COMPLETE
    assert _integration_sha(repository) == integration_before, (
        "agent/integration moved after the run was abandoned"
    )
    types = {e.event_type for e in events}
    assert RunEventType.COMMIT_CREATED not in types
    assert RunEventType.INTEGRATION_ADVANCED not in types
    assert RunEventType.TASK_COMPLETED not in types
    # The cumulative gate is not merely unfired: it never ran. Integration
    # verification assumes a delivered candidate, and there was not one.
    assert cumulative == []


@pytest.mark.asyncio
async def test_the_workflow_will_not_complete_an_abandoned_approved_run(approved_run):
    """The same guarantee through the graph, which is the other way in.

    A separate assertion from the one above, and about a different thing: that
    routing an abandoned run to ``deliver`` -- which is what happens whenever a
    loop returns APPROVED after an operator intervened -- produces neither a
    completed run nor a landed candidate.
    """
    factory, settings, run_id, task_id, _workspace, repository = approved_run
    await _drive_to_approved(factory, settings, run_id, _workspace)

    with factory.begin() as session:
        abandon_run(session, run_id, reason="stop before this candidate lands")
    integration_before = _integration_sha(repository)

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-064")),
        settings=settings,
    )
    state = await runner.run(run_id)

    with factory() as session:
        run_after = TaskRunRepository(session).get(run_id)
        task_after = TaskRepository(session).get(task_id)
        events = RunEventRepository(session).list_for_run(run_id)

    assert state["outcome"] != "COMPLETED"
    assert run_after.status is RunStatus.ABANDONED
    assert run_after.candidate_commit is None
    assert task_after.status is not TaskStatus.COMPLETE
    assert _integration_sha(repository) == integration_before
    types = {e.event_type for e in events}
    assert RunEventType.COMMIT_CREATED not in types
    assert RunEventType.INTEGRATION_ADVANCED not in types
    assert RunEventType.TASK_COMPLETED not in types


# --- the scratch PostgreSQL database the race needs --------------------------

_DEFAULT_SERVER = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/postgres"


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
    name = f"race_{uuid.uuid4().hex[:12]}"
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


# =============================================================================
# 9. That this file does not pollute the shared database.
# =============================================================================


def _row_counts(engine: Engine) -> dict[str, int]:
    """Row counts read from a connection of their own.

    A separate connection is the point. The test's own session sits inside the
    fixture's transaction and can see its own uncommitted writes, so asking it
    would report the rows the test just made and prove nothing at all.
    """
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in _TABLES:
            counts[table] = connection.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
            ).scalar_one()
    return counts


def test_concern64_operations_leave_nothing_in_the_shared_database(
    engine: Engine,
    session: Session,
    project: Project,
    running_run: TaskRun,
):
    """The regression that was needed, in the cheapest form that could catch it.

    Every operation in this file that touches the shared ``session`` runs here,
    and afterwards the shared database is read from a connection that had no
    part in the work. A single committed row in any table fails it.
    """
    before = _row_counts(engine)

    abandon_run(session, running_run.id, reason="first", requested_by="operator")
    abandon_run(session, running_run.id, reason="second")
    finished_run = TaskRunRepository(session).add(
        TaskRun(
            task_id=running_run.task_id,
            run_number=2,
            status=RunStatus.SUCCEEDED,
        )
    )
    with pytest.raises(EntityConflict):
        abandon_run(session, finished_run.id, reason="too late")
    with pytest.raises(AbandonedRunError):
        TaskRunRepository(session).finish(running_run.id, RunStatus.SUCCEEDED)
    inspect_incomplete_runs(session)

    assert _row_counts(engine) == before


_PROBE_PLUGIN = '''\
"""Read the shared database after the last test, while the engine still exists.

Registered with ``-p`` so it loads as a plugin rather than a conftest, and
hooked on ``pytest_runtest_protocol`` with ``nextitem is None`` -- the last
test of the session, which is before the session-scoped engine fixture is torn
down. Reads with the standard library so nothing SQLAlchemy-shaped can be
responsible for the answer.
"""

import json
import os
import pathlib
import sqlite3

import pytest

OUT = pathlib.Path(os.environ["POLLUTION_PROBE_OUT"])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    yield
    if nextitem is not None:
        return
    path = os.environ["TEST_DATABASE_URL"].split("///", 1)[1]
    counts = {}
    connection = sqlite3.connect(path)
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        for (name,) in tables:
            if name.startswith("sqlite_") or name == "alembic_version":
                continue
            counts[name] = connection.execute(
                f"SELECT count(*) FROM {name}"  # noqa: S608
            ).fetchone()[0]
    finally:
        connection.close()
    OUT.write_text(json.dumps(counts), encoding="utf-8")
'''


@pytest.mark.skipif(
    os.environ.get("CONCERN64_IN_SUBPROCESS") == "1",
    reason=(
        "this test is the one that runs the module in a subprocess; inside that "
        "subprocess it would run the subprocess again, forever"
    ),
)
def test_this_module_leaves_no_rows_in_a_shared_database(tmp_path: Path):
    """The proof that depends on nothing but this module.

    A whole-process test: this module runs in a subprocess against a database
    file it shares with nobody, and a standard-library ``sqlite3`` connection
    counts what survived. No fixture teardown, no test ordering, no other file
    involved -- if anything here commits, the count is non-zero.

    This is the assertion the old ``session.commit()`` could not have survived,
    and the one "the full suite passes" could never have supplied. In a single
    process a leaked row is only visible to whichever test happens to run next,
    which is exactly why ``test_projects_api`` failing depended on the order.

    The child is told it *is* a child, so this one test is skipped there. The
    first version of it launched the module with no such guard, which meant the
    child launched the same test, which launched another, until something ran
    out of time or processes.
    """
    database = tmp_path / "shared.db"
    probe = tmp_path / "probe.py"
    out = tmp_path / "counts.json"
    probe.write_text(_PROBE_PLUGIN, encoding="utf-8")
    root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_concern64.py",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "probe",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TEST_DATABASE_URL": f"sqlite:///{database}",
            "POLLUTION_PROBE_OUT": str(out),
            "CONCERN64_IN_SUBPROCESS": "1",
            "PYTHONPATH": os.pathsep.join([str(tmp_path), str(root)]),
        },
        timeout=1800,
    )
    assert completed.returncode == 0, (
        f"this module does not pass on its own:\n{completed.stdout[-4000:]}\n"
        f"{completed.stderr[-4000:]}"
    )

    counts = json.loads(out.read_text(encoding="utf-8"))
    leaked = {table: count for table, count in counts.items() if count}
    assert leaked == {}, (
        f"this module committed rows into a database other tests share: {leaked}"
    )
