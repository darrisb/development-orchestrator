"""Rebuilding a fix loop from what survived an interruption (section 28).

The fix loop used to hold its whole history in two local variables -- the list
of turns it had made and the correction text for the next one -- and a turn was
made durable only once it had *finished*. A provider that timed out half way
through a correction therefore lost everything: the transaction unwound, the
in-memory list went with the process, and the resumed run started the same
attempt over with nothing to act on. The reviewer had asked for something
specific, the retry was given no such thing, and the review budget was spent
on the same mistake a second time. Observed on TraceStack TS-105: two HIGH
findings, a 600-second timeout, a blind retry, an escalation.

This module is the other half of the fix, and it is deliberately *not* a second
book of record. Everything here is read from something already durable:

* ``reviews`` -- the reviewer's verdict and its findings, committed at the end
  of the cycle that produced them.
* ``model_runs`` -- every model call, committed the moment it was made, so a
  call whose turn was unwound is still a call that happened.
* ``run_events`` -- ``CODING_STARTED``, each stamped with the attempt it
  belongs to, and ``VERIFICATION_REPAIR_GRANTED``, which is the whole of
  concern 79's bounded allowance: granted once, appended inside the turn
  boundary that granted it, and read back here so a resumed run cannot be
  handed a second one.
* the run's artifact directory -- ``attempt-N-cycle-M/`` exists on disk as
  soon as a turn starts writing, and a file is inside nobody's transaction.

Two different rules, because two different things are being counted:

**An attempt is charged when a model is asked.** It cost provider time, it left
a recorded call, and it left a directory. Re-running it under the same number
would overwrite the evidence of why it failed, and a rollback must not be able
to hand the number back.

**A review cycle is charged when a reviewer answers.** Not before -- the review
agent has always said this, because counting a cycle no reviewer ever read
would let three unreachable-reviewer retries exhaust a task's budget without
anyone having looked at the change. A turn interrupted *before* the review
therefore leaves the cycle unspent, and the retry reuses it.

The consequence is that a resumed correction is the correction: the successor
of the same attempt, in the same cycle, carrying the same findings. That is
the invariant this module exists to restore.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings
from ..domain.enums import (
    CorrectionSource,
    ModelPurpose,
    ReviewDecision,
    RunEventType,
)
from ..domain.models import ModelRun, Review, TaskRun
from ..domain.review import ReviewResult, issue_fingerprint, render_review_feedback
from ..repositories import (
    ModelRunRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRunRepository,
)
from ..services import artifact_store

logger = get_logger(__name__)

#: What makes two findings the same finding across cycles. Aliased here so a
#: caller building the stagnation history does not have to spell out the
#: four-tuple.
type Fingerprint = frozenset[tuple[str, str, str, str]]

#: ``attempt-2-cycle-3/``. Parsed rather than listed so an unexpected directory
#: cannot be mistaken for an attempt; anything that does not parse is ignored
#: the way a stray file in a run directory is.
_ATTEMPT_DIR_RE = re.compile(r"^attempt-(\d+)-cycle-(\d+)$")

#: A decision that sent the work back. These are the reviews whose findings the
#: next attempt is given.
_CORRECTION_DECISIONS = frozenset(
    {ReviewDecision.CHANGES_REQUESTED, ReviewDecision.HUMAN_REVIEW_REQUIRED}
)

_CODING_PURPOSES = frozenset({ModelPurpose.CODE, ModelPurpose.FIX})


@dataclass(frozen=True, slots=True)
class RecoveredTurn:
    """One turn the loop made before the interruption, as a durable record.

    Not a ``FixIteration``: those carry the live objects a turn produced -- the
    change set, the diff, the review package -- which is exactly what an
    interruption destroyed and what no honest reconstruction can invent. What
    survives is the part a reader of the history needs: which attempt it was,
    where it stopped, and what it said.
    """

    attempt: int
    cycle: int
    stage: str
    detail: str

    def summary(self) -> str:
        return f"attempt {self.attempt} ({self.stage}): {_one_line(self.detail)}"

    def describe(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "cycle": self.cycle,
            "stage": self.stage,
            "detail": self.detail,
            "recovered": True,
        }


@dataclass(frozen=True, slots=True)
class RecoveredLoopState:
    """What a resumed run starts from."""

    #: The correction text for the next attempt: the latest reviewer's
    #: findings, rebuilt from the stored review. ``None`` when no review has
    #: asked for anything.
    feedback: str | None
    #: The attempt number the next turn must use. Always greater than every
    #: attempt already begun, so its artifacts cannot land in a directory that
    #: exists.
    next_attempt: int
    #: The review cycle the next turn belongs to. Reuses a cycle that was begun
    #: but never judged, and never one a reviewer has answered.
    cycle: int
    #: Attempts begun, recovered or live. What ``attempts_used`` reports.
    attempts_started: int
    #: Reviews that returned a verdict. What ``cycles_used`` reports.
    reviews_completed: int
    #: Review findings per completed cycle, oldest first, for the stagnation
    #: check. Without these a resumed run cannot see that the last two reviews
    #: asked for the same thing.
    fingerprints: tuple[Fingerprint, ...] = ()
    #: The recovered turns, for the escalation's attempt history.
    turns: tuple[RecoveredTurn, ...] = ()
    #: Whether any of this came from a record rather than from this process.
    recovered: bool = False
    #: The attempt that was begun and never reached a verdict, when there is
    #: one. For the log and the report; the accounting does not depend on it.
    interrupted_attempt: int | None = None
    #: Where ``feedback`` came from (concern 79). A reviewer's findings outrank
    #: a person's answer, and both outrank having nothing, which is the same
    #: order ``feedback`` itself is resolved in -- so this is a label for the
    #: choice that was already being made, not a second decision.
    feedback_source: CorrectionSource = CorrectionSource.INITIAL
    #: Concern 79 allowances already granted to this run, counted from the
    #: ``VERIFICATION_REPAIR_GRANTED`` events. The one piece of this state that
    #: exists purely to be able to *refuse*: a resumed run reads its own grant
    #: back and is not given another.
    verification_repairs_granted: int = 0

    def describe(self) -> dict[str, object]:
        return {
            "recovered": self.recovered,
            "next_attempt": self.next_attempt,
            "cycle": self.cycle,
            "attempts_started": self.attempts_started,
            "reviews_completed": self.reviews_completed,
            "interrupted_attempt": self.interrupted_attempt,
            "feedback_reconstructed": self.feedback is not None,
            "feedback_source": self.feedback_source.value,
            "verification_repairs_granted": self.verification_repairs_granted,
            "turns": [turn.describe() for turn in self.turns],
        }


def recover_loop_state(
    session: Session,
    run: TaskRun,
    *,
    settings: Settings,
    initial_feedback: str | None = None,
) -> RecoveredLoopState:
    """Read this run's durable records and work out where the loop should be.

    A run that has done nothing is not a special case: it has no reviews, no
    calls and no directories, so the same arithmetic yields attempt 1, cycle 1
    and no feedback.

    ``initial_feedback`` is a person's answer to an escalation. It is used only
    when no reviewer has asked for anything: a reviewer's findings are more
    specific, and a human who said "keep going" has not retracted them.

    The run's own row is re-read rather than taken from the caller. Everything
    this function concludes is about what is on the record, and a caller holding
    a copy read before the last turn -- which is what a long-lived session hands
    over -- would quietly answer a different question.
    """
    run = TaskRunRepository(session).get(run.id) or run
    reviews = ReviewRepository(session).list_for_run(run.id)
    calls = [
        call
        for call in ModelRunRepository(session).list_for_run(run.id)
        if call.purpose in _CODING_PURPOSES
    ]
    history = RunEventRepository(session).list_for_run(run.id)
    events = [
        event
        for event in history
        if event.event_type is RunEventType.CODING_STARTED and event.attempt
    ]
    # Concern 79. The allowance is a run event and nothing else, which is what
    # makes it survive the process that granted it. Counted rather than tested
    # for presence so that a record somehow carrying two is read as two spent,
    # not as one still available.
    # ``==``, not ``is``: ``RunEvent.event_type`` is typed and stored as a
    # plain ``str``, so a row read back from the database is a string and an
    # identity test against the enum member is always false. Getting this wrong
    # here would make the refusal silently stop working -- a resumed run would
    # count zero grants and hand out a second allowance -- which is the one
    # failure mode this count exists to prevent. ``StrEnum`` compares equal to
    # its value, so this holds for a live event object too.
    repairs_granted = sum(
        1
        for event in history
        if event.event_type == RunEventType.VERIFICATION_REPAIR_GRANTED
    )
    directories = _attempt_directories(run, settings=settings)

    started_attempts = {
        int(event.attempt) for event in events if event.attempt
    }
    started_attempts.update(call.attempt for call in calls if call.attempt is not None)
    started_attempts.update(attempt for attempt, _ in directories)

    judged_cycles = {review.cycle for review in reviews}
    started_cycles = {cycle for _, cycle in directories} | judged_cycles

    reviews_completed = max(run.review_cycle, len(reviews))
    # A turn that began but never reached a reviewer has begun its cycle
    # without having spent it, so the cycle in progress is the highest one
    # anything has touched.
    cycle = max(reviews_completed, max(started_cycles, default=0)) or 1

    highest_started = max(started_attempts, default=0)
    # The run's own row is a record too, and it is the only one that can speak
    # for work done by an earlier *run* of the same task: a run opened at
    # attempt 2 is a retry of an attempt somebody else made, and the ceiling it
    # is held to is the task's, so that attempt counts even though none of its
    # records are here. Where the row and the records disagree, the row sets the
    # floor for the number to continue at, and the records set the number to
    # continue after -- so the loop never files a turn under a number that is
    # already on disk, and never re-runs one that the row says was begun.
    attempts_started = max(highest_started, run.attempt_number - 1)
    next_attempt = max(run.attempt_number, highest_started + 1)
    interrupted = _interrupted_attempt(highest_started, calls, judged_cycles)
    review_feedback = _correction_feedback(reviews)
    state = RecoveredLoopState(
        feedback=review_feedback or initial_feedback,
        next_attempt=next_attempt,
        cycle=cycle,
        attempts_started=attempts_started,
        reviews_completed=reviews_completed,
        fingerprints=tuple(_fingerprints(review) for review in reviews),
        turns=_recovered_turns(reviews, calls, highest_started),
        recovered=bool(reviews or started_attempts or run.attempt_number > 1),
        interrupted_attempt=interrupted,
        feedback_source=(
            CorrectionSource.REVIEW
            if review_feedback
            else CorrectionSource.HUMAN
            if initial_feedback
            else CorrectionSource.INITIAL
        ),
        verification_repairs_granted=repairs_granted,
    )
    if state.recovered:
        logger.info(
            "fix_loop_state_recovered",
            run_id=str(run.id),
            next_attempt=state.next_attempt,
            cycle=state.cycle,
            attempts_started=state.attempts_started,
            reviews_completed=state.reviews_completed,
            interrupted_attempt=state.interrupted_attempt,
            feedback_reconstructed=state.feedback is not None,
            feedback_source=state.feedback_source.value,
            verification_repairs_granted=state.verification_repairs_granted,
        )
    return state


def review_result_for(review: Review) -> ReviewResult:
    """A ``ReviewResult`` rebuilt from a stored ``Review``.

    Enough of it to render the same correction text the original cycle sent.
    The fields that are not on the row -- what reconciliation changed, the
    reported task id, how long the call took -- do not appear in the feedback,
    and are left at their defaults rather than invented.

    The stored review is the *reviewer's* answer, so rendering from it cannot
    produce a correction prompt the reviewer did not ask for. Where the
    question is instead "did the answer need reconciling", the ``review.json``
    artifact from that cycle holds the warnings, and it is on disk.
    """
    return ReviewResult(
        decision=review.decision,
        summary=review.summary,
        issues=tuple(review.issues),
        confidence=review.confidence,
        risk=review.risk,
        provider_id=review.reviewer_provider,
        model_name=review.reviewer_model,
    )


# ------------------------------------------------------------------ internals


def _fingerprints(review: Review) -> Fingerprint:
    """The cycle's blocking findings, under the issue-closing identity.

    Blocking only, and under the same ``issue_fingerprint`` the live
    stagnation check uses: a recovered cycle has to be comparable with a
    live one, and a fingerprint taken over a different set of findings
    would make the two histories disagree about what repeated.
    """
    return frozenset(
        issue_fingerprint(issue) for issue in review.issues if issue.is_blocking
    )


def _correction_feedback(reviews: Sequence[Review]) -> str | None:
    """The findings of the latest review that asked for a correction.

    Rendered from the stored review rather than remembered, which is the whole
    point: there is no in-memory copy to lose, and the text a resumed attempt
    receives is the text the original cycle would have sent.

    A review with no issues is skipped even when it asked for changes. Section
    23 falls back to the non-blocking findings in that case, and a run with
    nothing to correct is better served by no correction text than by one
    assembled from advice nobody asked for.
    """
    for review in reversed(reviews):
        if review.decision in _CORRECTION_DECISIONS and review.issues:
            return render_review_feedback(review_result_for(review))
    return None


def _attempt_directories(run: TaskRun, *, settings: Settings) -> tuple[tuple[int, int], ...]:
    """The ``attempt-N-cycle-M`` directories this run has written.

    Read from disk because that is where they are durable. A turn writes its
    first artifact before it calls a model, so this is the only record that
    exists even for a turn unwound before it could write a row -- which is
    exactly the turn a resume must not overwrite.
    """
    if not run.external_run_id:
        return ()
    try:
        root = artifact_store.run_directory(run.external_run_id, settings=settings)
    except artifact_store.ArtifactPathRejected:
        return ()
    found: list[tuple[int, int]] = []
    for entry in root.iterdir():
        if entry.is_dir():
            matched = _ATTEMPT_DIR_RE.match(entry.name)
            if matched:
                found.append((int(matched.group(1)), int(matched.group(2))))
    return tuple(sorted(found))


def _recovered_turns(
    reviews: Sequence[Review], calls: Sequence[ModelRun], highest_attempt: int
) -> tuple[RecoveredTurn, ...]:
    """One line per attempt already begun, for an escalation's history.

    From the reviews (a cycle's verdict and the reviewer's own summary) and
    from the coding calls no review sits behind (an attempt made and never
    judged). An escalation that a person has to read should not need its
    history reconstructed by hand.
    """
    judged_cycles = {review.cycle for review in reviews}
    # Which attempt was made for which cycle is on the coding calls, both as
    # ``attempt`` and as ``review_cycle``, so a review is attributed to the
    # attempt that asked for it rather than to a cycle number that only happens
    # to coincide with it. The earliest call for a cycle wins, because that is
    # the one the reviewer judged: a cycle can hold several attempts when a
    # correction was interrupted and retried inside it.
    attempt_for_cycle: dict[int, int] = {}
    for call in sorted(calls, key=lambda item: item.attempt or 0):
        if call.review_cycle is None or call.attempt is None:
            continue
        attempt_for_cycle.setdefault(call.review_cycle, call.attempt)
    turns: list[RecoveredTurn] = [
        RecoveredTurn(
            attempt=attempt_for_cycle.get(review.cycle)
            or _attempt_for_cycle(review.cycle, highest_attempt),
            cycle=review.cycle,
            stage="review",
            detail=review.summary or review.decision.value,
        )
        for review in reviews
    ]
    seen_attempts = {turn.attempt for turn in turns}
    for call in calls:
        if call.review_cycle is not None and call.review_cycle in judged_cycles:
            continue
        attempt = call.attempt or 1
        if attempt in seen_attempts:
            continue
        seen_attempts.add(attempt)
        turns.append(
            RecoveredTurn(
                attempt=attempt,
                cycle=call.review_cycle or 0,
                stage="coding",
                detail=call.error_detail or (
                    f"a {call.purpose.value} call that was recorded and never reviewed"
                ),
            )
        )
    return tuple(sorted(turns, key=lambda turn: (turn.attempt, turn.cycle)))


def _attempt_for_cycle(cycle: int, highest_attempt: int) -> int:
    """Which attempt a reviewed cycle belonged to, when no row says.

    A review carries its cycle but not its attempt. Where the two are in step
    -- one attempt per cycle, the ordinary case -- the cycle is the attempt.
    After an interruption they can differ, and then the highest attempt is the
    honest answer: it is the one a reader most needs named.
    """
    if highest_attempt <= cycle:
        return cycle
    return highest_attempt


def _interrupted_attempt(
    highest_attempt: int, calls: Sequence[ModelRun], judged_cycles: set[int]
) -> int | None:
    """The newest attempt that was begun and never reached a verdict.

    An attempt whose coding call was made for a cycle that no review ever
    answered: the coder was asked, the reviewer was not, and whatever happened
    next is the interruption this reconstruction is about.
    """
    if highest_attempt <= 0:
        return None
    for call in calls:
        if call.attempt != highest_attempt:
            continue
        if call.review_cycle is None or call.review_cycle not in judged_cycles:
            return highest_attempt
    return None


def _one_line(text: str, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


__all__ = [
    "Fingerprint",
    "RecoveredLoopState",
    "RecoveredTurn",
    "recover_loop_state",
    "review_result_for",
]
