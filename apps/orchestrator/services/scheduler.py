"""Readiness refresh and next-task selection (build.md section 26).

The orchestrator, never a model, decides what runs next. Selection is a pure
function of persisted task state plus the dependency graph, so the same
database always yields the same choice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.dependencies import ReadinessReport, evaluate_readiness
from ..domain.enums import ProjectStatus, TaskStatus
from ..domain.models import Task
from ..domain.state_machine import is_active
from ..repositories import ProjectRepository, TaskRepository

logger = get_logger(__name__)

#: Project states in which new work may be scheduled.
RUNNABLE_PROJECT_STATES: frozenset[ProjectStatus] = frozenset(
    {ProjectStatus.REGISTERED, ProjectStatus.RUNNING}
)


class NoTaskReason(StrEnum):
    """Why selection returned nothing. Exposed over the API for operators."""

    PROJECT_NOT_RUNNABLE = "PROJECT_NOT_RUNNABLE"
    NO_TASKS = "NO_TASKS"
    TASK_IN_FLIGHT = "TASK_IN_FLIGHT"
    ALL_TASKS_COMPLETE = "ALL_TASKS_COMPLETE"
    NO_READY_TASK = "NO_READY_TASK"


@dataclass(frozen=True, slots=True)
class Selection:
    """Outcome of a selection attempt: a task, or the reason there is none."""

    task: Task | None = None
    reason: NoTaskReason | None = None
    readiness: ReadinessReport = ReadinessReport()

    def __post_init__(self) -> None:
        if (self.task is None) == (self.reason is None):
            raise ValueError("Selection carries exactly one of task or reason")


def _sort_key(task: Task) -> tuple[int, int, str]:
    """Section order first, unsectioned tasks last, then task id.

    Sorting by id as the final key keeps selection deterministic; database row
    order must never decide what the orchestrator works on.
    """
    if task.section is None:
        return (1, 0, task.external_task_id)
    return (0, task.section, task.external_task_id)


def evaluate_project_readiness(session: Session, project_id: UUID) -> ReadinessReport:
    """Classify the project's tasks without writing anything."""
    repository = TaskRepository(session)
    tasks = repository.list_for_project(project_id)
    graph = {task.external_task_id: tuple(task.depends_on) for task in tasks}
    statuses = {task.external_task_id: task.status for task in tasks}
    return evaluate_readiness(
        graph, statuses, unintegrated=_unintegrated(tasks)
    )


def _unintegrated(tasks: Iterable[Task]) -> frozenset[str]:
    """Tasks whose accepted output is not in the integration baseline.

    Concern 51's second half: ``COMPLETE`` says a task was done and this says
    whether its work is in the tree the next task would start from. A dependency
    needs both, so this set is what turns a blocked integration into a scheduling
    fact instead of a log line.
    """
    return frozenset(
        task.external_task_id for task in tasks if task.unintegrated_commit is not None
    )


def refresh_readiness(session: Session, project_id: UUID) -> ReadinessReport:
    """Persist the READY/BLOCKED consequences of the current dependency state.

    A task whose dependencies are all ``COMPLETE`` *and in the integration
    baseline* becomes ``READY``; one whose
    dependencies cannot currently be satisfied becomes ``BLOCKED``. A task that
    was already ``READY`` but is now waiting on an incomplete dependency -- only
    possible after a manifest re-sync widened its dependencies -- is also moved
    to ``BLOCKED``, because the state machine has no path back to ``PENDING``
    and leaving it ``READY`` would let it be selected with unmet dependencies.
    """
    tasks = TaskRepository(session)
    by_id = {task.external_task_id: task for task in tasks.list_for_project(project_id)}
    graph = {task_id: tuple(task.depends_on) for task_id, task in by_id.items()}
    report = evaluate_readiness(
        graph,
        {k: task.status for k, task in by_id.items()},
        unintegrated=_unintegrated(by_id.values()),
    )

    for task_id in report.ready:
        task = by_id[task_id]
        if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
            tasks.transition(task.id, TaskStatus.READY)

    for task_id, unsatisfiable in report.blocked.items():
        task = by_id[task_id]
        if task.status in (TaskStatus.PENDING, TaskStatus.READY):
            tasks.transition(task.id, TaskStatus.BLOCKED)
            logger.info(
                "task_blocked",
                project_id=str(project_id),
                task=task_id,
                blocked_by=list(unsatisfiable),
                unintegrated_dependencies=list(report.unintegrated.get(task_id, ())),
            )

    # A task merely waiting its turn stays PENDING; only one already promoted to
    # READY has to be demoted, and BLOCKED is the sole legal destination.
    for task_id, outstanding in report.waiting.items():
        task = by_id[task_id]
        if task.status is TaskStatus.READY:
            tasks.transition(task.id, TaskStatus.BLOCKED)
            logger.info(
                "ready_task_demoted",
                project_id=str(project_id),
                task=task_id,
                waiting_on=list(outstanding),
            )
    return report


def active_tasks(session: Session, project_id: UUID) -> list[Task]:
    """Tasks with a run in flight. V1 expects at most one (section 26)."""
    return [
        task
        for task in TaskRepository(session).list_for_project(project_id)
        if is_active(task.status)
    ]


def select_next_task(session: Session, project_id: UUID) -> Selection:
    """Return the single task that should run next for this project.

    V1 executes one task at a time, so an in-flight task suppresses selection
    rather than queueing a second one.
    """
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise LookupError(f"Project {project_id} not found")
    if project.status not in RUNNABLE_PROJECT_STATES:
        return Selection(reason=NoTaskReason.PROJECT_NOT_RUNNABLE)

    tasks = TaskRepository(session).list_for_project(project_id)
    if not tasks:
        return Selection(reason=NoTaskReason.NO_TASKS)
    if any(is_active(task.status) for task in tasks):
        return Selection(reason=NoTaskReason.TASK_IN_FLIGHT)

    report = refresh_readiness(session, project_id)
    if not report.ready:
        reason = (
            NoTaskReason.ALL_TASKS_COMPLETE
            if all(task.status is TaskStatus.COMPLETE for task in tasks)
            else NoTaskReason.NO_READY_TASK
        )
        return Selection(reason=reason, readiness=report)

    ready = sorted(
        (task for task in tasks if task.external_task_id in report.ready), key=_sort_key
    )
    # refresh_readiness promoted the task; re-read so the caller sees READY.
    chosen = TaskRepository(session).get(ready[0].id)
    if chosen is None:  # pragma: no cover - the row was read in this transaction
        raise LookupError(f"Task {ready[0].id} disappeared during selection")
    return Selection(task=chosen, readiness=report)
