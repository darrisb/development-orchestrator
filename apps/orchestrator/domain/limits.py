"""Attempt and review-cycle ceilings (build.md section 23).

Never loop indefinitely: once a ceiling is hit the run escalates.
"""

from __future__ import annotations

from .enums import CorrectionSource, FailureReason
from .models import TaskLimits

#: Concern 79: how many bounded verification repairs one run may be granted.
#:
#: One, and the number is here rather than in the loop because it is a ceiling
#: and every other ceiling in this system is. The allowance is not an extra
#: retry: it is reachable from exactly one lifecycle (a reviewer asked for a
#: correction, the correction was made, and deterministic verification rejected
#: the newly generated candidate on the last attempt the task allowed), and it
#: exists because that candidate is usually the most complete one the run
#: produced. Stranding it for want of one repair turn throws away the work the
#: reviewer asked for.
VERIFICATION_REPAIR_ALLOWANCE = 1


def attempts_remaining(limits: TaskLimits, attempt_number: int) -> int:
    return max(0, limits.max_attempts - attempt_number)


def review_cycles_remaining(limits: TaskLimits, review_cycle: int) -> int:
    return max(0, limits.max_review_cycles - review_cycle)


def can_retry_coding(limits: TaskLimits, attempt_number: int) -> bool:
    return attempts_remaining(limits, attempt_number) > 0


def can_request_another_review(limits: TaskLimits, review_cycle: int) -> bool:
    return review_cycles_remaining(limits, review_cycle) > 0


def can_repair_after_reviewer_correction(
    limits: TaskLimits,
    attempt_number: int,
    *,
    correction_source: CorrectionSource,
    repairs_granted: int,
) -> bool:
    """Whether concern 79's one bounded verification repair may be granted now.

    Every clause is a refusal, and together they are the whole of the
    allowance. The caller supplies the two facts that are not about ceilings --
    that a candidate was produced and that deterministic verification rejected
    it -- by calling this only from the branch where both are true.

    * **``correction_source`` must be ``REVIEW``.** This is the clause that
      keeps the allowance from becoming a general extra retry. An initial
      implementation that fails, a verification failure corrected by another
      verification failure, a turn whose predecessor never produced a
      candidate: none of them is the lifecycle this exists for, and none of
      them gets it.
    * **The allowance must be unspent.** ``repairs_granted`` is counted from
      the durable record, so a resumed run cannot be handed a second one.
    * **The normal budget must already be exhausted.** While an ordinary
      attempt remains, the ordinary attempt is the answer and the allowance is
      not needed; granting it earlier would widen ``max_attempts`` by one for
      every task that ever had a reviewer ask for something.

    ``max_attempts`` itself is untouched: this is not consulted by
    ``can_retry_coding`` or by ``attempts_remaining``, and a run that is never
    granted a repair sees exactly the budget it saw before.
    """
    if correction_source is not CorrectionSource.REVIEW:
        return False
    if repairs_granted >= VERIFICATION_REPAIR_ALLOWANCE:
        return False
    return not can_retry_coding(limits, attempt_number)


def exhaustion_reason(
    limits: TaskLimits, attempt_number: int, review_cycle: int
) -> FailureReason | None:
    """Return ``RETRY_EXHAUSTED`` once either ceiling is reached."""
    if not can_retry_coding(limits, attempt_number):
        return FailureReason.RETRY_EXHAUSTED
    if not can_request_another_review(limits, review_cycle):
        return FailureReason.RETRY_EXHAUSTED
    return None
