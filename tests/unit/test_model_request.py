"""Prompt assembly and the context-window guard (build.md section 13)."""

from __future__ import annotations

import pytest

from apps.orchestrator.providers import (
    Message,
    MessageRole,
    ModelRequest,
    PromptTooLarge,
    StructuredSchema,
)


def make_request(**overrides) -> ModelRequest:
    defaults = {
        "system_instructions": "You are a coding agent.",
        "task_instructions": "TS-001: add a health endpoint.",
    }
    return ModelRequest(**{**defaults, **overrides})


def test_messages_start_with_the_system_instructions() -> None:
    messages = make_request().messages()

    assert messages[0] == Message(MessageRole.SYSTEM, "You are a coding agent.")
    assert len(messages) == 2


def test_context_and_feedback_are_ordered_after_the_task() -> None:
    """The coder must read the task before the evidence about the last attempt."""
    request = make_request(context="src/app.py: ...", review_feedback="Missing a test.")

    user_content = request.messages()[-1].content

    assert user_content.index("TS-001") < user_content.index("Repository context")
    assert user_content.index("Repository context") < user_content.index("Review feedback")


def test_history_sits_between_system_and_the_new_turn() -> None:
    history = (Message(MessageRole.ASSISTANT, "previous patch"),)
    messages = make_request(history=history).messages()

    assert [message.role for message in messages] == [
        MessageRole.SYSTEM,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]


def test_absent_context_adds_no_empty_section() -> None:
    assert "Repository context" not in make_request().messages()[-1].content


def test_a_prompt_within_the_window_is_allowed() -> None:
    make_request(context="x" * 1000).assert_fits(32768)


def test_a_prompt_that_leaves_no_room_to_answer_is_rejected() -> None:
    """The guard reserves output space: a prompt that only just fits cannot
    produce a complete patch, and a truncated patch looks like a model defect."""
    request = make_request(context="x" * 100_000)

    with pytest.raises(PromptTooLarge) as caught:
        request.assert_fits(32768)

    assert caught.value.context_window == 32768
    assert caught.value.estimated_tokens > 32768


def test_an_explicit_output_reserve_replaces_the_default() -> None:
    context = "x" * 80_000  # ~22.8k prompt tokens
    ModelRequest(
        system_instructions="s",
        task_instructions="t",
        context=context,
        max_output_tokens=512,
    ).assert_fits(32768)

    with pytest.raises(PromptTooLarge):
        ModelRequest(
            system_instructions="s",
            task_instructions="t",
            context=context,
            max_output_tokens=16000,
        ).assert_fits(32768)


def test_an_unknown_window_disables_the_guard() -> None:
    """A provider that does not declare a window must not block a run; the
    endpoint's own limit is then the only one."""
    make_request(context="x" * 500_000).assert_fits(None)


def test_schema_is_carried_on_the_request_not_the_provider() -> None:
    schema = StructuredSchema(name="plan", schema={"type": "object"})
    assert make_request(schema=schema).schema is schema
