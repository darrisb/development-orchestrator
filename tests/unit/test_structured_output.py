"""Recovering a validated object from model text (build.md phase E item 5)."""

from __future__ import annotations

import pytest

from apps.orchestrator.providers.errors import InvalidModelResponse
from apps.orchestrator.providers.structured import (
    extract_json_object,
    parse_structured,
    strip_reasoning,
)

PLAN_SCHEMA = {
    "type": "object",
    "required": ["filesToModify", "approach"],
    "properties": {
        "filesToModify": {"type": "array"},
        "approach": {"type": "array"},
    },
}


def parse(text: str) -> dict:
    return parse_structured(text, PLAN_SCHEMA, schema_name="plan")


def test_reasoning_blocks_are_removed() -> None:
    text = "<think>The user wants JSON.</think>\n{\"a\": 1}"
    assert strip_reasoning(text) == '{"a": 1}'


def test_an_unclosed_reasoning_block_leaves_no_answer() -> None:
    """A cut-off reasoning block means the answer never arrived; keeping the
    tail would hand the caller the model's deliberation as its output."""
    assert strip_reasoning("Answer:\n<think>still thinking and then cut") == "Answer:"


def test_json_inside_a_markdown_fence_is_found() -> None:
    parsed = parse('Here is the plan:\n```json\n{"filesToModify": [], "approach": []}\n```\nDone.')
    assert parsed == {"filesToModify": [], "approach": []}


def test_json_surrounded_by_commentary_is_found() -> None:
    parsed = parse('Sure! {"filesToModify": ["a.py"], "approach": ["edit"]} Hope that helps.')
    assert parsed["filesToModify"] == ["a.py"]


def test_braces_inside_strings_do_not_end_the_document() -> None:
    parsed = parse('{"filesToModify": ["a.py"], "approach": ["use f\\"{x}\\" here }"]}')
    assert parsed["approach"] == ['use f"{x}" here }']


def test_nested_objects_are_kept_whole() -> None:
    document = extract_json_object('prefix {"a": {"b": {"c": 1}}} suffix')
    assert document == '{"a": {"b": {"c": 1}}}'


def test_a_missing_required_field_is_invalid() -> None:
    with pytest.raises(InvalidModelResponse, match="missing required field"):
        parse('{"approach": []}')

    with pytest.raises(InvalidModelResponse, match="filesToModify"):
        parse('{"approach": []}')


def test_malformed_json_reports_where_it_broke() -> None:
    with pytest.raises(InvalidModelResponse, match="line 1"):
        parse('{"filesToModify": [,], "approach": []}')


def test_prose_with_no_json_is_invalid() -> None:
    with pytest.raises(InvalidModelResponse, match="no JSON document"):
        parse("I could not complete this task.")


def test_an_empty_response_is_invalid() -> None:
    with pytest.raises(InvalidModelResponse, match="empty"):
        parse("   ")


def test_an_array_is_rejected_where_an_object_is_required() -> None:
    with pytest.raises(InvalidModelResponse, match="should be a JSON object"):
        parse('[{"filesToModify": [], "approach": []}]')


def test_a_schema_without_required_fields_accepts_any_object() -> None:
    assert parse_structured('{"x": 1}', {"type": "object"}, schema_name="loose") == {"x": 1}
