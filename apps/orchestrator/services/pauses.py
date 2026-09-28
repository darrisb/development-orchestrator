"""Task-scoped pause and resume requests (build.md section 28)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..domain.enums import RunStatus, TaskStatus
from ..domain.models import PauseRequest, Task
from ..domain.state_machine import is_active
from ..repositories import PauseRequestRepository, TaskRepository, TaskRunRepository
from .errors import EntityConflict, EntityNotFound
from .scheduler import unsatisfied_dependencies


def pause_task(
    session: Session,
    task_id: UUID,
    *,
    reason: str | None = None,
    requested_by: str | None = None,
) -> PauseRequest:
    tasks = TaskRepository(session)
    task = tasks.get(task_id)
    if task is None:
        raise EntityNotFound("Task", task_id)
    pauses = PauseRequestRepository(session)
    existing = pauses.list_in_force(project_id=task.project_id, task_id=task.id)
    if existing:
        return existing[0]
    stored = pauses.add(
        PauseRequest(
            project_id=task.project_id,
            task_id=task.id,
            reason=reason,
            requested_by=requested_by,
        )
    )
    # A task not in flight is already at a safe boundary. An active task is
    # moved only by the graph after its current service call returns.
    if not is_active(task.status) and task.status not in {
        TaskStatus.PAUSED,
        TaskStatus.COMPLETE,
        TaskStatus.HUMAN_REVIEW,
        TaskStatus.FAILED,
    }:
        tasks.transition(task.id, TaskStatus.PAUSED)
        pauses.mark_honoured(stored.id)
        stored = pauses.get(stored.id) or stored
    return stored


def resume_task(session: Session, task_id: UUID) -> Task:
    tasks = TaskRepository(session)
    task = tasks.get(task_id)
    if task is None:
        raise EntityNotFound("Task", task_id)
    PauseRequestRepository(session).release(
        project_id=task.project_id, task_id=task.id
    )
    if task.status is not TaskStatus.PAUSED:
        raise EntityConflict(f"Task {task.external_task_id} is not paused")
    incomplete = [
        run
        for run in TaskRunRepository(session).list_for_task(task.id)
        if run.status in {RunStatus.PENDING, RunStatus.RUNNING}
    ]
    if incomplete:
        # The persisted graph owns restoring the correct safe boundary. Moving
        # it here would let the scheduler open a second run for the same task.
        return task

    # A dependency is satisfied only when it is complete *and* its accepted work
    # is in the integration baseline (concern 51). Resuming is one of the places
    # a task is promoted without the scheduler's readiness pass, so it has to
    # apply the same rule or a resume would smuggle a task past a blocked
    # dependency. The rule itself is the scheduler's, shared rather than restated
    # here: two copies of "satisfied" would eventually disagree, and the one
    # that disagreed would be the one with no readiness pass to correct it.
    target = (
        TaskStatus.READY
        if not unsatisfied_dependencies(session, task)
        else TaskStatus.PENDING
    )
    return tasks.transition(task.id, target)


__all__ = ["pause_task", "resume_task"]
