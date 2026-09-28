"""Task state machine (build.md section 26).

The orchestrator -- not a model -- owns task state. Every transition is
explicit here so that an illegal move raises rather than silently corrupting
run history.
"""

from __future__ import annotations

from .enums import TaskStatus
from .errors import InvalidStateTransition

#: Allowed transitions. A state absent from a value set is unreachable from
#: that key by design.
#:
#: ``PENDING`` and ``READY`` may reach ``FAILED`` (concern 64). Neither is
#: "in flight" -- no work has started on either -- so a state machine built only
#: from the workflow's own vocabulary had no way to record that a person's
#: decision stopped the work: the only moves out of PENDING and READY are the
#: ones that start or defer it. That left an abandoned run's task parked in
#: READY, which the scheduler then treats as unstarted work and picks up again,
#: so the run was stopped and the work was not. FAILED is the state that means
#: "a person has to look at this", and from FAILED only a person can move on.
#: Every state except COMPLETE can now reach it.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.PAUSED, TaskStatus.FAILED}
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.CODING,
            TaskStatus.BLOCKED,
            TaskStatus.HUMAN_REVIEW,
            TaskStatus.PAUSED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.PLANNING: frozenset(
        {TaskStatus.CODING, TaskStatus.FAILED, TaskStatus.HUMAN_REVIEW, TaskStatus.PAUSED}
    ),
    TaskStatus.CODING: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.HUMAN_REVIEW, TaskStatus.PAUSED}
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.REVIEW_PENDING,
            TaskStatus.CODING,  # deterministic failure routed back to the coder
            TaskStatus.FAILED,
            TaskStatus.HUMAN_REVIEW,
            TaskStatus.PAUSED,
        }
    ),
    TaskStatus.REVIEW_PENDING: frozenset(
        {TaskStatus.REVIEWING, TaskStatus.FAILED, TaskStatus.HUMAN_REVIEW, TaskStatus.PAUSED}
    ),
    TaskStatus.REVIEWING: frozenset(
        {
            TaskStatus.APPROVED,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.HUMAN_REVIEW,
            TaskStatus.FAILED,
            TaskStatus.PAUSED,
        }
    ),
    TaskStatus.CHANGES_REQUESTED: frozenset(
        {TaskStatus.CODING, TaskStatus.HUMAN_REVIEW, TaskStatus.FAILED, TaskStatus.PAUSED}
    ),
    TaskStatus.APPROVED: frozenset({TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.PAUSED}),
    TaskStatus.COMPLETE: frozenset(),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.FAILED, TaskStatus.PAUSED}),
    TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.HUMAN_REVIEW}),
    TaskStatus.HUMAN_REVIEW: frozenset(
        {TaskStatus.READY, TaskStatus.CODING, TaskStatus.COMPLETE, TaskStatus.FAILED}
    ),
    # Resume restores the task to a safe boundary, never mid-flight.
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.PENDING,
            TaskStatus.READY,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
        }
    ),
}

#: States from which no further automated work will be scheduled.
TERMINAL_STATES: frozenset[TaskStatus] = frozenset({TaskStatus.COMPLETE})

#: States that mean a run is currently in flight.
ACTIVE_STATES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
    }
)


def can_transition(current: TaskStatus, requested: TaskStatus) -> bool:
    return requested in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: TaskStatus, requested: TaskStatus) -> TaskStatus:
    """Return ``requested`` if the move is legal, else raise.

    Raises:
        InvalidStateTransition: if the transition is not permitted.
    """
    if not can_transition(current, requested):
        raise InvalidStateTransition(current, requested)
    return requested


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATES


def is_active(status: TaskStatus) -> bool:
    return status in ACTIVE_STATES
