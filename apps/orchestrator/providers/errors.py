"""Model provider errors (build.md sections 13 and 49).

Every failure a provider can raise carries a ``FailureReason``, so the caller
never has to guess a policy from an exception type or a message string: it
hands the reason to ``domain.failure_policy`` and gets one deterministic
action. There is deliberately no catch-all "model error occurred" path.
"""

from __future__ import annotations

from ..domain.enums import FailureReason


class ModelProviderError(Exception):
    """Base class for provider failures.

    Args:
        message: operator-facing description. Never include the prompt, the
            response body or credentials (section 36).
        reason: the failure class the workflow acts on.
    """

    reason: FailureReason = FailureReason.MODEL_UNAVAILABLE

    def __init__(self, message: str, *, reason: FailureReason | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class ModelUnavailable(ModelProviderError):
    """The endpoint could not be reached, or answered that it cannot serve.

    Covers connection failures, 5xx and 429. Retryable: the model host being
    down or busy says nothing about the quality of the task.
    """

    reason = FailureReason.MODEL_UNAVAILABLE


class ModelTimeout(ModelProviderError):
    """The request exceeded the configured timeout."""

    reason = FailureReason.MODEL_TIMEOUT

    def __init__(self, message: str, *, timeout_seconds: float | None = None) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds


class ModelRequestRejected(ModelProviderError):
    """The endpoint rejected the request itself (4xx other than 429).

    Almost always a configuration fault: an unknown model name, an unsupported
    parameter, a missing key. It is classified ``MODEL_UNAVAILABLE`` because
    the request never reached inference -- the coder has nothing to fix, and
    the attempt ceiling is what stops the loop rather than an endless retry.
    """

    reason = FailureReason.MODEL_UNAVAILABLE

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidModelResponse(ModelProviderError):
    """The model answered, but not in the shape the caller requires.

    Unlike the errors above this is evidence about the model's output, so the
    policy routes it back to the coder rather than retrying blindly.
    """

    reason = FailureReason.INVALID_MODEL_RESPONSE


class PromptTooLarge(ModelProviderError):
    """The assembled prompt does not fit the provider's context window.

    A backstop, not a budget: the context builder (phase F) is responsible for
    fitting the prompt. Reaching this means it failed to, which is a defect in
    the orchestrator and not something to retry against the same input.
    """

    reason = FailureReason.INVALID_MODEL_RESPONSE

    def __init__(self, *, estimated_tokens: int, context_window: int) -> None:
        super().__init__(
            f"Prompt is approximately {estimated_tokens} tokens, "
            f"which exceeds the {context_window}-token context window"
        )
        self.estimated_tokens = estimated_tokens
        self.context_window = context_window


class ProviderNotConfigured(ModelProviderError):
    """No enabled provider is configured for the requested role.

    Raised instead of quietly substituting another provider: principle 10
    forbids a silent local-to-cloud (or any cross-provider) fallback.
    """

    reason = FailureReason.RESOURCE_UNAVAILABLE
