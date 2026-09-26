"""Next-ready-task selection (build.md section 26, Phase B exit condition)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import ProjectStatus, TaskStatus
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services.scheduler import (
    NoTaskReason,
    active_tasks,
    refresh_readiness,
    select_next_task,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(name="Scheduling", repository_path="/workspace/scheduling")
    )


def _add(session: Session, project: Project, external_id: str, **kwargs) -> Task:
    return TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id=external_id,
            title=external_id,
            **kwargs,
        )
    )


def _complete(session: Session, task: Task) -> None:
    """Drive a task to COMPLETE through legal transitions only."""
    tasks = TaskRepository(session)
    for status in (
        TaskStatus.READY,
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
        TaskStatus.COMPLETE,
    ):
        current = tasks.get(task.id)
        assert current is not None
        if current.status is not status:
            tasks.transition(task.id, status)


def test_the_first_dependency_free_task_is_selected(session: Session, project: Project):
    _add(session, project, "T-2", section=2, depends_on=["T-1"])
    _add(session, project, "T-1", section=1)

    selection = select_next_task(session, project.id)

    assert selection.task is not None
    assert selection.task.external_task_id == "T-1"
    assert selection.task.status is TaskStatus.READY
    assert selection.reason is None


def test_selection_follows_section_order_not_row_order(session: Session, project: Project):
    _add(session, project, "T-9", section=3)
    _add(session, project, "T-5", section=1)
    _add(session, project, "T-7", section=2)

    selection = select_next_task(session, project.id)

    assert selection.task is not None
    assert selection.task.external_task_id == "T-5"


def test_tasks_without_a_section_are_selected_last(session: Session, project: Project):
    _add(session, project, "A-1")
    _add(session, project, "B-1", section=7)

    selection = select_next_task(session, project.id)

    assert selection.task is not None
    assert selection.task.external_task_id == "B-1"


def test_the_successor_is_selected_once_its_dependency_completes(
    session: Session, project: Project
):
    first = _add(session, project, "T-1", section=1)
    _add(session, project, "T-2", section=2, depends_on=["T-1"])
    _complete(session, first)

    selection = select_next_task(session, project.id)

    assert selection.task is not None
    assert selection.task.external_task_id == "T-2"


def test_a_task_in_flight_suppresses_selection(session: Session, project: Project):
    """V1 executes one task at a time (section 26)."""
    first = _add(session, project, "T-1", section=1)
    _add(session, project, "T-2", section=2)
    tasks = TaskRepository(session)
    tasks.transition(first.id, TaskStatus.READY)
    tasks.transition(first.id, TaskStatus.CODING)

    selection = select_next_task(session, project.id)

    assert selection.task is None
    assert selection.reason is NoTaskReason.TASK_IN_FLIGHT
    assert [task.external_task_id for task in active_tasks(session, project.id)] == ["T-1"]


def test_nothing_is_selected_while_a_dependency_has_failed(session: Session, project: Project):
    first = _add(session, project, "T-1", section=1)
    _add(session, project, "T-2", section=2, depends_on=["T-1"])
    tasks = TaskRepository(session)
    tasks.transition(first.id, TaskStatus.READY)
    tasks.transition(first.id, TaskStatus.CODING)
    tasks.transition(first.id, TaskStatus.FAILED)

    selection = select_next_task(session, project.id)

    assert selection.task is None
    assert selection.reason is NoTaskReason.NO_READY_TASK
    assert selection.readiness.blocked == {"T-2": ("T-1",)}
    successor = tasks.get_by_external_id(project.id, "T-2")
    assert successor is not None
    assert successor.status is TaskStatus.BLOCKED


def test_a_completed_project_reports_all_tasks_complete(session: Session, project: Project):
    _complete(session, _add(session, project, "T-1", section=1))

    selection = select_next_task(session, project.id)

    assert selection.task is None
    assert selection.reason is NoTaskReason.ALL_TASKS_COMPLETE


def test_a_project_without_tasks_reports_no_tasks(session: Session, project: Project):
    selection = select_next_task(session, project.id)
    assert selection.reason is NoTaskReason.NO_TASKS


def test_a_paused_project_schedules_nothing(session: Session, project: Project):
    _add(session, project, "T-1", section=1)
    ProjectRepository(session).set_status(project.id, ProjectStatus.PAUSED)

    selection = select_next_task(session, project.id)

    assert selection.task is None
    assert selection.reason is NoTaskReason.PROJECT_NOT_RUNNABLE


def test_selecting_for_an_unknown_project_raises(session: Session):
    with pytest.raises(LookupError):
        select_next_task(session, uuid4())


def test_a_blocked_task_is_promoted_again_once_its_dependency_completes(
    session: Session, project: Project
):
    first = _add(session, project, "T-1", section=1)
    second = _add(session, project, "T-2", section=2, depends_on=["T-1"])
    tasks = TaskRepository(session)
    tasks.transition(second.id, TaskStatus.BLOCKED)
    _complete(session, first)

    refresh_readiness(session, project.id)

    reloaded = tasks.get(second.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.READY


def test_a_ready_task_is_demoted_when_a_resync_adds_an_unmet_dependency(
    session: Session, project: Project
):
    """The state machine has no READY -> PENDING edge, so BLOCKED is used."""
    _add(session, project, "T-1", section=1)
    second = _add(session, project, "T-2", section=2)
    tasks = TaskRepository(session)
    refresh_readiness(session, project.id)
    assert tasks.get(second.id).status is TaskStatus.READY  # type: ignore[union-attr]

    tasks.update_fields(second.id, depends_on=["T-1"])
    refresh_readiness(session, project.id)

    reloaded = tasks.get(second.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.BLOCKED


def test_refresh_is_idempotent(session: Session, project: Project):
    _add(session, project, "T-1", section=1)
    _add(session, project, "T-2", section=2, depends_on=["T-1"])

    first = refresh_readiness(session, project.id)
    second = refresh_readiness(session, project.id)

    assert first == second
    assert second.ready == ("T-1",)
