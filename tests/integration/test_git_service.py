"""GitService against a real fixture repository (build.md sections 10 and 46).

These tests run Git for real. Mocking it would only prove that the mock
matches the implementation's assumptions, which is exactly what tends to be
wrong about Git.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.git import ChangeType
from apps.orchestrator.services.git_errors import (
    BranchAlreadyExists,
    BranchMissing,
    DirtyWorktree,
    GitCommandFailed,
    MergeConflict,
    NotARepository,
    NothingToCommit,
    ProtectedBranch,
    PushNotPermitted,
    WorktreePathRejected,
)
from apps.orchestrator.services.git_service import DIFF_TRUNCATION_MARKER, GitService
from tests.conftest import run_git

pytestmark = pytest.mark.integration


@pytest.fixture
def git(fixture_repo: Path, git_settings: Settings) -> GitService:
    return GitService(fixture_repo, default_branch="main", settings=git_settings)


# --- reads ------------------------------------------------------------------


def test_reads_branch_and_head(git: GitService, fixture_repo: Path):
    assert git.get_current_branch() == "main"
    assert git.get_head_sha() == run_git(fixture_repo, "rev-parse", "HEAD").strip()


def test_rejects_a_directory_that_is_not_a_repository(tmp_path: Path, git_settings: Settings):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(NotARepository):
        GitService(plain, settings=git_settings)


def test_status_lists_untracked_files_individually(git: GitService, fixture_repo: Path):
    (fixture_repo / "src" / "new.js").write_text("// new\n", encoding="utf-8")

    entries = git.get_status()

    assert [entry.path for entry in entries] == ["src/new.js"]
    assert entries[0].is_untracked
    assert not git.is_clean()


def test_clean_repository_passes_the_clean_check(git: GitService):
    git.ensure_clean_worktree()


def test_dirty_repository_is_refused(git: GitService, fixture_repo: Path):
    """Section 10 rule 4."""
    (fixture_repo / "README.md").write_text("# Edited\n", encoding="utf-8")

    with pytest.raises(DirtyWorktree) as excinfo:
        git.ensure_clean_worktree()
    assert excinfo.value.entries == ("README.md",)


def test_dirty_repository_is_allowed_when_policy_says_so(git: GitService, fixture_repo: Path):
    (fixture_repo / "README.md").write_text("# Edited\n", encoding="utf-8")

    git.ensure_clean_worktree(allow_dirty=True)


def test_dirty_start_policy_comes_from_settings(fixture_repo: Path, git_settings: Settings):
    permissive = git_settings.model_copy(update={"git_allow_dirty_start": True})
    (fixture_repo / "README.md").write_text("# Edited\n", encoding="utf-8")

    GitService(fixture_repo, settings=permissive).ensure_clean_worktree()


# --- branches ---------------------------------------------------------------


def test_creates_a_task_branch_without_leaving_the_current_one(git: GitService):
    start = git.get_head_sha()

    recorded = git.create_task_branch("agent/TS-001-first")

    assert recorded == start
    assert git.branch_exists("agent/TS-001-first")
    assert git.get_current_branch() == "main"


def test_creating_the_same_branch_twice_is_refused(git: GitService):
    git.create_task_branch("agent/TS-001-first")

    with pytest.raises(BranchAlreadyExists):
        git.create_task_branch("agent/TS-001-first")


def test_the_default_branch_cannot_be_recreated_or_deleted(git: GitService):
    with pytest.raises(ProtectedBranch):
        git.create_task_branch("main")
    with pytest.raises(ProtectedBranch):
        git.delete_branch("main")


def test_extra_protected_patterns_are_honoured(fixture_repo: Path, git_settings: Settings):
    git = GitService(
        fixture_repo,
        default_branch="main",
        protected_branches=("release/*",),
        settings=git_settings,
    )

    with pytest.raises(ProtectedBranch):
        git.create_task_branch("release/2026-01")


# --- worktrees --------------------------------------------------------------


def test_creates_an_isolated_worktree_on_a_new_branch(git: GitService, git_settings: Settings):
    path = git_settings.worktree_root / "ts-001-run1"

    worktree = git.create_worktree(path, "agent/TS-001-first")

    assert worktree.path == path.resolve()
    assert (path / "src" / "app.js").read_text(encoding="utf-8") == "export const answer = 41;\n"
    assert worktree.get_current_branch() == "agent/TS-001-first"
    # The managed repository itself is untouched.
    assert git.get_current_branch() == "main"
    assert git.is_clean()


def test_worktree_paths_outside_the_configured_root_are_refused(
    git: GitService, tmp_path: Path
):
    """The worktree root is the isolation boundary, so it is not negotiable."""
    with pytest.raises(WorktreePathRejected):
        git.create_worktree(tmp_path / "elsewhere", "agent/TS-001-first")


def test_a_worktree_path_may_not_be_the_root_itself(git: GitService, git_settings: Settings):
    with pytest.raises(WorktreePathRejected):
        git.create_worktree(git_settings.worktree_root, "agent/TS-001-first")


def test_a_worktree_cannot_reuse_a_non_empty_directory(git: GitService, git_settings: Settings):
    path = git_settings.worktree_root / "ts-001-run1"
    path.mkdir(parents=True)
    (path / "leftover.txt").write_text("stale\n", encoding="utf-8")

    with pytest.raises(WorktreePathRejected):
        git.create_worktree(path, "agent/TS-001-first")


def test_worktree_lifecycle_cleans_up_after_itself(git: GitService, git_settings: Settings):
    path = git_settings.worktree_root / "ts-001-run1"
    git.create_worktree(path, "agent/TS-001-first")
    assert [entry.path for entry in git.list_worktrees()].count(path.resolve()) == 1

    git.remove_worktree(path, delete_branch="agent/TS-001-first")

    assert not path.exists()
    assert [entry.path for entry in git.list_worktrees()] == [git.path]
    assert not git.branch_exists("agent/TS-001-first")


def test_removing_a_worktree_keeps_its_branch_by_default(
    git: GitService, git_settings: Settings
):
    """The branch is the run's audit trail (rule 7)."""
    path = git_settings.worktree_root / "ts-001-run1"
    git.create_worktree(path, "agent/TS-001-first")

    git.remove_worktree(path)

    assert git.branch_exists("agent/TS-001-first")


def test_removing_a_worktree_whose_directory_vanished_does_not_raise(
    git: GitService, git_settings: Settings
):
    """Cleanup runs on failure paths; it must not mask the original failure."""
    path = git_settings.worktree_root / "ts-001-run1"
    git.create_worktree(path, "agent/TS-001-first")
    shutil.rmtree(path)

    git.remove_worktree(path)

    assert [entry.path for entry in git.list_worktrees()] == [git.path]


def test_creating_a_worktree_for_a_missing_branch_is_refused(
    git: GitService, git_settings: Settings
):
    with pytest.raises(BranchMissing):
        git.create_worktree(
            git_settings.worktree_root / "ts-001-run1", "agent/nope", create_branch=False
        )


# --- diffs ------------------------------------------------------------------


@pytest.fixture
def worktree(git: GitService, git_settings: Settings) -> GitService:
    return git.create_worktree(git_settings.worktree_root / "ts-001-run1", "agent/TS-001-first")


def test_diff_includes_uncommitted_and_untracked_work(worktree: GitService):
    """The coder writes files; the orchestrator decides what is committed."""
    (worktree.path / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    (worktree.path / "src" / "added.js").write_text("export const extra = 1;\n", encoding="utf-8")

    diff = worktree.get_diff()

    assert "export const answer = 42;" in diff
    assert "src/added.js" in diff


def test_changed_files_report_paths_types_and_counts(worktree: GitService):
    (worktree.path / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    (worktree.path / "src" / "added.js").write_text("a\nb\n", encoding="utf-8")
    (worktree.path / "README.md").unlink()

    changes = {change.path: change for change in worktree.get_changed_files()}

    assert set(changes) == {"README.md", "src/added.js", "src/app.js"}
    assert changes["src/added.js"].change_type is ChangeType.ADDED
    assert changes["src/added.js"].insertions == 2
    assert changes["README.md"].change_type is ChangeType.DELETED
    assert changes["src/app.js"].change_type is ChangeType.MODIFIED
    assert changes["src/app.js"].insertions == 1
    assert changes["src/app.js"].deletions == 1


def test_changed_files_are_ordered_deterministically(worktree: GitService):
    for name in ("z.js", "a.js", "m.js"):
        (worktree.path / "src" / name).write_text("x\n", encoding="utf-8")

    paths = [change.path for change in worktree.get_changed_files()]

    assert paths == sorted(paths)


def test_binary_files_report_no_line_counts(worktree: GitService):
    (worktree.path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")

    change = next(c for c in worktree.get_changed_files() if c.path == "logo.png")

    assert change.is_binary
    assert change.insertions is None


def test_diff_line_count_totals_insertions_and_deletions(worktree: GitService):
    (worktree.path / "src" / "app.js").write_text("a\nb\nc\n", encoding="utf-8")

    assert worktree.get_diff_line_count() == 4  # three added, one replaced


def test_a_clean_worktree_has_an_empty_diff(worktree: GitService):
    assert worktree.get_diff() == ""
    assert worktree.get_diff_line_count() == 0
    assert worktree.get_diff_summary().files_changed == 0


def test_truncated_diffs_say_so(worktree: GitService):
    """A reviewer must never mistake a clipped diff for the whole change."""
    (worktree.path / "src" / "big.js").write_text("x\n" * 4000, encoding="utf-8")

    diff = worktree.get_diff(max_bytes=512)

    assert diff.endswith(DIFF_TRUNCATION_MARKER)
    assert len(diff) < 4000


def test_diff_between_two_commits_ignores_the_working_tree(worktree: GitService):
    base = worktree.get_head_sha()
    (worktree.path / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    head = worktree.commit("TS-001: bump the answer")
    (worktree.path / "src" / "uncommitted.js").write_text("later\n", encoding="utf-8")

    diff = worktree.get_diff_between(base, head)
    changes = worktree.get_changed_files_between(base, head)

    assert "42" in diff
    assert "uncommitted.js" not in diff
    assert [change.path for change in changes] == ["src/app.js"]


# --- commits ----------------------------------------------------------------


def test_commit_stages_everything_and_returns_the_new_sha(worktree: GitService):
    (worktree.path / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    (worktree.path / "src" / "added.js").write_text("export const extra = 1;\n", encoding="utf-8")

    sha = worktree.commit("TS-001: implement the answer")

    assert sha == worktree.get_head_sha()
    assert worktree.is_clean()
    log = run_git(worktree.path, "log", "-1", "--pretty=%s%n%an")
    assert log.splitlines() == ["TS-001: implement the answer", "AI Orchestrator"]


def test_commit_refuses_when_nothing_changed(worktree: GitService):
    with pytest.raises(NothingToCommit):
        worktree.commit("TS-001: nothing happened")


def test_commit_to_the_default_branch_is_refused(git: GitService, fixture_repo: Path):
    """Rule 6: integration into the default branch is not the run's decision."""
    (fixture_repo / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")

    with pytest.raises(ProtectedBranch):
        git.commit("TS-001: straight to main")


def test_commit_refuses_while_conflicts_are_unresolved(
    git: GitService, worktree: GitService, git_settings: Settings
):
    other = git.create_worktree(git_settings.worktree_root / "ts-002-run1", "agent/TS-002-other")
    (other.path / "src" / "app.js").write_text("export const answer = 1;\n", encoding="utf-8")
    other.commit("TS-002: one")
    (worktree.path / "src" / "app.js").write_text("export const answer = 2;\n", encoding="utf-8")
    worktree.commit("TS-001: two")
    # Merging is done with raw Git on purpose: GitService exposes no merge, so
    # the only way to reach a conflicted worktree is from outside it.
    with pytest.raises(subprocess.CalledProcessError):
        run_git(worktree.path, "merge", "agent/TS-002-other")

    with pytest.raises(MergeConflict):
        worktree.commit("TS-001: resolve nothing")


# --- rollback and checkpoints -----------------------------------------------


def test_reset_restores_the_starting_commit_and_removes_new_files(worktree: GitService):
    """Rule 8: a failed attempt returns to a known state."""
    start = worktree.get_head_sha()
    (worktree.path / "src" / "app.js").write_text("broken\n", encoding="utf-8")
    (worktree.path / "src" / "garbage.js").write_text("junk\n", encoding="utf-8")
    worktree.commit("TS-001: a bad attempt")

    worktree.reset_hard_to_sha(start)

    assert worktree.get_head_sha() == start
    assert worktree.is_clean()
    assert not (worktree.path / "src" / "garbage.js").exists()
    assert (worktree.path / "src" / "app.js").read_text(encoding="utf-8") == (
        "export const answer = 41;\n"
    )


def test_reset_on_the_default_branch_is_refused(git: GitService):
    with pytest.raises(ProtectedBranch):
        git.reset_hard_to_sha(git.get_head_sha())


def test_checkpoint_tag_points_at_the_commit(worktree: GitService):
    (worktree.path / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    sha = worktree.commit("TS-001: the answer")

    tagged = worktree.tag_checkpoint("checkpoint/TS-001/attempt-1", message="attempt 1")

    assert tagged == sha
    assert run_git(worktree.path, "rev-list", "-1", "checkpoint/TS-001/attempt-1").strip() == sha


# --- push policy ------------------------------------------------------------


def test_push_is_refused_while_disabled(git: GitService):
    """The default is off, and a run cannot turn it on."""
    git.create_task_branch("agent/TS-001-first")

    with pytest.raises(PushNotPermitted):
        git.push("agent/TS-001-first")


def test_push_to_a_protected_branch_is_refused(fixture_repo: Path, git_settings: Settings):
    enabled = git_settings.model_copy(update={"git_push_enabled": True})
    git = GitService(fixture_repo, default_branch="main", settings=enabled)

    with pytest.raises(ProtectedBranch):
        git.push("main")


def test_force_push_needs_its_own_switch(fixture_repo: Path, git_settings: Settings):
    enabled = git_settings.model_copy(update={"git_push_enabled": True})
    git = GitService(fixture_repo, default_branch="main", settings=enabled)
    git.create_task_branch("agent/TS-001-first")

    with pytest.raises(PushNotPermitted):
        git.push("agent/TS-001-first", force=True)


def test_push_reaches_a_configured_remote(
    fixture_repo: Path, tmp_path: Path, git_settings: Settings
):
    remote = tmp_path / "remote.git"
    run_git(fixture_repo, "init", "--bare", "--quiet", str(remote))
    run_git(fixture_repo, "remote", "add", "origin", str(remote))
    enabled = git_settings.model_copy(update={"git_push_enabled": True})
    git = GitService(fixture_repo, default_branch="main", settings=enabled)
    git.create_task_branch("agent/TS-001-first")

    git.push("agent/TS-001-first")

    assert "agent/TS-001-first" in run_git(fixture_repo, "ls-remote", "--heads", "origin")


# --- command handling -------------------------------------------------------


def test_a_failing_git_command_carries_its_exit_code_and_stderr(git: GitService):
    with pytest.raises(GitCommandFailed) as excinfo:
        git.checkout_branch("agent/does-not-exist")

    assert excinfo.value.exit_code != 0
    assert excinfo.value.stderr


def test_repository_hooks_do_not_run_during_a_commit(worktree: GitService, fixture_repo: Path):
    """Project code must not execute in the orchestrator, only in a worker."""
    hooks = fixture_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = fixture_repo / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    (worktree.path / "src" / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")

    worktree.commit("TS-001: the answer")

    assert not marker.exists()
