"""Deterministic policy per failure class (build.md section 49).

Every failure reason maps to exactly one action so that no failure falls
through a generic handler.
"""

from __future__ import annotations

from .enums import FailureAction, FailureReason

FAILURE_POLICY: dict[FailureReason, FailureAction] = {
    # Infrastructure hiccups: retry the same attempt.
    FailureReason.MODEL_UNAVAILABLE: FailureAction.RETRY,
    FailureReason.MODEL_TIMEOUT: FailureAction.RETRY,
    FailureReason.REVIEWER_UNAVAILABLE: FailureAction.RETRY,
    FailureReason.WORKER_FAILURE: FailureAction.RETRY,
    FailureReason.RESOURCE_UNAVAILABLE: FailureAction.PAUSE,
    # Deterministic evidence the coder can act on.
    FailureReason.INVALID_MODEL_RESPONSE: FailureAction.SEND_TO_CODER,
    FailureReason.BUILD_FAILED: FailureAction.SEND_TO_CODER,
    FailureReason.LINT_FAILED: FailureAction.SEND_TO_CODER,
    FailureReason.TEST_FAILED: FailureAction.SEND_TO_CODER,
    FailureReason.REVIEW_CHANGES_REQUESTED: FailureAction.SEND_TO_CODER,
    # Unsafe candidates never reach review.
    FailureReason.SECURITY_FAILED: FailureAction.ROLLBACK,
    FailureReason.SCOPE_VIOLATION: FailureAction.ROLLBACK,
    FailureReason.GIT_CONFLICT: FailureAction.ROLLBACK,
    # A human owns the decision.
    FailureReason.RETRY_EXHAUSTED: FailureAction.ESCALATE,
    FailureReason.RUNTIME_EXHAUSTED: FailureAction.ESCALATE,
    FailureReason.HUMAN_DECISION_REQUIRED: FailureAction.ESCALATE,
    # Nothing automatic can resolve a blocked integration: the candidate is
    # already accepted, so retrying the task would discard reviewed work, and
    # resolving a merge is not something the coding model is asked to do
    # (concern 51). A person owns it, and the baseline waits.
    FailureReason.INTEGRATION_BLOCKED: FailureAction.ESCALATE,
}


def action_for(reason: FailureReason) -> FailureAction:
    """Return the configured action for a failure reason.

    Raises:
        KeyError: if a new reason was added without a policy. This is
            intentional -- an unclassified failure must not be silently
            retried.
    """
    return FAILURE_POLICY[reason]


def is_retryable(reason: FailureReason) -> bool:
    return action_for(reason) in {FailureAction.RETRY, FailureAction.SEND_TO_CODER}
