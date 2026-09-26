"""Workflow policy (build.md sections 27, 28 and 24, phase K).

The graph in ``apps.orchestrator.workflow`` decides *when* things happen. What
each decision means is here, as plain functions over plain values, for the
reason the rest of the domain exists: a rule that lives inside a graph node is
a rule that can only be tested by running a graph.

Three things live here.

* **Where a run is** (``WorkflowPhase``) and **how it ended**
  (``WorkflowOutcome``). Both are written into the graph's state and survive a
  checkpoint, so they are named for what a person reading a stopped run would
  want to know, not for the function that was executing.
* **The run deadline** (section 23's ``max_runtime_minutes``, concern 35).
  A bound nothing enforced until the workflow owned it.
* **What a human's answer does** (``effect_of``, concern 32). An escalation's
  options are prose and its intents are an enumeration; this is the table that
  turns one of those intents into the moves the orchestrator may make.

Pure: no I/O, no database, no LangGraph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .enums import TaskStatus
from .escalation import EscalationIntent
from .models import TaskLimits


class WorkflowPhase(StrEnum):
    """Where a run has got to, in the words section 27 uses.

    Coarser than section 27's node list, and deliberately so: the steps from
    ``build_context`` to ``route_review`` are one phase here because they are
    one bounded retry loop in ``agents.fix_loop``, and splitting them across
    graph nodes would mean the graph, rather than the loop, counting the
    attempts. See ``workflow.graph`` for the mapping.
    """

    LOADING = "LOADING"
    PREPARING_WORKSPACE = "PREPARING_WORKSPACE"
    EXECUTING = "EXECUTING"
    COMMITTING = "COMMITTING"
    PUSHING = "PUSHING"
    COMPLETING = "COMPLETING"
    ESCALATING = "ESCALATING"
    RELEASING = "RELEASING"
    DONE = "DONE"


class WorkflowOutcome(StrEnum):
    """How a workflow invocation ended.

    ``PAUSED`` is not a failure and ``NOT_STARTED`` is not an error: an
    operator who pauses a project and asks the orchestrator to run the next
    task should get "nothing to do", not an exception.
    """

    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    NOT_STARTED = "NOT_STARTED"

    @property
    def is_terminal(self) -> bool:
        """Whether the run is over. A paused run is not; it is waiting."""
        return self in {
            WorkflowOutcome.COMPLETED,
            WorkflowOutcome.ESCALATED,
            WorkflowOutcome.FAILED,
        }


# ------------------------------------------------------------------ deadlines


def run_deadline(started_at: datetime, limits: TaskLimits) -> datetime:
    """When the run must stop starting new work (section 23, concern 35).

    Measured from the run's own ``started_at`` rather than from the moment the
    workflow was invoked: a run that is resumed after a pause has already spent
    the time it spent, and restarting the clock would let a task with a
    30-minute budget occupy a worker all day in 30-minute instalments.
    """
    reference = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    return reference + timedelta(minutes=max(1, limits.max_runtime_minutes))


def deadline_exceeded(deadline: datetime, *, now: datetime | None = None) -> bool:
    """Whether the budget is spent.

    Checked before a turn and never during one: a command already running is
    left to finish. Killing a worker mid-write is how a worktree ends up in a
    state neither the diff nor the next attempt can explain, and the whole
    point of the ceiling is to stop *starting* work that cannot finish.
    """
    moment = now or datetime.now(UTC)
    reference = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return reference >= deadline


# ------------------------------------------------- what a human's answer does


@dataclass(frozen=True, slots=True)
class ResolutionEffect:
    """The moves an answered escalation authorises (section 24, concern 32).

    Every field is something the orchestrator can do without interpreting
    prose. A field this leaves false is a thing a person's answer did not ask
    for, and the workflow does it anyway at its peril.
    """

    intent: EscalationIntent
    #: Where the task ends up. Reached from ``HUMAN_REVIEW``, which is where
    #: an escalated task sits.
    task_status: TaskStatus
    #: Commit the run's worktree and record the candidate SHA. Only for an
    #: answer that accepted the candidate.
    commit_candidate: bool = False
    #: Remove the run's worktree. False only where the tree is still the only
    #: copy of something -- which, after an answer, it never is.
    release_worktree: bool = True
    #: Carry the answer text into the next run as the coder's feedback.
    feedback_to_coder: bool = False
    #: Whether the task will be picked up again by the scheduler.
    reopens_task: bool = False

    @property
    def completes_task(self) -> bool:
        return self.task_status is TaskStatus.COMPLETE


_EFFECTS: dict[EscalationIntent, ResolutionEffect] = {
    # The person read the candidate and took it. This is the only intent that
    # commits, and it is why an escalated worktree is preserved rather than
    # rolled back: the tree is the candidate.
    EscalationIntent.ACCEPT_CANDIDATE: ResolutionEffect(
        intent=EscalationIntent.ACCEPT_CANDIDATE,
        task_status=TaskStatus.COMPLETE,
        commit_candidate=True,
    ),
    # The answer is a correction, so the task goes back into the queue and the
    # words travel with it. A new run rather than a resumed one: the escalated
    # run is finished and section 25 marked it failed.
    EscalationIntent.REQUEST_CHANGES: ResolutionEffect(
        intent=EscalationIntent.REQUEST_CHANGES,
        task_status=TaskStatus.READY,
        feedback_to_coder=True,
        reopens_task=True,
    ),
    EscalationIntent.RETRY_TASK: ResolutionEffect(
        intent=EscalationIntent.RETRY_TASK,
        task_status=TaskStatus.READY,
        reopens_task=True,
    ),
    # The work exists, but not as anything this run produced, so there is
    # nothing to commit and the worktree is only taking up disk.
    EscalationIntent.COMPLETED_BY_HAND: ResolutionEffect(
        intent=EscalationIntent.COMPLETED_BY_HAND,
        task_status=TaskStatus.COMPLETE,
    ),
    EscalationIntent.ABANDON_TASK: ResolutionEffect(
        intent=EscalationIntent.ABANDON_TASK,
        task_status=TaskStatus.FAILED,
    ),
}


def effect_of(intent: EscalationIntent) -> ResolutionEffect:
    """What answering with ``intent`` authorises.

    Raises:
        KeyError: an intent with no effect defined. Deliberately unhandled:
            a new member of the enumeration must be given a meaning here
            rather than falling through to a default that guesses.
    """
    return _EFFECTS[intent]


__all__ = [
    "ResolutionEffect",
    "WorkflowOutcome",
    "WorkflowPhase",
    "deadline_exceeded",
    "effect_of",
    "run_deadline",
]
