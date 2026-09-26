from __future__ import annotations

from apps.orchestrator.domain.enums import FailureReason
from apps.orchestrator.domain.limits import (
    attempts_remaining,
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
