"""The ``ReviewProvider`` contract and its model-backed implementation (section 21).

Section 21 asks for one thing above all: *the workflow must consume the same
structured ``ReviewResult`` regardless of provider.* So there are two layers
here and the split matters.

* ``ReviewProvider`` is the boundary. It takes a ``ReviewRequest`` -- a
  bounded package and the task it belongs to -- and returns a ``ReviewCall``,
  which carries the ``ReviewResult`` (``domain.review``) the workflow acts on
  plus whatever raw exchange there was to keep as a run artifact. Nothing
  about prompts, JSON or endpoints appears in the signature, because a future
  adapter might be a hosted review API, a static analyser or a human queue,
  and none of those are chat completions; such an adapter returns a result
  with the raw fields left empty and nothing downstream changes.
* ``ModelReviewProvider`` is the implementation that covers both of section
  2's listed cases -- a remote OpenAI-compatible reviewer and a local one --
  because from here they differ only in a base URL. It wraps any
  ``ModelProvider``, which is what keeps "the reviewer is a stronger model"
  a configuration choice rather than a second code path.

**A reviewer's failure is not a verdict.** An endpoint that is unreachable, or
that answers with something that is not a review, must never be read as
approval or as a rejection. Both raise: ``ReviewerUnavailable`` is retryable
infrastructure, ``InvalidModelResponse`` is evidence about the model, and the
workflow's policy (``domain.failure_policy``) decides between them. There is
no default decision anywhere in this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..domain.enums import FailureReason, ModelRole
from ..domain.review import REVIEW_SCHEMA, MalformedReview, ReviewResult, parse_review
from ..domain.review_package import ReviewPackage
from .base import (
    ConnectionReport,
    ModelProvider,
    ModelRequest,
    ProviderConfig,
    StructuredSchema,
    TokenUsage,
)
from .errors import InvalidModelResponse, ModelProviderError


class ReviewerUnavailable(ModelProviderError):
    """The reviewer could not be reached, or refused the request.

    Its own class rather than ``ModelUnavailable`` because the policy differs
    in an important way: a coder that cannot be reached stops the attempt,
    while a reviewer that cannot be reached leaves a *verified* candidate
    sitting in the worktree with nothing wrong with it. Section 49 classifies
    that as ``REVIEWER_UNAVAILABLE``, and it retries.
    """

    reason = FailureReason.REVIEWER_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """One bounded review (section 21).

    Attributes:
        package: what the reviewer is shown. Already budgeted; a provider
            never adds to it, because "only additional source required to
            understand the diff" is a decision made once, upstream, and
            recorded in the package manifest.
        cycle: which review cycle this is, counting from 1.
        timeout_seconds: overrides the provider's configured timeout.
    """

    package: ReviewPackage
    cycle: int = 1
    timeout_seconds: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def external_task_id(self) -> str:
        return self.package.external_task_id


@runtime_checkable
class ReviewProvider(Protocol):
    """The review boundary. Implementations must be replaceable (principle 8)."""

    config: ProviderConfig

    async def review(self, request: ReviewRequest) -> ReviewCall:
        """Review one candidate.

        Raises:
            ReviewerUnavailable: the provider could not produce a review.
            InvalidModelResponse: it answered with something that is not one.
        """
        ...

    async def check_connection(self) -> ConnectionReport:
        """Probe the reviewer without running a review."""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...


@dataclass(frozen=True, slots=True)
class ReviewCall:
    """A completed review and the raw exchange behind it, for the artifacts.

    ``prompt_text`` and ``raw_response`` are empty for a provider that is not
    a language model. They exist because a review that cannot be read back
    verbatim cannot be audited (section 9), not because every provider has
    them.
    """

    result: ReviewResult
    prompt_text: str = ""
    raw_response: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


class ModelReviewProvider:
    """A ``ReviewProvider`` backed by any ``ModelProvider`` (section 21).

    The whole adapter is: render the package, ask for JSON against
    ``REVIEW_SCHEMA``, and parse. What it deliberately does *not* do is
    recover from a bad answer by inventing a decision -- see the module
    docstring.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        system_prompt: str,
        instruction_renderer,
        temperature: float = 0.0,
    ) -> None:
        """
        Args:
            provider: the endpoint. Its role is not enforced here -- routing
                decides which model reviews (section 31) -- but a provider
                registered as a coder is logged by the caller, not silently
                accepted as a reviewer.
            system_prompt: the reviewer's standing instructions.
            instruction_renderer: ``(ReviewRequest) -> str``, the per-review
                instructions. Injected so the prompt lives in ``agents`` and
                this module stays free of prompt text.
            temperature: zero by default. A review is a judgement that should
                not move between two runs over the same diff.
        """
        self._provider = provider
        self._system_prompt = system_prompt
        self._render_instructions = instruction_renderer
        self._temperature = temperature

    @property
    def config(self) -> ProviderConfig:
        return self._provider.config

    @property
    def is_reviewer_role(self) -> bool:
        """Whether the wrapped provider was configured as a reviewer."""
        return self._provider.config.role is ModelRole.REVIEWER

    async def review(self, request: ReviewRequest) -> ReviewCall:
        """Run one review, keeping the raw exchange for the run artifacts.

        Raises:
            ReviewerUnavailable: the endpoint failed or timed out.
            InvalidModelResponse: the answer is not a review.
        """
        model_request = self._build_request(request)
        try:
            response = await self._provider.generate(model_request)
        except InvalidModelResponse:
            # The endpoint answered with something unparseable. That is
            # evidence about the reviewer, not an outage, so it keeps its own
            # class and its own policy.
            raise
        except ModelProviderError as error:
            raise ReviewerUnavailable(
                f"Reviewer '{self.config.provider_id}' could not review "
                f"{request.external_task_id}: {error}"
            ) from error

        if response.truncated:
            # A clipped review is not a partial verdict to be salvaged: the
            # issues list may have been cut mid-way, so an "approval" here
            # could simply be the findings not having been reached yet.
            raise InvalidModelResponse(
                f"Reviewer '{self.config.provider_id}' hit its output limit "
                f"while reviewing {request.external_task_id}; the verdict is "
                f"incomplete and was discarded"
            )

        try:
            result = parse_review(
                response.data or {}, external_task_id=request.external_task_id
            )
        except MalformedReview as error:
            raise InvalidModelResponse(
                f"Reviewer '{self.config.provider_id}' did not return a review "
                f"for {request.external_task_id}: {error}"
            ) from error

        return ReviewCall(
            result=ReviewResult(
                decision=result.decision,
                summary=result.summary,
                issues=result.issues,
                confidence=result.confidence,
                risk=result.risk,
                reported_task_id=result.reported_task_id,
                warnings=result.warnings,
                provider_id=response.provider_id,
                model_name=response.model_name,
                duration_ms=response.duration_ms,
            ),
            prompt_text=render_prompt(model_request),
            raw_response=response.raw_text,
            usage=response.usage,
        )

    async def check_connection(self) -> ConnectionReport:
        return await self._provider.check_connection()

    async def aclose(self) -> None:
        await self._provider.aclose()

    def _build_request(self, request: ReviewRequest) -> ModelRequest:
        return ModelRequest(
            system_instructions=self._system_prompt,
            task_instructions=self._render_instructions(request),
            context=request.package.render(),
            schema=StructuredSchema(name="code_review", schema=REVIEW_SCHEMA),
            temperature=self._temperature,
            timeout_seconds=request.timeout_seconds,
            metadata={
                "task": request.external_task_id,
                "purpose": "review",
                "cycle": request.cycle,
                **dict(request.metadata),
            },
        )


def render_prompt(request: ModelRequest) -> str:
    """The prompt exactly as it was sent, as one readable artifact."""
    return "\n\n".join(
        f"=== {message.role.value} ===\n{message.content}"
        for message in request.messages()
    )


def describe_providers(providers: Sequence[ReviewProvider]) -> list[dict[str, object]]:
    """Safe-to-log description of configured reviewers (section 36)."""
    return [provider.config.describe() for provider in providers]


__all__ = [
    "ModelReviewProvider",
    "ReviewCall",
    "ReviewProvider",
    "ReviewRequest",
    "ReviewerUnavailable",
    "render_prompt",
]
