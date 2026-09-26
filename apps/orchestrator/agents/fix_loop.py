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
    ReviewIssue,
    RunEvent,
    Task,
    TaskRun,
)
from ..domain.review import HumanApprovalPolicy, issue_fingerprint, unreraised_issues
from ..domain.state_machine import can_transition
from ..domain.verification import VerificationReport
from ..domain.workflow import deadline_exceeded, run_deadline
from ..providers import ModelProvider
from ..providers.review import ReviewProvider
from ..repositories import (
    EscalationRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from ..services import artifact_store
from ..services.verification import verify_candidate
from ..services.workspace import TaskWorkspace, load_run_context, rollback_workspace
from .coding_agent import CodingAttempt, run_coding_attempt
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

    @property
    def approved(self) -> bool:
        return self.outcome is LoopOutcome.APPROVED

    @property
    def attempts_used(self) -> int:
        return len(self.iterations)

    @property
    def cycles_used(self) -> int:
        return sum(1 for iteration in self.iterations if iteration.review is not None)

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
            "rolled_back": self.rolled_back,
            "escalation_id": str(self.escalation.id) if self.escalation else None,
            "iterations": [iteration.describe() for iteration in self.iterations],
        }


async def run_fix_loop(
    session: Session,
    workspace: TaskWorkspace,
    *,
    coder: ModelProvider,
    reviewer: ReviewProvider,
    settings: Settings | None = None,
    policy: HumanApprovalPolicy | None = None,
    secrets: Mapping[str, str] | None = None,
    max_attempts: int | None = None,
    initial_feedback: str | None = None,
    deadline: datetime | None = None,
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
        reviewer: the model that judges it.
        policy: the human-approval policy (section 37), passed to every review
            so that all cycles of one run are judged against the same gate.
        secrets: values the verification commands need, injected per command
            and redacted out of every log (section 36).
        max_attempts: lowers the task's own ``max_attempts`` for this run.
            It can only lower it: a caller must not be able to talk the loop
            into more attempts than the task's manifest allows.

    Returns:
        The result. Approved means a reviewer accepted the candidate and the
        gates cleared -- not that anything was committed.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        ModelProviderError: the coder's endpoint failed or timed out.
        ReviewerUnavailable: the reviewer could not produce a review.
        WorkerBackendUnavailable: the container runtime is not usable.
        CommandRejected: a configured verification command is not permitted.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    runs = TaskRunRepository(session)
    # Attempt numbers continue the run rather than restarting at one. A run
    # opened at attempt 2 -- a retry of work that was already tried once --
    # would otherwise file its first turn under the attempt it inherited and its
    # second under the same number again, and the two would share a directory.
    first = max(1, run.attempt_number)
    ceiling = min(task.limits.max_attempts, max_attempts or task.limits.max_attempts)

    iterations: list[FixIteration] = []
    feedback = initial_feedback
    started_at = run.started_at or datetime.now(UTC)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    effective_deadline = min(
        deadline or run_deadline(started_at, task.limits),
        run_deadline(started_at, task.limits),
        started_at + timedelta(seconds=config.worker_timeout_seconds),
    )

    for number in range(first, ceiling + 1):
        if deadline_exceeded(effective_deadline):
            return _settle(
                session,
                workspace,
                run,
                task,
                project,
                iterations,
                outcome=LoopOutcome.ESCALATED,
                reason=FailureReason.RETRY_EXHAUSTED,
                config=config,
            )
        if number > first:
            # The attempt number is advanced before the attempt, not after it:
            # everything the attempt writes is filed under it, so a run that
            # dies mid-attempt still has its artifacts under the attempt that
            # was being made rather than the one before.
            run = runs.update_fields(run.id, attempt_number=number)
            _emit(
                session, run, task, project, RunEventType.FIX_STARTED,
                {
                    "attempt": number,
                    "of": ceiling,
                    "cycle": run.review_cycle + 1,
                    "after": iterations[-1].stage,
                    "failure_reason": (
                        iterations[-1].failure_reason.value
                        if iterations[-1].failure_reason
                        else None
                    ),
                },
            )
        cycle = run.review_cycle + 1

        iteration = await _turn(
            session,
            workspace,
            run,
            task,
            project,
            number=len(iterations) + 1,
            cycle=cycle,
            coder=coder,
            reviewer=reviewer,
            feedback=feedback,
            config=config,
            policy=policy,
            secrets=secrets,
        )
        iterations.append(iteration)
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

        if _reviews_are_stagnant(
            iterations, limit=config.fix_loop_stagnant_review_limit
        ):
            logger.warning(
                "fix_loop_stagnant_reviews",
                run_id=str(run.id),
                task=task.external_task_id,
                consecutive_reviews=config.fix_loop_stagnant_review_limit,
            )
            return _settle(
                session,
                workspace,
                run,
                task,
                project,
                iterations,
                outcome=LoopOutcome.ESCALATED,
                reason=FailureReason.RETRY_EXHAUSTED,
                config=config,
            )

        if iteration.succeeded:
            return _settle(
                session, workspace, run, task, project, iterations,
                outcome=LoopOutcome.APPROVED, config=config,
            )
        if iteration.action is FailureAction.ESCALATE:
            return _settle(
                session, workspace, run, task, project, iterations,
                outcome=LoopOutcome.ESCALATED,
                reason=iteration.failure_reason,
                # A review that escalated has already written the escalation a
                # person will read, and it can say more than this module can.
                escalation=iteration.review.escalation if iteration.review else None,
                config=config,
            )
        if iteration.action is FailureAction.ROLLBACK:
            return _settle(
                session, workspace, run, task, project, iterations,
                outcome=LoopOutcome.FAILED,
                reason=iteration.failure_reason,
                rollback=True,
                config=config,
            )
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
        return _settle(
            session, workspace, run, task, project, iterations,
            outcome=LoopOutcome.ESCALATED,
            reason=FailureReason.HUMAN_DECISION_REQUIRED,
            config=config,
        )

    # The attempts the task allows are spent and no reviewer has accepted the
    # candidate. Section 23: after the limit, create a human escalation.
    return _settle(
        session, workspace, run, task, project, iterations,
        outcome=LoopOutcome.ESCALATED,
        reason=FailureReason.RETRY_EXHAUSTED,
        config=config,
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
    reviewer: ReviewProvider,
    feedback: str | None,
    config: Settings,
    policy: HumanApprovalPolicy | None,
    secrets: Mapping[str, str] | None,
) -> FixIteration:
    """Code, verify, review. Stops at the first of the three that has a verdict.

    The order is section 17's and the early exits are what make it honest: a
    candidate whose edits were refused has nothing to verify, and one that does
    not compile has nothing a reviewer can say anything useful about.
    """
    attempt = await run_coding_attempt(
        session,
        workspace,
        provider=coder,
        settings=config,
        review_feedback=feedback,
        # A correction attempt does not re-plan: see ``run_coding_attempt``.
        plan_required=False if number > 1 else None,
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
    )
    resolved = _close_unreraised_issues(session, run, task, project, review)
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
        return replace(iteration, failure_reason=reason, action=action, feedback=None)
    if limits_exhausted:
        return replace(
            iteration,
            failure_reason=FailureReason.RETRY_EXHAUSTED,
            action=FailureAction.ESCALATE,
            feedback=feedback,
        )
    return replace(iteration, failure_reason=reason, action=action, feedback=feedback)


def _reviews_are_stagnant(
    iterations: Sequence[FixIteration], *, limit: int
) -> bool:
    """Whether the last reviews repeated the same blocking findings.

    Empty sets never count: repeated approvals are handled by the ordinary
    success route, and repeated human-only decisions already have their own
    policy. Fingerprints deliberately use the same identity as issue closing.
    """
    if limit <= 0:
        return False
    reviews = [item.review for item in iterations if item.review is not None]
    if len(reviews) < limit:
        return False
    recent = reviews[-limit:]
    fingerprints = [
        frozenset(issue_fingerprint(issue) for issue in review.result.blocking_issues)
        for review in recent
    ]
    return bool(fingerprints[0]) and all(
        current == fingerprints[0] for current in fingerprints[1:]
    )


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


def _close_unreraised_issues(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    review: ReviewOutcome,
) -> tuple[UUID, ...]:
    """Mark earlier findings this review did not raise again (concern 27).

    Every open finding from an earlier cycle was in this review's package, so a
    reviewer that read the new diff and did not repeat one is the only witness
    this system has that it was addressed. Nothing is closed on the first
    cycle: there is nothing earlier to close.
    """
    if review.cycle <= 1:
        return ()
    reviews = ReviewRepository(session)
    earlier: list[ReviewIssue] = [
        issue
        for stored in reviews.list_for_run(run.id)
        if stored.cycle < review.cycle
        for issue in stored.issues
        if not issue.resolved
    ]
    closed = unreraised_issues(earlier, review.result.issues)
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
) -> FixLoopResult:
    """Close the loop: the worktree, the run row, the task, the artifact.

    Section 25's list for a run that failed beyond policy, in its order:
    reset the disposable worktree, mark the run failed, preserve the artifacts,
    create the escalation, and merge or push nothing. The reset is conditional
    and the condition matters -- a candidate a person has been asked to decide
    about must still be there when they look, so an escalated worktree is
    preserved and only a rejected one is thrown away.
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

    if outcome is LoopOutcome.ESCALATED and escalation is None:
        escalation = _escalate(
            session, run, task, project, iterations,
            reason=reason or FailureReason.HUMAN_DECISION_REQUIRED,
            rolled_back=workspace_reset,
            config=config,
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
    )
    stored = artifact_store.write_json(
        session, run.id, FIX_LOOP_ARTIFACT, result.describe(),
        kind=FIX_LOOP_ARTIFACT, settings=config,
    )
    result = replace(result, artifacts={FIX_LOOP_ARTIFACT: stored.relative_path})
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
) -> HumanEscalation:
    """Write the escalation for a run no reviewer escalated (section 24).

    Reached when the attempts ran out on deterministic failures, or when an
    attempt was refused before a reviewer could see it. The review agent's own
    escalation is better where it exists -- it has the reviewer's words -- so
    this is only ever the one nobody else wrote.
    """
    last = iterations[-1] if iterations else None
    options = run_escalation_options(reason)
    summary = render_run_escalation(
        external_task_id=task.external_task_id,
        reason=(
            f"{len(iterations)} of the task's {task.limits.max_attempts} permitted "
            f"attempts were made and none produced a change a reviewer accepted."
            if reason is FailureReason.RETRY_EXHAUSTED
            else "The run reached a decision the orchestrator may not take."
        ),
        requirement=task.instructions or task.title,
        blocker=_blocker(last),
        attempts=[iteration.summary() for iteration in iterations],
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
            "attempts_used": len(iterations),
            "blocker": _blocker(last),
        },
    )
    logger.warning(
        "fix_loop_escalated",
        run_id=str(run.id),
        task=task.external_task_id,
        escalation_id=str(escalation.id),
        failure_reason=reason.value,
        attempts_used=len(iterations),
    )
    return escalation


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
