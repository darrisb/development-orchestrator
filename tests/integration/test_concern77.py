"""Concern 77: OpenAI strict structured output vs. the canonical edit schema.

A real smoke test against OpenAI's Responses API returned HTTP 400 before any
token was generated::

    Invalid schema for response_format 'code_edits': In
    context=('properties', 'edits', 'items'), 'required' is required to be
    supplied and to be an array including every key in properties. Missing
    'oldText'...

``oldText``/``newText`` are optional in the canonical schema on purpose
(concern 70): they are valid only for ``operation: "replace"``, and their
presence on any other operation is a parse rejection. The fix is therefore a
transport adaptation at the OpenAI Responses boundary, not a change to the
contract -- these tests pin both halves of it, and that the canonical schema
and the chat-completions path are untouched.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Mapping

import httpx
import pytest

from apps.orchestrator.domain.edits import (
    EDIT_SCHEMA,
    CodeChangeSet,
    EditOperation,
    MalformedChangeSet,
)
from apps.orchestrator.domain.enums import ModelRole
from apps.orchestrator.providers import (
    ApiMode,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfig,
    StructuredSchema,
)
from apps.orchestrator.providers.strict_schema import (
    normalise_strict_payload,
    to_strict_schema,
)

pytestmark = pytest.mark.integration

CODE_EDITS = StructuredSchema(name="code_edits", schema=EDIT_SCHEMA)


# --- Helpers -----------------------------------------------------------------


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response], *, api_mode: ApiMode
) -> OpenAICompatibleProvider:
    config = ProviderConfig(
        provider_id="test-coder",
        base_url="http://model-host:8080/v1",
        model_name="gpt-5",
        role=ModelRole.CODER,
        api_mode=api_mode,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.base_url,
        headers={"content-type": "application/json"},
    )
    return OpenAICompatibleProvider(config, client=client)


def responses_body(text: str) -> dict:
    return {
        "id": "resp-1",
        "model": "gpt-5",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


def chat_body(text: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "gpt-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.fixture
def request_() -> ModelRequest:
    return ModelRequest(
        system_instructions="You are a coding agent.",
        task_instructions="TS-001: add a health endpoint.",
        schema=CODE_EDITS,
    )


def every_object(node: object) -> list[Mapping[str, object]]:
    """Every object schema declaring ``properties``, at any depth."""
    found: list[Mapping[str, object]] = []
    if isinstance(node, Mapping):
        if isinstance(node.get("properties"), Mapping):
            found.append(node)
        for value in node.values():
            found.extend(every_object(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(every_object(item))
    return found


# --- The canonical schema is the canonical schema ----------------------------


def test_the_canonical_edit_schema_is_unchanged():
    """The contract local endpoints decode against still has its own shape."""
    item = EDIT_SCHEMA["properties"]["edits"]["items"]  # type: ignore[index]

    assert EDIT_SCHEMA["required"] == ["summary", "edits"]
    assert item["required"] == ["path", "operation", "content"]  # type: ignore[index]
    assert "oldText" in item["properties"]  # type: ignore[operator]
    assert "newText" in item["properties"]  # type: ignore[operator]
    assert item["properties"]["oldText"] == {"type": "string"}  # type: ignore[index]


def test_the_transformation_does_not_mutate_its_input():
    before = copy.deepcopy(EDIT_SCHEMA)

    to_strict_schema(EDIT_SCHEMA)

    assert before == EDIT_SCHEMA


def test_the_strict_schema_is_a_copy_all_the_way_down():
    strict = to_strict_schema(EDIT_SCHEMA)

    assert strict is not EDIT_SCHEMA
    assert strict["properties"] is not EDIT_SCHEMA["properties"]
    strict["properties"]["edits"]["items"]["properties"]["path"]["type"] = "integer"
    assert (
        EDIT_SCHEMA["properties"]["edits"]["items"]["properties"]["path"]  # type: ignore[index]
        == {"type": "string"}
    )


# --- What OpenAI strict mode demands -----------------------------------------


def test_every_object_in_the_strict_schema_requires_every_property():
    """The exact rule the 400 cited, checked recursively rather than for one key."""
    strict = to_strict_schema(EDIT_SCHEMA)

    objects = every_object(strict)
    assert len(objects) >= 2  # the top level and the edit item
    for node in objects:
        assert set(node["required"]) == set(node["properties"])  # type: ignore[arg-type]
        assert list(node["required"]) == list(node["properties"])  # type: ignore[arg-type]
        assert node["additionalProperties"] is False


def test_optional_properties_become_nullable_and_required_ones_do_not():
    strict = to_strict_schema(EDIT_SCHEMA)
    item = strict["properties"]["edits"]["items"]
    properties = item["properties"]

    # Canonically required: unchanged types.
    assert properties["path"] == {"type": "string"}
    assert properties["content"] == {"type": "string"}
    assert properties["operation"]["type"] == "string"
    assert properties["operation"]["enum"] == [op.value for op in EditOperation]
    # Canonically optional: required on the wire, but allowed to be null.
    assert properties["oldText"] == {"type": ["string", "null"]}
    assert properties["newText"] == {"type": ["string", "null"]}
    assert "oldText" in item["required"]
    assert "newText" in item["required"]


def test_the_optional_top_level_properties_are_handled_too():
    """Not just the first validation error: every optional key, including arrays."""
    strict = to_strict_schema(EDIT_SCHEMA)
    top = strict["properties"]

    for name in ("requirementsMet", "testsAdded", "followUps", "deviationsFromPlan"):
        assert top[name]["type"] == ["array", "null"], name
        # The array's own item schema survives the widening.
        assert top[name]["items"] == {"type": "string"}, name
        assert name in strict["required"]
    assert top["summary"] == {"type": "string"}
    assert strict["properties"]["edits"]["type"] == "array"


def test_an_object_schema_without_properties_is_left_alone():
    """A structural object has nothing to require; a planner schema still works."""
    plan = {"type": "object", "required": ["filesToModify"]}

    assert to_strict_schema(plan) == plan


def test_nested_objects_are_transformed_at_any_depth():
    schema = {
        "type": "object",
        "required": ["outer"],
        "properties": {
            "outer": {
                "type": "object",
                "required": ["kept"],
                "properties": {
                    "kept": {"type": "string"},
                    "maybe": {"type": "integer"},
                    "deeper": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [],
                            "properties": {"x": {"type": "boolean"}},
                        },
                    },
                },
            },
            "spare": {"type": "string"},
        },
    }

    strict = to_strict_schema(schema)

    outer = strict["properties"]["outer"]
    assert outer["required"] == ["kept", "maybe", "deeper"]
    assert outer["properties"]["maybe"]["type"] == ["integer", "null"]
    assert outer["properties"]["deeper"]["type"] == ["array", "null"]
    deepest = outer["properties"]["deeper"]["items"]
    assert deepest["required"] == ["x"]
    assert deepest["properties"]["x"]["type"] == ["boolean", "null"]
    assert deepest["additionalProperties"] is False
    assert strict["properties"]["spare"]["type"] == ["string", "null"]


def test_an_explicit_additional_properties_setting_is_preserved():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["a"],
        "properties": {"a": {"type": "string"}},
    }

    assert to_strict_schema(schema)["additionalProperties"] is False


# --- Normalising the reply back to the canonical shape -----------------------


def test_null_optional_transport_values_normalise_away():
    payload = {
        "summary": "done",
        "edits": [
            {
                "path": "app.py",
                "operation": "create",
                "content": "print('hi')\n",
                "oldText": None,
                "newText": None,
            }
        ],
        "requirementsMet": None,
        "testsAdded": None,
        "followUps": None,
        "deviationsFromPlan": None,
    }

    normalised = normalise_strict_payload(payload, EDIT_SCHEMA)

    assert normalised == {
        "summary": "done",
        "edits": [
            {"path": "app.py", "operation": "create", "content": "print('hi')\n"}
        ],
    }
    # And the input survived it.
    assert payload["edits"][0]["oldText"] is None


def test_normalisation_makes_a_create_acceptable_to_concern_70():
    """The failure this exists to prevent: a null placeholder read as a value."""
    payload = {
        "summary": "done",
        "edits": [
            {
                "path": "app.py",
                "operation": "create",
                "content": "x = 1\n",
                "oldText": None,
                "newText": None,
            }
        ],
    }

    # Unnormalised, the canonical parser rightly refuses it...
    with pytest.raises(MalformedChangeSet):
        CodeChangeSet.from_payload(payload)

    # ...and after normalisation it is an ordinary create.
    change_set = CodeChangeSet.from_payload(normalise_strict_payload(payload, EDIT_SCHEMA))
    assert change_set.edits[0].operation is EditOperation.CREATE
    assert change_set.edits[0].old_text is None


def test_replace_strings_survive_normalisation():
    payload = {
        "summary": "targeted",
        "edits": [
            {
                "path": "app.py",
                "operation": "replace",
                "content": "",
                "oldText": "old line\n",
                "newText": "new line\n",
            }
        ],
        "requirementsMet": ["TS-001"],
        "testsAdded": None,
        "followUps": [],
        "deviationsFromPlan": None,
    }

    change_set = CodeChangeSet.from_payload(normalise_strict_payload(payload, EDIT_SCHEMA))

    edit = change_set.edits[0]
    assert edit.operation is EditOperation.REPLACE
    assert edit.old_text == "old line\n"
    assert edit.new_text == "new line\n"
    assert change_set.requirements_met == ("TS-001",)
    assert change_set.tests_added == ()


def test_an_empty_new_text_is_not_treated_as_absence():
    """'' removes the matched text and is a value, not a placeholder."""
    payload = {
        "summary": "deletion",
        "edits": [
            {
                "path": "app.py",
                "operation": "replace",
                "content": "",
                "oldText": "dead code\n",
                "newText": "",
            }
        ],
    }

    change_set = CodeChangeSet.from_payload(normalise_strict_payload(payload, EDIT_SCHEMA))

    assert change_set.edits[0].new_text == ""


def test_a_null_under_a_canonically_required_key_is_left_for_the_domain():
    """Normalisation hides absences, never model errors: this must still fail."""
    payload = {
        "summary": "broken",
        "edits": [{"path": "app.py", "operation": "replace", "content": None,
                   "oldText": None, "newText": None}],
    }

    normalised = normalise_strict_payload(payload, EDIT_SCHEMA)

    assert normalised["edits"][0]["content"] is None
    with pytest.raises(MalformedChangeSet):
        CodeChangeSet.from_payload(normalised)


def test_normalisation_leaves_unknown_keys_and_scalars_alone():
    payload = {"summary": "x", "edits": [], "surprise": None, "other": 3}

    assert normalise_strict_payload(payload, EDIT_SCHEMA) == payload


# --- The provider boundary ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_responses_request_sends_the_strict_transport_schema(
    request_: ModelRequest,
) -> None:
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(
            200,
            json=responses_body(
                json.dumps(
                    {
                        "summary": "s",
                        "edits": [
                            {
                                "path": "a.py",
                                "operation": "create",
                                "content": "x = 1\n",
                                "oldText": None,
                                "newText": None,
                            }
                        ],
                        "requirementsMet": None,
                        "testsAdded": None,
                        "followUps": None,
                        "deviationsFromPlan": None,
                    }
                )
            ),
        )

    response = await make_provider(handler, api_mode=ApiMode.RESPONSES).generate(request_)

    wire = captured["text"]["format"]["schema"]
    assert wire == to_strict_schema(EDIT_SCHEMA)
    assert wire != EDIT_SCHEMA
    item = wire["properties"]["edits"]["items"]
    assert set(item["required"]) == set(item["properties"])

    # ...and what comes back out is the canonical shape, straight into the domain.
    assert response.data == {
        "summary": "s",
        "edits": [{"path": "a.py", "operation": "create", "content": "x = 1\n"}],
    }
    assert CodeChangeSet.from_payload(response.data).edits[0].path == "a.py"


@pytest.mark.asyncio
async def test_a_non_strict_responses_schema_is_sent_as_written(
    request_: ModelRequest,
) -> None:
    """``strict=False`` asks for no enforcement, so there is nothing to adapt."""
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(
            200,
            json=responses_body(
                json.dumps({"summary": "s", "edits": [], "testsAdded": None})
            ),
        )

    lenient = dataclasses.replace(
        request_, schema=StructuredSchema(name="code_edits", schema=EDIT_SCHEMA, strict=False)
    )
    response = await make_provider(handler, api_mode=ApiMode.RESPONSES).generate(lenient)

    assert captured["text"]["format"]["schema"] == EDIT_SCHEMA
    assert response.data["testsAdded"] is None


@pytest.mark.asyncio
async def test_the_chat_completions_path_is_unchanged(request_: ModelRequest) -> None:
    """Local endpoints decode against the canonical schema, exactly as before."""
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(
            200,
            json=chat_body(
                json.dumps(
                    {
                        "summary": "s",
                        "edits": [
                            {"path": "a.py", "operation": "create", "content": "x = 1\n"}
                        ],
                    }
                )
            ),
        )

    response = await make_provider(handler, api_mode=ApiMode.CHAT_COMPLETIONS).generate(
        request_
    )

    sent = captured["response_format"]["json_schema"]["schema"]
    assert sent == EDIT_SCHEMA
    assert sent["properties"]["edits"]["items"]["required"] == [
        "path",
        "operation",
        "content",
    ]
    assert "oldText" not in response.data["edits"][0]


@pytest.mark.asyncio
async def test_unsafe_edits_through_the_strict_transport_still_fail_closed(
    request_: ModelRequest,
) -> None:
    """Normalisation is a spelling change, not a relaxation of any guard."""
    provider = make_provider(
        responds := lambda _r: httpx.Response(
            200,
            json=responses_body(
                json.dumps(
                    {
                        "summary": "escape",
                        "edits": [
                            {
                                "path": "../../etc/passwd",
                                "operation": "update",
                                "content": "root\n",
                                "oldText": None,
                                "newText": None,
                            }
                        ],
                        "requirementsMet": None,
                        "testsAdded": None,
                        "followUps": None,
                        "deviationsFromPlan": None,
                    }
                )
            ),
        ),
        api_mode=ApiMode.RESPONSES,
    )
    assert responds is not None

    response = await provider.generate(request_)

    with pytest.raises(MalformedChangeSet):
        CodeChangeSet.from_payload(response.data)
