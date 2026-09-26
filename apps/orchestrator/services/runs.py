"""Task-run creation (build.md sections 7, 23 and 40).

A run is the unit of accountable work on one task: it pins the starting commit
and records every event that follows. Creating one is the orchestrator's job,
so the guards here (task state, attempt ceiling) are enforced before any model
or worker is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import FailureReason, RunEventType, RunStatus, TaskStatus
from ..domain.errors import LimitExceeded
from ..domain.models import RunEvent, Task, TaskRun
from ..repositories import RunEventRepository, TaskRepository, TaskRunRepository
from .errors import EntityConflict, EntityNotFound

logger = get_logger(__name__)

#: A run may only start from a task that is waiting to be worked on. Anything
#: in flight already has a run; anything complete or failed needs an explicit
#: state change first.
STARTABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.READY, TaskStatus.CHANGES_REQUESTED}
)


def create_run(
    session: Session,
    task_id: UUID,
    *,
    attempt_number: int = 1,
    starting_commit: str | None = None,
    branch_name: str | None = None,
    worker_image: str | None = None,
    coder_model_id: UUID | None = None,
    prompt_version: str | None = None,
) -> TaskRun:
    """Open a new run for ``task_id`` and record ``TASK_SELECTED``.

    The run is created ``PENDING`` and ``started_at`` is stamped now; the
    workflow moves it to ``RUNNING`` when a worker actually picks it up.

    Raises:
        EntityNotFound: no such task.
        EntityConflict: the task is not in a startable state.
        LimitExceeded: ``attempt_number`` exceeds the task's ``max_attempts``.
    """
    tasks = TaskRepository(session)
    task = tasks.get(task_id)
    if task is None:
        raise EntityNotFound("Task", task_id)
    if task.status not in STARTABLE_STATUSES:
        startable = ", ".join(sorted(STARTABLE_STATUSES))
        raise EntityConflict(
            f"Task {task.external_task_id} is {task.status}; a run needs one of: {startable}"
        )
    _assert_attempt_allowed(task, attempt_number)

    runs = TaskRunRepository(session)
    run = runs.add(
        TaskRun(
            task_id=task.id,
            run_number=runs.next_run_number(task.id),
            attempt_number=attempt_number,
            status=RunStatus.PENDING,
            starting_commit=starting_commit,
            branch_name=branch_name,
            worker_image=worker_image,
            coder_model_id=coder_model_id,
            prompt_version=prompt_version,
            started_at=datetime.now(UTC),
        )
    )

    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=task.project_id,
            task_id=task.id,
            event_type=RunEventType.TASK_SELECTED,
            attempt=run.attempt_number,
            payload={
                "external_task_id": task.external_task_id,
                "run_number": run.run_number,
                "starting_commit": starting_commit,
            },
        )
    )
    logger.info(
        "run_created",
        run_id=str(run.id),
        task=task.external_task_id,
        run_number=run.run_number,
        attempt=run.attempt_number,
    )
    return run


def _assert_attempt_allowed(task: Task, attempt_number: int) -> None:
    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")
    if attempt_number > task.limits.max_attempts:
        raise LimitExceeded(
            f"Task {task.external_task_id} attempt {attempt_number} exceeds "
            f"max_attempts={task.limits.max_attempts}",
            FailureReason.RETRY_EXHAUSTED,
        )


def get_run(session: Session, run_id: UUID) -> TaskRun:
    """Raises:
    EntityNotFound: no such run.
    """
    run = TaskRunRepository(session).get(run_id)
    if run is None:
        raise EntityNotFound("Run", run_id)
    return run


def list_runs_for_task(session: Session, task_id: UUID) -> list[TaskRun]:
    """Raises:
    EntityNotFound: no such task.
    """
    if TaskRepository(session).get(task_id) is None:
        raise EntityNotFound("Task", task_id)
    return TaskRunRepository(session).list_for_task(task_id)
