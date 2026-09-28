"""Operator-initiated retry of a failed task (concern 65).

Concern 64 made it possible to stop an invalid run. It left a task in
``FAILED``, which is the state that means "a person has to decide what happens
next" -- and the next TS-106 experiment found that nothing could act on that
decision. ``create_run`` accepts only ``READY`` and ``CHANGES_REQUESTED``, the
scheduler does not manage ``FAILED``, task resume accepts only ``PAUSED``, and
escalation resolution needs an ``OPEN`` escalation that a consumed ``RETRY_TASK``
answer had already closed. A human was left holding a decision with no supported
way to record it, which is why the workaround was a direct database mutation and
a manifest re-import: both were refused, correctly, and neither was a supported
operation.

**What this operation means.** "I authorize a new run for this failed task."
Not "pretend this task was never failed": the history stays exactly as it is,
the run that failed stays exactly as it is, and the escalations that were
answered stay answered. Not a resume either: the abandoned run is terminal and
stays terminal, and nothing is ever continued from it.

**Authorization, not execution.** The operation moves the task from ``FAILED``
to ``READY`` and records that a person asked for it. The run itself is created
by the next project execution, through ``services.runs.create_run``, by the same
scheduler that creates every other run. That is a deliberate choice, and the
alternative was measured rather than assumed:

*The obvious alternative is for this operation to create the run itself.* It
would be more direct -- one request, one run, a run id in the response. It also
duplicates the scheduler: the pause check, the project-runnable check, the
"one task at a time" check, the dependency check, and then ``create_run`` itself,
all re-derived in a second place that does not own them. And it introduces a new
invariant the existing path does not have -- a ``PENDING`` run whose task is
``READY`` -- which the scheduler would then have to learn to respect. The
authorization is the part a person decides; the run is the part the orchestrator
already knows how to create, safely, from the accepted integration baseline.

**The FAILED task that abandonment leaves behind is a legitimate resting place,
not a defect.** It is also unreachable-but-for-a-person, which is why this is an
operator surface and not an agent tool: the orchestrator never retries its own
failures from here, and the state machine still permits only the moves it
permits. ``COMPLETE`` is not retryable and cannot be made so -- its work is
delivered and in the integration baseline, and there is no move out of it.

**Two refusals are enforced by the database, in one statement.** The task must
still be ``FAILED`` and no run of it may be in flight, and both are predicates
on the same guarded ``UPDATE`` that performs the move
(:meth:`TaskRepository.transition_guarded`). A read-then-write would answer
"there was no run in flight a moment ago" and be wrong the moment after; the
guard is evaluated while the row lock is held, so commit order decides. Two
operator retries racing therefore cannot both succeed, and a retry racing a run
creation cannot end up with two runs: whichever commits first, the other finds
the task out of ``FAILED`` or a run in flight, and says so.

**Idempotency is the transition itself.** A repeated request finds a task that
is no longer ``FAILED`` and is refused with a truthful 409. It does not create a
second run, and it does not quietly authorize another one -- "retry" is not a
thing this endpoint can be asked to do twice.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..db.models import TaskRow
from ..domain.enums import RunEventType, TaskStatus
from ..domain.models import RunEvent, Task
from ..repositories import (
    PauseRequestRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from .errors import EntityConflict, EntityNotFound
from .scheduler import RUNNABLE_PROJECT_STATES, unsatisfied_dependencies

logger = get_logger(__name__)

#: The only task state this operation will act on.
#:
#: ``FAILED``, and nothing else, for V1. ``HUMAN_REVIEW`` also reaches ``READY``
#: in the state machine and is deliberately *not* here: that state is the task
#: waiting for an answer to an open escalation, and answering it is
#: ``apply_escalation_answer``'s job, not this endpoint's. ``COMPLETE`` is
#: terminal in the state machine and its work is already in the integration
#: baseline; there is nothing left to authorize a run for.
#:
#: A single member is still a constant rather than an inline comparison, because
#: the eligibility rule is the thing a later change has to argue with, and it
#: should be one edit in one place.
RETRYABLE_TASK_STATES: frozenset[TaskStatus] = frozenset({TaskStatus.FAILED})

#: Where a retried task goes. ``READY`` is the state the scheduler selects from,
#: and it is the only state from which a new run may be created, so authorizing
#: a retry is exactly "make this task eligible for a new run" -- no more.
RETRY_AUTHORIZED_STATUS: TaskStatus = TaskStatus.READY


def retry_failed_task(
    session: Session,
    task_id: UUID,
    *,
    reason: str,
    requested_by: str | None = None,
) -> Task:
    """Authorize a new run for a failed task.

    The task and its audit event are written in the caller's transaction, so
    the caller's commit or rollback decides whether the authorization happened.

    Args:
        session: The caller's transaction.
        task_id: The failed task to retry.
        reason: Required operator explanation, recorded in the event payload.
        requested_by: Optional operator identifier, recorded in the payload.

    Returns:
        The task, now ``READY`` and eligible for a new run.

    Raises:
        ValueError: ``reason`` is empty or whitespace.
        EntityNotFound: No such task, or its project.
        EntityConflict: The task is not ``FAILED``; a run of it is in flight; a
            dependency is not satisfied; the project is not runnable; or a pause
            is in force.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required")

    tasks = TaskRepository(session)
    runs = TaskRunRepository(session)

    # The authoritative read. A plain read is a snapshot, and everything below
    # is a decision made from it; holding the row lock is what stops the task
    # changing between the decision and the write that records it. See
    # TaskRepository.lock.
    try:
        task = tasks.lock(task_id)
    except LookupError:
        raise EntityNotFound("Task", task_id) from None

    project = ProjectRepository(session).get(task.project_id)
    if project is None:
        raise EntityNotFound("Project", task.project_id)

    # Eligibility, in the order an operator would want to be told why not. Each
    # of these is a reason the *world* is not ready, stated in the terms of the
    # lifecycle rule it comes from rather than as a generic refusal.
    if task.status not in RETRYABLE_TASK_STATES:
        retryable = ", ".join(sorted(state.value for state in RETRYABLE_TASK_STATES))
        raise EntityConflict(
            f"Task {task.external_task_id} is {task.status}; "
            f"only {retryable} may be retried by an operator"
        )
    if project.status not in RUNNABLE_PROJECT_STATES:
        raise EntityConflict(
            f"Project {project.name} is {project.status}; a project that is not "
            f"runnable has no run to authorize. Resume it first."
        )
    pause = PauseRequestRepository(session).in_force_for_task(project.id, task.id)
    if pause is not None:
        raise EntityConflict(
            f"Task {task.external_task_id} is under a pause in force"
            + (f" (reason: {pause.reason})" if pause.reason else "")
            + "; release the pause before authorizing a run"
        )
    unsatisfied = unsatisfied_dependencies(session, task)
    if unsatisfied:
        raise EntityConflict(
            f"Task {task.external_task_id} depends on {', '.join(unsatisfied)}, "
            "which are not complete in the integration baseline; a new run would "
            "start from a tree that is missing their work"
        )

    # The guarded move, and the two refusals that only the database can make
    # truthfully. Everything above was a read; this is the write, and its
    # predicate is re-evaluated against the committed row while it holds the
    # lock. A competing retry, a run created since the read above, or a workflow
    # that moved the task all land here and lose.
    retried = tasks.transition_guarded(
        task.id,
        expected_status=task.status,
        new_status=RETRY_AUTHORIZED_STATUS,
        require_no_in_flight_run=True,
    )
    if retried is None:
        raise _explain_refusal(session, tasks, runs, task)

    history = runs.list_for_task(task.id)
    RunEventRepository(session).append(
        RunEvent(
            # No run: this is a decision about the task, made before any run
            # exists. A null task_run_id is what the column is for (see
            # services.lessons._lesson_event); filing this against the abandoned
            # run would put a new decision on the record of an execution that
            # had nothing to do with it.
            task_run_id=None,
            project_id=task.project_id,
            task_id=task.id,
            event_type=RunEventType.TASK_RETRY_AUTHORIZED,
            payload={
                "external_task_id": task.external_task_id,
                "reason": reason,
                "requested_by": requested_by,
                "previous_status": task.status.value,
                "authorized_status": RETRY_AUTHORIZED_STATUS.value,
                # A fact, not a prediction: how many runs the task had when the
                # authorization was made, which is also the number the run that
                # follows it will take. Recorded so a later reader can tell a
                # new run from a reused one without counting rows themselves.
                "historical_runs": len(history),
            },
        )
    )

    logger.info(
        "task_retry_authorized",
        task_id=str(task.id),
        task=task.external_task_id,
        previous_status=task.status.value,
        authorized_status=RETRY_AUTHORIZED_STATUS.value,
        historical_runs=len(history),
        reason=reason,
        requested_by=requested_by,
    )
    return retried


def _explain_refusal(
    session: Session,
    tasks: TaskRepository,
    runs: TaskRunRepository,
    task: Task,
) -> EntityConflict:
    """Why the guarded move did not match, reported as the truth about it now.

    A read, not an enforcement: the refusal happened in the guarded ``UPDATE``
    above and this only says which of its two predicates failed. Both are read
    with statements rather than through the identity map, because the identity
    map is the stale copy the whole guard exists about, and a confident false
    explanation is worse than a generic one.
    """
    current = session.scalar(select(TaskRow.status).where(TaskRow.id == task.id))
    if current is not None and current != task.status:
        return EntityConflict(
            f"Task {task.external_task_id} is {TaskStatus(current)} now, not "
            f"{task.status}; the retry was not authorized because the task moved "
            "while the request was in flight"
        )
    in_flight = runs.in_flight_for_task(task.id)
    if in_flight is not None:
        return EntityConflict(
            f"Task {task.external_task_id} still has run {in_flight.run_number} "
            f"in flight ({in_flight.status}); a task with a live run has nothing "
            "to authorize"
        )
    return EntityConflict(
        f"Task {task.external_task_id} could not be retried; the task is "
        f"{TaskStatus(current) if current is not None else task.status} and no "
        "run of it is in flight"
    )


__all__ = [
    "RETRYABLE_TASK_STATES",
    "RETRY_AUTHORIZED_STATUS",
    "retry_failed_task",
]
