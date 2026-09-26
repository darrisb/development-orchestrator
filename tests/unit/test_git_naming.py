"""Branch, commit and diff value objects (build.md section 10)."""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.git import (
    MAX_SLUG_LENGTH,
    ChangeType,
    DiffSummary,
    FileChange,
    StatusEntry,
    assert_safe_ref_component,
    checkpoint_tag_name,
    slugify,
    task_branch_name,
    task_commit_message,
    worktree_dir_name,
)


def test_branch_name_matches_the_documented_example():
    assert task_branch_name("TS-004", "Navigation tree") == "agent/TS-004-navigation-tree"


def test_commit_message_matches_the_documented_example():
    assert task_commit_message("TS-004", "implement navigation tree") == (
        "TS-004: implement navigation tree"
    )


def test_branch_name_keeps_the_task_id_verbatim():
    """Traceability (rule 7) depends on the id surviving unslugified."""
    assert task_branch_name("TS-004", "x").startswith("agent/TS-004-")


def test_branch_name_survives_a_title_with_no_usable_characters():
    assert task_branch_name("TS-004", "!!! ???") == "agent/TS-004"


def test_slug_is_truncated_at_a_word_boundary():
    slug = slugify("a" * 20 + " " + "b" * 40)
    assert len(slug) <= MAX_SLUG_LENGTH
    assert slug == "a" * 20


def test_slug_truncates_mid_word_when_there_is_no_boundary():
    slug = slugify("z" * 80)
    assert slug == "z" * MAX_SLUG_LENGTH


@pytest.mark.parametrize(
    "task_id",
    ["", "TS 004", "TS-004~1", "TS:004", "refs/../TS-004", "-TS-004", "TS-004.lock"],
)
def test_unsafe_task_ids_are_rejected(task_id: str):
    """A manifest is hand-edited; a bad id must never reach a command line."""
    with pytest.raises(ValueError):
        task_branch_name(task_id, "title")


def test_safe_ref_component_returns_its_input():
    assert assert_safe_ref_component("TS-004") == "TS-004"


def test_checkpoint_tag_names_the_attempt():
    assert checkpoint_tag_name("TS-004", 2) == "checkpoint/TS-004/attempt-2"


def test_checkpoint_tag_rejects_a_zeroth_attempt():
    with pytest.raises(ValueError):
        checkpoint_tag_name("TS-004", 0)


def test_worktree_directory_includes_the_run_number():
    """A retry must not land in the leftovers of an earlier run."""
    assert worktree_dir_name("TS-004", 2) == "ts-004-run2"


def test_diff_summary_counts_lines_as_insertions_plus_deletions():
    summary = DiffSummary(
        (
            FileChange("src/a.js", ChangeType.MODIFIED, insertions=10, deletions=4),
            FileChange("src/b.js", ChangeType.ADDED, insertions=7, deletions=0),
        )
    )

    assert summary.files_changed == 2
    assert summary.insertions == 17
    assert summary.deletions == 4
    assert summary.line_count == 21


def test_binary_files_are_not_counted_as_zero_line_changes():
    """``None`` and ``0`` mean different things to the scope guard."""
    summary = DiffSummary((FileChange("logo.png", ChangeType.ADDED),))

    assert summary.binary_paths == ("logo.png",)
    assert summary.files_changed == 1
    assert summary.line_count == 0


def test_diff_summary_reports_deleted_paths():
    summary = DiffSummary(
        (
            FileChange("gone.js", ChangeType.DELETED, insertions=0, deletions=12),
            FileChange("kept.js", ChangeType.MODIFIED, insertions=1, deletions=1),
        )
    )

    assert summary.deleted_paths == ("gone.js",)
    assert summary.paths == ("gone.js", "kept.js")


def test_status_entry_recognises_untracked_and_unmerged():
    assert StatusEntry("new.js", "?", "?").is_untracked
    assert StatusEntry("conflict.js", "U", "U").is_unmerged
    assert StatusEntry("conflict.js", "A", "A").is_unmerged
    assert not StatusEntry("edited.js", " ", "M").is_unmerged
