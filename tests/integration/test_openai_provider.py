"""The OpenAI-compatible provider against a mock endpoint.

Section 46 calls for a "local model mock provider" test. This mocks the HTTP
transport rather than the provider, so the request body, the error mapping and
the response parsing are all exercised -- a mock provider object would prove
none of them.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import httpx
import pytest

from apps.orchestrator.domain.enums import FailureReason, ModelRole
from apps.orchestrator.domain.models import Model
from apps.orchestrator.providers import (
    ApiMode,
    InvalidModelResponse,
    Message,
    MessageRole,
    ModelRequest,
    ModelRequestRejected,
    ModelTimeout,
    ModelUnavailable,
    OpenAICompatibleProvider,
    PromptTooLarge,
    ProviderConfig,
    StructuredSchema,
    config_from_model,
)

pytestmark = pytest.mark.integration

PLAN_SCHEMA = StructuredSchema(
    name="plan",
    schema={"type": "object", "required": ["filesToModify"]},
)


def completion(
    content: str, *, finish_reason: str = "stop", usage: dict | None = None, model: str = "qwen"
) -> dict:
    body = {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response], **config_overrides
) -> OpenAICompatibleProvider:
    config = ProviderConfig(
        provider_id="test-coder",
        base_url="http://model-host:8080/v1",
        model_name="qwen",
        role=ModelRole.CODER,
        timeout_seconds=30.0,
        **config_overrides,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.base_url,
        headers={"content-type": "application/json"},
    )
    return OpenAICompatibleProvider(config, client=client)


def responds(body: dict, status_code: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status_code, json=body)


@pytest.fixture
def request_() -> ModelRequest:
    return ModelRequest(
        system_instructions="You are a coding agent.",
        task_instructions="TS-001: add a health endpoint.",
    )


# --- The happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bounded_prompt_returns_a_valid_response(request_: ModelRequest) -> None:
    """Phase E exit condition."""
    provider = make_provider(
        responds(
            completion(
                "diff --git a/app.py b/app.py",
                usage={"prompt_tokens": 1200, "completion_tokens": 300},
            )
        )
    )

    response = await provider.generate(request_)

    assert response.text == "diff --git a/app.py b/app.py"
    assert response.model_name == "qwen"
    assert response.provider_id == "test-coder"
    assert response.usage.input_tokens == 1200
    assert response.usage.total == 1500
    assert response.finish_reason == "stop"
    assert response.truncated is False


@pytest.mark.asyncio
async def test_the_request_body_carries_the_configured_model_and_messages(
    request_: ModelRequest,
) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        assert http_request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json=completion("ok"))

    await make_provider(handler).generate(request_)

    assert captured["model"] == "qwen"
    assert captured["stream"] is False
    assert [message["role"] for message in captured["messages"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_sampling_settings_are_passed_through(request_: ModelRequest) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=completion("ok"))

    await make_provider(handler).generate(
        dataclasses.replace(
            request_, temperature=0.0, max_output_tokens=2048, seed=7, stop=("</patch>",)
        )
    )

    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 2048
    assert captured["seed"] == 7
    assert captured["stop"] == ["</patch>"]


@pytest.mark.asyncio
async def test_the_output_limit_parameter_is_configurable_for_openai_reasoning_models(
    request_: ModelRequest,
) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=completion("ok"))

    await make_provider(handler, max_output_tokens_parameter="max_completion_tokens").generate(
        dataclasses.replace(request_, max_output_tokens=2048)
    )

    assert captured["max_completion_tokens"] == 2048
    assert "max_tokens" not in captured


@pytest.mark.asyncio
async def test_endpoint_specific_options_are_merged_into_the_body(
    request_: ModelRequest,
) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=completion("ok"))

    await make_provider(handler, extra_body={"cache_prompt": True}).generate(request_)

    assert captured["cache_prompt"] is True


@pytest.mark.asyncio
async def test_an_api_key_becomes_a_bearer_header_and_is_not_logged() -> None:
    seen: dict[str, str] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["authorization"] = http_request.headers.get("authorization", "")
        return httpx.Response(200, json=completion("ok"))

    config = ProviderConfig(
        provider_id="reviewer",
        base_url="http://reviewer:9000/v1",
        model_name="reviewer-model",
        role=ModelRole.REVIEWER,
        api_key="sk-secret-value",
    )
    provider = OpenAICompatibleProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.base_url,
        headers=provider._headers(),
    )

    await provider.generate(
        ModelRequest(system_instructions="s", task_instructions="Review this diff.")
    )

    assert seen["authorization"] == "Bearer sk-secret-value"
    assert "sk-secret-value" not in str(config.describe())


@pytest.mark.asyncio
async def test_a_local_provider_continues_without_authentication(
    request_: ModelRequest,
) -> None:
    seen: dict[str, str | None] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["authorization"] = http_request.headers.get("authorization")
        return httpx.Response(200, json=completion("ok"))

    await make_provider(handler).generate(request_)

    assert seen["authorization"] is None


@pytest.mark.asyncio
async def test_a_registered_openai_model_uses_the_named_environment_key(
    request_: ModelRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    seen: dict[str, str] = {}
    config = config_from_model(
        Model(
            provider="openai_compatible",
            model_name="openai-reviewer",
            external_model_id="gpt-5-mini",
            role=ModelRole.REVIEWER,
            endpoint="https://api.openai.com/v1",
            metadata={"api_key_env": "OPENAI_API_KEY"},
        )
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["authorization"] = http_request.headers.get("authorization", "")
        seen["path"] = http_request.url.path
        return httpx.Response(200, json=completion("ok", model="gpt-5-mini"))

    provider = OpenAICompatibleProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.base_url,
        headers=provider._headers(),
    )

    response = await provider.generate(request_)

    assert seen == {
        "authorization": "Bearer sk-secret-value",
        "path": "/v1/chat/completions",
    }
    assert response.model_name == "gpt-5-mini"


# --- Structured output -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_schema_request_asks_the_endpoint_to_constrain_decoding(
    request_: ModelRequest,
) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=completion('{"filesToModify": ["a.py"]}'))

    response = await make_provider(handler).generate(
        dataclasses.replace(request_, schema=PLAN_SCHEMA)
    )

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["name"] == "plan"
    assert response.data == {"filesToModify": ["a.py"]}


@pytest.mark.asyncio
async def test_a_reasoning_block_is_stripped_but_kept_in_the_artifact_text(
    request_: ModelRequest,
) -> None:
    """``raw_text`` is what the model said; ``text`` is what the caller acts on."""
    provider = make_provider(
        responds(completion('<think>Which files?</think>\n{"filesToModify": []}'))
    )

    response = await provider.generate(dataclasses.replace(request_, schema=PLAN_SCHEMA))

    assert response.data == {"filesToModify": []}
    assert "<think>" not in response.text
    assert "<think>" in response.raw_text


@pytest.mark.asyncio
async def test_an_answer_that_ignores_the_schema_is_an_invalid_response(
    request_: ModelRequest,
) -> None:
    provider = make_provider(responds(completion("I will edit app.py.")))

    with pytest.raises(InvalidModelResponse) as caught:
        await provider.generate(dataclasses.replace(request_, schema=PLAN_SCHEMA))

    # Evidence the coder can act on, so the policy sends it back rather than
    # retrying the same call.
    assert caught.value.reason is FailureReason.INVALID_MODEL_RESPONSE


@pytest.mark.asyncio
async def test_truncated_structured_output_is_refused_even_if_it_parses(
    request_: ModelRequest,
) -> None:
    """A plan cut short at the token limit can still be valid JSON. Acting on
    it would mean acting on half a plan."""
    provider = make_provider(
        responds(completion('{"filesToModify": ["a.py"]}', finish_reason="length"))
    )

    with pytest.raises(InvalidModelResponse, match="truncated"):
        await provider.generate(dataclasses.replace(request_, schema=PLAN_SCHEMA))


@pytest.mark.asyncio
async def test_truncated_free_text_is_returned_but_flagged(request_: ModelRequest) -> None:
    provider = make_provider(responds(completion("diff --git", finish_reason="length")))

    response = await provider.generate(request_)

    assert response.truncated is True


# --- Failure mapping ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timeout_maps_to_model_timeout(request_: ModelRequest) -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=http_request)

    with pytest.raises(ModelTimeout) as caught:
        await make_provider(handler).generate(request_)

    assert caught.value.reason is FailureReason.MODEL_TIMEOUT
    assert caught.value.timeout_seconds == 30.0


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_maps_to_model_unavailable(
    request_: ModelRequest,
) -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=http_request)

    with pytest.raises(ModelUnavailable) as caught:
        await make_provider(handler).generate(request_)

    assert caught.value.reason is FailureReason.MODEL_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_server_errors_map_to_model_unavailable(
    request_: ModelRequest, status_code: int
) -> None:
    provider = make_provider(responds({"error": "overloaded"}, status_code))

    with pytest.raises(ModelUnavailable):
        await provider.generate(request_)


@pytest.mark.asyncio
async def test_rate_limiting_maps_to_model_unavailable(request_: ModelRequest) -> None:
    with pytest.raises(ModelUnavailable, match="rate limited"):
        await make_provider(responds({}, 429)).generate(request_)


@pytest.mark.asyncio
async def test_a_rejected_request_reports_the_endpoints_reason(
    request_: ModelRequest,
) -> None:
    provider = make_provider(
        responds({"error": {"message": "model 'qwen' not found"}}, 404)
    )

    with pytest.raises(ModelRequestRejected) as caught:
        await provider.generate(request_)

    assert caught.value.status_code == 404
    assert "not found" in str(caught.value)


@pytest.mark.asyncio
async def test_the_prompt_is_never_echoed_into_an_error(request_: ModelRequest) -> None:
    """An error message that repeated the request back would put source code --
    and anything else in the prompt -- into the logs (section 36)."""
    provider = make_provider(responds({"error": {"message": "bad request"}}, 400))

    with pytest.raises(ModelRequestRejected) as caught:
        await provider.generate(
            dataclasses.replace(request_, context="SECRET_TOKEN = 'abc123'")
        )

    assert "SECRET_TOKEN" not in str(caught.value)


@pytest.mark.asyncio
async def test_a_non_json_body_is_an_invalid_response(request_: ModelRequest) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    with pytest.raises(InvalidModelResponse, match="non-JSON"):
        await make_provider(handler).generate(request_)


@pytest.mark.asyncio
async def test_a_response_with_no_choices_is_invalid(request_: ModelRequest) -> None:
    with pytest.raises(InvalidModelResponse, match="no choices"):
        await make_provider(responds({"model": "qwen", "choices": []})).generate(request_)


@pytest.mark.asyncio
async def test_empty_content_is_invalid(request_: ModelRequest) -> None:
    with pytest.raises(InvalidModelResponse, match="empty content"):
        await make_provider(responds(completion(""))).generate(request_)


@pytest.mark.asyncio
async def test_a_reply_that_is_only_reasoning_is_invalid(request_: ModelRequest) -> None:
    provider = make_provider(responds(completion("<think>I am thinking</think>")))

    with pytest.raises(InvalidModelResponse, match="only a reasoning block"):
        await provider.generate(request_)


@pytest.mark.asyncio
async def test_missing_usage_is_recorded_as_unknown(request_: ModelRequest) -> None:
    """An estimated token count in the training data is worse than none."""
    response = await make_provider(responds(completion("ok"))).generate(request_)

    assert response.usage.input_tokens is None
    assert response.usage.total is None


@pytest.mark.asyncio
async def test_the_provider_does_not_retry_on_its_own(request_: ModelRequest) -> None:
    """Retries carry a ceiling the workflow owns (sections 23 and 25)."""
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=http_request)

    with pytest.raises(ModelUnavailable):
        await make_provider(handler).generate(request_)

    assert calls == 1


# --- Connection test ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reachable_endpoint_serving_the_model_is_healthy() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.method == "GET"
        assert http_request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen"}, {"id": "other"}]})

    report = await make_provider(handler).check_connection()

    assert report.healthy is True
    assert report.model_available is True
    assert list(report.available_models) == ["qwen", "other"]


@pytest.mark.asyncio
async def test_an_endpoint_serving_a_different_model_is_reported_unhealthy() -> None:
    """Up, but serving the wrong model: a misconfiguration the operator must
    see before a run starts, not during one."""
    report = await make_provider(responds({"data": [{"id": "llama"}]})).check_connection()

    assert report.reachable is True
    assert report.model_available is False
    assert report.healthy is False
    assert "does not serve" in (report.detail or "")


@pytest.mark.asyncio
async def test_an_endpoint_listing_no_models_is_not_judged() -> None:
    """Some single-model servers list nothing; that is not evidence of absence."""
    report = await make_provider(responds({"data": []})).check_connection()

    assert report.reachable is True
    assert report.model_available is None
    assert report.healthy is True


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_reports_rather_than_raises() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=http_request)

    report = await make_provider(handler).check_connection()

    assert report.reachable is False
    assert report.healthy is False
    assert report.detail


@pytest.mark.asyncio
async def test_the_context_window_guard_runs_before_the_request_is_sent(
    request_: ModelRequest,
) -> None:
    """No call is made at all: an oversized prompt is the orchestrator's bug,
    and sending it would just burn a slow inference to be told so."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=completion("ok"))

    provider = make_provider(handler, context_window=4096)

    with pytest.raises(PromptTooLarge):
        await provider.generate(dataclasses.replace(request_, context="x" * 100_000))

    assert calls == 0


# --- The Responses API transport ---------------------------------------------
#
# A second generation API behind the same provider boundary. Every test here
# asserts that the difference stops at the boundary: the request is still a
# `ModelRequest` and the result is still a `ModelResponse`.


def responses_body(
    text: str,
    *,
    status: str = "completed",
    usage: dict | None = None,
    model: str = "gpt-5.3-codex",
    incomplete_details: dict | None = None,
) -> dict:
    body: dict = {
        "id": "resp_1",
        "object": "response",
        "model": model,
        "status": status,
        "output": [
            {"id": "rs_1", "type": "reasoning", "summary": []},
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": status,
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
    }
    if usage is not None:
        body["usage"] = usage
    if incomplete_details is not None:
        body["incomplete_details"] = incomplete_details
    return body


def make_responses_provider(
    handler: Callable[[httpx.Request], httpx.Response], **config_overrides
) -> OpenAICompatibleProvider:
    return make_provider(handler, api_mode=ApiMode.RESPONSES, **config_overrides)


@pytest.mark.asyncio
async def test_the_default_api_mode_still_posts_to_chat_completions(
    request_: ModelRequest,
) -> None:
    """Local llama.cpp/Qwen providers must be untouched by the new option."""
    seen: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["path"] = http_request.url.path
        seen["body"] = json.loads(http_request.content)
        return httpx.Response(200, json=completion("ok"))

    provider = make_provider(handler)

    assert provider.config.api_mode is ApiMode.CHAT_COMPLETIONS
    await provider.generate(request_)

    assert seen["path"] == "/v1/chat/completions"
    assert "messages" in seen["body"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_responses_mode_posts_to_the_responses_endpoint(
    request_: ModelRequest,
) -> None:
    seen: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["path"] = http_request.url.path
        seen["body"] = json.loads(http_request.content)
        return httpx.Response(200, json=responses_body("ok"))

    await make_responses_provider(handler).generate(request_)

    assert seen["path"] == "/v1/responses"
    assert "messages" not in seen["body"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_the_responses_request_carries_the_whole_model_request(
    request_: ModelRequest,
) -> None:
    """System, history, task, context and review feedback all survive the
    translation, in the order `ModelRequest.messages()` fixes."""
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=responses_body("ok"))

    await make_responses_provider(handler).generate(
        dataclasses.replace(
            request_,
            context="def health(): ...",
            review_feedback="Missing a test.",
            history=(Message(MessageRole.ASSISTANT, "Earlier attempt."),),
            max_output_tokens=4096,
        )
    )

    assert captured["model"] == "qwen"
    assert captured["instructions"] == "You are a coding agent."
    assert captured["max_output_tokens"] == 4096
    assert [turn["role"] for turn in captured["input"]] == ["assistant", "user"]
    assert captured["input"][0]["content"][0]["type"] == "output_text"
    user_text = captured["input"][1]["content"][0]["text"]
    assert captured["input"][1]["content"][0]["type"] == "input_text"
    assert "TS-001: add a health endpoint." in user_text
    assert "def health(): ..." in user_text
    assert "Missing a test." in user_text


@pytest.mark.asyncio
async def test_responses_mode_omits_parameters_that_api_rejects(
    request_: ModelRequest,
) -> None:
    """`seed` and `stop` do not exist in this API, and the reasoning models
    that need it reject the sampling knobs. Sending them would 400 every
    call; an endpoint that accepts them can get them via `extra_body`."""
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=responses_body("ok"))

    await make_responses_provider(handler, extra_body={"reasoning": {"effort": "high"}}).generate(
        dataclasses.replace(request_, temperature=0.9, top_p=0.5, seed=7, stop=("</patch>",))
    )

    assert "temperature" not in captured
    assert "top_p" not in captured
    assert "seed" not in captured
    assert "stop" not in captured
    assert "max_tokens" not in captured
    assert captured["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_a_responses_reply_becomes_an_ordinary_model_response(
    request_: ModelRequest,
) -> None:
    provider = make_responses_provider(
        responds(
            responses_body(
                "diff --git a/app.py b/app.py",
                usage={"input_tokens": 1200, "output_tokens": 300},
            )
        )
    )

    response = await provider.generate(request_)

    assert response.text == "diff --git a/app.py b/app.py"
    assert response.raw_text == "diff --git a/app.py b/app.py"
    assert response.model_name == "gpt-5.3-codex"
    assert response.provider_id == "test-coder"
    assert response.finish_reason == "stop"
    assert response.truncated is False
    assert response.duration_ms >= 0


@pytest.mark.asyncio
async def test_responses_token_usage_is_mapped_onto_token_usage(
    request_: ModelRequest,
) -> None:
    """This API names the fields differently from chat completions."""
    provider = make_responses_provider(
        responds(responses_body("ok", usage={"input_tokens": 1200, "output_tokens": 300}))
    )

    response = await provider.generate(request_)

    assert response.usage.input_tokens == 1200
    assert response.usage.output_tokens == 300
    assert response.usage.total == 1500


@pytest.mark.asyncio
async def test_missing_responses_usage_is_recorded_as_unknown(
    request_: ModelRequest,
) -> None:
    response = await make_responses_provider(responds(responses_body("ok"))).generate(request_)

    assert response.usage.input_tokens is None
    assert response.usage.total is None


@pytest.mark.asyncio
async def test_an_output_text_shortcut_is_used_when_the_endpoint_sends_one(
    request_: ModelRequest,
) -> None:
    body = responses_body("ignored")
    body["output_text"] = "the answer"

    response = await make_responses_provider(responds(body)).generate(request_)

    assert response.text == "the answer"


@pytest.mark.asyncio
async def test_a_truncated_responses_reply_is_flagged_as_length(
    request_: ModelRequest,
) -> None:
    provider = make_responses_provider(
        responds(
            responses_body(
                "diff --git",
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            )
        )
    )

    response = await provider.generate(request_)

    assert response.finish_reason == "length"
    assert response.truncated is True


# --- Responses API structured output -----------------------------------------


@pytest.mark.asyncio
async def test_a_responses_schema_request_asks_for_constrained_decoding(
    request_: ModelRequest,
) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(200, json=responses_body('{"filesToModify": ["a.py"]}'))

    response = await make_responses_provider(handler).generate(
        dataclasses.replace(request_, schema=PLAN_SCHEMA)
    )

    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["name"] == "plan"
    assert captured["text"]["format"]["schema"] == {
        "type": "object",
        "required": ["filesToModify"],
    }
    assert response.data == {"filesToModify": ["a.py"]}


@pytest.mark.asyncio
async def test_a_malformed_responses_schema_answer_fails_closed(
    request_: ModelRequest,
) -> None:
    provider = make_responses_provider(responds(responses_body("I will edit app.py.")))

    with pytest.raises(InvalidModelResponse) as caught:
        await provider.generate(dataclasses.replace(request_, schema=PLAN_SCHEMA))

    assert caught.value.reason is FailureReason.INVALID_MODEL_RESPONSE


@pytest.mark.asyncio
async def test_truncated_responses_structured_output_is_refused(
    request_: ModelRequest,
) -> None:
    provider = make_responses_provider(
        responds(
            responses_body(
                '{"filesToModify": ["a.py"]}',
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            )
        )
    )

    with pytest.raises(InvalidModelResponse, match="truncated"):
        await provider.generate(dataclasses.replace(request_, schema=PLAN_SCHEMA))


# --- Responses API failure mapping -------------------------------------------


@pytest.mark.asyncio
async def test_a_responses_refusal_is_an_invalid_response(request_: ModelRequest) -> None:
    body = responses_body("")
    body["output"][1]["content"] = [{"type": "refusal", "refusal": "I cannot help."}]

    with pytest.raises(InvalidModelResponse, match="refused"):
        await make_responses_provider(responds(body)).generate(request_)


@pytest.mark.asyncio
async def test_a_failed_responses_status_is_an_invalid_response(
    request_: ModelRequest,
) -> None:
    body = responses_body("", status="failed")
    body["error"] = {"message": "server had a problem"}

    with pytest.raises(InvalidModelResponse, match="failed"):
        await make_responses_provider(responds(body)).generate(request_)


@pytest.mark.asyncio
async def test_an_empty_responses_output_is_an_invalid_response(
    request_: ModelRequest,
) -> None:
    with pytest.raises(InvalidModelResponse, match="empty content"):
        await make_responses_provider(responds(responses_body(""))).generate(request_)


@pytest.mark.asyncio
async def test_responses_mode_maps_transport_failures_the_same_way(
    request_: ModelRequest,
) -> None:
    def timing_out(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=http_request)

    with pytest.raises(ModelTimeout):
        await make_responses_provider(timing_out).generate(request_)

    with pytest.raises(ModelRequestRejected) as caught:
        await make_responses_provider(
            responds({"error": {"message": "unsupported model"}}, 404)
        ).generate(request_)
    assert caught.value.status_code == 404

    with pytest.raises(ModelUnavailable):
        await make_responses_provider(responds({}, 429)).generate(request_)


@pytest.mark.asyncio
async def test_responses_mode_does_not_retry_on_its_own(request_: ModelRequest) -> None:
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused", request=http_request)

    with pytest.raises(ModelUnavailable):
        await make_responses_provider(handler).generate(request_)

    assert calls == 1


@pytest.mark.asyncio
async def test_responses_mode_still_authenticates_without_logging_the_key() -> None:
    seen: dict[str, str] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen["authorization"] = http_request.headers.get("authorization", "")
        seen["path"] = http_request.url.path
        return httpx.Response(200, json=responses_body("ok"))

    config = ProviderConfig(
        provider_id="openai-coder",
        base_url="https://api.openai.com/v1",
        model_name="gpt-5.3-codex",
        role=ModelRole.CODER,
        api_key="sk-secret-value",
        api_mode=ApiMode.RESPONSES,
    )
    provider = OpenAICompatibleProvider(config)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.base_url,
        headers=provider._headers(),
    )

    await provider.generate(
        ModelRequest(system_instructions="s", task_instructions="Write a patch.")
    )

    assert seen == {"authorization": "Bearer sk-secret-value", "path": "/v1/responses"}
    assert "sk-secret-value" not in str(config.describe())
    assert "sk-secret-value" not in repr(config)
    assert config.describe()["api_mode"] == "responses"


@pytest.mark.asyncio
async def test_the_connection_probe_is_unchanged_in_responses_mode() -> None:
    """`GET /models` is the same on both transports; only generation differs."""

    def handler(http_request: httpx.Request) -> httpx.Response:
        assert http_request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})

    report = await make_responses_provider(handler).check_connection()

    assert report.healthy is True


@pytest.mark.asyncio
async def test_the_context_window_guard_runs_in_responses_mode_too(
    request_: ModelRequest,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=responses_body("ok"))

    provider = make_responses_provider(handler, context_window=4096)

    with pytest.raises(PromptTooLarge):
        await provider.generate(dataclasses.replace(request_, context="x" * 100_000))

    assert calls == 0
