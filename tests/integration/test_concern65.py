"""Concern 65: a supported way to start a fresh run for a terminal failed task.

Concern 64 could stop an invalid run and left its task in ``FAILED``. On the
next TS-106 experiment that state turned out to be a decision with no supported
way to record it: ``create_run`` accepts only ``READY`` and ``CHANGES_REQUESTED``,
the scheduler does not manage ``FAILED``, resume accepts only ``PAUSED``, and
the escalation that asked for a retry had already been closed. The workaround
was a direct database mutation and a manifest re-import, and both were refused.

**What these tests claim.** An operator can authorize a new run for a failed
task, and the run that follows is a new run: new durable and external identity,
new branch and worktree, the *current* accepted integration baseline, none of the
old run's attempt, review, runtime or candidate state -- and the abandoned run
left untouched, terminal, unresumable, with its event log unchanged. Five
historical runs on TS-106 and four resolved escalations are the shape of the
reality this was built for, and the fixture builds that shape the only way it
can honestly be built: each historical run was created after an earlier operator
authorization and abandoned by an operator.

**What they refuse, and why that is the point.** A retry is a decision a person
makes once. A second request finds a task that is no longer ``FAILED`` and is
told so; a task with a run in flight, a dependency that is not complete and
integrated, a pause in force, or a project that is not runnable is refused
against the specific rule it violates rather than with a generic conflict. The
two refusals that a read cannot make truthfully -- "the task is still FAILED" and
"no run of it is in flight" -- are predicates on the single guarded ``UPDATE``
that performs the move, so two operators racing produce one authorization, and a
retry racing a run creation cannot produce two runs.

**Where the races are tested for real.** SQLite serializes writers, so "one
transaction reads while another writes" is not a thing it can do; the last
section therefore uses a scratch PostgreSQL database, two sessions and two
threads over the real HTTP boundary, and is skipped rather than failed when no
server is available -- the same bargain ``test_db_locking.py`` strikes.

**Test isolation, asserted rather than assumed.** The tests that need committed
rows (a workspace's branch name, a worktree path) own a private database file.
The service and HTTP tests share the suite's rolled-back ``session`` and commit
nothing. The last section proves that: row counts are read from a separate
connection at the start of this module and at the end of it.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend, get_settings
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import RunEventRow
from apps.orchestrator.db.session import create_db_engine, get_db, reset_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    EscalationStatus,
    ProjectStatus,
    RunEventType,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.errors import AbandonedRunError, InvalidStateTransition
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import (
    HumanEscalation,
    PauseRequest,
    Project,
    Task,
    TaskLimits,
    TaskRun,
)
from apps.orchestrator.domain.state_machine import assert_transition
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.main import create_app
from apps.orchestrator.repositories import (
    EscalationRepository,
    PauseRequestRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.abandon import abandon_run
from apps.orchestrator.services.artifact_store import ensure_run_id
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound
from apps.orchestrator.services.git_service import GitService
from apps.orchestrator.services.pauses import resume_task
from apps.orchestrator.services.retry import (
    RETRY_AUTHORIZED_STATUS,
    RETRYABLE_TASK_STATES,
    retry_failed_task,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.scheduler import refresh_readiness, select_next_task
from apps.orchestrator.services.workspace import prepare_workspace
from apps.orchestrator.workflow import WorkflowRunner
from apps.orchestrator.workflow.recovery import inspect_incomplete_runs
from tests.conftest import run_git

# Concern 74: Safety guard for destructive operations.
from tests.db_safety import (
    assert_test_database_safe,
    drop_all_tables_for_test,
    drop_test_database,
)
from tests.integration.test_fix_loop import (
    PYTHON,
    STUB,
    WORKING,
    ScriptedModel,
    _code,
    _review,
    reviewer,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
REASON = "the preflight left no supported path; re-run against the current image"
OPERATOR = "darri"

#: Five historical runs, the shape of the reality concern 65 came out of.
HISTORY = 5

#: Statuses a task must refuse a retry in, each reached through the state
#: machine rather than written straight into the column.
_STATUS_PATHS: dict[TaskStatus, tuple[TaskStatus, ...]] = {
    TaskStatus.READY: (TaskStatus.READY,),
    TaskStatus.PLANNING: (TaskStatus.READY, TaskStatus.PLANNING),
    TaskStatus.CODING: (TaskStatus.READY, TaskStatus.CODING),
    TaskStatus.BLOCKED: (TaskStatus.READY, TaskStatus.BLOCKED),
    TaskStatus.HUMAN_REVIEW: (TaskStatus.READY, TaskStatus.HUMAN_REVIEW),
    TaskStatus.PAUSED: (TaskStatus.READY, TaskStatus.PAUSED),
    TaskStatus.APPROVED: (
        TaskStatus.READY,
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
    ),
    TaskStatus.CHANGES_REQUESTED: (
        TaskStatus.READY,
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.CHANGES_REQUESTED,
    ),
    TaskStatus.COMPLETE: (
        TaskStatus.READY,
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
        TaskStatus.COMPLETE,
    ),
}


# =============================================================================
# Fixtures and builders
# =============================================================================


def _project(session: Session, repository: str = "/tmp/tracestack") -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            repository_path=repository,
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=VerificationProfile(tests=(f"{PYTHON} tools/test.py",)),
        )
    )


def _task(session: Session, project: Project, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "project_id": project.id,
        "external_task_id": "TS-106",
        "title": "Implement navigate",
        "instructions": "navigate must return its target.",
        "complexity": Complexity.LOW,
        "files_to_modify": ["src/nav.py"],
        "limits": TaskLimits(max_files_changed=3, max_diff_lines=200),
    }
    fields.update(overrides)
    return TaskRepository(session).add(Task(**fields))


def _authorize(session: Session, task_id: uuid.UUID, *, reason: str = REASON) -> Task:
    return retry_failed_task(session, task_id, reason=reason, requested_by=OPERATOR)


def _failed_task(
    session: Session,
    project: Project,
    *,
    history: int = 1,
    **overrides: Any,
) -> tuple[Task, list[TaskRun]]:
    """A ``FAILED`` task with abandoned history, built the supported way.

    Run one was the original attempt: the task went ``READY`` and the run was
    created for it directly. Every run after that was created by ``create_run``
    after an earlier operator authorization, and every one was abandoned by an
    operator -- which is how TS-106 reached five runs with none in flight, and
    the shape the retry has to survive. A fixture that wrote ``status =
    FAILED`` into the column would prove the retry works and nothing about the
    state it has to survive.
    """
    task = _task(session, project, **overrides)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    runs: list[TaskRun] = []
    for index in range(history):
        if index:
            _authorize(session, task.id, reason=f"historical authorization {index + 1}")
        run = create_run(session, task.id)
        abandon_run(
            session,
            run.id,
            reason="began under a stale supervisor image",
            requested_by=OPERATOR,
        )
        # Re-read: the object create_run returned is the run as it was opened,
        # and what a test compares against is the run as it was abandoned.
        runs.append(TaskRunRepository(session).get(run.id))
    return TaskRepository(session).get(task.id), runs


def _escalate(session: Session, task_id: uuid.UUID, **overrides: Any) -> HumanEscalation:
    fields: dict[str, Any] = {
        "task_id": task_id,
        "reason": "MODEL_UNAVAILABLE",
        "summary": "the coder could not be reached",
    }
    fields.update(overrides)
    return EscalationRepository(session).add(HumanEscalation(**fields))


def _event_rows(session: Session, task_id: uuid.UUID) -> list[RunEventRow]:
    return list(
        session.scalars(
            select(RunEventRow)
            .where(RunEventRow.task_id == task_id, RunEventRow.task_run_id.is_(None))
            .order_by(RunEventRow.created_at, RunEventRow.sequence)
        )
    )


def _retry_events(session: Session, task_id: uuid.UUID) -> list[RunEventRow]:
    return [
        row
        for row in _event_rows(session, task_id)
        if row.event_type == RunEventType.TASK_RETRY_AUTHORIZED
    ]


def _set_status(session: Session, task_id: uuid.UUID, status: TaskStatus) -> Task:
    for step in _STATUS_PATHS[status]:
        TaskRepository(session).transition(task_id, step)
    return TaskRepository(session).get(task_id)


def _complete_dependency(
    session: Session,
    project: Project,
    *,
    external_task_id: str = "TS-105",
    integrated: bool = True,
) -> Task:
    dependency = _task(
        session,
        project,
        external_task_id=external_task_id,
        title="Add the router",
        files_to_modify=[],
    )
    _set_status(session, dependency.id, TaskStatus.COMPLETE)
    # The column holds the commit whose work is *not* in the baseline, so
    # "complete but unintegrated" is a commit that has not been delivered.
    TaskRepository(session).record_integration(
        dependency.id, unintegrated_commit=None if integrated else "abc1234"
    )
    return TaskRepository(session).get(dependency.id)


@pytest.fixture
def project(session: Session) -> Project:
    return _project(session)


@pytest.fixture
def failed_task(session: Session, project: Project) -> tuple[Task, list[TaskRun]]:
    return _failed_task(session, project, history=1)


@pytest.fixture
def ts106(session: Session, project: Project) -> tuple[Task, list[TaskRun]]:
    """The task as the campaign left it: five abandoned runs, no run in flight."""
    return _failed_task(session, project, history=HISTORY)


@pytest.fixture
def api_client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The project's normal API test shape: a real app driven by a TestClient.

    Every request shares the test's transaction through ``get_db``, so these are
    real HTTP requests through a real router, a real pydantic body and the real
    exception handlers -- and they commit nothing.
    """
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("WORKER_BACKEND", "subprocess")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


# =============================================================================
# 1. The authorization, and nothing else
# =============================================================================


def test_a_failed_task_becomes_ready_after_an_authorized_retry(
    session: Session, failed_task: tuple[Task, list[TaskRun]]
) -> None:
    task, _runs = failed_task
    assert task.status is TaskStatus.FAILED

    retried = _authorize(session, task.id)

    assert retried.status is RETRY_AUTHORIZED_STATUS is TaskStatus.READY
    assert TaskRepository(session).get(task.id).status is TaskStatus.READY
    assert retried.external_task_id == "TS-106"
    assert retried.project_id == task.project_id


def test_the_authorization_is_a_decision_about_the_task_not_about_a_run(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    before_types = [
        event.event_type for event in RunEventRepository(session).list_for_run(runs[-1].id)
    ]
    _authorize(session, task.id)
    events = RunEventRepository(session).list_for_run(runs[-1].id)
    rows = _retry_events(session, task.id)

    assert len(rows) == HISTORY, (
        "four historical authorizations and this one: the decisions already taken "
        "stay on the record, which is what makes this one auditable"
    )
    row = rows[-1]
    assert row.task_run_id is None, (
        "the decision is about the task; filing it against the abandoned run would "
        "put it on the record of an execution that had nothing to do with it"
    )
    assert row.task_id == task.id
    assert row.project_id == task.project_id
    assert row.created_at is not None
    assert row.sequence > rows[-2].sequence, (
        "task-level events keep one increasing sequence, so the order of the "
        "decisions does not depend on their timestamps"
    )
    assert [e.event_type for e in events] == before_types
    assert events[-1].event_type == RunEventType.RUN_ABANDONED

    payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
    assert payload["external_task_id"] == "TS-106"
    assert payload["reason"] == REASON
    assert payload["requested_by"] == OPERATOR
    assert payload["previous_status"] == "FAILED"
    assert payload["authorized_status"] == "READY"
    assert payload["historical_runs"] == HISTORY
    assert [row_.event_type for row_ in rows] == [RunEventType.TASK_RETRY_AUTHORIZED] * HISTORY


def test_the_authorization_creates_no_run_of_its_own(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    next_before = TaskRunRepository(session).next_run_number(task.id)

    _authorize(session, task.id)

    assert TaskRunRepository(session).list_for_task(task.id) == runs
    assert TaskRunRepository(session).next_run_number(task.id) == next_before == HISTORY + 1
    assert TaskRunRepository(session).list_incomplete() == []


def test_only_failed_is_retryable() -> None:
    assert frozenset({TaskStatus.FAILED}) == RETRYABLE_TASK_STATES
    assert RETRY_AUTHORIZED_STATUS is TaskStatus.READY


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(status, id=status.value)
        for status in (
            TaskStatus.READY,
            TaskStatus.PLANNING,
            TaskStatus.CODING,
            TaskStatus.BLOCKED,
            TaskStatus.HUMAN_REVIEW,
            TaskStatus.PAUSED,
            TaskStatus.APPROVED,
            TaskStatus.CHANGES_REQUESTED,
        )
    ],
)
def test_a_task_that_is_not_failed_is_refused(
    session: Session, project: Project, status: TaskStatus
) -> None:
    task = _task(session, project)
    _set_status(session, task.id, status)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert status.value in str(refusal.value)
    assert "only FAILED may be retried" in str(refusal.value)
    assert TaskRepository(session).get(task.id).status is status


def test_a_complete_task_cannot_be_retried_and_cannot_be_made_retriable(
    session: Session, project: Project
) -> None:
    task = _task(session, project)
    _set_status(session, task.id, TaskStatus.COMPLETE)

    with pytest.raises(EntityConflict):
        _authorize(session, task.id)

    assert TaskRepository(session).get(task.id).status is TaskStatus.COMPLETE
    with pytest.raises(InvalidStateTransition):
        assert_transition(TaskStatus.COMPLETE, TaskStatus.READY)


def test_retrying_the_same_task_twice_is_refused_and_authorizes_once(
    session: Session, failed_task: tuple[Task, list[TaskRun]]
) -> None:
    task, _runs = failed_task
    _authorize(session, task.id)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert "is READY" in str(refusal.value)
    assert len(_retry_events(session, task.id)) == 1
    assert TaskRepository(session).get(task.id).status is TaskStatus.READY


def test_the_run_itself_is_created_by_the_next_project_execution(
    session: Session, failed_task: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = failed_task
    _authorize(session, task.id)

    selection = select_next_task(session, task.project_id)

    assert selection.task is not None
    assert selection.task.id == task.id
    fresh = create_run(session, task.id)
    assert fresh.run_number == len(runs) + 1
    assert fresh.id not in {run.id for run in runs}
    assert [candidate.run_id for candidate in inspect_incomplete_runs(session)] == [fresh.id], (
        "the new run is the one recovery finds, so the next project pass continues "
        "it rather than opening a second one for the same task"
    )


@pytest.mark.parametrize("reason", ["", " ", "\n\t  "])
def test_a_blank_reason_is_refused_before_anything_is_read(
    session: Session, failed_task: tuple[Task, list[TaskRun]], reason: str
) -> None:
    task, _runs = failed_task
    with pytest.raises(ValueError, match="reason is required"):
        retry_failed_task(session, task.id, reason=reason)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED


def test_an_unknown_task_is_not_found(session: Session) -> None:
    with pytest.raises(EntityNotFound):
        retry_failed_task(session, uuid.uuid4(), reason=REASON)


# =============================================================================
# 2. The refusals, and the rules they come from
# =============================================================================


def test_a_task_with_a_run_in_flight_is_refused(session: Session, project: Project) -> None:
    """The defensive state, refused rather than papered over.

    A ``FAILED`` task with a live run cannot be reached through the supported
    paths -- abandonment leaves the run terminal, and a run is only created for
    a task that is ``READY`` -- so this state means something outside the
    orchestrator wrote to the database. If it ever exists, the guard still holds:
    the retry refuses and names the run rather than authorizing a second one.
    """
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    create_run(session, task.id)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert "still has run 1 in flight" in str(refusal.value)
    assert "PENDING" in str(refusal.value)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED
    assert TaskRunRepository(session).in_flight_for_task(task.id) is not None


def test_a_retried_task_that_has_already_started_its_run_is_refused(
    session: Session, project: Project
) -> None:
    """The ordinary double request, one step later than the other test.

    After the authorization the orchestrator opens the run, and the task is no
    longer ``FAILED``. A second operator request must not authorize anything on
    top of a run that is already executing the first one -- not even though the
    task is still ``READY``, which is the state a retry writes.
    """
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)
    _authorize(session, task.id)
    run = create_run(session, task.id)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert TaskStatus.READY.value in str(refusal.value)
    assert TaskRunRepository(session).in_flight_for_task(task.id).id == run.id
    assert len(_retry_events(session, task.id)) == 1
    assert len(TaskRunRepository(session).list_for_task(task.id)) == 1


def test_a_task_whose_dependency_has_not_completed_is_refused(
    session: Session, project: Project
) -> None:
    dependency = _task(
        session, project, external_task_id="TS-105", title="Add the router", files_to_modify=[]
    )
    task = _task(session, project, depends_on=["TS-105"])
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert "depends on TS-105" in str(refusal.value)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED
    assert TaskRepository(session).get(dependency.id).status is TaskStatus.PENDING


def test_a_task_whose_dependency_is_complete_but_unintegrated_is_refused(
    session: Session, project: Project
) -> None:
    """Concern 51's rule, applied to the retry as well as to the scheduler.

    ``COMPLETE`` says the work was done; ``is_integrated`` says it is in the tree
    the next run starts from. A retry authorized against the first one would put
    a run on a tree missing the work it was told to build on.
    """
    _complete_dependency(session, project, integrated=False)
    task = _task(session, project, depends_on=["TS-105"])
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert "depends on TS-105" in str(refusal.value)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED


def test_a_task_whose_dependency_does_not_exist_is_refused(
    session: Session, project: Project
) -> None:
    task = _task(session, project, depends_on=["TS-999"])
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    with pytest.raises(EntityConflict, match="TS-999"):
        _authorize(session, task.id)


def test_a_task_with_satisfied_dependencies_is_authorized(
    session: Session, project: Project
) -> None:
    """The negative control for the three tests above.

    Without it, a retry that refused every dependent task would pass them all.
    """
    _complete_dependency(session, project)
    task = _task(session, project, depends_on=["TS-105"])
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    assert _authorize(session, task.id).status is TaskStatus.READY


def test_a_task_under_a_pause_in_force_is_refused(session: Session, project: Project) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)
    PauseRequestRepository(session).add(
        PauseRequest(project_id=project.id, task_id=task.id, reason="hold for the incident review")
    )

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert "pause in force" in str(refusal.value)
    assert "hold for the incident review" in str(refusal.value)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED


def test_a_pause_on_the_whole_project_also_refuses(session: Session, project: Project) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)
    PauseRequestRepository(session).add(PauseRequest(project_id=project.id, reason="bad image"))

    with pytest.raises(EntityConflict, match="pause in force"):
        _authorize(session, task.id)


def test_a_released_pause_does_not_block_a_retry(session: Session, project: Project) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)
    request = PauseRequestRepository(session).add(
        PauseRequest(project_id=project.id, reason="bad image")
    )
    assert request.id is not None
    PauseRequestRepository(session).release(project_id=project.id)

    assert _authorize(session, task.id).status is TaskStatus.READY


def test_a_project_that_is_not_runnable_is_refused(session: Session, project: Project) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)
    ProjectRepository(session).set_status(project.id, ProjectStatus.PAUSED)

    with pytest.raises(EntityConflict) as refusal:
        _authorize(session, task.id)

    assert "not runnable" in str(refusal.value)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED


def test_the_retry_and_the_resume_share_one_dependency_rule(
    session: Session, project: Project
) -> None:
    """One rule, asked in two places, with the same answer either way.

    Resume and retry both promote a task, and both are operator decisions. A
    dependency that is complete but not integrated leaves the task un-runnable
    either way: ``READY`` for a retry (refused outright) and ``PENDING`` for a
    resume (deferred, not authorized). What must never happen is either path
    producing a ``READY`` task the scheduler would create a run for.
    """
    _complete_dependency(session, project, integrated=False)
    paused = _task(session, project, external_task_id="TS-107", depends_on=["TS-105"])
    TaskRepository(session).transition(paused.id, TaskStatus.READY)
    TaskRepository(session).transition(paused.id, TaskStatus.PAUSED)
    retryable = _task(session, project, external_task_id="TS-108", depends_on=["TS-105"])
    TaskRepository(session).transition(retryable.id, TaskStatus.READY)
    TaskRepository(session).transition(retryable.id, TaskStatus.FAILED)

    resumed = resume_task(session, paused.id)
    with pytest.raises(EntityConflict):
        _authorize(session, retryable.id)

    assert resumed.status is TaskStatus.PENDING
    assert TaskRepository(session).get(retryable.id).status is TaskStatus.FAILED
    assert select_next_task(session, project.id).task is None


# =============================================================================
# 3. What the authorization must not disturb
# =============================================================================


def test_the_abandoned_run_is_left_exactly_as_it_was(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    abandoned = TaskRunRepository(session).get(runs[-1].id)
    before = RunEventRepository(session).list_for_run(abandoned.id)

    _authorize(session, task.id)

    after = TaskRunRepository(session).get(abandoned.id)
    assert after.status is RunStatus.ABANDONED is abandoned.status
    assert after.completed_at == abandoned.completed_at
    assert after.failure_reason == abandoned.failure_reason
    assert after.branch_name == abandoned.branch_name
    assert after.starting_commit == abandoned.starting_commit
    assert after.candidate_commit is None
    assert after.external_run_id == abandoned.external_run_id
    assert after.attempt_number == abandoned.attempt_number
    assert after.review_cycle == abandoned.review_cycle
    assert after.active_runtime_ms == abandoned.active_runtime_ms
    assert RunEventRepository(session).list_for_run(abandoned.id) == before


def test_the_run_history_is_neither_reused_nor_rewritten(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    before = {run.id: run for run in runs}

    _authorize(session, task.id)

    after = {run.id: run for run in TaskRunRepository(session).list_for_task(task.id)}
    assert set(after) == set(before)
    for run_id, run in after.items():
        assert run.status is before[run_id].status is RunStatus.ABANDONED
        assert run.run_number == before[run_id].run_number
    assert [run.run_number for run in TaskRunRepository(session).list_for_task(task.id)] == list(
        range(1, HISTORY + 1)
    )


def test_a_resolved_escalation_stays_resolved_and_none_is_reopened(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, _runs = ts106
    escalations = EscalationRepository(session)
    for index in range(4):
        escalation = _escalate(session, task.id, summary=f"historical escalation {index}")
        escalations.resolve(escalation.id, resolution="retry the task")
    before = [escalation.id for escalation in escalations.list_for_task(task.id)]

    _authorize(session, task.id)

    after = escalations.list_for_task(task.id)
    assert [escalation.id for escalation in after] == before
    assert all(escalation.status is EscalationStatus.RESOLVED for escalation in after), (
        "an authorization is not an answer, and must not answer an escalation"
    )
    assert escalations.list_open(task_id=task.id) == []


def test_the_authorization_never_reaches_a_run_event_log(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    _authorize(session, task.id)

    for run in runs:
        types = {event.event_type for event in RunEventRepository(session).list_for_run(run.id)}
        assert RunEventType.TASK_RETRY_AUTHORIZED not in types


def test_the_abandoned_run_cannot_be_resumed_or_written_after_a_retry(
    session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    abandoned = runs[-1]
    _authorize(session, task.id)

    assert TaskRunRepository(session).list_incomplete() == []
    with pytest.raises(AbandonedRunError):
        TaskRunRepository(session).finish(abandoned.id, RunStatus.SUCCEEDED)
    with pytest.raises(AbandonedRunError):
        TaskRunRepository(session).update_fields(abandoned.id, status=RunStatus.RUNNING)
    with pytest.raises(AbandonedRunError):
        TaskRunRepository(session).require_in_flight(abandoned.id)
    assert TaskRunRepository(session).get(abandoned.id).status is RunStatus.ABANDONED


# =============================================================================
# 4. The run that follows: new identity, current baseline
# =============================================================================


@pytest.fixture
def race_repo(tmp_path: Path) -> Path:
    """The fixture project, built locally so this file's repo is this file's."""
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


@pytest.fixture
def project_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


@pytest.fixture
def private_db(tmp_path: Path) -> Iterator[tuple[Engine, sessionmaker]]:
    """A private database file, for the tests that need committed rows.

    The suite's ``session`` fixture rolls back, and a workspace's branch name
    and starting commit have to survive to be re-read by the run that follows.
    """
    engine = create_db_engine(f"sqlite:///{tmp_path / 'retry.db'}")
    Base.metadata.create_all(engine)
    try:
        yield engine, sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _register_failed(
    factory: sessionmaker,
    repository: Path,
    *,
    history: int = HISTORY,
    dependency: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """A project, a FAILED task with abandoned history, and a ready dependency."""
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
                external_task_id="TS-106",
                title="Implement navigate",
                instructions="navigate must return its target.",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
                depends_on=["TS-105"] if dependency else [],
                limits=TaskLimits(max_files_changed=3, max_diff_lines=200),
            )
        )
        if dependency:
            router = TaskRepository(session).add(
                Task(
                    project_id=project.id,
                    external_task_id="TS-105",
                    title="Add the router",
                    complexity=Complexity.LOW,
                    files_to_modify=[],
                )
            )
            _set_status(session, router.id, TaskStatus.COMPLETE)
            TaskRepository(session).record_integration(router.id, unintegrated_commit=None)
        task = TaskRepository(session).transition(task.id, TaskStatus.READY)
        run_ids: list[uuid.UUID] = []
        for index in range(history):
            if index:
                _authorize(session, task.id, reason=f"historical authorization {index + 1}")
            run = create_run(session, task.id)
            run_ids.append(run.id)
            abandon_run(
                session,
                run.id,
                reason="began under a stale supervisor image",
                requested_by=OPERATOR,
            )
        return project.id, task.id, run_ids


def _integration_sha(repository: Path) -> str | None:
    git = GitService(repository, default_branch="main", settings=Settings(_env_file=None))
    if not git.branch_exists(INTEGRATION_BRANCH):
        return None
    return git.resolve_sha(INTEGRATION_BRANCH)


def _advance_integration(repository: Path, contents: str) -> str:
    """Deliver something to the integration baseline, as a prior task would."""
    git = GitService(repository, default_branch="main", settings=Settings(_env_file=None))
    (repository / "src" / "router.py").write_text(contents, encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "Add the router")
    if not git.branch_exists(INTEGRATION_BRANCH):
        run_git(repository, "branch", INTEGRATION_BRANCH, "HEAD")
    else:
        run_git(repository, "checkout", "--quiet", INTEGRATION_BRANCH)
        run_git(repository, "merge", "--ff-only", "--quiet", "HEAD")
        run_git(repository, "checkout", "--quiet", "-")
    return git.resolve_sha(INTEGRATION_BRANCH)


def test_the_run_after_a_retry_is_a_new_run_with_new_identity(
    private_db: tuple[Engine, sessionmaker],
    race_repo: Path,
    project_settings: Settings,
) -> None:
    """The claim the whole concern rests on, in one test.

    Five abandoned runs exist. The authorization creates no run. The next
    project execution creates run six, with its own id, branch, worktree and
    external id, starting from the accepted integration baseline -- and with none
    of the state the previous runs accumulated.
    """
    _engine, factory = private_db
    project_id, task_id, run_ids = _register_failed(factory, race_repo)
    baseline = _advance_integration(race_repo, "ROUTES = ()\n")

    with factory.begin() as session:
        _authorize(session, task_id)
        run = create_run(session, task_id)
        session.flush()
        prepare_workspace(session, run.id, settings=project_settings)
        external = ensure_run_id(session, run.id)
        fresh = TaskRunRepository(session).get(run.id)
        historical = [TaskRunRepository(session).get(run_id) for run_id in run_ids]

    assert fresh.task_id == task_id
    assert fresh.id not in set(run_ids)
    assert fresh.run_number == HISTORY + 1
    assert fresh.status is RunStatus.PENDING
    assert fresh.attempt_number == 1
    assert fresh.review_cycle == 0
    assert fresh.active_runtime_ms == 0
    assert fresh.candidate_commit is None
    assert fresh.failure_reason is None
    assert fresh.context_hash is None
    assert fresh.worker_image is None
    assert fresh.branch_name is not None and "run6" in fresh.branch_name
    assert fresh.branch_name not in {old.branch_name for old in historical}
    assert fresh.starting_commit == baseline
    assert fresh.starting_commit != historical[-1].starting_commit
    assert external.startswith("RUN-")
    assert external not in {old.external_run_id for old in historical}
    for old in historical:
        assert old.status is RunStatus.ABANDONED
        assert old.candidate_commit is None


def test_the_new_run_starts_from_the_baseline_at_the_time_it_runs(
    private_db: tuple[Engine, sessionmaker],
    race_repo: Path,
    project_settings: Settings,
) -> None:
    """Integration moving between the authorization and the run is followed.

    The run starts from the accepted baseline when it is created, which is the
    rule the whole system uses; a retry that froze a baseline at authorization
    time would start the next attempt from a tree that had already moved.
    """
    _engine, factory = private_db
    _project_id, task_id, _run_ids = _register_failed(factory, race_repo, history=1)
    _advance_integration(race_repo, "ROUTES = ()\n")
    with factory.begin() as session:
        _authorize(session, task_id)
    moved = _advance_integration(race_repo, "ROUTES = ('/home',)\n")

    with factory.begin() as session:
        run = create_run(session, task_id)
        session.flush()
        prepare_workspace(session, run.id, settings=project_settings)
        started_from = TaskRunRepository(session).get(run.id).starting_commit

    assert started_from == moved


def test_a_dependency_that_regresses_after_the_authorization_blocks_the_run(
    private_db: tuple[Engine, sessionmaker], race_repo: Path
) -> None:
    """The rule is enforced where the run is created, not only where it is asked for.

    Authorization is a decision made at one moment; execution happens at
    another. A dependency that stops being satisfied in between must not produce
    a run on a tree missing its work, so the readiness refresh demotes the task
    and the scheduler declines to select it.
    """
    _engine, factory = private_db
    project_id, task_id, _run_ids = _register_failed(factory, race_repo, dependency=True)
    with factory.begin() as session:
        _authorize(session, task_id)
        assert TaskRepository(session).get(task_id).status is TaskStatus.READY

    with factory.begin() as session:
        router = TaskRepository(session).get_by_external_id(project_id, "TS-105")
        TaskRepository(session).record_integration(router.id, unintegrated_commit="abc1234")
        refresh_readiness(session, project_id)
        selection = select_next_task(session, project_id)
        demoted = TaskRepository(session).get(task_id)

    assert demoted.status is TaskStatus.BLOCKED
    assert selection.task is None
    with factory() as session, pytest.raises(EntityConflict, match="BLOCKED"):
        create_run(session, task_id)


def test_the_scheduler_creates_the_run_and_the_next_pass_continues_that_run(
    private_db: tuple[Engine, sessionmaker], race_repo: Path
) -> None:
    _engine, factory = private_db
    project_id, task_id, run_ids = _register_failed(factory, race_repo, history=1)
    with factory.begin() as session:
        _authorize(session, task_id)
    with factory() as session:
        first = select_next_task(session, project_id)
    with factory.begin() as session:
        run = create_run(session, first.task.id)
    with factory() as session:
        incomplete = [candidate.run_id for candidate in inspect_incomplete_runs(session)]

    assert first.task is not None and first.task.id == task_id
    assert run.run_number == 2
    assert incomplete == [run.id]
    with factory() as session:
        assert [old.id for old in TaskRunRepository(session).list_for_task(task_id)] == [
            *run_ids,
            run.id,
        ]


@pytest.mark.asyncio
async def test_a_pause_that_arrives_between_authorization_and_execution_stops_the_run(
    private_db: tuple[Engine, sessionmaker],
    race_repo: Path,
    project_settings: Settings,
) -> None:
    """The window between the decision and the run is real, and it is guarded.

    ``run_next`` returns before creating anything when a pause is in force, so an
    operator who pauses a project between the two does not get a run started
    anyway.
    """
    _engine, factory = private_db
    project_id, task_id, run_ids = _register_failed(factory, race_repo, history=1)
    with factory.begin() as session:
        _authorize(session, task_id)
    with factory.begin() as session:
        PauseRequestRepository(session).add(
            PauseRequest(project_id=project_id, reason="hold the image change")
        )

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-106")),
        settings=project_settings,
    )
    selection, state = await runner.run_next(project_id)

    assert selection.task is not None
    assert state is None
    with factory() as session:
        assert [run.id for run in TaskRunRepository(session).list_for_task(task_id)] == run_ids, (
            "the authorized task was selected and then left alone: no run opened"
        )
        assert TaskRepository(session).get(task_id).status is TaskStatus.READY


@pytest.mark.asyncio
async def test_the_new_run_executes_and_completes_through_the_real_graph(
    private_db: tuple[Engine, sessionmaker],
    race_repo: Path,
    project_settings: Settings,
) -> None:
    """The whole path, end to end, with nothing stubbed but the two models.

    Operator authorization, then the ordinary project execution, then delivery:
    the retried task's work lands in the integration baseline, and the five
    abandoned runs are exactly as terminal as they were.
    """
    _engine, factory = private_db
    project_id, task_id, run_ids = _register_failed(factory, race_repo, history=2)
    integration_before = _advance_integration(race_repo, "ROUTES = ()\n")
    with factory.begin() as session:
        _authorize(session, task_id)
    assert _integration_sha(race_repo) == integration_before, (
        "authorizing a retry must not move the integration baseline"
    )

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-106")),
        settings=project_settings,
    )
    selection, state = await runner.run_next(project_id)

    assert state is not None
    assert state["outcome"] == "COMPLETED"
    with factory() as session:
        task = TaskRepository(session).get(task_id)
        runs = TaskRunRepository(session).list_for_task(task_id)
        fresh = [run for run in runs if run.id not in set(run_ids)]
        old = [run for run in runs if run.id in set(run_ids)]

    assert task.status is TaskStatus.COMPLETE
    assert len(fresh) == 1
    assert fresh[0].run_number == 3
    assert fresh[0].status is RunStatus.SUCCEEDED
    assert fresh[0].candidate_commit is not None
    assert fresh[0].starting_commit == integration_before
    assert len(old) == 2
    for run in old:
        assert run.status is RunStatus.ABANDONED
        assert run.candidate_commit is None
    assert _integration_sha(race_repo) == fresh[0].candidate_commit, (
        "the new run's candidate is what reached the integration baseline"
    )


# =============================================================================
# 5. The HTTP boundary
# =============================================================================


def test_a_failed_task_can_be_retried_over_http(
    api_client: TestClient, session: Session, ts106: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = ts106
    response = api_client.post(
        f"/tasks/{task.id}/retry",
        json={"reason": REASON, "requestedBy": OPERATOR},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(task.id)
    assert body["external_task_id"] == "TS-106"
    assert body["status"] == "READY"
    assert TaskRepository(session).get(task.id).status is TaskStatus.READY
    assert len(TaskRunRepository(session).list_for_task(task.id)) == len(runs)


def test_the_reason_is_required_over_http(
    api_client: TestClient, session: Session, failed_task: tuple[Task, list[TaskRun]]
) -> None:
    task, _runs = failed_task
    for body in ({}, {"reason": ""}, {"reason": "   "}, {"requestedBy": OPERATOR}):
        response = api_client.post(f"/tasks/{task.id}/retry", json=body)
        assert response.status_code == 422, (body, response.text)
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED
    assert _retry_events(session, task.id) == []


def test_a_task_that_is_not_failed_is_a_conflict_over_http(
    api_client: TestClient, session: Session, project: Project
) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)

    response = api_client.post(f"/tasks/{task.id}/retry", json={"reason": REASON})

    assert response.status_code == 409, response.text
    assert "READY" in response.text
    assert "only FAILED may be retried" in response.text
    assert _retry_events(session, task.id) == []


def test_a_task_with_a_run_in_flight_is_a_conflict_over_http(
    api_client: TestClient, session: Session, project: Project
) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    create_run(session, task.id)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    response = api_client.post(f"/tasks/{task.id}/retry", json={"reason": REASON})

    assert response.status_code == 409, response.text
    assert "still has run 1 in flight" in response.text
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED


def test_a_task_with_unmet_dependencies_is_a_conflict_over_http(
    api_client: TestClient, session: Session, project: Project
) -> None:
    task = _task(session, project, depends_on=["TS-105"])
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)

    response = api_client.post(f"/tasks/{task.id}/retry", json={"reason": REASON})

    assert response.status_code == 409, response.text
    assert "TS-105" in response.text
    assert TaskRepository(session).get(task.id).status is TaskStatus.FAILED


def test_a_paused_task_is_a_conflict_over_http(
    api_client: TestClient, session: Session, project: Project
) -> None:
    task = _task(session, project)
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    TaskRepository(session).transition(task.id, TaskStatus.FAILED)
    PauseRequestRepository(session).add(
        PauseRequest(project_id=project.id, task_id=task.id, reason="incident review")
    )

    response = api_client.post(f"/tasks/{task.id}/retry", json={"reason": REASON})

    assert response.status_code == 409, response.text
    assert "pause in force" in response.text


def test_an_unknown_task_is_a_404_over_http(api_client: TestClient) -> None:
    response = api_client.post(f"/tasks/{uuid.uuid4()}/retry", json={"reason": REASON})
    assert response.status_code == 404, response.text


def test_repeating_the_request_over_http_conflicts_and_creates_nothing(
    api_client: TestClient, session: Session, failed_task: tuple[Task, list[TaskRun]]
) -> None:
    task, runs = failed_task
    first = api_client.post(f"/tasks/{task.id}/retry", json={"reason": REASON})
    second = api_client.post(f"/tasks/{task.id}/retry", json={"reason": REASON})

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert len(_retry_events(session, task.id)) == 1
    assert len(TaskRunRepository(session).list_for_task(task.id)) == len(runs)


def test_the_route_is_part_of_the_documented_contract(
    api_client: TestClient, failed_task: tuple[Task, list[TaskRun]]
) -> None:
    """Published twice: in the schema and in the document operators read.

    The generated schema only proves a router was registered. An operator API
    that is not in the README does not exist as far as the person deciding
    whether to press it is concerned, so both are checked and the reason is
    required in each.
    """
    schema = api_client.get("/openapi.json").json()
    operation = schema["paths"]["/tasks/{task_id}/retry"]["post"]
    body = schema["components"]["schemas"]["RetryTaskRequest"]

    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "TaskResponse"
    )
    assert body["required"] == ["reason"]
    assert "reason" in body["properties"]

    readme = (REPO_ROOT / "README.md").read_text()

    assert "POST localhost:8000/tasks/$TASK_ID/retry" in readme
    assert "TASK_RETRY_AUTHORIZED" in readme
    assert "/tasks/{task_id}/retry" not in readme.split("### Retrying a failed task")[0]


# =============================================================================
# 6. Durability and reconstruction
# =============================================================================


def test_the_authorization_survives_a_commit_and_a_rebuilt_process(
    tmp_path: Path, ts106: tuple[Task, list[TaskRun]]
) -> None:
    """What a supervisor restart would find, read by a process that started fresh.

    A private database file, a real commit, then a child interpreter that opens
    the same file and reports what it sees with nothing but the standard library:
    the task is ``READY``, the decision is on the record with its reason, the
    abandoned runs are still terminal, and recovery finds nothing to resume.
    """
    database = tmp_path / "durable.db"
    engine = create_db_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory.begin() as session:
            project = _project(session)
            task, runs = _failed_task(session, project, history=2)
            _authorize(session, task.id)
            task_id = task.id
            run_ids = [run.id for run in runs]
        engine.dispose()

        script = (
            "import json, sqlite3, sys\n"
            "connection = sqlite3.connect(sys.argv[1])\n"
            "rows = connection.execute("
            "'SELECT id, status FROM tasks').fetchall()\n"
            "events = connection.execute("
            "'SELECT event_type, task_run_id, payload FROM run_events "
            "WHERE event_type = ? ORDER BY created_at, sequence',"
            " ('TASK_RETRY_AUTHORIZED',)).fetchall()\n"
            "abandoned = connection.execute("
            "'SELECT status, candidate_commit FROM task_runs').fetchall()\n"
            "print(json.dumps({'tasks': rows, 'events': events, 'runs': abandoned}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(database)],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
        )
        report = json.loads(completed.stdout.strip().splitlines()[-1])
    finally:
        engine.dispose()

    assert [row[1] for row in report["tasks"]] == ["READY"]
    assert report["tasks"][0][0] == task_id.hex, (
        "the task is READY in a database this process did not write"
    )
    assert len(report["events"]) == 2, "the authorization that produced run two, and this one"
    event_type, run_id, payload = report["events"][-1]
    payload = json.loads(payload)
    assert event_type == "TASK_RETRY_AUTHORIZED"
    assert run_id is None, "the decision belongs to the task, and survives with it"
    assert payload["reason"] == REASON
    assert payload["requested_by"] == OPERATOR
    assert payload["previous_status"] == "FAILED"
    assert payload["authorized_status"] == "READY"
    assert payload["historical_runs"] == 2
    assert report["runs"] == [["ABANDONED", None] for _ in run_ids]

    engine = create_db_engine(f"sqlite:///{database}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            assert TaskRepository(session).get(task_id).status is TaskStatus.READY
            assert TaskRunRepository(session).list_incomplete() == []
            for run_id in run_ids:
                assert TaskRunRepository(session).get(run_id).status is RunStatus.ABANDONED
            selection = select_next_task(session, TaskRepository(session).get(task_id).project_id)
            assert selection.task is not None
            run = create_run(session, task_id)
            session.commit()
        with factory() as session:
            assert TaskRunRepository(session).get(run.id).run_number == 3
    finally:
        engine.dispose()


# =============================================================================
# 7. The races, for real
# =============================================================================

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
    name = f"c65_{uuid.uuid4().hex[:12]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.rsplit("/", 1)[0] + f"/{name}"
    finally:
        drop_test_database(server, name)
        admin.dispose()


@pytest.fixture
def scratch_postgres() -> Iterator[tuple[str, Engine, sessionmaker]]:
    """A scratch PostgreSQL database with the whole schema in it.

    Here, and not in the rest of this file, because these tests need two
    sessions with real transactions open against each other. SQLite serializes
    writers, so a test written against it would be testing the harness. The URL
    comes back too, so the HTTP test can point the app's own session factory at
    it rather than substituting a session of its own.
    """
    with _scratch_postgres() as url:
        # Concern 74: Validate the scratch database is safe before destructive ops.
        assert_test_database_safe(url)
        engine = create_db_engine(url)
        Base.metadata.create_all(engine)
        try:
            yield url, engine, sessionmaker(bind=engine, expire_on_commit=False)
        finally:
            drop_all_tables_for_test(engine, url)
            engine.dispose()


def test_two_simultaneous_retries_over_http_cannot_both_authorize(
    scratch_postgres: tuple[Engine, sessionmaker], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two operators, one button, two real connections, one outcome.

    Each request opens its own session from its own thread, so the two
    transactions really do contend for the task row in PostgreSQL rather than
    sharing a session that would have serialized them. Exactly one is authorized;
    the other is told the task is no longer ``FAILED``. Then the scheduler runs
    once, and there is still exactly one new run.
    """
    url, _engine, factory = scratch_postgres
    project_id, task_id, run_ids = _register_failed(factory, Path("/tmp/tracestack"))

    # The app's own session factory, pointed at the scratch database. No
    # dependency override: a request that overrode ``get_db`` with a session of
    # its own would never commit, and a session left holding the task row lock
    # would make the other request time out rather than lose the race. This way
    # each request gets a real session, a real commit and its own connection.
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("WORKER_BACKEND", "subprocess")
    get_settings.cache_clear()
    reset_engine()
    try:
        app = create_app()
        barrier = threading.Barrier(2)
        statuses: list[int] = []
        lock = threading.Lock()
        failures: list[BaseException] = []

        def _post() -> None:
            with TestClient(app) as client:
                try:
                    barrier.wait(timeout=30)
                    response = client.post(
                        f"/tasks/{task_id}/retry",
                        json={"reason": REASON, "requestedBy": OPERATOR},
                    )
                    with lock:
                        statuses.append(response.status_code)
                except BaseException as exc:  # pragma: no cover - reported below
                    with lock:
                        failures.append(exc)

        threads = [threading.Thread(target=_post) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert failures == []
        assert sorted(statuses) == [200, 409], (
            "two concurrent retries of one failed task: exactly one may be authorized"
        )
    finally:
        reset_engine()
        get_settings.cache_clear()
    with factory() as session:
        assert TaskRepository(session).get(task_id).status is TaskStatus.READY
        assert len(_retry_events(session, task_id)) == HISTORY, (
            "the four historical authorizations and one new one, however many requests arrived"
        )
    with factory.begin() as session:
        run = create_run(session, task_id)
    with factory() as session:
        runs = TaskRunRepository(session).list_for_task(task_id)
    assert run.run_number == HISTORY + 1
    assert len(runs) == HISTORY + 1
    assert {run_.id for run_ in runs} == set(run_ids) | {run.id}


def test_a_retry_racing_a_run_creation_cannot_duplicate_execution(
    scratch_postgres: tuple[Engine, sessionmaker],
) -> None:
    """Whichever commits first, the task ends up with one new run and no lost request.

    The operator authorizes; the orchestrator selects and creates. Started
    together, they either serialize (one authorizes, the other then creates run
    six) or the orchestrator finds nothing ``READY`` yet and the operator's
    authorization stands for the next pass. The failure this rules out is a
    world with two runs of a task that was authorized once.
    """
    _url, _engine, factory = scratch_postgres
    project_id, task_id, run_ids = _register_failed(factory, Path("/tmp/tracestack"))
    barrier = threading.Barrier(2)
    authorized: list[str] = []
    created: list[uuid.UUID] = []
    lock = threading.Lock()
    failures: list[BaseException] = []

    def _operator() -> None:
        try:
            with factory() as session:
                barrier.wait(timeout=30)
                try:
                    _authorize(session, task_id)
                    session.commit()
                    with lock:
                        authorized.append("authorized")
                except EntityConflict as exc:
                    session.rollback()
                    with lock:
                        authorized.append(f"refused: {exc}")
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                failures.append(exc)

    def _orchestrator() -> None:
        try:
            with factory() as session:
                barrier.wait(timeout=30)
                selection = select_next_task(session, project_id)
                if selection.task is not None:
                    run = create_run(session, selection.task.id)
                    session.commit()
                    with lock:
                        created.append(run.id)
                else:
                    session.rollback()
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=_operator), threading.Thread(target=_orchestrator)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    assert len(created) <= 1, "the scheduler created two runs of one task"
    assert authorized != []

    with factory() as session:
        selection = select_next_task(session, project_id)
        if selection.task is not None:
            create_run(session, selection.task.id)
            session.commit()
    with factory() as session:
        runs = TaskRunRepository(session).list_for_task(task_id)
        task = TaskRepository(session).get(task_id)
        events = _retry_events(session, task_id)
        in_flight = TaskRunRepository(session).in_flight_for_task(task_id)

    assert len(runs) == HISTORY + 1, "one new run, whichever order they committed in"
    assert [run_.run_number for run_ in runs] == list(range(1, HISTORY + 2))
    assert {run_.id for run_ in runs} >= set(run_ids)
    assert len(events) == HISTORY
    assert task.status in {TaskStatus.READY, TaskStatus.CODING}
    assert in_flight is not None and in_flight.run_number == HISTORY + 1


def test_a_committed_run_in_flight_closes_the_door_on_postgres(
    scratch_postgres: tuple[Engine, sessionmaker],
) -> None:
    """The guard against a real committed run, on a real database.

    Not a synthetic state: a task is authorized, the orchestrator creates its
    run and commits, and the operator's second request -- from another
    connection -- finds a task that is ``CODING`` with run six in flight. Nothing
    is authorized and no second run is created.
    """
    _url, _engine, factory = scratch_postgres
    _project_id, task_id, _run_ids = _register_failed(factory, Path("/tmp/tracestack"), history=1)
    with factory.begin() as session:
        _authorize(session, task_id)
    with factory.begin() as session:
        run = create_run(session, task_id)
    with factory() as session:
        with pytest.raises(EntityConflict):
            _authorize(session, task_id)
        session.rollback()
    with factory() as session:
        runs = TaskRunRepository(session).list_for_task(task_id)
        in_flight = TaskRunRepository(session).in_flight_for_task(task_id)
        events = _retry_events(session, task_id)

    assert len(runs) == 2
    assert in_flight is not None and in_flight.id == run.id
    assert len(events) == 1


# =============================================================================
# 8. That this file does not pollute the shared database
# =============================================================================

_TABLES = (
    "projects",
    "tasks",
    "task_runs",
    "run_events",
    "artifacts",
    "human_escalations",
    "pause_requests",
    "verification_runs",
)


def _row_counts(engine: Engine) -> dict[str, int]:
    """Row counts read from a connection of their own.

    A separate connection is the point: a session inside a test's own
    transaction can see its own uncommitted writes, so asking it would report
    the rows the test just made and prove nothing at all.
    """
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in _TABLES:
            counts[table] = connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0
    return counts


@pytest.fixture(autouse=True, scope="module")
def _shared_database_is_left_alone(engine: Engine) -> Iterator[None]:
    """The suite's shared database, snapshotted at both ends of this module.

    Every test here either runs inside the rolled-back ``session`` fixture or
    against a private database of its own. Module scope rather than a final
    test, because the suite randomizes test order (``pytest-randomly`` is
    installed and active by default): this measures the file, not whichever test
    happened to run last.
    """
    before = _row_counts(engine)
    yield
    after = _row_counts(engine)
    assert after == before, (
        "this module committed rows into the shared database; every test here "
        "should roll back or use a private database"
    )
