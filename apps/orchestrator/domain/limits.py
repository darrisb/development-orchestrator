"""Attempt and review-cycle ceilings (build.md section 23).

Never loop indefinitely: once a ceiling is hit the run escalates.
"""

from __future__ import annotations

from .enums import FailureReason
from .models import TaskLimits


def attempts_remaining(limits: TaskLimits, attempt_number: int) -> int:
    return max(0, limits.max_attempts - attempt_number)


def review_cycles_remaining(limits: TaskLimits, review_cycle: int) -> int:
    return max(0, limits.max_review_cycles - review_cycle)


def can_retry_coding(limits: TaskLimits, attempt_number: int) -> bool:
    return attempts_remaining(limits, attempt_number) > 0


def can_request_another_review(limits: TaskLimits, review_cycle: int) -> bool:
    return review_cycles_remaining(limits, review_cycle) > 0


def exhaustion_reason(
    limits: TaskLimits, attempt_number: int, review_cycle: int
) -> FailureReason | None:
    """Return ``RETRY_EXHAUSTED`` once either ceiling is reached."""
    if not can_retry_coding(limits, attempt_number):
        return FailureReason.RETRY_EXHAUSTED
    if not can_request_another_review(limits, review_cycle):
        return FailureReason.RETRY_EXHAUSTED
    return None
