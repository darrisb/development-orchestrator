"""Applying edits to a checkout (build.md phase G item 4).

These tests exist for one claim: this is the only code that writes a file for a
model, and nothing it writes lands outside the worktree or outside the task's
allowance. So most of them assert about the filesystem after a refusal, not
just about the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.orchestrator.domain.edits import CodeChangeSet, EditOperation, FileEdit
from apps.orchestrator.domain.scope import ScopeFindingKind, ScopePolicy
from apps.orchestrator.services.code_edits import apply_change_set

pytestmark = pytest.mark.integration


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    tree = tmp_path / "worktree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "navigation.ts").write_text("export const old = 1;\n", encoding="utf-8")
    (tree / "src" / "widgets").mkdir()
    (tree / "src" / "widgets" / "tree.ts").write_text("export interface T {}\n", encoding="utf-8")
    (tree / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return tree


@pytest.fixture
def policy() -> ScopePolicy:
    return ScopePolicy(
        allowed_paths=("src/navigation.ts", "src/navigationTree.ts", "tests/nav.test.ts"),
        inspect_only_paths=("src/widgets/tree.ts",),
        max_files_changed=4,
        max_diff_lines=200,
    )


def _change_set(*edits: FileEdit) -> CodeChangeSet:
    return CodeChangeSet(summary="test change", edits=edits)


def _update(path: str, content: str = "export const updated = 2;\n") -> FileEdit:
    return FileEdit(path=path, operation=EditOperation.UPDATE, content=content)


def _create(path: str, content: str = "export const created = 3;\n") -> FileEdit:
    return FileEdit(path=path, operation=EditOperation.CREATE, content=content)


# --- the permitted case ------------------------------------------------------


def test_permitted_edits_are_written_and_nested_directories_are_created(
    worktree: Path, policy: ScopePolicy
):
    application = apply_change_set(
        _change_set(_update("src/navigation.ts"), _create("tests/nav.test.ts")),
        root=worktree,
        policy=policy,
    )

    assert application.written == ("src/navigation.ts", "tests/nav.test.ts")
    assert application.rejected == ()
    assert (worktree / "src" / "navigation.ts").read_text() == "export const updated = 2;\n"
    assert (worktree / "tests" / "nav.test.ts").is_file()


def test_a_delete_removes_the_file(worktree: Path):
    policy = ScopePolicy(allowed_paths=("src/navigation.ts",), max_files_changed=4)
    application = apply_change_set(
        _change_set(FileEdit(path="src/navigation.ts", operation=EditOperation.DELETE)),
        root=worktree,
        policy=policy,
    )

    assert application.deleted == ("src/navigation.ts",)
    assert not (worktree / "src" / "navigation.ts").exists()


def test_a_create_over_an_existing_file_is_written_with_a_warning(
    worktree: Path, policy: ScopePolicy
):
    """A labelling slip, not an escape attempt: the path is inside the
    allowance either way."""
    application = apply_change_set(
        _change_set(_create("src/navigation.ts", "replaced\n")), root=worktree, policy=policy
    )

    assert application.written == ("src/navigation.ts",)
    assert "created over an existing file" in application.warnings[0]


# --- refusals ----------------------------------------------------------------


def test_a_protected_path_is_not_written(worktree: Path, policy: ScopePolicy):
    application = apply_change_set(
        _change_set(_update(".env", "SECRET=leaked\n")), root=worktree, policy=policy
    )

    assert application.written == ()
    assert application.scope_refusals
    assert (worktree / ".env").read_text() == "SECRET=1\n"


def test_a_path_outside_the_allowance_is_not_written(worktree: Path, policy: ScopePolicy):
    application = apply_change_set(
        _change_set(_create("src/billing.ts")), root=worktree, policy=policy
    )

    assert application.written == ()
    assert not (worktree / "src" / "billing.ts").exists()
    assert ScopeFindingKind.OUTSIDE_ALLOWANCE in application.refusal_kinds


def test_an_inspect_only_file_is_not_written(worktree: Path, policy: ScopePolicy):
    application = apply_change_set(
        _change_set(_update("src/widgets/tree.ts", "tampered\n")), root=worktree, policy=policy
    )

    assert application.written == ()
    assert (worktree / "src" / "widgets" / "tree.ts").read_text() == "export interface T {}\n"


def test_one_refusal_does_not_stop_the_edits_after_it(worktree: Path, policy: ScopePolicy):
    application = apply_change_set(
        _change_set(_update(".env"), _update("src/navigation.ts")),
        root=worktree,
        policy=policy,
    )

    assert application.written == ("src/navigation.ts",)
    assert len(application.rejected) == 1


def test_an_update_to_a_file_that_does_not_exist_is_refused_with_advice(
    worktree: Path, policy: ScopePolicy
):
    application = apply_change_set(
        _change_set(_update("src/navigationTree.ts")), root=worktree, policy=policy
    )

    assert application.written == ()
    assert "needs operation 'create'" in application.rejected[0].reason
    # Not a scope refusal: the coder misread the tree, it did not leave bounds.
    assert application.scope_refusals == ()


def test_deleting_a_file_that_is_not_there_is_refused(worktree: Path, policy: ScopePolicy):
    application = apply_change_set(
        _change_set(FileEdit(path="src/navigationTree.ts", operation=EditOperation.DELETE)),
        root=worktree,
        policy=policy,
    )

    assert "nothing to delete" in application.rejected[0].reason


def test_a_change_set_wider_than_the_task_allows_is_refused_whole(
    worktree: Path, policy: ScopePolicy
):
    """Applying the first four of ten edits leaves a half-implemented candidate
    that would spend a verification cycle proving it does not work."""
    application = apply_change_set(
        _change_set(*[_create(f"src/file{index}.ts") for index in range(5)]),
        root=worktree,
        policy=policy,
    )

    assert application.written == ()
    assert len(application.rejected) == 5
    assert ScopeFindingKind.TOO_MANY_FILES in application.refusal_kinds
    assert not any(worktree.glob("src/file*.ts"))


# --- escaping the worktree ---------------------------------------------------


def test_a_symlinked_directory_may_not_be_written_through(tmp_path: Path):
    """A relative path plus a symlink is how an edit reaches the rest of the
    machine, and a checkout can contain a symlink legitimately."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.ts").write_text("original\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "linked").symlink_to(outside, target_is_directory=True)

    application = apply_change_set(
        _change_set(_update("linked/target.ts", "tampered\n")),
        root=worktree,
        policy=ScopePolicy(max_files_changed=4),
    )

    assert application.written == ()
    assert "symbolic link" in application.rejected[0].reason
    assert (outside / "target.ts").read_text() == "original\n"


def test_a_symlinked_file_may_not_be_written_through(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("original\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "notes.txt").symlink_to(outside / "secret.txt")

    application = apply_change_set(
        _change_set(_update("notes.txt", "tampered\n")),
        root=worktree,
        policy=ScopePolicy(max_files_changed=4),
    )

    assert application.written == ()
    assert (outside / "secret.txt").read_text() == "original\n"


def test_a_traversing_path_is_refused_rather_than_normalised(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (tmp_path / "sibling.ts").write_text("original\n", encoding="utf-8")

    application = apply_change_set(
        _change_set(
            FileEdit(path="../sibling.ts", operation=EditOperation.UPDATE, content="tampered\n")
        ),
        root=worktree,
        policy=ScopePolicy(max_files_changed=4),
    )

    assert application.written == ()
    assert (tmp_path / "sibling.ts").read_text() == "original\n"
