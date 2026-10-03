"""OpenAI-compatible provider (build.md section 13, phase E item 2).

Speaks ``POST /chat/completions`` and ``GET /models``, which is the subset
llama.cpp's server, Ollama and the remote reviewers all implement. Nothing
specific to any one of them is in the domain: server-specific knobs travel in
``ProviderConfig.extra_body`` (section 2).

Some OpenAI models are not served on ``/chat/completions`` at all and require
``POST /responses`` instead. That is a transport difference, not a domain one,
so it is selected by ``ProviderConfig.api_mode`` and absorbed here: the same
``ModelRequest`` goes in and the same ``ModelResponse`` comes out, and no
agent, planner, reviewer or fix loop learns that two APIs exist.

The provider does not retry. A retry is a workflow decision with a ceiling
attached (sections 23 and 25); a provider that quietly retried would spend
that ceiling without the orchestrator knowing, and would turn one timeout
into several.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx

from ..config.logging import get_logger
from .base import (
    ApiMode,
    ConnectionReport,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    TokenUsage,
)
from .errors import (
    InvalidModelResponse,
    ModelRequestRejected,
    ModelTimeout,
    ModelUnavailable,
)
from .structured import parse_structured, strip_reasoning

logger = get_logger(__name__)

#: Connect quickly or not at all: a local endpoint that is down should fail in
#: seconds, while generation itself is allowed the full configured timeout.
_CONNECT_TIMEOUT_SECONDS = 10.0


class OpenAICompatibleProvider:
    """A model served over an OpenAI-compatible HTTP API.

    Args:
        config: endpoint, model name, role, timeout and context window.
        client: injected transport. Tests pass one built on
            ``httpx.MockTransport``; production leaves it unset.
    """

    def __init__(
        self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_seconds, connect=_CONNECT_TIMEOUT_SECONDS),
            headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.config.api_key:
            headers["authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Run one bounded request against the endpoint.

        Raises:
            PromptTooLarge: the prompt cannot fit the served context window.
            ModelTimeout: the endpoint did not answer in time.
            ModelUnavailable: unreachable, overloaded (429) or failing (5xx).
            ModelRequestRejected: the endpoint refused the request (other 4xx).
            InvalidModelResponse: the reply was empty, malformed, or did not
                satisfy the requested schema.
        """
        request.assert_fits(self.config.context_window)
        responses_api = self.config.api_mode is ApiMode.RESPONSES
        path = "/responses" if responses_api else "/chat/completions"
        payload = (
            self._build_responses_payload(request)
            if responses_api
            else self._build_payload(request)
        )
        timeout = request.timeout_seconds or self.config.timeout_seconds

        logger.info(
            "model_request_started",
            provider_id=self.config.provider_id,
            model_name=self.config.model_name,
            api_mode=self.config.api_mode.value,
            estimated_prompt_tokens=request.estimated_prompt_tokens(),
            structured=request.schema is not None,
            timeout_seconds=timeout,
        )
        started = time.monotonic()
        body = await self._post_json(path, payload, timeout=timeout, operation="generate")
        duration_ms = int((time.monotonic() - started) * 1000)

        response = (
            self._to_response_from_responses_api(body, request, duration_ms)
            if responses_api
            else self._to_response(body, request, duration_ms)
        )
        logger.info(
            "model_request_completed",
            provider_id=self.config.provider_id,
            model_name=response.model_name,
            duration_ms=duration_ms,
            finish_reason=response.finish_reason,
            truncated=response.truncated,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages()
            ],
            "temperature": request.temperature,
            "stream": False,
        }
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_output_tokens is not None:
            payload[self.config.max_output_tokens_parameter] = request.max_output_tokens
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.schema is not None:
            # Endpoints that support constrained decoding honour this; those
            # that ignore it still get the schema in the prompt from the
            # agent, and `parse_structured` is the backstop either way.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema.name,
                    "schema": dict(request.schema.schema),
                    "strict": request.schema.strict,
                },
            }
        payload.update(self.config.extra_body)
        return payload

    def _build_responses_payload(self, request: ModelRequest) -> dict[str, Any]:
        """Translate a ``ModelRequest`` into a Responses API request body.

        The mapping is deliberately narrow:

        *   system instructions become ``instructions``;
        *   history, then the assembled task/context/review-feedback message,
            become the ``input`` turns, in the same order
            ``ModelRequest.messages()`` puts them;
        *   ``max_output_tokens`` is the API's own parameter name, so
            ``max_output_tokens_parameter`` does not apply here;
        *   a schema becomes ``text.format``.

        ``temperature``, ``top_p``, ``seed`` and ``stop`` are not sent.
        ``seed`` and ``stop`` have no equivalent in this API, and the
        reasoning models that require it reject the two sampling parameters
        outright -- sending them would make every call a 400. An endpoint that
        does accept them can be given them through ``extra_body``, which is
        where endpoint-specific knobs already live (section 2).
        """
        messages = request.messages()
        payload: dict[str, Any] = {
            "model": self.config.model_name,
            "instructions": messages[0].content,
            "input": [
                {
                    "role": message.role.value,
                    "content": [
                        {
                            "type": "output_text"
                            if message.role is MessageRole.ASSISTANT
                            else "input_text",
                            "text": message.content,
                        }
                    ],
                }
                for message in messages[1:]
            ],
            "stream": False,
        }
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema.name,
                    "schema": dict(request.schema.schema),
                    "strict": request.schema.strict,
                }
            }
        payload.update(self.config.extra_body)
        return payload

    def _to_response_from_responses_api(
        self, body: Mapping[str, Any], request: ModelRequest, duration_ms: int
    ) -> ModelResponse:
        """Normalise a Responses API reply into the one ``ModelResponse``.

        Fails closed: a refused, failed or empty reply is an
        ``InvalidModelResponse`` rather than an empty answer handed on to a
        patch applier.
        """
        status = body.get("status")
        if status in {"failed", "cancelled"}:
            raise InvalidModelResponse(
                f"{self.config.provider_id} returned status '{status}': "
                f"{_responses_error_detail(body)}"
            )

        raw_text, refusal = _responses_output_text(body)
        if refusal is not None and not raw_text.strip():
            raise InvalidModelResponse(
                f"{self.config.provider_id} refused the request: {refusal[:200]}"
            )

        finish_reason = _responses_finish_reason(body)
        text = strip_reasoning(raw_text)
        if not text:
            detail = (
                "Model returned only a reasoning block with no answer"
                if raw_text.strip()
                else "Model returned empty content"
            )
            raise InvalidModelResponse(detail)

        data = None
        if request.schema is not None:
            if finish_reason == "length":
                raise InvalidModelResponse(
                    f"Model response for schema '{request.schema.name}' was truncated "
                    "at the output-token limit"
                )
            data = parse_structured(
                text, request.schema.schema, schema_name=request.schema.name
            )

        return ModelResponse(
            text=text,
            raw_text=raw_text,
            model_name=str(body.get("model") or self.config.model_name),
            provider_id=self.config.provider_id,
            data=data,
            finish_reason=finish_reason,
            usage=_responses_usage_from(body.get("usage")),
            duration_ms=duration_ms,
        )

    def _to_response(
        self, body: Mapping[str, Any], request: ModelRequest, duration_ms: int
    ) -> ModelResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InvalidModelResponse("Model response contained no choices")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise InvalidModelResponse("Model response choice is not an object")

        message = choice.get("message")
        raw_text = ""
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                raw_text = content
            elif isinstance(content, list):
                # Some servers return content parts rather than a string.
                raw_text = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, Mapping) and part.get("type") == "text"
                )

        finish_reason = choice.get("finish_reason")
        text = strip_reasoning(raw_text)
        if not text:
            detail = (
                "Model returned only a reasoning block with no answer"
                if raw_text.strip()
                else "Model returned empty content"
            )
            raise InvalidModelResponse(detail)

        data = None
        if request.schema is not None:
            if finish_reason == "length":
                # Truncated JSON may still parse into a plausible-looking but
                # incomplete object, so refuse it before it can be acted on.
                raise InvalidModelResponse(
                    f"Model response for schema '{request.schema.name}' was truncated "
                    "at the output-token limit"
                )
            data = parse_structured(
                text, request.schema.schema, schema_name=request.schema.name
            )

        return ModelResponse(
            text=text,
            raw_text=raw_text,
            model_name=str(body.get("model") or self.config.model_name),
            provider_id=self.config.provider_id,
            data=data,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            usage=_usage_from(body.get("usage")),
            duration_ms=duration_ms,
        )

    async def check_connection(self) -> ConnectionReport:
        """Probe ``GET /models`` (phase E item 4).

        Never raises: a connection test reports, and the operator decides.
        Inference is not run, so the probe costs nothing and cannot be
        mistaken for a warm-up.
        """
        started = time.monotonic()
        try:
            body = await self._get_json("/models", timeout=_CONNECT_TIMEOUT_SECONDS)
        except ModelTimeout as exc:
            return ConnectionReport(
                provider_id=self.config.provider_id, reachable=False, detail=str(exc)
            )
        except (ModelUnavailable, ModelRequestRejected, InvalidModelResponse) as exc:
            return ConnectionReport(
                provider_id=self.config.provider_id, reachable=False, detail=str(exc)
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        served = _model_ids(body)
        return ConnectionReport(
            provider_id=self.config.provider_id,
            reachable=True,
            # An endpoint that lists nothing is not evidence either way: some
            # single-model servers report an empty list.
            model_available=(self.config.model_name in served) if served else None,
            detail=None
            if not served or self.config.model_name in served
            else f"Endpoint does not serve '{self.config.model_name}'",
            latency_ms=latency_ms,
            available_models=served,
        )

    async def _post_json(
        self, path: str, payload: Mapping[str, Any], *, timeout: float, operation: str
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.post(path, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ModelTimeout(
                f"{self.config.provider_id} did not respond to {operation} "
                f"within {timeout:g}s",
                timeout_seconds=timeout,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                f"{self.config.provider_id} at {self.config.base_url} is unreachable: "
                f"{type(exc).__name__}"
            ) from exc
        return self._read_json(response)

    async def _get_json(self, path: str, *, timeout: float) -> Mapping[str, Any]:
        try:
            response = await self._client.get(path, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ModelTimeout(
                f"{self.config.provider_id} did not respond within {timeout:g}s",
                timeout_seconds=timeout,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                f"{self.config.provider_id} at {self.config.base_url} is unreachable: "
                f"{type(exc).__name__}"
            ) from exc
        return self._read_json(response)

    def _read_json(self, response: httpx.Response) -> Mapping[str, Any]:
        """Map an HTTP reply to either a body or a classified failure.

        The endpoint's error text is summarised but never echoed in full: a
        provider that repeats a request back in its error message is a way for
        prompt content -- and anything in it -- to end up in a log.
        """
        status = response.status_code
        if status == 429:
            raise ModelUnavailable(f"{self.config.provider_id} is rate limited (429)")
        if status >= 500:
            raise ModelUnavailable(
                f"{self.config.provider_id} returned {status} from {response.request.url.path}"
            )
        if status >= 400:
            raise ModelRequestRejected(
                f"{self.config.provider_id} rejected the request with {status}: "
                f"{_error_summary(response)}",
                status_code=status,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise InvalidModelResponse(
                f"{self.config.provider_id} returned a non-JSON body "
                f"({response.headers.get('content-type', 'unknown content type')})"
            ) from exc
        if not isinstance(body, Mapping):
            raise InvalidModelResponse(
                f"{self.config.provider_id} returned {type(body).__name__}, expected an object"
            )
        return body

    async def aclose(self) -> None:
        """Close the transport, unless it was injected by the caller."""
        if self._owns_client:
            await self._client.aclose()


def _usage_from(usage: object) -> TokenUsage:
    if not isinstance(usage, Mapping):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_int_or_none(usage.get("prompt_tokens")),
        output_tokens=_int_or_none(usage.get("completion_tokens")),
    )


def _responses_usage_from(usage: object) -> TokenUsage:
    """Responses API usage, which names its fields differently from chat."""
    if not isinstance(usage, Mapping):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
    )


def _responses_output_text(body: Mapping[str, Any]) -> tuple[str, str | None]:
    """The assistant text and any refusal from a Responses API reply.

    ``output`` is a list of items -- reasoning, tool calls, messages -- and
    only the message items carry an answer. ``output_text`` is accepted when
    the endpoint provides it, because some do and it costs nothing to use.
    """
    direct = body.get("output_text")
    if isinstance(direct, str) and direct:
        return direct, None

    items = body.get("output")
    if not isinstance(items, list):
        return "", None
    parts: list[str] = []
    refusal: str | None = None
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                refusal = part["refusal"]
    return "".join(parts), refusal


def _responses_finish_reason(body: Mapping[str, Any]) -> str | None:
    """Map Responses API completion state onto the chat ``finish_reason``.

    ``"length"`` keeps its meaning across both APIs, which is what
    ``ModelResponse.truncated`` and the structured-output guard rely on.
    """
    status = body.get("status")
    if status == "incomplete":
        details = body.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, Mapping) else None
        if reason == "max_output_tokens":
            return "length"
        return str(reason) if isinstance(reason, str) else "incomplete"
    if status == "completed":
        return "stop"
    return str(status) if isinstance(status, str) else None


def _responses_error_detail(body: Mapping[str, Any], limit: int = 200) -> str:
    error = body.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return str(error["message"])[:limit]
    return "no error detail"


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _model_ids(body: Mapping[str, Any]) -> tuple[str, ...]:
    entries = body.get("data")
    if not isinstance(entries, list):
        return ()
    return tuple(
        str(entry["id"])
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)
    )


def _error_summary(response: httpx.Response, limit: int = 200) -> str:
    """A short, safe excerpt of an error body."""
    try:
        body = response.json()
    except ValueError:
        return "no error detail"
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str):
                return message[:limit]
        if isinstance(error, str):
            return error[:limit]
    return "no error detail"
