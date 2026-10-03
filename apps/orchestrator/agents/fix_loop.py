"""The Fix Loop (build.md sections 23, 24 and 25, phase J).

Phases G, H and I each built one step and each stopped on purpose: the coding
agent leaves a candidate, the verification pipeline leaves a report, the review
agent leaves a routing and a correction text. None of them decides what happens
next, and until this module existed nothing did -- a failing candidate sat in
``VERIFYING`` and a ``CHANGES_REQUESTED`` review was a string in an outcome that
never reached a model (concerns 9, 23 and 26).

This is that decision, and section 23 is the whole of it:

```text
store review and issues -> correction prompt of actionable blocking issues
-> coder modifies candidate worktree -> rerun deterministic verification
-> regenerate diff -> rerun review -> increment review cycle
```

with the sentence that governs the shape of the code: **never loop
indefinitely.**

Four properties are worth knowing before reading it.

* **One turn of the loop spends exactly one coding attempt.** So the loop is
  bounded by ``max_attempts`` structurally, by a ``for`` over the attempts the
  task is allowed, not by a ceiling check that could be got wrong. The review
  cycle ceiling is enforced where the cycle is counted -- inside
  ``route_review`` -- for the same reason.
* **Feedback is never invented here.** A deterministic failure travels as
  ``VerificationReport.feedback`` (the real command and its real output) and a
  review as ``ReviewRouting.feedback`` (the actionable blocking issues). This
  module chooses which of them the next attempt is given; it does not write
  either, because a correction prompt assembled by the thing counting the
  retries would be describing its own summary of the evidence rather than the
  evidence.
* **What ends a run comes from ``domain.failure_policy``**, one action per
  failure class, so no failure reaches a generic handler. ``SEND_TO_CODER``
  turns the loop, ``ROLLBACK`` resets the worktree and ends it, ``ESCALATE``
  writes for a person. Nothing is decided by reading a reason's name here.
* **An issue is closed by a reviewer, not by an attempt.** After each cycle
  the findings a re-review did *not* raise again are marked resolved
  (concern 27). The coder's claim to have fixed something is exactly the kind
  of claim this system does not believe.

What it does not do: commit, tag, push or complete. An approval is where this
module stops, because merging is section 10's business and the workflow's
decision (phase K). It also does not retry infrastructure -- an unreachable
model, a dead worker and a rejected command all propagate, because their policy
is retry and the workflow owns that ceiling too.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import (
    EscalationStatus,
    FailureAction,
    FailureReason,
    ReviewDecision,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from ..domain.escalation import render_run_escalation, run_escalation_options
from ..domain.failure_policy import action_for
from ..domain.limits import can_retry_coding
from ..domain.models import (
    HumanEscalation,
    Project,
    Review,
    ReviewIssue,
    RunEvent,
    Task,
    TaskRun,
)
from ..domain.review import (
    HumanApprovalPolicy,
    ReviewRouting,
    issue_fingerprint,
    render_review_feedback,
    reraised_unresolved_blocking_issues,
    unreraised_issues,
)
from ..domain.state_machine import can_transition
from ..domain.verification import VerificationReport
from ..domain.workflow import deadline_exceeded, run_deadline
from ..providers import ModelProvider, ModelProviderError
from ..providers.review import ReviewProvider
from ..repositories import (
    EscalationRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from ..services import artifact_store
from ..services.runtime import RuntimeBudget, configured_runtime_ms
from ..services.verification import verify_candidate
from ..services.workspace import TaskWorkspace, load_run_context, rollback_workspace
from .coding_agent import CodingAttempt, run_coding_attempt
from .loop_recovery import (
    Fingerprint,
    RecoveredLoopState,
    recover_loop_state,
    review_result_for,
)
from .review_agent import ESCALATION_ARTIFACT, ReviewOutcome, run_review

logger = get_logger(__name__)

#: The loop's own record: every turn, what ended it, and what it sent back.
#: Written at the run's root rather than under an attempt prefix, because it is
#: the one artifact that is about the run as a whole (section 9).
FIX_LOOP_ARTIFACT = "fix-loop.json"


class LoopOutcome(StrEnum):
    """How a run left the loop.

    ``FAILED`` and ``ESCALATED`` are both unsuccessful and are deliberately
    not one value: a rolled-back candidate needs nobody, and an escalation is
    a person's queue. Telling them apart is what lets an operator's dashboard
    show work that is waiting rather than work that is over.
    """

    APPROVED = "APPROVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class FixIteration:
    """One turn of the loop: an attempt, its verification and its review."""

    #: Which turn of this loop it was, counting from one. Not the same as
    #: ``attempt`` -- a run that continues earlier work starts at the attempt
    #: number it inherited.
    number: int
    #: The run's attempt number and the review cycle this turn belongs to. Both
    #: are recorded because the artifact directory is named from the pair, and
    #: a reader of the report needs to be able to find the files.
    attempt: int
    cycle: int
    coding: CodingAttempt
    verification: VerificationReport | None = None
    review: ReviewOutcome | None = None
    failure_reason: FailureReason | None = None
    action: FailureAction | None = None
    #: What the *next* turn was given, when there was one.
    feedback: str | None = None
    #: Earlier findings this turn's review did not raise again (concern 27).
    resolved_issues: tuple[UUID, ...] = ()

    @property
    def stage(self) -> str:
        """Where this turn stopped, in the words section 17 uses."""
        if self.review is not None:
            return "review"
        if self.verification is not None:
            return "verification"
        return "coding"

    @property
    def succeeded(self) -> bool:
        return self.review is not None and self.review.approved

    def summary(self) -> str:
        """One line for an escalation's attempt history (section 24)."""
        if self.review is not None:
            detail = self.review.routing.summary()
        elif self.verification is not None and not self.verification.passed:
            detail = self.verification.summary()
        elif self.coding.failure_reason is not None:
            detail = self.coding.feedback or str(self.coding.failure_reason)
        else:
            detail = "no verdict was reached"
        return f"attempt {self.attempt} ({self.stage}): {_one_line(detail)}"

    def describe(self) -> dict[str, object]:
        return {
            "iteration": self.number,
            "attempt": self.attempt,
            "cycle": self.cycle,
            "stage": self.stage,
            "failure_reason": self.failure_reason.value if self.failure_reason else None,
            "action": self.action.value if self.action else None,
            "coding": {
                "failure_reason": (
                    self.coding.failure_reason.value if self.coding.failure_reason else None
                ),
                "changed_paths": list(self.coding.changed_paths),
                "context_hash": self.coding.context_hash,
                "model_name": self.coding.model_name,
                "artifacts": dict(self.coding.artifacts),
            },
            "verification": (
                {
                    "passed": self.verification.passed,
                    "verified": self.verification.verified,
                    "failure_reason": (
                        self.verification.failure_reason.value
                        if self.verification.failure_reason
                        else None
                    ),
                    "summary": self.verification.summary(),
                }
                if self.verification
                else None
            ),
            "review": (
                {
                    "cycle": self.review.cycle,
                    "reviewer_decision": self.review.result.decision.value,
                    "routing": self.review.routing.describe(),
                    "artifacts": dict(self.review.artifacts),
                }
                if self.review
                else None
            ),
            "resolved_issues": [str(issue_id) for issue_id in self.resolved_issues],
            "sent_to_coder": self.feedback is not None,
        }


@dataclass(frozen=True, slots=True)
class FixLoopResult:
    """Everything the loop did, and where it left the task."""

    task_run_id: UUID
    external_task_id: str
    outcome: LoopOutcome
    iterations: tuple[FixIteration, ...]
    task_status: TaskStatus
    failure_reason: FailureReason | None = None
    escalation: HumanEscalation | None = None
    #: True when the worktree was reset to the run's starting commit.
    rolled_back: bool = False
    artifacts: Mapping[str, str] = field(default_factory=dict)
    #: Coding attempts begun, across every process this run has been in. Not
    #: ``len(iterations)``: after a resume that counts the turns this process
    #: made, and would report one attempt for a run that made three.
    attempts_used: int = 0
    #: Reviews that returned a verdict, across every process.
    cycles_used: int = 0
    #: The state this run was resumed from, when it was resumed.
    recovery: RecoveredLoopState | None = None
    configured_runtime_ms: int | None = None
    consumed_runtime_ms: int | None = None
    remaining_runtime_ms: int | None = None

    @property
    def approved(self) -> bool:
        return self.outcome is LoopOutcome.APPROVED

    @property
    def final(self) -> FixIteration | None:
        return self.iterations[-1] if self.iterations else None

    @property
    def review(self) -> ReviewOutcome | None:
        """The last review that happened, if any."""
        for iteration in reversed(self.iterations):
            if iteration.review is not None:
                return iteration.review
        return None

    def describe(self) -> dict[str, object]:
        return {
            "task_run_id": str(self.task_run_id),
            "external_task_id": self.external_task_id,
            "outcome": self.outcome.value,
            "task_status": self.task_status.value,
            "failure_reason": self.failure_reason.value if self.failure_reason else None,
            "attempts_used": self.attempts_used,
            "cycles_used": self.cycles_used,
            "runtime": {
                "configured_ms": self.configured_runtime_ms,
                "consumed_ms": self.consumed_runtime_ms,
                "remaining_ms": self.remaining_runtime_ms,
            },
            "rolled_back": self.rolled_back,
            "escalation_id": str(self.escalation.id) if self.escalation else None,
            "recovery": self.recovery.describe() if self.recovery else None,
            "iterations": [iteration.describe() for iteration in self.iterations],
        }


def durable_checkpoint(
    session: Session,
    run_id: UUID,
    commit: Callable[[], None],
    *,
    expected_generation: int | None = None,
) -> Callable[[], None]:
    """The turn-boundary checkpoint, guarded by the run row (concern 64).

    A turn is made durable at three points: immediately before an external
    model call, after that call is recorded, and after every turn.  The first
    boundary is Concern 66's transaction-lifetime invariant; the latter two
    keep the evidence durable.  Every durable boundary is also where an
    operator's abandonment has to be able to stop the workflow.

    So the barrier runs immediately before the commit, inside the same
    transaction: ``TaskRunRepository.require_in_flight`` locks the run row and
    evaluates the in-flight predicate under that lock. If an operator's
    transaction committed first, the commit never happens, the turn is rolled
    back, and the workflow stops with ``AbandonedRunError`` rather than making
    a run that a person stopped look like work that completed.

    Wrapping the caller's commit rather than adding a second callback keeps the
    ordering impossible to get wrong: there is no way to make a turn durable
    without passing the guard.

    **Concern 67 threads the executor's ownership token through the same
    guard.** ``expected_generation`` is the ``execution_generation`` this
    dispatch acquired, and the barrier now refuses a commit from an executor
    whose generation has been superseded by an operator recovery. It is checked
    here, at the commit, rather than only at the top of the loop, for exactly
    the reason concern 64 gave about abandonment: everything before the commit
    is a read, and a read is true until the moment it is not. An executor that
    was recovered out from under itself while waiting on a provider gets as far
    as this line and no further, so its work is discarded rather than layered
    on top of the executor that replaced it.

    ``None`` means "do not ask the ownership question", which is the
    pre-concern-67 behaviour and what a caller with no dispatch of its own
    passes.
    """

    def checkpoint() -> None:
        TaskRunRepository(session).require_in_flight(
            run_id, expected_generation=expected_generation
        )
        commit()

    return checkpoint


async def run_fix_loop(
    session: Session,
    workspace: TaskWorkspace,
    *,
    coder: ModelProvider,
    planner: ModelProvider | None = None,
    reviewer: ReviewProvider,
    settings: Settings | None = None,
    policy: HumanApprovalPolicy | None = None,
    secrets: Mapping[str, str] | None = None,
    max_attempts: int | None = None,
    initial_feedback: str | None = None,
    deadline: datetime | None = None,
    worker_deadline: datetime | None = None,
    runtime_budget: RuntimeBudget | None = None,
    checkpoint_turn: Callable[[], None] | None = None,
) -> FixLoopResult:
    """Code, verify and review the task in ``workspace`` until it settles.

    The first turn is an ordinary coding attempt, so this is the whole of a
    task's work and not only the corrections: a task that is right first time
    passes through one turn and comes back approved.

    Args:
        workspace: the run's isolated worktree. Every write lands here.
        coder: the model that writes the code. Selected by the caller, because
            which model serves a role is routing policy (section 31).
        planner: the model that writes initial plans. Defaults to the coder for
            installations without a separately configured planner.
        reviewer: the model that judges it.
        policy: the human-approval policy (section 37), passed to every review
            so that all cycles of one run are judged against the same gate.
        secrets: values the verification commands need, injected per command
            and redacted out of every log (section 36).
        max_attempts: lowers the task's own ``max_attempts`` for this run.
            It can only lower it: a caller must not be able to talk the loop
            into more attempts than the task's manifest allows.
        initial_feedback: what a human asked for when the run was opened. Only
            read when no reviewer has asked for anything: from the first
            ``CHANGES_REQUESTED`` on, the findings in ``reviews`` are the
            instruction, and they are the ones that are still on the record.
        checkpoint_turn: makes the turn so far durable. Called after every
            turn, and handed down to every model call as the checkpoint it
            makes, so a provider failure cannot roll away the record of the
            call that failed.

    Returns:
        The result. Approved means a reviewer accepted the candidate and the
        gates cleared -- not that anything was committed.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        ReviewerUnavailable: the reviewer could not produce a review.
        WorkerBackendUnavailable: the container runtime is not usable.
        CommandRejected: a configured verification command is not permitted.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    runs = TaskRunRepository(session)
    # Where this run actually is, read from what survived. On a first
    # invocation this is attempt 1, cycle 1 and no feedback, which is not a
    # special case: there is no record to read. On a resume it is the attempt
    # after the last one begun, the cycle the last one was for, and the
    # findings of the last reviewer who asked for anything -- so a correction
    # that was interrupted comes back as the same correction rather than as a
    # blank first try.
    recovered = recover_loop_state(
        session, run, settings=config, initial_feedback=initial_feedback
    )
    # Attempt numbers continue the run rather than restarting at one, and they
    # only ever move forward: a run that died mid-attempt resumes at the next
    # number, not at the one whose directory it would otherwise overwrite.
    first = max(1, recovered.next_attempt)
    ceiling = min(task.limits.max_attempts, max_attempts or task.limits.max_attempts)

    iterations: list[FixIteration] = []
    fingerprints: list[Fingerprint] = list(recovered.fingerprints)
    feedback = recovered.feedback
    # What a reader of the outcome is told. Counted from the durable records
    # and incremented per turn, rather than measured from ``iterations``,
    # which is empty after a resume and would report one attempt for a run
    # that made three.
    attempts_used = recovered.attempts_started
    # Reviews that returned a verdict. A cycle is charged when the reviewer
    # answers, not when the loop starts one, so a run that died waiting on a
    # reviewer is resumed inside the same cycle instead of being charged for
    # a review that never happened.
    cycles_used = recovered.reviews_completed
    invocation_started_at = datetime.now(UTC)
    runtime_deadline = deadline or run_deadline(
        invocation_started_at,
        task.limits,
        consumed_runtime_ms=run.active_runtime_ms,
    )
    invocation_worker_deadline = worker_deadline or (
        invocation_started_at + timedelta(seconds=config.worker_timeout_seconds)
    )
    effective_deadline = min(runtime_deadline, invocation_worker_deadline)

    def settle(**kwargs: object) -> FixLoopResult:
        """Settle the loop with the durable counts, not this process's view."""
        return _settle(
            session,
            workspace,
            run,
            task,
            project,
            iterations,
            outcome=kwargs["outcome"],
            config=config,
            reason=kwargs.get("reason"),
            escalation=kwargs.get("escalation"),
            rollback=bool(kwargs.get("rollback")),
            attempts_used=attempts_used,
            cycles_used=cycles_used,
            recovered=recovered,
            runtime_budget=runtime_budget,
        )

    recovered_dispute = _recovered_repeated_blocking_dispute(session, run, task)
    if recovered_dispute is not None:
        logger.warning(
            "fix_loop_recovered_repeated_blocking_dispute",
            run_id=str(run.id),
            task=task.external_task_id,
            cycle=recovered_dispute.review.cycle,
            repeated=len(recovered_dispute.repeated),
        )
        iterations.append(
            FixIteration(
                number=0,
                attempt=run.attempt_number,
                cycle=recovered_dispute.review.cycle,
                coding=CodingAttempt(
                    task_run_id=run.id,
                    external_task_id=task.external_task_id,
                    attempt=run.attempt_number,
                    context_hash=run.context_hash or "",
                    provider_id="recovered",
                    model_name="recovered",
                ),
                review=recovered_dispute.outcome,
                resolved_issues=recovered_dispute.resolved,
                failure_reason=FailureReason.HUMAN_DECISION_REQUIRED,
                action=FailureAction.ESCALATE,
            )
        )
        return settle(
            outcome=LoopOutcome.ESCALATED,
            reason=FailureReason.HUMAN_DECISION_REQUIRED,
        )

    if first > ceiling:
        # A resumed run whose attempts are already spent must not re-make the
        # turn it was interrupted in: the interruption is not a free retry. The
        # budget is the task's, and it is spent.
        logger.warning(
            "fix_loop_attempts_already_spent",
            run_id=str(run.id),
            task=task.external_task_id,
            attempts_started=attempts_used,
            ceiling=ceiling,
            interrupted_attempt=recovered.interrupted_attempt,
        )

    for number in range(first, ceiling + 1):
        if deadline_exceeded(effective_deadline):
            runtime_exhausted = effective_deadline == runtime_deadline
            return settle(
                outcome=LoopOutcome.ESCALATED,
                reason=(
                    FailureReason.RUNTIME_EXHAUSTED
                    if runtime_exhausted
                    else FailureReason.WORKER_FAILURE
                ),
            )
        # The attempt number is advanced before the attempt, not after it:
        # everything the attempt writes is filed under it, so a run that dies
        # mid-attempt still has its artifacts under the attempt that was being
        # made rather than the one before. Written whenever the row disagrees,
        # not only for a second turn: after a resume the row is behind the loop
        # and skipping the update would file this turn under the attempt before
        # the one that was interrupted.
        if number != run.attempt_number:
            run = runs.update_fields(run.id, attempt_number=number)
            _emit(
                session, run, task, project, RunEventType.FIX_STARTED,
                {
                    "attempt": number,
                    "of": ceiling,
                    "cycle": recovered.cycle,
                    "after": iterations[-1].stage if iterations else "recovered state",
                    "failure_reason": (
                        iterations[-1].failure_reason.value
                        if iterations and iterations[-1].failure_reason
                        else None
                    ),
                    "recovered": recovered.recovered,
                    "interrupted_attempt": recovered.interrupted_attempt,
                },
            )
        # A review cycle is spent when a reviewer answers, and that is counted
        # on the run, so the cycle for this turn is one past the reviews that
        # are actually on the record. Re-read every turn: the answer to the
        # previous turn was written by the reviewer, and it is what makes the
        # next turn a different cycle. ``recovered.reviews_completed`` is the
        # floor for the same reason -- if a verdict was committed but the count
        # was not, the count is wrong and the rows are not.
        cycle = max(run.review_cycle, recovered.reviews_completed) + 1

        iteration = await _turn(
            session,
            workspace,
            run,
            task,
            project,
            number=len(iterations) + 1,
            cycle=cycle,
            coder=coder,
            planner=planner,
            reviewer=reviewer,
            feedback=feedback,
            config=config,
            policy=policy,
            secrets=secrets,
            checkpoint_call=checkpoint_turn,
        )
        iterations.append(iteration)
        # The attempt was made, and charged, whether or not it succeeded: it
        # cost a provider call and left a directory. Charged here rather than
        # derived from the loop's own length so that a rollback upstream cannot
        # make the run look like it had not tried.
        attempts_used = max(attempts_used, number)
        if iteration.review is not None:
            # Same rule as the attempt: a review that answered is spent, and a
            # resumed run's answer belongs to the same count.
            cycles_used = max(cycles_used, cycle)
            fingerprints.append(
                frozenset(
                    issue_fingerprint(issue)
                    for issue in iteration.review.result.blocking_issues
                )
            )
        # Both rows are re-read, because the turn advanced them: the agents move
        # the task and count the review cycle against the database, not against
        # the copies held here. A stale status would make the terminal
        # transition below look illegal and be skipped, which is how a finished
        # run ends up parked in ``VERIFYING`` with nobody coming for it.
        run = runs.get(run.id) or run
        task = TaskRepository(session).get(task.id) or task

        # Leave a root-level account of an in-flight run before making the
        # turn durable. A crash after this boundary can therefore be inspected
        # without reconstructing state from every attempt artifact.
        _record_progress(session, run, task, config=config)

        # One coding/verification/review turn is the recovery unit. Keeping it
        # durable here means a restart loses at most the graph cursor, never
        # the evidence and counters from every earlier turn (concern 36).
        if checkpoint_turn is not None:
            checkpoint_turn()

        if iteration.action is not FailureAction.ESCALATE and _reviews_are_stagnant(
            [*fingerprints],
            limit=config.fix_loop_stagnant_review_limit,
        ):
            logger.warning(
                "fix_loop_stagnant_reviews",
                run_id=str(run.id),
                task=task.external_task_id,
                consecutive_reviews=config.fix_loop_stagnant_review_limit,
            )
            return settle(
                outcome=LoopOutcome.ESCALATED,
                reason=FailureReason.RETRY_EXHAUSTED,
            )

        if iteration.succeeded:
            return settle(outcome=LoopOutcome.APPROVED)
        if iteration.action is FailureAction.ESCALATE:
            return settle(
                outcome=LoopOutcome.ESCALATED,
                reason=iteration.failure_reason,
                # A review that escalated has already written the escalation a
                # person will read, and it can say more than this module can.
                escalation=iteration.review.escalation if iteration.review else None,
            )
        if iteration.action is FailureAction.ROLLBACK:
            return settle(
                outcome=LoopOutcome.FAILED,
                reason=iteration.failure_reason,
                rollback=True,
            )
        if iteration.action is FailureAction.RETRY:
            continue
        if iteration.action is FailureAction.SEND_TO_CODER and iteration.feedback:
            feedback = _correction_feedback(
                iterations,
                iteration.feedback,
                history_limit=config.fix_loop_feedback_history_limit,
            )
            continue

        # Everything else. ``RETRY`` and ``PAUSE`` belong to failures the agents
        # raise rather than return, so they should not arrive here at all, and
        # ``SEND_TO_CODER`` with nothing to send would mean sending an attempt
        # back with no evidence -- it would repeat the one that just failed.
        # None of the three is the loop's decision to take, so a person takes
        # it, and the reason on the record is the one that was really seen.
        logger.warning(
            "fix_loop_unroutable_outcome",
            run_id=str(run.id),
            task=task.external_task_id,
            attempt=number,
            turn=len(iterations),
            failure_reason=(
                iteration.failure_reason.value if iteration.failure_reason else None
            ),
            action=iteration.action.value if iteration.action else None,
            has_feedback=iteration.feedback is not None,
        )
        return settle(
            outcome=LoopOutcome.ESCALATED,
            reason=FailureReason.HUMAN_DECISION_REQUIRED,
        )

    # The attempts the task allows are spent and no reviewer has accepted the
    # candidate. Section 23: after the limit, create a human escalation.
    return settle(
        outcome=LoopOutcome.ESCALATED,
        reason=FailureReason.RETRY_EXHAUSTED,
    )


# ----------------------------------------------------------------------- turn


async def _turn(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    task: Task,
    project: Project,
    *,
    number: int,
    cycle: int,
    coder: ModelProvider,
    planner: ModelProvider | None,
    reviewer: ReviewProvider,
    feedback: str | None,
    config: Settings,
    policy: HumanApprovalPolicy | None,
    secrets: Mapping[str, str] | None,
    checkpoint_call: Callable[[], None] | None = None,
) -> FixIteration:
    """Code, verify, review. Stops at the first of the three that has a verdict.

    The order is section 17's and the early exits are what make it honest: a
    candidate whose edits were refused has nothing to verify, and one that does
    not compile has nothing a reviewer can say anything useful about.

    ``checkpoint_call`` is handed to each model call so that a call's record is
    committed the moment it is made. Without it a provider failure takes the
    record of the call with it, and the resumed run cannot tell an attempt that
    was made from one that never was.
    """
    try:
        attempt = await run_coding_attempt(
            session,
            workspace,
            provider=coder,
            planner_provider=planner,
            settings=config,
            review_feedback=feedback,
            # A correction attempt does not re-plan: see ``run_coding_attempt``.
            plan_required=False if number > 1 else None,
            review_cycle=cycle,
            checkpoint_call=checkpoint_call,
        )
    except ModelProviderError as error:
        attempt = _provider_failed_attempt(
            session,
            run,
            task,
            project,
            provider=coder,
            error=error,
        )
    if attempt.failure_reason is not None:
        return _stop(
            FixIteration(number=number, attempt=run.attempt_number, cycle=cycle,
                         coding=attempt),
            reason=attempt.failure_reason,
            feedback=attempt.feedback,
            limits_exhausted=not can_retry_coding(task.limits, run.attempt_number),
        )

    report = verify_candidate(session, workspace, settings=config, secrets=secrets)
    if not report.passed:
        reason = report.failure_reason or FailureReason.TEST_FAILED
        return _stop(
            FixIteration(number=number, attempt=run.attempt_number, cycle=cycle,
                         coding=attempt, verification=report),
            reason=reason,
            feedback=report.feedback,
            limits_exhausted=not can_retry_coding(task.limits, run.attempt_number),
        )

    review = await run_review(
        session,
        workspace,
        provider=reviewer,
        verification=report,
        completion_report=attempt.report,
        # Not ``attempt.diff``: a build can write into the worktree, so the diff
        # the reviewer is shown is re-captured after the commands have run.
        settings=config,
        policy=policy,
        checkpoint_call=checkpoint_call,
    )
    earlier_issues = _open_earlier_review_issues(session, run, review.cycle)
    resolved = _close_unreraised_issues(
        session, run, task, project, review, earlier=earlier_issues
    )
    repeated = reraised_unresolved_blocking_issues(
        earlier_issues, review.result.issues
    )
    if review.routing.needs_fix and repeated:
        review = _escalate_repeated_blocking_dispute(review, repeated)
    iteration = FixIteration(
        number=number, attempt=run.attempt_number, cycle=review.cycle,
        coding=attempt, verification=report, review=review, resolved_issues=resolved,
    )
    if review.approved:
        return iteration
    return _stop(
        iteration,
        reason=review.routing.failure_reason or FailureReason.REVIEW_CHANGES_REQUESTED,
        feedback=review.feedback,
        # A review that requests changes on its last permitted cycle has
        # already been routed to a human by ``route_review``; the attempt
        # ceiling is the one this module still has to apply.
        limits_exhausted=not can_retry_coding(task.limits, run.attempt_number),
    )


def _stop(
    iteration: FixIteration,
    *,
    reason: FailureReason,
    feedback: str | None,
    limits_exhausted: bool,
) -> FixIteration:
    """Attach the failure policy's action to a turn that produced one.

    ``SEND_TO_CODER`` with no attempts left is not a retry that happens to
    fail: it is ``RETRY_EXHAUSTED``, which is a different failure with a
    different policy, and recording it as the original reason would make an
    exhausted task look like one that was still being worked on.

    Feedback is kept on an exhausted turn even though no attempt will read it.
    It is the last thing the coder would have been told, and an escalation is
    easier to act on for having it.
    """
    action = action_for(reason)
    if action is not FailureAction.SEND_TO_CODER:
        if action is FailureAction.RETRY and limits_exhausted:
            return replace(
                iteration,
                failure_reason=FailureReason.RETRY_EXHAUSTED,
                action=FailureAction.ESCALATE,
                feedback=feedback,
            )
        return replace(iteration, failure_reason=reason, action=action, feedback=None)
    if limits_exhausted:
        return replace(
            iteration,
            failure_reason=FailureReason.RETRY_EXHAUSTED,
            action=FailureAction.ESCALATE,
            feedback=feedback,
        )
    return replace(iteration, failure_reason=reason, action=action, feedback=feedback)


def _provider_failed_attempt(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    *,
    provider: ModelProvider,
    error: ModelProviderError,
) -> CodingAttempt:
    """Represent a recorded provider failure as this turn's failed attempt.

    ``run_coding_attempt`` has already recorded and checkpointed the failed
    model call before raising. This adapter deliberately does not write another
    model-call or advance any counters; it only gives the fix loop the same
    shape it already knows how to route.
    """
    stored_run = TaskRunRepository(session).get(run.id) or run
    feedback = str(error)
    _emit(
        session,
        stored_run,
        task,
        project,
        RunEventType.CODING_COMPLETED,
        {"failure_reason": error.reason.value, "feedback": feedback},
    )
    logger.warning(
        "coding_attempt_provider_failed",
        run_id=str(run.id),
        task=task.external_task_id,
        attempt=stored_run.attempt_number,
        failure_reason=error.reason.value,
        error=str(error),
    )
    return CodingAttempt(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        attempt=stored_run.attempt_number,
        context_hash=stored_run.context_hash or "",
        provider_id=provider.config.provider_id,
        model_name=provider.config.model_name,
        failure_reason=error.reason,
        feedback=feedback,
    )


def _reviews_are_stagnant(
    fingerprints: Sequence[Fingerprint], *, limit: int
) -> bool:
    """Whether the last reviews repeated the same blocking findings.

    Takes the per-cycle finding sets rather than the turns, because a resumed
    run has no turns for the cycles it recovered: comparing only this process's
    reviews would let a run whose last two reviews asked for the same thing
    cycle past the limit, one fresh review at a time.

    Empty sets never count: repeated approvals are handled by the ordinary
    success route, and repeated human-only decisions already have their own
    policy. Fingerprints deliberately use the same identity as issue closing.
    """
    if limit <= 0 or len(fingerprints) < limit:
        return False
    recent = fingerprints[-limit:]
    return bool(recent[0]) and all(current == recent[0] for current in recent[1:])


def _correction_feedback(
    iterations: Sequence[FixIteration],
    current: str,
    *,
    history_limit: int,
) -> str:
    """Newest evidence plus bounded earlier guidance that must not regress."""
    if history_limit <= 0:
        return current
    earlier: list[str] = []
    for item in reversed(iterations[:-1]):
        text = item.feedback
        if text and text != current and text not in earlier:
            earlier.append(text)
        if len(earlier) >= history_limit:
            break
    if not earlier:
        return current
    return (
        current
        + "\n\nEarlier correction guidance — do not regress these fixes:\n\n"
        + "\n\n---\n\n".join(reversed(earlier))
    )


# ------------------------------------------------------------ issue bookkeeping


def _open_earlier_review_issues(
    session: Session, run: TaskRun, cycle: int
) -> tuple[ReviewIssue, ...]:
    if cycle <= 1:
        return ()
    return tuple(
        issue
        for stored in ReviewRepository(session).list_for_run(run.id)
        if stored.cycle < cycle
        for issue in stored.issues
        if not issue.resolved
    )


@dataclass(frozen=True, slots=True)
class _RecoveredDispute:
    review: Review
    outcome: ReviewOutcome
    repeated: tuple[ReviewIssue, ...]
    resolved: tuple[UUID, ...]


def _recovered_repeated_blocking_dispute(
    session: Session, run: TaskRun, task: Task
) -> _RecoveredDispute | None:
    reviews = ReviewRepository(session).list_for_run(run.id)
    if len(reviews) < 2:
        return None
    latest = reviews[-1]
    if (
        latest.decision is not ReviewDecision.CHANGES_REQUESTED
        or latest.cycle >= task.limits.max_review_cycles
    ):
        return None

    earlier = tuple(
        issue
        for stored in reviews[:-1]
        for issue in stored.issues
        if not issue.resolved
    )
    if not earlier:
        return None

    resolved = _close_unreraised_review_issues(
        session, run, task, latest, earlier=earlier
    )
    resolved_ids = set(resolved)
    still_open = tuple(
        issue for issue in earlier if issue.id is None or issue.id not in resolved_ids
    )
    repeated = reraised_unresolved_blocking_issues(still_open, latest.issues)
    if not repeated:
        return None

    result = review_result_for(latest)
    base = ReviewOutcome(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        cycle=latest.cycle,
        package=None,  # type: ignore[arg-type]
        result=result,
        review=latest,
        routing=ReviewRouting(
            decision=ReviewDecision.CHANGES_REQUESTED,
            reviewer_decision=result.decision,
            task_status=TaskStatus.CHANGES_REQUESTED,
            failure_reason=FailureReason.REVIEW_CHANGES_REQUESTED,
            blocking_issues=result.blocking_issues,
            feedback=render_review_feedback(result),
        ),
    )
    return _RecoveredDispute(
        review=latest,
        outcome=_escalate_repeated_blocking_dispute(base, repeated),
        repeated=tuple(repeated),
        resolved=resolved,
    )


def _escalate_repeated_blocking_dispute(
    review: ReviewOutcome, repeated: Sequence[ReviewIssue]
) -> ReviewOutcome:
    reason = (
        "a blocking review issue persisted across a coder repair cycle and "
        "successful deterministic verification; human review is required to "
        "resolve the repeated coder/reviewer disagreement"
    )
    routing = ReviewRouting(
        decision=ReviewDecision.HUMAN_REVIEW_REQUIRED,
        reviewer_decision=review.result.decision,
        task_status=TaskStatus.HUMAN_REVIEW,
        failure_reason=FailureReason.HUMAN_DECISION_REQUIRED,
        blocking_issues=tuple(repeated),
        human_review_reasons=(reason,),
        feedback=review.routing.feedback,
    )
    return replace(review, routing=routing)


def _close_unreraised_review_issues(
    session: Session,
    run: TaskRun,
    task: Task,
    review: Review,
    *,
    earlier: Sequence[ReviewIssue],
) -> tuple[UUID, ...]:
    closed = unreraised_issues(earlier, review.issues)
    reviews = ReviewRepository(session)
    for issue in closed:
        if issue.id is not None:
            reviews.mark_issue_resolved(issue.id)
    if closed:
        logger.info(
            "review_issues_resolved",
            run_id=str(run.id),
            task=task.external_task_id,
            cycle=review.cycle,
            resolved=len(closed),
            still_open=len(earlier) - len(closed),
        )
    return tuple(issue.id for issue in closed if issue.id is not None)


def _close_unreraised_issues(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    review: ReviewOutcome,
    *,
    earlier: Sequence[ReviewIssue] | None = None,
) -> tuple[UUID, ...]:
    """Mark earlier findings this review did not raise again (concern 27).

    Every open finding from an earlier cycle was in this review's package, so a
    reviewer that read the new diff and did not repeat one is the only witness
    this system has that it was addressed. Nothing is closed on the first
    cycle: there is nothing earlier to close.
    """
    earlier_issues = (
        tuple(earlier)
        if earlier is not None
        else _open_earlier_review_issues(session, run, review.cycle)
    )
    if not earlier_issues:
        return ()
    reviews = ReviewRepository(session)
    closed = unreraised_issues(earlier_issues, review.result.issues)
    for issue in closed:
        if issue.id is not None:
            reviews.mark_issue_resolved(issue.id)
    if closed:
        logger.info(
            "review_issues_resolved",
            run_id=str(run.id),
            task=task.external_task_id,
            cycle=review.cycle,
            resolved=len(closed),
            still_open=len(earlier_issues) - len(closed),
        )
    return tuple(issue.id for issue in closed if issue.id is not None)


# ------------------------------------------------------------------- settling


def _settle(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    task: Task,
    project: Project,
    iterations: Sequence[FixIteration],
    *,
    outcome: LoopOutcome,
    config: Settings,
    reason: FailureReason | None = None,
    escalation: HumanEscalation | None = None,
    rollback: bool = False,
    attempts_used: int | None = None,
    cycles_used: int | None = None,
    recovered: RecoveredLoopState | None = None,
    runtime_budget: RuntimeBudget | None = None,
) -> FixLoopResult:
    """Close the loop: the worktree, the run row, the task, the artifact.

    Section 25's list for a run that failed beyond policy, in its order:
    reset the disposable worktree, mark the run failed, preserve the artifacts,
    create the escalation, and merge or push nothing. The reset is conditional
    and the condition matters -- a candidate a person has been asked to decide
    about must still be there when they look, so an escalated worktree is
    preserved and only a rejected one is thrown away.

    ``attempts_used`` and ``cycles_used`` arrive from the caller rather than
    being measured from ``iterations``, because ``iterations`` holds only the
    turns *this* process made. Deriving them here is what made a resumed run
    report one attempt for a run that had made several, and made its escalation
    say "1 of 3 attempts were made" about work that had been tried three times.
    """
    workspace_reset = False
    if rollback:
        rollback_workspace(workspace)
        workspace_reset = True

    if outcome is LoopOutcome.APPROVED:
        # The run is not finished: the candidate still has to be committed, and
        # that is the workflow's step (phase K). Leaving it RUNNING is what says
        # so; marking it succeeded here would make an uncommitted candidate look
        # like delivered work.
        status = TaskStatus.APPROVED
    else:
        status = (
            TaskStatus.HUMAN_REVIEW
            if outcome is LoopOutcome.ESCALATED
            else TaskStatus.FAILED
        )
        TaskRunRepository(session).finish(
            run.id, RunStatus.FAILED, reason.value if reason else None
        )

    attempts = attempts_used if attempts_used is not None else len(iterations)
    cycles = (
        cycles_used
        if cycles_used is not None
        else sum(1 for iteration in iterations if iteration.review is not None)
    )
    configured_ms = (
        runtime_budget.configured_ms
        if runtime_budget is not None
        else configured_runtime_ms(task.limits)
    )
    if reason is FailureReason.RUNTIME_EXHAUSTED:
        consumed_ms = configured_ms
        remaining_ms = 0
    elif runtime_budget is not None:
        consumed_ms = runtime_budget.consumed_ms
        remaining_ms = runtime_budget.remaining_ms
    else:
        consumed_ms = max(0, run.active_runtime_ms)
        remaining_ms = max(0, configured_ms - consumed_ms)
    if outcome is LoopOutcome.ESCALATED and escalation is None:
        escalation = _escalate(
            session, run, task, project, iterations,
            reason=reason or FailureReason.HUMAN_DECISION_REQUIRED,
            rolled_back=workspace_reset,
            config=config,
            attempts_used=attempts,
            recovered=recovered,
            cycles_used=cycles,
            configured_runtime_ms=configured_ms,
            consumed_runtime_ms=consumed_ms,
            remaining_runtime_ms=remaining_ms,
        )
    _transition(session, task, status)

    result = FixLoopResult(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        outcome=outcome,
        iterations=tuple(iterations),
        task_status=status,
        failure_reason=reason,
        escalation=escalation,
        rolled_back=workspace_reset,
        attempts_used=attempts,
        cycles_used=cycles,
        recovery=recovered,
        configured_runtime_ms=configured_ms,
        consumed_runtime_ms=consumed_ms,
        remaining_runtime_ms=remaining_ms,
    )
    stored = artifact_store.write_json(
        session, run.id, FIX_LOOP_ARTIFACT, result.describe(),
        kind=FIX_LOOP_ARTIFACT, settings=config,
    )
    result = replace(result, artifacts={FIX_LOOP_ARTIFACT: stored.relative_path})
    if outcome is not LoopOutcome.APPROVED:
        # Concern 48: an approved run has *not* settled here. Its candidate is
        # still uncommitted and delivery is the workflow's next step, which is
        # why the branch above leaves the run RUNNING. Writing a terminal
        # outcome now would be writing the wrong one -- `_record_outcome` maps
        # everything that is not an escalation to "rejected" -- and if delivery
        # or the process then failed, the durable history would say an approved
        # run was rejected. The provisional "in_progress" from the last turn
        # boundary stands until delivery replaces it with "accepted".
        _record_outcome(session, run, task, outcome, config=config)
    logger.info(
        "fix_loop_finished",
        run_id=str(run.id),
        task=task.external_task_id,
        outcome=outcome.value,
        attempts_used=result.attempts_used,
        cycles_used=result.cycles_used,
        failure_reason=reason.value if reason else None,
        escalation_id=str(escalation.id) if escalation else None,
        rolled_back=workspace_reset,
    )
    return result


def _record_outcome(
    session: Session,
    run: TaskRun,
    task: Task,
    outcome: LoopOutcome,
    *,
    config: Settings,
) -> None:
    """Write ``outcome.json`` for a run that did not get delivered (phase L).

    Phase L's exit condition covers rejected runs, not just accepted ones, and
    this is where they are rejected. An escalated run is recorded as
    ``escalated`` rather than as a failure: it is waiting on a person, and the
    difference decides whether "this project needs help" or "this model is
    failing" is the right reading of the history.

    No training example and no lessons come from this path, and deliberately.
    A run that was rejected has not demonstrated anything worth copying, and its
    findings were never confirmed addressed, so there is no verified cycle to
    generalise from.

    Best-effort for the same reason as delivery: the run has already been marked
    failed, and failing to write a summary of it must not change that.
    """
    if outcome is LoopOutcome.APPROVED:
        # Concern 48, second guard. This function maps everything that is not an
        # escalation to "rejected", so being called with an approved outcome can
        # only produce a false record. Refusing here rather than trusting the
        # caller means the bug cannot come back through a new call site.
        logger.error(
            "outcome_recording_refused",
            run_id=str(run.id),
            task=task.external_task_id,
            outcome=outcome.value,
            detail=(
                "an approved run has not settled until delivery commits it; "
                "recording a terminal outcome here would record the wrong one"
            ),
        )
        return
    try:
        from ..services.training import record_outcome

        record_outcome(
            session,
            run.id,
            outcome="escalated" if outcome is LoopOutcome.ESCALATED else "rejected",
            settings=config,
        )
    except Exception as error:  # noqa: BLE001 - bookkeeping must not fail the loop
        logger.error(
            "outcome_recording_failed",
            run_id=str(run.id),
            task=task.external_task_id,
            outcome=outcome.value,
            error=str(error),
            exc_info=True,
        )


def _record_progress(
    session: Session,
    run: TaskRun,
    task: Task,
    *,
    config: Settings,
) -> None:
    """Best-effort provisional outcome at each durable turn boundary."""
    try:
        from ..services.training import record_outcome

        record_outcome(session, run.id, outcome="in_progress", settings=config)
    except Exception as error:  # noqa: BLE001 - bookkeeping must not fail the loop
        logger.error(
            "progress_recording_failed",
            run_id=str(run.id),
            task=task.external_task_id,
            error=str(error),
            exc_info=True,
        )


def _escalate(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    iterations: Sequence[FixIteration],
    *,
    reason: FailureReason,
    rolled_back: bool,
    config: Settings,
    attempts_used: int | None = None,
    recovered: RecoveredLoopState | None = None,
    cycles_used: int = 0,
    configured_runtime_ms: int | None = None,
    consumed_runtime_ms: int | None = None,
    remaining_runtime_ms: int | None = None,
) -> HumanEscalation:
    """Write the escalation for a run no reviewer escalated (section 24).

    Reached when the attempts ran out on deterministic failures, or when an
    attempt was refused before a reviewer could see it. The review agent's own
    escalation is better where it exists -- it has the reviewer's words -- so
    this is only ever the one nobody else wrote.

    The attempt history is the recovered turns followed by this process's,
    because after a resume the in-memory list on its own would tell a person
    that the run tried once when it tried three.
    """
    last = iterations[-1] if iterations else None
    attempts = attempts_used if attempts_used is not None else len(iterations)
    history: list[str] = (
        [turn.summary() for turn in recovered.turns] if recovered else []
    )
    history.extend(iteration.summary() for iteration in iterations)
    options = run_escalation_options(reason)
    if reason is FailureReason.RETRY_EXHAUSTED:
        reason_text = (
            f"{attempts} of the task's {task.limits.max_attempts} permitted "
            "attempts were made and none produced a change a reviewer accepted."
        )
    elif reason is FailureReason.RUNTIME_EXHAUSTED:
        reason_text = (
            "The run consumed its active-execution runtime budget: "
            f"configured {_duration(configured_runtime_ms)}, "
            f"consumed {_duration(consumed_runtime_ms)}, "
            f"remaining {_duration(remaining_runtime_ms)}. "
            f"It stopped because runtime, not retries, was exhausted; "
            f"{attempts} coding attempt(s) started and {cycles_used} review cycle(s) completed."
        )
    elif (
        reason is FailureReason.HUMAN_DECISION_REQUIRED
        and last is not None
        and last.review is not None
        and last.review.routing.human_review_reasons
    ):
        reason_text = " ".join(last.review.routing.human_review_reasons)
    else:
        reason_text = "The run reached a decision the orchestrator may not take."
    summary = render_run_escalation(
        external_task_id=task.external_task_id,
        reason=reason_text,
        requirement=task.instructions or task.title,
        blocker=_blocker(last),
        attempts=history,
        options=options,
        starting_commit=run.starting_commit,
        rolled_back=rolled_back,
    )
    prefix = artifact_store.attempt_prefix(run, cycle=last.cycle if last else None)
    artifact_store.write_text(
        session, run.id, prefix + ESCALATION_ARTIFACT, summary,
        kind=ESCALATION_ARTIFACT, settings=config,
    )
    escalation = EscalationRepository(session).add(
        HumanEscalation(
            task_id=task.id,
            task_run_id=run.id,
            reason=reason.value,
            summary=summary,
            options=list(options),
            status=EscalationStatus.OPEN,
        )
    )
    _emit(
        session, run, task, project, RunEventType.HUMAN_REVIEW_REQUIRED,
        {
            "escalation_id": str(escalation.id),
            "failure_reason": reason.value,
            "attempts_used": attempts,
            "blocker": _blocker(last),
            "recovered_attempts": len(recovered.turns) if recovered else 0,
            "configured_runtime_ms": configured_runtime_ms,
            "consumed_active_runtime_ms": consumed_runtime_ms,
            "remaining_runtime_ms": remaining_runtime_ms,
            "review_cycles_used": cycles_used,
            "budget_exhausted": (
                "runtime" if reason is FailureReason.RUNTIME_EXHAUSTED else "retry"
                if reason is FailureReason.RETRY_EXHAUSTED else None
            ),
        },
    )
    logger.warning(
        "fix_loop_escalated",
        run_id=str(run.id),
        task=task.external_task_id,
        escalation_id=str(escalation.id),
        failure_reason=reason.value,
        attempts_used=attempts,
        recovered_attempts=len(recovered.turns) if recovered else 0,
    )
    return escalation


def _duration(milliseconds: int | None) -> str:
    """Compact, exact runtime evidence for an operator-facing escalation."""
    value = max(0, milliseconds or 0)
    return f"{value} ms ({value / 60_000:.3f} min)"


def _blocker(iteration: FixIteration | None) -> str:
    """What actually stopped the run, for the escalation's own words."""
    if iteration is None:
        return "no attempt was made"
    if iteration.verification is not None and not iteration.verification.passed:
        failures = iteration.verification.failures
        if failures:
            step = failures[0]
            return (
                f"{step.verification_type.value.casefold()} failed: "
                f"{step.command or step.detail} "
                f"(exit {step.exit_code if step.exit_code is not None else 'n/a'})"
            )
    if iteration.review is not None:
        if iteration.review.routing.needs_human and iteration.review.routing.human_review_reasons:
            return _one_line("; ".join(iteration.review.routing.human_review_reasons))
        blocking = iteration.review.result.blocking_issues
        if blocking:
            return _one_line(blocking[0].problem)
        return _one_line(iteration.review.routing.summary())
    if iteration.coding.failure_reason is not None:
        return _one_line(iteration.coding.feedback or str(iteration.coding.failure_reason))
    return "the run produced no verdict"


def _one_line(text: str, limit: int = 300) -> str:
    """A multi-line failure as one readable line of history."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# -------------------------------------------------------------------- helpers


def _emit(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    event_type: RunEventType,
    payload: dict[str, object],
) -> None:
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=event_type,
            attempt=run.attempt_number,
            payload=payload,
        )
    )


def _transition(session: Session, task: Task, status: TaskStatus) -> None:
    """Move the task to ``status`` when the move is legal.

    Logged rather than raised when it is not, as in the other agents: the run's
    outcome is the record worth keeping, and losing it to a bookkeeping error
    would be the worse failure. Unlike the other agents, a skipped transition
    here leaves a task in a state no scheduler will pick up, so the warning
    names both states.
    """
    if task.status is status:
        return
    if not can_transition(task.status, status):
        logger.warning(
            "fix_loop_transition_skipped",
            task=task.external_task_id,
            current=task.status.value,
            requested=status.value,
        )
        return
    TaskRepository(session).transition(task.id, status)
    task.status = status


__all__ = [
    "FIX_LOOP_ARTIFACT",
    "FixIteration",
    "FixLoopResult",
    "LoopOutcome",
    "run_fix_loop",
]
