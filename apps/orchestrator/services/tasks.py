"""Task reads for the API (build.md section 39)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..domain.enums import TaskStatus
from ..domain.models import Task
from ..repositories import ProjectRepository, TaskRepository
from .errors import EntityNotFound


def get_task(session: Session, task_id: UUID) -> Task:
    """Raises:
    EntityNotFound: no such task.
    """
    task = TaskRepository(session).get(task_id)
    if task is None:
        raise EntityNotFound("Task", task_id)
    return task


def list_tasks(
    session: Session, project_id: UUID, status: TaskStatus | None = None
) -> list[Task]:
    """Raises:
    EntityNotFound: no such project.
    """
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    return TaskRepository(session).list_for_project(project_id, status)
