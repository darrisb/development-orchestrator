"""Operator-initiated run abandonment (concern 64).

An authorized operator may abandon an in-flight durable run when it is known to
be invalid -- which is what happened to ``RUN-20260927-000020``, a run that
began under a stale supervisor image. The operation:

1. Transitions the run to RunStatus.ABANDONED (terminal).
2. Moves the owning task to FAILED, from which a person can explicitly request
   a retry. Every task state except COMPLETE can be failed, including one that
   never left READY: READY is what the scheduler selects from, so a task left
   there has its work started again on the next pass.
3. Records the operator's reason as an append-only event.
4. Leaves the run undiscoverable by recovery, so it is never resumed.
5. Preserves every artifact and every piece of forensic evidence.
6. Never creates or accepts a candidate commit, and never moves agent/integration.

Three properties are load-bearing, and none of them is enforced by reading a
status and hoping:

**The transition is a compare-and-swap.** ``TaskRunRepository.abandon`` only
writes ABANDONED if the run is still PENDING or RUNNING, and
``TaskRunRepository.finish`` refuses to write anything at all to a run that is
already ABANDONED. The two predicates are mirror images, so the operator and
the workflow can race and exactly one of them wins, decided by commit order.
The first cut of this module called ``finish`` unconditionally, which meant a
late ``SUCCEEDED`` from an in-flight workflow silently overwrote the operator.

**A refused terminal write is not the whole job.** A run in flight is a
*workflow* in flight, and the workflow holds a transaction open across a model
call -- seconds to minutes during which it reads nothing and writes nothing to
the run row. Its own fence is a status read in an earlier node; between that
read and its next write the operator's transaction commits. So the refusal that
matters is not only on ``finish``: the loop must also be stopped at every turn
boundary, which is what ``TaskRunRepository.require_in_flight`` does --
``SELECT ... FOR UPDATE`` with a predicate on the in-flight statuses, evaluated
by the database while it holds the row lock. It is a barrier, not a read, and
it is what makes the operator's decision effective against a workflow that is
already mid-turn.

**The run and its task move together or not at all.** Both writes are in the
caller's transaction, and nothing here catches an exception from the task
transition, so a failure propagates and the caller's rollback undoes the run
with it. The first catch did the opposite: it logged a warning and returned
success, so the operator was told a run had been abandoned while its task sat
in CODING, waiting for a workflow that was no longer going to run.

The operation is idempotent: abandoning an already-ABANDONED run returns the
existing state without writing a second event.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import ABANDONABLE_RUN_STATUSES, RunEventType, RunStatus, TaskStatus
from ..domain.models import RunEvent, TaskRun
from ..domain.state_machine import assert_transition
from ..repositories import RunEventRepository, TaskRepository, TaskRunRepository
from .errors import EntityConflict, EntityNotFound

logger = get_logger(__name__)

#: Task states this operation will *not* move to FAILED.
#:
#: A COMPLETE task's work is already delivered and in the integration baseline,
#: and there is no move out of COMPLETE by design -- so a run whose task reached
#: COMPLETE is left alone, and the run is abandoned on its own.
#:
#: Everything else is moved, and that includes PENDING and READY. The first cut
#: of this module moved only the states the state machine called "in flight",
#: reasoning that a task which had not started had nothing to stop. Two things
#: are wrong with that. A run in PENDING or RUNNING *is* work in progress from
#: the scheduler's point of view, and leaving the task in READY is the worst
#: possible answer: READY is precisely the state the scheduler selects from, so
#: the operator stops the run and the same work starts again on the next pass.
#: And the state machine could not express the alternative -- before concern
#: 64 there was no path from PENDING or READY to FAILED, which is why the run
#: needed its own idea of what to leave behind.
UNFAILABLE_TASK_STATES: frozenset[TaskStatus] = frozenset({TaskStatus.COMPLETE})

#: The status written to the run, and the reason recorded alongside it. The
#: reason column is a machine-readable code, not prose: the operator's prose
#: lives in the event payload, where it is preserved verbatim.
ABANDON_FAILURE_REASON = "OPERATOR_ABANDONED"


def abandon_run(
    session: Session,
    run_id: UUID,
    *,
    reason: str,
    requested_by: str | None = None,
) -> TaskRun:
    """Abandon an in-flight durable run.

    Args:
        session: The caller's transaction. The run and its task are written in
            this one transaction, so the caller's commit or rollback decides
            whether the abandonment happened at all.
        run_id: The run to abandon.
        reason: Required operator explanation. Recorded in the event payload.
        requested_by: Optional operator identifier, recorded in the payload.

    Returns:
        The abandoned run. For an already-abandoned run, the existing run,
        unchanged and with no second event.

    Raises:
        ValueError: ``reason`` is empty or whitespace.
        EntityNotFound: No such run, or the run has no task.
        EntityConflict: The run is not in an abandonable state.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required")

    runs = TaskRunRepository(session)
    run = runs.get(run_id)
    if run is None:
        raise EntityNotFound("Run", run_id)

    # Idempotency, handled before eligibility is considered. "This run is
    # already abandoned" is a different question from "may this run be
    # abandoned", and is_run_abandonable deliberately answers only the second
    # one -- which is why ABANDONED is not in ABANDONABLE_RUN_STATUSES.
    if run.status is RunStatus.ABANDONED:
        logger.info(
            "run_already_abandoned",
            run_id=str(run.id),
            reason=reason,
        )
        return run

    if not is_run_abandonable(run):
        allowed = ", ".join(sorted(s.value for s in ABANDONABLE_RUN_STATUSES))
        raise EntityConflict(
            f"Run {run.id} is {run.status}; only {allowed} may be abandoned"
        )

    tasks = TaskRepository(session)
    task = tasks.get(run.task_id)
    if task is None:
        raise EntityNotFound("Task", run.task_id)

    previous_status = run.status
    moves_task = task.status not in UNFAILABLE_TASK_STATES

    # Decide the task transition *before* writing the run, and let an illegal
    # one raise. Asserting first means the failure happens while nothing has
    # been written, rather than half way through; the same transaction then
    # guarantees that a task transition which fails for any other reason still
    # takes the run with it. Neither case is recoverable into a success.
    if moves_task:
        assert_transition(task.status, TaskStatus.FAILED)

    # The compare-and-swap. Losing it means another transaction terminalized
    # the run between the read above and this write, and the run's actual state
    # is the only authority on what to say about it.
    abandoned = runs.abandon(run.id, failure_reason=ABANDON_FAILURE_REASON)
    if abandoned is None:
        current = runs.get(run.id)
        if current is None:
            raise EntityNotFound("Run", run_id)
        if current.status is RunStatus.ABANDONED:
            # The operator's own request, committed concurrently by a second
            # request. Same outcome as the idempotent branch above, and it is
            # already committed, so returning it is correct.
            logger.info("run_already_abandoned", run_id=str(run.id), reason=reason)
            return current
        raise EntityConflict(
            f"Run {run.id} is {current.status}; only "
            f"{', '.join(sorted(s.value for s in ABANDONABLE_RUN_STATUSES))} "
            f"may be abandoned"
        )
    run = abandoned

    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=task.project_id,
            task_id=run.task_id,
            event_type=RunEventType.RUN_ABANDONED,
            attempt=run.attempt_number,
            payload={
                "reason": reason,
                "requested_by": requested_by,
                "previous_status": previous_status.value,
            },
        )
    )

    if moves_task:
        # No handler. This shares the caller's transaction with the run write
        # above, so raising here rolls the run back: an operator is never told
        # a run was abandoned while its task is still in CODING.
        tasks.transition(task.id, TaskStatus.FAILED)

    logger.info(
        "run_abandoned",
        run_id=str(run.id),
        task_id=str(run.task_id),
        task_transitioned=moves_task,
        reason=reason,
        requested_by=requested_by,
    )

    return run


def is_run_abandonable(run: TaskRun) -> bool:
    """Whether this run is an *active* run an operator could abandon.

    Truthful about its own question: PENDING and RUNNING are in flight and can
    be stopped; ABANDONED cannot, because there is nothing left in flight to
    stop. Idempotency for an already-abandoned run is a separate concern that
    :func:`abandon_run` handles directly, and the first cut of this module
    conflated the two by putting ABANDONED in this set.
    """
    return run.status in ABANDONABLE_RUN_STATUSES


__all__ = [
    "abandon_run",
    "is_run_abandonable",
    "ABANDON_FAILURE_REASON",
    "ABANDONABLE_RUN_STATUSES",
    "UNFAILABLE_TASK_STATES",
]
