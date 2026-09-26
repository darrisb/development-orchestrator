"""Run creation (build.md sections 7, 23 and 40)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import RunEventType, RunStatus, TaskStatus
from apps.orchestrator.domain.errors import LimitExceeded
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.repositories import (
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
)
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound
from apps.orchestrator.services.runs import create_run, get_run, list_runs_for_task

pytestmark = pytest.mark.integration


@pytest.fixture
def ready_task(session: Session) -> Task:
    project = ProjectRepository(session).add(
        Project(name="Runs", repository_path="/workspace/runs")
    )
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="T-1", title="First")
    )
    return TaskRepository(session).transition(task.id, TaskStatus.READY)


def test_a_run_starts_pending_and_pins_its_starting_commit(session: Session, ready_task: Task):
    run = create_run(session, ready_task.id, starting_commit="abc123", branch_name="task/T-1")

    assert run.run_number == 1
    assert run.attempt_number == 1
    assert run.status is RunStatus.PENDING
    assert run.starting_commit == "abc123"
    assert run.branch_name == "task/T-1"
    assert run.started_at is not None


def test_creating_a_run_records_task_selected(session: Session, ready_task: Task):
    run = create_run(session, ready_task.id, starting_commit="abc123")

    events = RunEventRepository(session).list_for_run(run.id)
    assert [event.event_type for event in events] == [RunEventType.TASK_SELECTED]
    payload = events[0].payload
    assert payload["external_task_id"] == "T-1"
    assert payload["run_number"] == 1
    assert events[0].task_id == ready_task.id
    assert events[0].project_id == ready_task.project_id


def test_run_numbers_increase_per_task(session: Session, ready_task: Task):
    create_run(session, ready_task.id)
    second = create_run(session, ready_task.id)

    assert second.run_number == 2
    assert [run.run_number for run in list_runs_for_task(session, ready_task.id)] == [1, 2]


def test_a_run_can_be_opened_for_a_task_with_requested_changes(session: Session, ready_task: Task):
    tasks = TaskRepository(session)
    for status in (TaskStatus.CODING, TaskStatus.VERIFYING, TaskStatus.REVIEW_PENDING):
        tasks.transition(ready_task.id, status)
    tasks.transition(ready_task.id, TaskStatus.REVIEWING)
    tasks.transition(ready_task.id, TaskStatus.CHANGES_REQUESTED)

    run = create_run(session, ready_task.id, attempt_number=2)

    assert run.attempt_number == 2


def test_a_run_cannot_be_opened_for_a_task_that_is_not_startable(
    session: Session, ready_task: Task
):
    TaskRepository(session).transition(ready_task.id, TaskStatus.CODING)

    with pytest.raises(EntityConflict, match="a run needs one of"):
        create_run(session, ready_task.id)


def test_the_attempt_ceiling_is_enforced_before_any_work_starts(session: Session):
    """Section 23: never loop indefinitely."""
    project = ProjectRepository(session).add(
        Project(name="Runs", repository_path="/workspace/runs")
    )
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="T-9",
            title="Capped",
            limits=TaskLimits(max_attempts=2),
        )
    )
    TaskRepository(session).transition(task.id, TaskStatus.READY)

    create_run(session, task.id, attempt_number=2)
    with pytest.raises(LimitExceeded, match="max_attempts=2"):
        create_run(session, task.id, attempt_number=3)


def test_an_unknown_task_cannot_open_a_run(session: Session):
    with pytest.raises(EntityNotFound):
        create_run(session, uuid4())


def test_reading_an_unknown_run_raises(session: Session):
    with pytest.raises(EntityNotFound):
        get_run(session, uuid4())


def test_listing_runs_for_an_unknown_task_raises(session: Session):
    with pytest.raises(EntityNotFound):
        list_runs_for_task(session, uuid4())
