"""Context package assembly and budget enforcement (build.md sections 15, 46)."""

from __future__ import annotations

import json

import pytest

from apps.orchestrator.domain.context import (
    TRUNCATION_MARKER,
    ContextBudget,
    ContextBudgetTooSmall,
    ContextItem,
    ContextPriority,
    assemble,
)


def _item(priority: ContextPriority, label: str, content: str, **overrides) -> ContextItem:
    defaults = {"reason": "test fixture", "path": overrides.pop("path", None)}
    return ContextItem(priority=priority, label=label, content=content, **defaults, **overrides)


def _task_item(content: str = "do the task") -> ContextItem:
    return _item(ContextPriority.TASK_INSTRUCTIONS, "Task T-1", content)


def _file(path: str, size: int = 200, priority=ContextPriority.DECLARED_FILE) -> ContextItem:
    return _item(priority, path, "x\n" * size, path=path)


def test_items_are_ordered_by_priority_then_path():
    package = assemble(
        [
            _file("src/z.ts", 1, priority=ContextPriority.RELEVANT_TEST),
            _file("src/a.ts", 1, priority=ContextPriority.INTERFACE),
            _file("src/b.ts", 1),
            _task_item(),
        ],
        ContextBudget(max_tokens=10_000, max_item_tokens=1_000, max_files=10),
    )

    assert [item.label for item in package.items] == [
        "Task T-1",
        "src/b.ts",
        "src/a.ts",
        "src/z.ts",
    ]


def test_the_same_inputs_in_any_order_produce_the_same_hash():
    budget = ContextBudget(max_tokens=10_000, max_item_tokens=1_000, max_files=10)
    items = [_task_item(), _file("src/a.ts", 3), _file("src/b.ts", 3)]

    first = assemble(items, budget)
    second = assemble(list(reversed(items)), budget)

    assert first.render() == second.render()
    assert first.content_hash == second.content_hash


def test_a_low_priority_item_is_dropped_before_a_high_priority_one():
    task, declared = _task_item(), _file("src/declared.ts", 40)
    lesson = _file("src/map.ts", 40, ContextPriority.LESSON)
    # Room for the task and the declared file, and one token short of the rest.
    budget = ContextBudget(
        max_tokens=task.estimated_tokens + declared.estimated_tokens + lesson.estimated_tokens - 1,
        max_item_tokens=1_000,
        max_files=10,
    )

    package = assemble([task, declared, lesson], budget)

    assert "src/declared.ts" in package.paths
    assert [dropped.label for dropped in package.dropped] == ["src/map.ts"]
    assert "max_tokens" in package.dropped[0].reason
    assert not package.complete


def test_nothing_is_backfilled_once_the_budget_is_exhausted():
    """A small item after a dropped one must not sneak in: priority decides."""
    task = _task_item()
    big = _file("src/big.ts", 60, ContextPriority.INTERFACE)
    small = _file("src/small.ts", 1, ContextPriority.RELEVANT_TEST)
    # The big file does not fit; the small one would, if backfilling were allowed.
    budget = ContextBudget(
        max_tokens=task.estimated_tokens + big.estimated_tokens - 1,
        max_item_tokens=1_000,
        max_files=10,
    )

    package = assemble([task, big, small], budget)

    assert package.paths == ()
    assert {dropped.label for dropped in package.dropped} == {"src/big.ts", "src/small.ts"}


def test_max_files_bounds_repository_files_only():
    budget = ContextBudget(max_tokens=100_000, max_item_tokens=10_000, max_files=1)

    package = assemble(
        [_task_item(), _file("src/a.ts", 2), _file("src/b.ts", 2),
         _item(ContextPriority.LESSON, "Lessons", "be careful")],
        budget,
    )

    assert package.file_count == 1
    assert package.paths == ("src/a.ts",)
    assert [dropped.label for dropped in package.dropped] == ["src/b.ts"]
    assert "max_files" in package.dropped[0].reason
    # The task block and the lessons are not repository files and are kept.
    assert [item.label for item in package.items] == ["Task T-1", "src/a.ts", "Lessons"]


def test_an_oversized_file_is_clipped_at_a_line_boundary_and_says_so():
    package = assemble(
        [_task_item(), _file("src/big.ts", 5_000)],
        ContextBudget(max_tokens=100_000, max_item_tokens=100, max_files=5),
    )
    clipped = package.items[1]

    assert clipped.truncated
    assert clipped.shown_lines is not None and clipped.shown_lines < 5_000
    assert "truncated by orchestrator" in clipped.content
    assert clipped.content.endswith(
        TRUNCATION_MARKER.format(shown=clipped.shown_lines, total=5_000)
    )
    assert clipped.source_lines == 5_000
    assert not package.complete
    assert package.truncated_paths == ("src/big.ts",)


def test_a_clipped_file_keeps_the_hash_of_the_whole_file():
    """The manifest must describe the file on disk, not the slice that was sent."""
    original = _file("src/big.ts", 5_000, ContextPriority.DECLARED_FILE)
    whole = ContextItem(
        priority=original.priority,
        label=original.label,
        content=original.content,
        reason=original.reason,
        path=original.path,
        sha256="deadbeef",
        source_bytes=len(original.content.encode()),
    )

    package = assemble(
        [_task_item(), whole], ContextBudget(max_tokens=100_000, max_item_tokens=50, max_files=5)
    )

    assert package.items[1].sha256 == "deadbeef"
    assert package.items[1].source_bytes == len(original.content.encode())


def test_a_file_offered_twice_is_kept_at_its_highest_priority():
    package = assemble(
        [
            _task_item(),
            _file("src/a.ts", 2, ContextPriority.INTERFACE),
            _file("src/a.ts", 2, ContextPriority.DECLARED_FILE),
        ],
        ContextBudget(max_tokens=100_000, max_item_tokens=1_000, max_files=5),
    )

    assert package.paths == ("src/a.ts",)
    assert package.items[1].priority is ContextPriority.DECLARED_FILE


def test_a_budget_too_small_for_the_task_itself_is_a_configuration_error():
    with pytest.raises(ContextBudgetTooSmall) as error:
        assemble([_task_item("word " * 500)], ContextBudget(max_tokens=10, max_item_tokens=10_000))

    assert "CONTEXT_MAX_TOKENS" in str(error.value)


def test_the_manifest_records_names_hashes_reasons_and_truncation():
    package = assemble(
        [
            _task_item(),
            ContextItem(
                priority=ContextPriority.DECLARED_FILE,
                label="src/a.ts",
                content="x\n" * 500,
                reason="declared by task T-1 (may modify)",
                path="src/a.ts",
                sha256="abc123",
                source_bytes=1_000,
                source_lines=500,
            ),
            _file("src/dropped.ts", 5_000, ContextPriority.LESSON),
        ],
        ContextBudget(max_tokens=100, max_item_tokens=60, max_files=5),
        metadata={"source_commit": "0" * 40},
    )
    manifest = package.manifest()

    # It must survive a round trip: this is written to context-manifest.json.
    assert json.loads(json.dumps(manifest)) == manifest
    entry = next(item for item in manifest["items"] if item["path"] == "src/a.ts")
    assert entry["sha256"] == "abc123"
    assert entry["reason"] == "declared by task T-1 (may modify)"
    assert entry["truncated"] is True
    assert entry["source_lines"] == 500
    assert manifest["dropped"][0]["path"] == "src/dropped.ts"
    assert manifest["context_hash"] == package.content_hash
    assert manifest["source_commit"] == "0" * 40
    assert manifest["complete"] is False


def test_a_complete_package_says_so():
    package = assemble(
        [_task_item(), _file("src/a.ts", 2)],
        ContextBudget(max_tokens=100_000, max_item_tokens=10_000, max_files=5),
    )

    assert package.complete
    assert package.manifest()["complete"] is True


def test_file_content_is_fenced_without_the_fence_being_clipped():
    package = assemble(
        [
            _task_item(),
            ContextItem(
                priority=ContextPriority.DECLARED_FILE,
                label="src/a.ts",
                content="line\n" * 500,
                reason="declared",
                path="src/a.ts",
                language="typescript",
            ),
        ],
        ContextBudget(max_tokens=100_000, max_item_tokens=60, max_files=5),
    )
    rendered = package.render()

    assert "```typescript" in rendered
    assert rendered.count("```") == 2


@pytest.mark.parametrize("field", ["max_tokens", "max_item_tokens"])
def test_a_nonsensical_budget_is_rejected(field: str):
    with pytest.raises(ValueError, match=field):
        ContextBudget(**{field: 0})
