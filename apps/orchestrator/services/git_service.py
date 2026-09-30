"""Deterministic Git access (build.md section 10).

Every Git operation the orchestrator performs goes through this service. A
model may cause a call to happen, but it never supplies the command: the
argument vectors here are fixed, the branch names come from the domain, and
each rule from section 10 is enforced in code rather than in a prompt.

The service is intentionally not a general Git wrapper. There is no ``run``
escape hatch, no shell, and no way to pass an arbitrary subcommand.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.git import (
    AGENT_BRANCH_PREFIX,
    ChangeType,
    DiffSummary,
    FileChange,
    StatusEntry,
    assert_safe_ref_component,
)
from .git_errors import (
    BranchAlreadyExists,
    BranchMissing,
    DirtyWorktree,
    GitCommandFailed,
    GitCommandTimeout,
    MergeConflict,
    NotARepository,
    NothingToCommit,
    ProtectedBranch,
    PushNotPermitted,
    WorktreePathRejected,
)

logger = get_logger(__name__)

#: Marker appended when diff text is truncated, so a reader (human or
#: reviewer model) can never mistake a clipped diff for a complete one.
DIFF_TRUNCATION_MARKER = "\n[diff truncated by orchestrator]\n"

@dataclass(frozen=True, slots=True)
class CommitSummary:
    """One line of history, for the "recent relevant changes" context slice."""

    sha: str
    author: str
    date: str
    subject: str

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


#: Environment forced on every invocation. Prompting must never block a run,
#: locale must not reorder or reword output, and the host's Git configuration
#: must not change what a command means.
_FORCED_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
    "LANG": "C",
}

#: Config applied per invocation rather than written into the repository, so
#: the orchestrator never mutates a managed repository's configuration.
#:
#: ``safe.directory=*`` is needed because a managed repository is bind-mounted
#: from the host and is therefore owned by a different uid than the container
#: user, which Git otherwise refuses to touch at all. It is scoped to these
#: invocations, and the paths come from the project record rather than from a
#: model, so this does not widen what the orchestrator can reach.
_FORCED_CONFIG = (
    "advice.detachedHead=false",
    "safe.directory=*",
)


@dataclass(frozen=True, slots=True)
class GitResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class WorktreeEntry:
    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    is_main: bool = False


class GitService:
    """Git operations against one worktree (the main repository or a task one).

    Args:
        repository_path: root of the working tree this instance operates on.
        default_branch: the project's integration branch. Protected by default:
            the orchestrator never commits to it directly and never pushes to
            it (section 10 rules 6 and 7).
        protected_branches: extra names or glob patterns to protect.
        settings: infrastructure policy (timeout, identity, push switches).
    """

    def __init__(
        self,
        repository_path: str | Path,
        *,
        default_branch: str = "main",
        protected_branches: Iterable[str] = (),
        settings: Settings | None = None,
    ) -> None:
        self.path = Path(repository_path).expanduser().resolve()
        self.default_branch = default_branch
        self.settings = settings or get_settings()
        self.protected_branches: frozenset[str] = frozenset(
            {default_branch, *protected_branches}
        )
        if not self.path.is_dir():
            raise NotARepository(self.path)
        # A main worktree has a .git directory; a linked worktree has a .git file
        # pointing at the main one. Both are valid roots for this service.
        if not (self.path / ".git").exists():
            raise NotARepository(self.path)

    # ---------------------------------------------------------------- plumbing

    def _run(
        self,
        *args: str,
        check: bool = True,
        timeout_seconds: int | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        argv: tuple[str, ...] = ("git", "--no-pager", *_config_flags(), *args)
        timeout = timeout_seconds or self.settings.git_command_timeout_seconds
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                argv,
                cwd=self.path,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                env={**os.environ, **_FORCED_ENV},
                check=False,
                input=input_text,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandTimeout(argv, timeout) from exc
        except FileNotFoundError as exc:  # git missing from the image
            raise NotARepository(self.path) from exc

        result = GitResult(argv, completed.returncode, completed.stdout, completed.stderr)
        if check and result.exit_code != 0:
            raise GitCommandFailed(argv, result.exit_code, result.stderr)
        return result

    def _identity_flags(self) -> tuple[str, ...]:
        return (
            "-c",
            f"user.name={self.settings.git_author_name}",
            "-c",
            f"user.email={self.settings.git_author_email}",
        )

    def for_worktree(self, path: str | Path) -> GitService:
        """A service rooted at ``path``, carrying this instance's policy."""
        return GitService(
            path,
            default_branch=self.default_branch,
            protected_branches=self.protected_branches - {self.default_branch},
            settings=self.settings,
        )

    # ------------------------------------------------------------------- reads

    def get_current_branch(self) -> str | None:
        """Current branch, or ``None`` when HEAD is detached."""
        branch = self._run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return branch.stdout.strip() or None

    def get_head_sha(self, rev: str = "HEAD") -> str:
        return self.resolve_sha(rev)

    def resolve_sha(self, rev: str) -> str:
        assert_safe_ref_component(rev, label="revision")
        return self._run("rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()

    def branch_exists(self, branch: str) -> bool:
        assert_safe_ref_component(branch, label="branch")
        return (
            self._run(
                "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
            ).exit_code
            == 0
        )

    def get_status(self) -> tuple[StatusEntry, ...]:
        """Porcelain status, untracked files included and individually listed."""
        raw = self._run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ).stdout
        return _parse_status(raw)

    def is_clean(self) -> bool:
        return not self.get_status()

    def is_ignored(self, path: str) -> bool:
        """Whether project Git policy excludes a repository-relative path."""
        return self._run("check-ignore", "--quiet", "--", path, check=False).exit_code == 0

    def ensure_clean_worktree(self, *, allow_dirty: bool | None = None) -> None:
        """Section 10 rule 4: refuse to begin against a dirty workspace.

        ``allow_dirty`` defaults to the ``GIT_ALLOW_DIRTY_START`` policy. When
        permitted, the dirty paths are logged rather than silently accepted --
        an operator needs to see what the run started on top of.
        """
        permitted = (
            self.settings.git_allow_dirty_start if allow_dirty is None else allow_dirty
        )
        entries = self.get_status()
        if not entries:
            return
        paths = tuple(entry.path for entry in entries)
        if not permitted:
            raise DirtyWorktree(self.path, paths)
        logger.warning(
            "dirty_worktree_allowed", path=str(self.path), paths=list(paths[:20])
        )

    def assert_no_conflicts(self) -> None:
        """Raise ``MergeConflict`` if the worktree has unresolved paths."""
        conflicted = tuple(entry.path for entry in self.get_status() if entry.is_unmerged)
        if conflicted:
            raise MergeConflict(self.path, conflicted)

    def list_tracked_files(self) -> tuple[str, ...]:
        """Every tracked path, repository-relative and sorted.

        Tracked rather than walked: ``.gitignore`` already says which files
        are generated, and a context builder that walked the directory would
        propose ``node_modules`` to the coder.
        """
        raw = self._run("ls-files", "-z").stdout
        return tuple(sorted(entry for entry in raw.split("\0") if entry))

    # -------------------------------------------------- tree/patch inspection
    #
    # Read-only plumbing added for the human conflict-resolution contract
    # (concern 73 follow-up). These exist so the resolution validator can
    # compare *trees* rather than trust an operator's description of what they
    # did: a resolution has to be proved faithful to the human commit it claims
    # to carry, and the only acceptable authority on that is Git.

    def list_changed_paths(self, base: str, head: str) -> tuple[str, ...]:
        """Paths whose content differs between two commits, sorted.

        ``git diff --name-only`` rather than a numstat parse: the question this
        answers is "which files did this commit touch at all", and a file that
        changed only in mode or only in whitespace still counts as touched.
        """
        assert_safe_ref_component(base, label="revision")
        assert_safe_ref_component(head, label="revision")
        raw = self._run("diff", "--name-only", "-z", f"{base}..{head}").stdout
        return tuple(sorted(path for path in raw.split("\0") if path))

    def commit_parents(self, rev: str) -> tuple[str, ...]:
        """A commit's parents in order; empty for a root commit."""
        return tuple(self._run("rev-list", "--parents", "-n", "1", rev).stdout.split()[1:])

    def read_file_at(self, rev: str, path: str) -> str | None:
        """A file's content at a commit, or ``None`` when it does not exist.

        ``None`` is a real answer rather than an error: "the resolution deleted
        a file the human edited" and "the resolution kept it" are different
        trees, and the validator has to be able to tell them apart.
        """
        assert_safe_ref_component(rev, label="revision")
        result = self._run("show", f"{rev}:{path}", check=False)
        if result.exit_code != 0:
            return None
        return result.stdout

    def added_lines_between(self, base: str, head: str, path: str) -> tuple[str, ...]:
        """Lines ``head`` adds to ``path`` relative to ``base``, verbatim.

        Parsed from a zero-context unified diff so the result depends only on
        the two trees, never on how a merge happened to be resolved.
        """
        assert_safe_ref_component(base, label="revision")
        assert_safe_ref_component(head, label="revision")
        diff = self._run(
            "diff", "--no-color", "--unified=0", "--no-renames", f"{base}..{head}", "--", path
        ).stdout
        added: list[str] = []
        for line in diff.splitlines():
            # "+++ b/path" is the file header, not added content. The
            # "\\ No newline at end of file" marker starts with a backslash and
            # is metadata about the previous line, so it is excluded too.
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added.append(line[1:])
        return tuple(added)

    def recent_commits(
        self, *, limit: int = 5, paths: Sequence[str] = (), rev: str = "HEAD"
    ) -> tuple[CommitSummary, ...]:
        """Recent commits, newest first, optionally restricted to ``paths``.

        Read-only history for context (section 15 priority 7). ``paths`` is
        passed after ``--`` so a path can never be read as a revision.
        """
        if limit < 1:
            return ()
        assert_safe_ref_component(rev, label="revision")
        args = [
            "log",
            f"--max-count={limit}",
            "--no-merges",
            "--date=short",
            "--format=%H%x1f%an%x1f%ad%x1f%s",
            rev,
        ]
        if paths:
            args.append("--")
            args.extend(str(path) for path in paths)
        result = self._run(*args, check=False)
        if result.exit_code != 0:
            # An unborn branch or a path with no history is not an error here.
            return ()
        return tuple(
            summary
            for line in result.stdout.splitlines()
            if (summary := _parse_commit_summary(line)) is not None
        )

    def list_worktrees(self) -> tuple[WorktreeEntry, ...]:
        return _parse_worktrees(self._run("worktree", "list", "--porcelain").stdout)

    # -------------------------------------------------------------- branch ops

    def create_task_branch(self, branch: str, *, start_point: str | None = None) -> str:
        """Create ``branch`` at ``start_point`` without checking it out.

        Returns the starting SHA, which the caller records on the run before any
        work happens (rule 3) so a failed attempt can always be reset (rule 8).
        """
        assert_safe_ref_component(branch, label="branch")
        self._assert_writable_branch(branch, "create")
        if self.branch_exists(branch):
            raise BranchAlreadyExists(branch)
        start_sha = self.resolve_sha(start_point or "HEAD")
        self._run("branch", branch, start_sha)
        logger.info("task_branch_created", branch=branch, start_sha=start_sha)
        return start_sha

    def checkout_branch(self, branch: str, *, create: bool = False) -> None:
        assert_safe_ref_component(branch, label="branch")
        if create:
            self._assert_writable_branch(branch, "create")
            self._run("checkout", "-b", branch)
        else:
            self._run("checkout", branch)

    def delete_branch(self, branch: str, *, force: bool = True) -> None:
        assert_safe_ref_component(branch, label="branch")
        self._assert_writable_branch(branch, "delete")
        self._run("branch", "-D" if force else "-d", branch)

    # ------------------------------------------------------------- worktree ops

    def create_worktree(
        self,
        path: str | Path,
        branch: str,
        *,
        start_point: str | None = None,
        create_branch: bool = True,
    ) -> GitService:
        """Add a linked worktree at ``path`` and return a service rooted there.

        The path must live under ``WORKTREE_ROOT``: an isolated checkout is the
        boundary that keeps a coding worker away from the managed repository's
        own working tree, so a path outside it is refused rather than fixed up.
        """
        assert_safe_ref_component(branch, label="branch")
        target = self._validated_worktree_path(path)
        if target.exists() and any(target.iterdir()):
            raise WorktreePathRejected(target, "an empty or absent directory")

        if create_branch:
            self._assert_writable_branch(branch, "create")
            if self.branch_exists(branch):
                raise BranchAlreadyExists(branch)
            start_sha = self.resolve_sha(start_point or "HEAD")
            target.parent.mkdir(parents=True, exist_ok=True)
            self._run("worktree", "add", "-b", branch, str(target), start_sha)
        else:
            if not self.branch_exists(branch):
                raise BranchMissing(branch)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._run("worktree", "add", str(target), branch)

        logger.info("worktree_created", path=str(target), branch=branch)
        return self.for_worktree(target)

    def create_detached_worktree(self, path: str | Path, start_point: str) -> GitService:
        """Add a linked worktree at ``path`` with no branch checked out.

        The integration worktree (concern 51) is detached so that the branch it
        will eventually advance is not checked out anywhere: a branch nobody has
        checked out can be moved with ``force_branch`` after the gates pass, and
        left alone when they do not.
        """
        target = self._validated_worktree_path(path)
        if target.exists() and any(target.iterdir()):
            raise WorktreePathRejected(target, "an empty or absent directory")
        start_sha = self.resolve_sha(start_point)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run("worktree", "add", "--detach", str(target), start_sha)
        logger.info("detached_worktree_created", path=str(target), sha=start_sha)
        return self.for_worktree(target)

    def remove_worktree(
        self, path: str | Path, *, force: bool = True, delete_branch: str | None = None
    ) -> None:
        """Remove a linked worktree, then prune stale administrative entries.

        Tolerant on purpose: cleanup runs in failure paths too, and a worktree
        whose directory is already gone must not turn a recoverable run failure
        into an unrecoverable one.
        """
        target = self._validated_worktree_path(path)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        result = self._run(*args, str(target), check=False)
        if result.exit_code != 0:
            logger.warning(
                "worktree_remove_failed",
                path=str(target),
                stderr=result.stderr.strip(),
            )
        self.prune_worktrees()
        if target.exists():
            logger.warning("worktree_path_remains", path=str(target))
        if delete_branch is not None and self.branch_exists(delete_branch):
            self.delete_branch(delete_branch)
        logger.info("worktree_removed", path=str(target))

    def prune_worktrees(self) -> None:
        self._run("worktree", "prune")

    # ----------------------------------------------------------------- diff ops

    def stage_all(self, *, intent_to_add: bool = False) -> None:
        """Stage every change, including deletions and untracked files.

        ``intent_to_add`` records untracked paths in the index without their
        content, which is what makes them visible to ``git diff`` while leaving
        the commit decision to the caller.
        """
        args = ["add", "--all"]
        if intent_to_add:
            args.append("--intent-to-add")
        self._run(*args, ".")

    def get_diff(
        self,
        base: str = "HEAD",
        *,
        include_untracked: bool = True,
        max_bytes: int | None = None,
    ) -> str:
        """Unified diff of the working tree against ``base``.

        Truncation is explicit: when ``max_bytes`` is given and exceeded, the
        text ends with ``DIFF_TRUNCATION_MARKER`` rather than being silently
        cut, because a reviewer must know it did not see everything.
        """
        if include_untracked:
            self.stage_all(intent_to_add=True)
        diff = self._run("diff", "--no-color", base).stdout
        if max_bytes is not None and len(diff.encode()) > max_bytes:
            return diff.encode()[:max_bytes].decode(errors="ignore") + DIFF_TRUNCATION_MARKER
        return diff

    def get_diff_between(self, base: str, head: str, *, max_bytes: int | None = None) -> str:
        """Diff of two commits, used once the candidate work is committed."""
        assert_safe_ref_component(base, label="revision")
        assert_safe_ref_component(head, label="revision")
        diff = self._run("diff", "--no-color", f"{base}..{head}").stdout
        if max_bytes is not None and len(diff.encode()) > max_bytes:
            return diff.encode()[:max_bytes].decode(errors="ignore") + DIFF_TRUNCATION_MARKER
        return diff

    def get_changed_files(
        self, base: str = "HEAD", *, include_untracked: bool = True
    ) -> tuple[FileChange, ...]:
        if include_untracked:
            self.stage_all(intent_to_add=True)
        return self._changed_files(("diff", base))

    def get_changed_files_between(self, base: str, head: str) -> tuple[FileChange, ...]:
        assert_safe_ref_component(base, label="revision")
        assert_safe_ref_component(head, label="revision")
        return self._changed_files(("diff", f"{base}..{head}"))

    def get_diff_summary(
        self, base: str = "HEAD", *, include_untracked: bool = True
    ) -> DiffSummary:
        return DiffSummary(self.get_changed_files(base, include_untracked=include_untracked))

    def get_diff_line_count(
        self, base: str = "HEAD", *, include_untracked: bool = True
    ) -> int:
        """Insertions plus deletions -- the number ``max_diff_lines`` bounds."""
        return self.get_diff_summary(base, include_untracked=include_untracked).line_count

    def _changed_files(self, diff_args: Sequence[str]) -> tuple[FileChange, ...]:
        numstat = _parse_numstat(self._run(*diff_args, "--numstat", "-z").stdout)
        statuses = _parse_name_status(self._run(*diff_args, "--name-status", "-z").stdout)
        changes = []
        for path, (insertions, deletions, original) in numstat.items():
            change_type, status_original = statuses.get(path, (ChangeType.MODIFIED, None))
            changes.append(
                FileChange(
                    path=path,
                    change_type=change_type,
                    insertions=insertions,
                    deletions=deletions,
                    original_path=original or status_original,
                )
            )
        # Deterministic order: Git's output order depends on its diff algorithm
        # and rename detection, and callers compare these lists across runs.
        return tuple(sorted(changes, key=lambda change: change.path))

    # --------------------------------------------------------------- write ops

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        """Stage everything and commit. Returns the new SHA.

        Refuses to commit onto a protected branch (rule 6/7): task work belongs
        on its own branch, and integration is a separate, human-gated step.
        """
        current = self.get_current_branch()
        if current is not None:
            self._assert_writable_branch(current, "commit to")
        self.assert_no_conflicts()
        self.stage_all()
        if not allow_empty and not self._has_staged_changes():
            raise NothingToCommit(f"Worktree {self.path} matches HEAD; nothing to commit")

        # --no-verify: a managed repository's hooks are project code, and this
        # process is the orchestrator, not the sandboxed worker. Verification is
        # run deliberately inside a worker (section 17), never as a commit side
        # effect in the container that holds the database connection.
        args = [*self._identity_flags(), "commit", "--no-verify", "--message", message]
        if allow_empty:
            args.append("--allow-empty")
        self._run(*args)
        sha = self.get_head_sha()
        logger.info("commit_created", sha=sha, branch=current, path=str(self.path))
        return sha

    def _has_staged_changes(self) -> bool:
        return self._run("diff", "--cached", "--quiet", check=False).exit_code != 0

    def write_tree(self) -> str:
        """The tree object the current index describes.

        Reads the index only: the result is a function of what was staged, so
        nothing untracked and nothing unstaged can reach it.
        """
        return self._run("write-tree").stdout.strip()

    def commit_tree(
        self,
        tree: str,
        *,
        message: str,
        parents: Sequence[str] = (),
    ) -> str:
        """Create a commit from an explicit tree and parent list.

        ``commit-tree`` rather than ``commit`` because the human
        conflict-resolution path needs a commit with *two* chosen parents and
        a tree it assembled, which is not a working-tree commit and cannot be
        expressed as a ``MERGE_HEAD`` side effect. Nothing is written to the
        index, ``HEAD`` is untouched, and no ref moves: the returned object is
        reachable only if the caller deliberately points a ref at it, which is
        what makes "every gate passed first" expressible at all.
        """
        assert_safe_ref_component(tree, label="tree")
        args: list[str] = [*self._identity_flags(), "commit-tree", tree]
        for parent in parents:
            assert_safe_ref_component(parent, label="revision")
            args += ["-p", parent]
        args += ["-m", message]
        sha = self._run(*args).stdout.strip()
        logger.info(
            "commit_tree_created",
            sha=sha,
            tree=tree,
            parents=list(parents),
            path=str(self.path),
        )
        return sha

    def push(self, branch: str, *, remote: str | None = None, force: bool = False) -> None:
        """Push a task branch, subject to policy.

        Three refusals, in order: pushing must be enabled at all (rule 5's
        spirit -- off by default), the target must not be a protected branch
        (rule 6), and a force push requires its own explicit switch (rule 5).
        """
        assert_safe_ref_component(branch, label="branch")
        if not self.settings.git_push_enabled:
            raise PushNotPermitted(
                f"Refusing to push {branch}: GIT_PUSH_ENABLED is false"
            )
        if self._is_protected(branch):
            raise ProtectedBranch(branch, "push to")
        if force and not self.settings.git_force_push_enabled:
            raise PushNotPermitted(
                f"Refusing to force-push {branch}: GIT_FORCE_PUSH_ENABLED is false"
            )
        target_remote = remote or self.settings.git_push_remote
        args = ["push", "--set-upstream"]
        if force:
            # Fails rather than overwrites when the remote moved unexpectedly.
            args.append("--force-with-lease")
        self._run(*args, target_remote, branch)
        logger.info("branch_pushed", branch=branch, remote=target_remote, force=force)

    def reset_hard_to_sha(self, sha: str, *, clean_untracked: bool = True) -> None:
        """Rule 8: return this worktree to a known starting commit.

        Untracked files are removed as well, otherwise a failed attempt leaves
        half-written new files behind for the next attempt to trip over.
        """
        current = self.get_current_branch()
        if current is not None:
            self._assert_writable_branch(current, "reset")
        resolved = self.resolve_sha(sha)
        self._run("reset", "--hard", resolved)
        if clean_untracked:
            self._run("clean", "-fd")
        logger.info("worktree_reset", path=str(self.path), sha=resolved)

    def checkout_detached(self, rev: str) -> str:
        """Check out ``rev`` with no branch attached. Returns the resolved SHA.

        The integration worktree is always detached (concern 51): a branch that
        is not checked out anywhere can be moved with ``force_branch`` once the
        gates have passed, which is what keeps a failed integration from
        advancing anything.
        """
        sha = self.resolve_sha(rev)
        self._run("checkout", "--detach", sha)
        return sha

    def merge(self, rev: str, *, message: str) -> str:
        """Merge ``rev`` into the current HEAD. Returns the resulting SHA.

        Always a real merge attempt with no interactive resolution and no
        ``rerere``: the result is a function of the two trees, so the same two
        commits always integrate the same way or fail the same way.

        A fast-forward is allowed and is the normal case, because a candidate
        built on top of the current baseline contains it already.

        Raises:
            MergeConflict: the merge left unresolved paths. The merge is aborted
                first, so the worktree is returned to where it was and the
                caller's ref is untouched.
        """
        result = self._run(
            *self._identity_flags(),
            "-c",
            "rerere.enabled=false",
            "merge",
            "--no-edit",
            "--no-verify",
            "--message",
            message,
            self.resolve_sha(rev),
            check=False,
        )
        if result.exit_code != 0:
            conflicts = tuple(
                entry.path for entry in self.get_status() if entry.is_unmerged
            )
            self._run("merge", "--abort", check=False)
            raise MergeConflict(self.path, conflicts or ("unknown",))
        return self.get_head_sha()

    def contains_commit(self, commit: str, *, ref: str = "HEAD") -> bool:
        """Whether ``ref``'s history already contains ``commit``.

        The deterministic form of the question "did that work actually land?".
        Asked instead of trusting an operator's word that a conflict was resolved
        by hand (concern 51): a commit either is an ancestor of the baseline or
        it is not, and Git is the only acceptable authority on which.

        Raises:
            GitError: neither revision resolves.
        """
        result = self._run(
            "merge-base",
            "--is-ancestor",
            self.resolve_sha(commit),
            self.resolve_sha(ref),
            check=False,
        )
        # Exit 1 is the answer "no"; anything else would be a broken repository,
        # and `resolve_sha` has already proved both revisions exist.
        return result.exit_code == 0

    def force_branch(self, branch: str, sha: str) -> str:
        """Point ``branch`` at ``sha``, creating it if it does not exist.

        The only way the integration ref moves. It refuses the project's own
        branch for the same reason ``commit`` does: the imported branch is the
        operator's, and advancing it is not this system's decision to make.
        """
        assert_safe_ref_component(branch, label="branch")
        self._assert_writable_branch(branch, "move")
        resolved = self.resolve_sha(sha)
        self._run("branch", "--force", branch, resolved)
        logger.info("branch_moved", branch=branch, sha=resolved)
        return resolved

    def restore_patch(self, base: str, patch: str) -> None:
        """Discard command side effects, then restore the measured candidate."""
        self.reset_hard_to_sha(base)
        if patch.strip():
            self._run("apply", "--whitespace=nowarn", "-", input_text=patch)
        logger.info("candidate_patch_restored", path=str(self.path), sha=base)

    def tag_checkpoint(
        self, tag: str, *, sha: str | None = None, message: str | None = None
    ) -> str:
        """Mark a commit so an operator can find it after an escalation."""
        assert_safe_ref_component(tag, label="tag")
        target = self.resolve_sha(sha or "HEAD")
        if message is None:
            self._run("tag", tag, target)
        else:
            self._run(
                *self._identity_flags(),
                "tag",
                "--annotate",
                "--message",
                message,
                tag,
                target,
            )
        logger.info("checkpoint_tagged", tag=tag, sha=target)
        return target

    # ---------------------------------------------------------------- policies

    def _is_protected(self, branch: str) -> bool:
        return any(fnmatch(branch, pattern) for pattern in self.protected_branches)

    def _assert_writable_branch(self, branch: str, operation: str) -> None:
        if self._is_protected(branch):
            raise ProtectedBranch(branch, operation)

    def _validated_worktree_path(self, path: str | Path) -> Path:
        root = self.settings.worktree_root
        candidate = Path(path).expanduser()
        if candidate.exists():
            candidate = candidate.resolve()
        else:
            # The leaf usually does not exist yet, so resolve the parent only --
            # while still resolving it, so a symlink cannot escape the root.
            candidate = candidate.parent.resolve() / candidate.name
        if candidate == root or root not in candidate.parents:
            raise WorktreePathRejected(candidate, root)
        return candidate


def _config_flags() -> tuple[str, ...]:
    flags: list[str] = []
    for setting in _FORCED_CONFIG:
        flags.extend(("-c", setting))
    return tuple(flags)


def _parse_status(raw: str) -> tuple[StatusEntry, ...]:
    """Parse ``git status --porcelain=v1 -z``.

    Records are NUL-terminated. A rename or copy is two records: the entry
    itself, then the original path.
    """
    fields = [field for field in raw.split("\0") if field]
    entries: list[StatusEntry] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        index_status, worktree_status, path = record[0], record[1], record[3:]
        original: str | None = None
        renamed = {index_status, worktree_status} & {"R", "C"}
        if renamed and index < len(fields):
            original = fields[index]
            index += 1
        entries.append(
            StatusEntry(
                path=path,
                index_status=index_status,
                worktree_status=worktree_status,
                original_path=original,
            )
        )
    return tuple(entries)


def _parse_numstat(raw: str) -> dict[str, tuple[int | None, int | None, str | None]]:
    """Parse ``git diff --numstat -z``.

    With ``-z`` a rename emits an empty path field followed by the old and new
    paths as separate records. ``-`` counts mean a binary file, which is kept
    as ``None`` rather than collapsed to zero.
    """
    fields = raw.split("\0")
    result: dict[str, tuple[int | None, int | None, str | None]] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        parts = record.split("\t")
        if len(parts) < 3:
            continue
        insertions, deletions, path = parts[0], parts[1], parts[2]
        original: str | None = None
        if path == "":  # rename or copy: old and new paths follow as records
            if index + 1 >= len(fields):
                break
            original, path = fields[index], fields[index + 1]
            index += 2
        result[path] = (_count(insertions), _count(deletions), original)
    return result


def _parse_name_status(raw: str) -> dict[str, tuple[ChangeType, str | None]]:
    """Parse ``git diff --name-status -z`` into change types by new path."""
    fields = [field for field in raw.split("\0") if field]
    result: dict[str, tuple[ChangeType, str | None]] = {}
    index = 0
    while index < len(fields):
        code = fields[index]
        index += 1
        if index >= len(fields):
            break
        letter = code[0]
        if letter in ("R", "C") and index + 1 < len(fields):
            original, path = fields[index], fields[index + 1]
            index += 2
            result[path] = (ChangeType(letter), original)
            continue
        path = fields[index]
        index += 1
        result[path] = (_change_type(letter), None)
    return result


def _change_type(letter: str) -> ChangeType:
    try:
        return ChangeType(letter)
    except ValueError:
        return ChangeType.MODIFIED


def _count(value: str) -> int | None:
    return None if value == "-" else int(value)


def _parse_worktrees(raw: str) -> tuple[WorktreeEntry, ...]:
    entries: list[WorktreeEntry] = []
    current: dict[str, object] = {}
    for line in raw.splitlines():
        if not line:
            if current:
                entries.append(_worktree_entry(current, is_main=not entries))
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value or True
    if current:
        entries.append(_worktree_entry(current, is_main=not entries))
    return tuple(entries)


def _worktree_entry(fields: dict[str, object], *, is_main: bool) -> WorktreeEntry:
    branch = fields.get("branch")
    return WorktreeEntry(
        path=Path(str(fields.get("worktree", ""))),
        head=str(fields["HEAD"]) if "HEAD" in fields else None,
        branch=str(branch).removeprefix("refs/heads/") if isinstance(branch, str) else None,
        detached="detached" in fields,
        is_main=is_main,
    )


def _parse_commit_summary(line: str) -> CommitSummary | None:
    fields = line.split("\x1f")
    if len(fields) != 4:
        return None
    sha, author, date, subject = (field.strip() for field in fields)
    return CommitSummary(sha=sha, author=author, date=date, subject=subject)


__all__ = [
    "AGENT_BRANCH_PREFIX",
    "DIFF_TRUNCATION_MARKER",
    "CommitSummary",
    "GitResult",
    "GitService",
    "WorktreeEntry",
]
