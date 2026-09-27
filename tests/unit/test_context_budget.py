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


# --- required complete writable files (concerns 55 and 56) -------------------
#
# The invariant under test: a model is never asked for the complete replacement
# contents of a file whose complete original contents were not supplied. Before
# these, a writable file simply met the per-item cap and was clipped, and the
# coding agent refused the attempt -- correctly, but a task that was perfectly
# possible became impossible as the file grew.


def _writable(path: str, size: int = 200) -> ContextItem:
    return _item(
        ContextPriority.DECLARED_FILE,
        path,
        "x\n" * size,
        path=path,
        requires_complete=True,
    )


def test_a_writable_file_over_the_per_item_cap_is_supplied_whole():
    """The TS-106 shape: 8110 bytes of writable test file against a 2000-token
    per-item cap, inside a 14745-token total budget with room to spare."""
    writable = _writable("src/test/navigation-stack.test.ts", 4_055)
    assert writable.estimated_tokens > 2_000

    package = assemble(
        [_task_item(), writable],
        ContextBudget(max_tokens=14_745, max_item_tokens=2_000, max_files=40),
    )

    included = next(item for item in package.items if item.path == writable.path)
    assert not included.truncated
    assert included.content == writable.content
    assert TRUNCATION_MARKER.split("{")[0] not in package.render()
    assert package.metadata["required_complete"] == {
        "paths": ["src/test/navigation-stack.test.ts"],
        "reserved_tokens": writable.estimated_tokens,
        "honoured": True,
    }


def test_supporting_context_is_dropped_before_writable_source_is_truncated():
    """Requirement 4, and the discriminating case: under the old rules both
    files fitted, because the writable one was first clipped to the per-item
    cap. Now the complete writable file is taken off the top and the supporting
    file is what does not fit."""
    task, writable = _task_item(), _writable("src/stack.ts", 1_000)
    supporting = _file("src/map.ts", 1_000, priority=ContextPriority.INTERFACE)
    budget = ContextBudget(
        max_tokens=task.estimated_tokens + writable.estimated_tokens + 10,
        max_item_tokens=100,
        max_files=40,
    )
    # Clipped to 100 tokens each, as they would have been, everything fits --
    # so this budget is not simply too small for the package.
    assert (
        task.estimated_tokens
        + writable.clipped(100).estimated_tokens
        + supporting.clipped(100).estimated_tokens
    ) < budget.max_tokens

    package = assemble([task, writable, supporting], budget)

    assert [item.path for item in package.items if item.path] == ["src/stack.ts"]
    assert not any(item.truncated for item in package.items)
    assert [dropped.path for dropped in package.dropped] == ["src/map.ts"]
    assert "reserved for files the task may replace" in package.dropped[0].reason


def test_several_writable_files_are_all_supplied_whole_and_deterministically():
    # Each one alone exceeds the per-item cap, so all three are exempted and
    # ordering has to come from the sort key rather than from arrival order.
    files = [
        _writable("src/c.ts", 4_000),
        _writable("src/a.ts", 4_000),
        _writable("src/b.ts", 4_000),
    ]
    budget = ContextBudget(max_tokens=14_745, max_item_tokens=2_000, max_files=40)
    assert all(item.estimated_tokens > budget.max_item_tokens for item in files)

    first = assemble([_task_item(), *files], budget)
    second = assemble([_task_item(), *reversed(files)], budget)

    assert [item.path for item in first.items if item.path] == [
        "src/a.ts",
        "src/b.ts",
        "src/c.ts",
    ]
    assert not any(item.truncated for item in first.items)
    assert first.render() == second.render()
    assert first.content_hash == second.content_hash


def test_writable_files_that_cannot_fit_are_clipped_exactly_as_before():
    """Requirement 5. The reservation is abandoned whole rather than honoured in
    part, the file is clipped and marked, and the coding agent's own guard is
    what refuses the attempt -- the behaviour that existed before this pass."""
    task, writable = _task_item(), _writable("src/huge.ts", 4_000)
    budget = ContextBudget(
        max_tokens=task.estimated_tokens + 200, max_item_tokens=100, max_files=40
    )

    package = assemble([task, writable], budget)

    included = next(item for item in package.items if item.path == "src/huge.ts")
    assert included.truncated
    assert included.requires_complete
    assert package.metadata["required_complete"]["honoured"] is False
    assert "do not fit in the context budget" in package.metadata["required_complete"]["reason"]
    assert any("raise CONTEXT_MAX_TOKENS" in w for w in package.metadata["warnings"])


def test_the_reservation_never_exceeds_the_configured_total_budget():
    """Requirement 6. Nothing here may grow the package past ``max_tokens``."""
    task = _task_item()
    files = [_writable(f"src/f{index}.ts", 900) for index in range(6)]
    budget = ContextBudget(max_tokens=5_000, max_item_tokens=2_000, max_files=40)

    package = assemble([task, *files], budget)

    assert package.estimated_tokens <= budget.max_tokens


def test_a_large_read_only_file_is_still_clipped():
    """Requirement 8. Clipping is what the per-item cap is for; only files the
    coder must reproduce whole are exempt."""
    read_only = _file("src/tree.ts", 4_000, priority=ContextPriority.INTERFACE)
    budget = ContextBudget(max_tokens=14_745, max_item_tokens=200, max_files=40)

    package = assemble([_task_item(), read_only], budget)

    included = next(item for item in package.items if item.path == "src/tree.ts")
    assert included.truncated
    assert not included.requires_complete
    assert package.metadata["required_complete"]["honoured"] is False
    assert package.metadata["required_complete"]["paths"] == []


def test_the_manifest_says_which_files_had_to_be_complete():
    """Requirement 7. An operator reading back a run can see what was required,
    what it cost, and whether it was honoured, without re-reading the prompt."""
    package = assemble(
        [_task_item(), _writable("src/stack.ts", 900), _file("src/map.ts", 5)],
        ContextBudget(max_tokens=14_745, max_item_tokens=2_000, max_files=40),
    )
    manifest = json.loads(json.dumps(package.manifest()))

    entries = {item["path"]: item for item in manifest["items"] if item["path"]}
    assert entries["src/stack.ts"]["requires_complete"] is True
    assert entries["src/stack.ts"]["truncated"] is False
    assert entries["src/map.ts"]["requires_complete"] is False
    assert manifest["required_complete"]["paths"] == ["src/stack.ts"]
    assert manifest["required_complete"]["honoured"] is True


# --- completeness is recorded, not inferred (concern 58) ---------------------


def test_a_required_path_that_never_became_an_item_is_recorded_incomplete():
    """The case truncation cannot see: nothing was clipped, nothing was
    dropped, and the file is simply not there."""
    package = assemble(
        [_task_item()],
        ContextBudget(max_tokens=14_745, max_item_tokens=2_000, max_files=40),
        required_paths={"src/huge.ts": "262145 bytes exceeds CONTEXT_MAX_FILE_BYTES=262144"},
    )

    assert package.truncated_paths == ()
    assert package.dropped == ()
    assert [source.path for source in package.incomplete_required] == ["src/huge.ts"]
    assert "CONTEXT_MAX_FILE_BYTES" in package.incomplete_required[0].reason


def test_a_required_path_with_no_stated_reason_still_reads_as_incomplete():
    package = assemble(
        [_task_item()],
        ContextBudget(max_tokens=14_745, max_item_tokens=2_000, max_files=40),
        required_paths={"src/gone.ts": None},
    )

    assert package.incomplete_required[0].reason == "not included in the context package"


def test_a_required_path_dropped_for_the_budget_carries_the_drop_reason():
    task = _task_item()
    writable = _writable("src/stack.ts", 4_000)
    budget = ContextBudget(
        max_tokens=task.estimated_tokens + 50, max_item_tokens=10, max_files=40
    )

    package = assemble([task, writable], budget, required_paths={"src/stack.ts": None})

    source = package.incomplete_required[0]
    assert source.path == "src/stack.ts"
    assert "lines" in (source.reason or "")


def test_a_required_path_supplied_whole_is_recorded_complete():
    writable = _writable("src/stack.ts", 4_000)

    package = assemble(
        [_task_item(), writable],
        ContextBudget(max_tokens=14_745, max_item_tokens=2_000, max_files=40),
        required_paths={"src/stack.ts": None},
    )

    assert package.incomplete_required == ()
    assert package.required_sources[0].complete is True
    manifest = json.loads(json.dumps(package.manifest()))
    assert manifest["required_sources"] == [
        {"path": "src/stack.ts", "complete": True, "reason": None}
    ]
