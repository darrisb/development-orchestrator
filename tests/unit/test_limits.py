from __future__ import annotations

from apps.orchestrator.domain.enums import CorrectionSource, FailureReason
from apps.orchestrator.domain.limits import (
    VERIFICATION_REPAIR_ALLOWANCE,
    attempts_remaining,
    can_repair_after_reviewer_correction,
    can_request_another_review,
    can_retry_coding,
    exhaustion_reason,
    review_cycles_remaining,
)
from apps.orchestrator.domain.models import TaskLimits


def test_defaults_match_build_spec():
    limits = TaskLimits()
    assert limits.max_attempts == 3
    assert limits.max_review_cycles == 3


def test_attempts_count_down_and_floor_at_zero():
    limits = TaskLimits(max_attempts=3)
    assert attempts_remaining(limits, 1) == 2
    assert attempts_remaining(limits, 3) == 0
    assert attempts_remaining(limits, 9) == 0


def test_retry_allowed_until_ceiling():
    limits = TaskLimits(max_attempts=3)
    assert can_retry_coding(limits, 2)
    assert not can_retry_coding(limits, 3)


def test_review_cycles_have_their_own_ceiling():
    limits = TaskLimits(max_review_cycles=3)
    assert review_cycles_remaining(limits, 1) == 2
    assert can_request_another_review(limits, 2)
    assert not can_request_another_review(limits, 3)


def test_exhaustion_reported_when_either_ceiling_is_hit():
    limits = TaskLimits(max_attempts=3, max_review_cycles=3)
    assert exhaustion_reason(limits, 1, 1) is None
    assert exhaustion_reason(limits, 3, 1) is FailureReason.RETRY_EXHAUSTED
    assert exhaustion_reason(limits, 1, 3) is FailureReason.RETRY_EXHAUSTED


# --- concern 79: the bounded verification-repair allowance -------------------
#
# The predicate in isolation. Every clause gets its own refusal, because the
# allowance is defined by what it refuses and a single happy-path assertion
# would pass just as well on a function that returned True.


def _may(
    attempt: int,
    *,
    source: CorrectionSource = CorrectionSource.REVIEW,
    granted: int = 0,
    max_attempts: int = 3,
) -> bool:
    return can_repair_after_reviewer_correction(
        TaskLimits(max_attempts=max_attempts),
        attempt,
        correction_source=source,
        repairs_granted=granted,
    )


def test_the_allowance_is_one():
    assert VERIFICATION_REPAIR_ALLOWANCE == 1


def test_granted_on_the_exhausted_attempt_of_a_reviewer_driven_correction():
    assert _may(3)


def test_not_granted_while_an_ordinary_attempt_remains():
    """The ordinary attempt is the answer; the allowance is not needed yet."""
    assert not _may(1)
    assert not _may(2)


def test_not_granted_for_any_other_correction_provenance():
    for source in CorrectionSource:
        if source is CorrectionSource.REVIEW:
            continue
        assert not _may(3, source=source), source


def test_not_granted_twice():
    assert not _may(3, granted=VERIFICATION_REPAIR_ALLOWANCE)
    assert not _may(3, granted=2)


def test_the_allowance_does_not_move_the_coding_ceiling():
    """``can_retry_coding`` is the budget, and it never consults the allowance."""
    limits = TaskLimits(max_attempts=3)
    assert not can_retry_coding(limits, 3)
    assert attempts_remaining(limits, 3) == 0
    # The repair's own attempt number is past the ceiling, so the predicate
    # refuses the turn after it as well -- there is no ladder.
    assert not _may(4, source=CorrectionSource.VERIFICATION)
