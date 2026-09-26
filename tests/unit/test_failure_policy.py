from __future__ import annotations

import pytest

from apps.orchestrator.domain.enums import FailureAction, FailureReason
from apps.orchestrator.domain.failure_policy import FAILURE_POLICY, action_for, is_retryable


def test_every_failure_reason_has_a_policy():
    """Section 49 forbids one generic exception path for everything."""
    assert set(FAILURE_POLICY) == set(FailureReason)


def test_deterministic_failures_go_back_to_the_coder():
    for reason in (
        FailureReason.BUILD_FAILED,
        FailureReason.LINT_FAILED,
        FailureReason.TEST_FAILED,
        FailureReason.REVIEW_CHANGES_REQUESTED,
    ):
        assert action_for(reason) is FailureAction.SEND_TO_CODER


def test_unsafe_candidates_roll_back_rather_than_retry():
    for reason in (
        FailureReason.SECURITY_FAILED,
        FailureReason.SCOPE_VIOLATION,
        FailureReason.GIT_CONFLICT,
    ):
        assert action_for(reason) is FailureAction.ROLLBACK
        assert not is_retryable(reason)


def test_exhaustion_escalates_to_a_human():
    assert action_for(FailureReason.RETRY_EXHAUSTED) is FailureAction.ESCALATE
    assert action_for(FailureReason.HUMAN_DECISION_REQUIRED) is FailureAction.ESCALATE


def test_unclassified_reason_raises_rather_than_defaulting():
    with pytest.raises(KeyError):
        action_for("NOT_A_REAL_REASON")  # type: ignore[arg-type]
