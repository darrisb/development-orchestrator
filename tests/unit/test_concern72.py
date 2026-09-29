"""Concern 72: targeted-edit limits are declared before generation."""

from __future__ import annotations

import pytest

from apps.orchestrator.agents import prompts
from apps.orchestrator.domain import edits
from apps.orchestrator.domain.edits import CodeChangeSet, MalformedChangeSet
from apps.orchestrator.domain.enums import Complexity
from apps.orchestrator.domain.models import Task


def _prompt() -> str:
    task = Task(
        id=72,
        project_id=1,
        external_task_id="TS-072",
        title="Keep producer and consumer contracts aligned",
        instructions="Make the targeted-edit output contract explicit.",
        complexity=Complexity.LOW,
    )
    return prompts.render_coding_instructions(task)


def test_targeted_replace_declares_its_exact_per_edit_byte_ceiling() -> None:
    rendered = _prompt()
    lowered = rendered.lower()

    assert str(edits.MAX_TARGETED_EDIT_PAYLOAD_BYTES) in rendered
    assert "oldText" in rendered and "newText" in rendered
    assert "combined UTF-8 byte length" in rendered
    assert "each 'replace' edit" in lowered


def test_targeted_replace_requires_the_smallest_sufficiently_unique_exact_fragment() -> None:
    rendered = _prompt()

    assert "smallest sufficiently unique exact fragment" in rendered
    assert "Do not copy the entire file into 'oldText'" in rendered


def test_targeted_replace_forbids_placeholder_preservation_claims() -> None:
    rendered = _prompt()

    assert "// ... existing tests unchanged ..." in rendered
    assert "placeholders do not preserve content" in rendered.lower()


def test_whole_file_operations_remain_available_when_a_replace_cannot_fit() -> None:
    rendered = _prompt()

    assert "If a replacement cannot fit" in rendered
    assert "operation 'update'" in rendered
    assert "operation 'create'" in rendered
    assert "complete new contents" in rendered


def test_prompt_and_validator_share_the_authoritative_targeted_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed_limit = 37
    monkeypatch.setattr(edits, "MAX_TARGETED_EDIT_PAYLOAD_BYTES", changed_limit)

    assert str(changed_limit) in _prompt()
    CodeChangeSet.from_payload(
        {
            "summary": "at the shared limit",
            "edits": [
                {
                    "path": "a.ts",
                    "operation": "replace",
                    "content": "",
                    "oldText": "x" * 17,
                    "newText": "y" * 20,
                }
            ],
        }
    )
    with pytest.raises(MalformedChangeSet, match="37"):
        CodeChangeSet.from_payload(
            {
                "summary": "past the shared limit",
                "edits": [
                    {
                        "path": "a.ts",
                        "operation": "replace",
                        "content": "",
                        "oldText": "x" * 17,
                        "newText": "y" * 21,
                    }
                ],
            }
        )


def test_prompt_byte_accounting_matches_multibyte_validator_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _prompt()

    assert "len(oldText.encode('utf-8')) + len(newText.encode('utf-8'))" in rendered
    assert "characters" not in rendered.split("combined UTF-8 byte length", 1)[1].split(".", 1)[0]

    # Two characters, but five UTF-8 bytes: the executable boundary must mean
    # the same thing as the formula shown to the coder.
    monkeypatch.setattr(edits, "MAX_TARGETED_EDIT_PAYLOAD_BYTES", 5)
    CodeChangeSet.from_payload(
        {
            "summary": "five UTF-8 bytes",
            "edits": [
                {
                    "path": "a.ts",
                    "operation": "replace",
                    "content": "",
                    "oldText": "é",
                    "newText": "€",
                }
            ],
        }
    )
    with pytest.raises(MalformedChangeSet, match="5"):
        CodeChangeSet.from_payload(
            {
                "summary": "six UTF-8 bytes",
                "edits": [
                    {
                        "path": "a.ts",
                        "operation": "replace",
                        "content": "",
                        "oldText": "éa",
                        "newText": "€",
                    }
                ],
            }
        )
