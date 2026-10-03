"""The model provider contract (build.md section 13).

One request type, one response type, one protocol. The workflow builds a
``ModelRequest`` without knowing which endpoint will serve it, and reads a
``ModelResponse`` without knowing which one did.

Nothing here imports httpx, an LLM SDK, FastAPI or SQLAlchemy: a provider is
a boundary, and this module is the shape of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..domain.enums import ModelRole

# Estimation is imported, not reimplemented: the pre-flight guard here and the
# context builder's budget must measure a prompt the same way (domain.tokens).
from ..domain.tokens import estimate_tokens_from_characters
from .errors import PromptTooLarge

#: Share of the context window reserved for the completion when a request
#: does not say. A coding answer is large, so leaving only a sliver would
#: produce truncated patches that look like model defects.
DEFAULT_OUTPUT_RESERVE = 0.25


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class StructuredSchema:
    """A JSON schema the response must satisfy (section 13, section 14 plans).

    Args:
        name: schema name sent to the endpoint; also used in error messages.
        schema: a JSON Schema object. Only the top-level ``required`` and
            ``type`` are enforced locally -- the endpoint does the rest when
            it supports constrained decoding, and the caller's Pydantic model
            does the rest when it does not.
        strict: ask the endpoint to enforce the schema during decoding.
    """

    name: str
    schema: Mapping[str, object]
    strict: bool = True


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """A bounded unit of work for a model.

    The field order is the prompt order: system instructions, then the task,
    then repository context, then prior review feedback. Assembly lives here
    rather than in each provider so that two providers cannot disagree about
    what the coder was actually asked.
    """

    system_instructions: str
    task_instructions: str
    #: Bounded repository context from the context builder (phase F).
    context: str | None = None
    #: Findings from the previous review cycle, for a fix attempt (section 23).
    review_feedback: str | None = None
    #: Prior turns, oldest first. Empty for a fresh attempt.
    history: Sequence[Message] = ()
    schema: StructuredSchema | None = None
    temperature: float = 0.2
    top_p: float | None = None
    max_output_tokens: int | None = None
    #: Passed through when the endpoint supports it. Determinism is worth
    #: asking for even where it cannot be guaranteed.
    seed: int | None = None
    stop: Sequence[str] = ()
    #: Overrides the provider's configured timeout for this one call.
    timeout_seconds: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def messages(self) -> list[Message]:
        """Assemble the wire messages in a fixed, auditable order."""
        messages = [Message(MessageRole.SYSTEM, self.system_instructions)]
        messages.extend(self.history)
        sections = [self.task_instructions]
        if self.context:
            sections.append(f"# Repository context\n\n{self.context}")
        if self.review_feedback:
            sections.append(f"# Review feedback to address\n\n{self.review_feedback}")
        messages.append(Message(MessageRole.USER, "\n\n".join(sections)))
        return messages

    def estimated_prompt_tokens(self) -> int:
        """Rough prompt size, for the pre-flight guard and for logging."""
        characters = sum(len(message.content) for message in self.messages())
        return estimate_tokens_from_characters(characters)

    def assert_fits(self, context_window: int | None) -> None:
        """Raise ``PromptTooLarge`` if the prompt cannot fit with room to answer.

        Raises:
            PromptTooLarge: the estimated prompt plus the reserved completion
                exceeds ``context_window``.
        """
        if not context_window:
            return
        reserve = self.max_output_tokens or int(context_window * DEFAULT_OUTPUT_RESERVE)
        estimated = self.estimated_prompt_tokens()
        if estimated + reserve > context_window:
            raise PromptTooLarge(
                estimated_tokens=estimated + reserve, context_window=context_window
            )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Reported usage. ``None`` where the endpoint does not report it -- an
    unknown count is recorded as unknown rather than estimated into the
    training data (section 34)."""

    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def total(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What every provider returns, whatever it talked to.

    Attributes:
        text: the assistant message with any reasoning block removed.
        raw_text: exactly what the endpoint returned, for the run artifact.
        data: the parsed object when the request carried a schema.
        finish_reason: endpoint-reported stop reason; ``"length"`` means the
            answer was truncated and must not be treated as complete.
        model_name: the model the endpoint says served the request, which is
            not always the one that was asked for.
    """

    text: str
    raw_text: str
    model_name: str
    provider_id: str
    data: Mapping[str, object] | None = None
    finish_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    duration_ms: int = 0

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider configuration (the record listed in section 13).

    Attributes:
        provider_id: stable identifier recorded against every attempt.
        base_url: OpenAI-compatible root, e.g. ``http://host:8080/v1``.
        model_name: the name sent to the endpoint.
        role: what this provider is allowed to be used for.
        api_key: never logged, never serialised, never sent to a reviewer.
        api_key_env: the environment variable name required to provide
            ``api_key``. Safe to keep in configuration; the secret value is not.
        context_window: the *served* window, not the model's trained maximum.
        enabled: a disabled provider is never selected and never probed.
        max_output_tokens_parameter: wire parameter used for
            ``ModelRequest.max_output_tokens``. Defaults to OpenAI-compatible
            servers' historical ``max_tokens``; newer OpenAI reasoning models
            can opt into ``max_completion_tokens`` via model metadata.
    """

    provider_id: str
    base_url: str
    model_name: str
    role: ModelRole
    api_key: str | None = field(default=None, repr=False)
    api_key_env: str | None = field(default=None, repr=False)
    timeout_seconds: float = 600.0
    context_window: int | None = None
    enabled: bool = True
    max_output_tokens_parameter: str = "max_tokens"
    #: Endpoint-specific extras merged into the request body (e.g. llama.cpp's
    #: ``cache_prompt``). Kept out of the domain: section 2 forbids making one
    #: server's concepts part of the model.
    extra_body: Mapping[str, object] = field(default_factory=dict)

    def describe(self) -> dict[str, object]:
        """Safe-to-log description (section 36: no credentials in logs)."""
        return {
            "provider_id": self.provider_id,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "role": self.role.value,
            "context_window": self.context_window,
            "enabled": self.enabled,
            "authenticated": bool(self.api_key),
        }


@runtime_checkable
class ModelProvider(Protocol):
    """The interface from section 13. Implementations must be replaceable."""

    config: ProviderConfig

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Run one bounded request.

        Raises:
            ModelProviderError: any failure, classified by ``FailureReason``.
        """
        ...

    async def check_connection(self) -> ConnectionReport:
        """Probe the endpoint without running inference."""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...


@dataclass(frozen=True, slots=True)
class ConnectionReport:
    """Result of a connection test (phase E item 4).

    ``reachable`` and ``model_available`` are separate on purpose: an endpoint
    that is up but serving a different model is a misconfiguration the
    operator must see before a run starts, not a runtime surprise.
    """

    provider_id: str
    reachable: bool
    model_available: bool | None = None
    detail: str | None = None
    latency_ms: int | None = None
    available_models: Sequence[str] = ()

    @property
    def healthy(self) -> bool:
        return self.reachable and self.model_available is not False
