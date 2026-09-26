"""Git failures, classified.

A single ``GitError`` would tell the workflow nothing: a dirty workspace is an
operator problem, a refused push is a policy decision, and a merge conflict is
a run failure with its own recovery path (build.md section 49). They are
distinct types so the caller can react without parsing messages.
"""

from __future__ import annotations

from .errors import ServiceError


class GitError(ServiceError):
    """Base class for Git failures."""


class GitCommandFailed(GitError):
    """A Git invocation exited non-zero."""

    def __init__(self, argv: tuple[str, ...], exit_code: int, stderr: str) -> None:
        command = " ".join(argv)
        super().__init__(f"git command failed ({exit_code}): {command}\n{stderr.strip()}")
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr


class GitCommandTimeout(GitError):
    def __init__(self, argv: tuple[str, ...], timeout_seconds: float) -> None:
        super().__init__(
            f"git command timed out after {timeout_seconds}s: {' '.join(argv)}"
        )
        self.argv = argv
        self.timeout_seconds = timeout_seconds


class NotARepository(GitError):
    def __init__(self, path: object) -> None:
        super().__init__(f"{path} is not a Git repository")
        self.path = path


class DirtyWorktree(GitError):
    """Section 10 rule 4: refuse to start against unexpected local changes."""

    def __init__(self, path: object, entries: tuple[str, ...]) -> None:
        shown = ", ".join(entries[:10])
        if len(entries) > 10:
            shown += f", ... ({len(entries)} paths total)"
        super().__init__(f"Worktree {path} has uncommitted changes: {shown}")
        self.path = path
        self.entries = entries


class BranchAlreadyExists(GitError):
    def __init__(self, branch: str) -> None:
        super().__init__(f"Branch {branch} already exists")
        self.branch = branch


class BranchMissing(GitError):
    def __init__(self, branch: str) -> None:
        super().__init__(f"Branch {branch} does not exist")
        self.branch = branch


class ProtectedBranch(GitError):
    """An operation targeted a branch the orchestrator must not write to."""

    def __init__(self, branch: str, operation: str) -> None:
        super().__init__(f"Refusing to {operation} protected branch {branch}")
        self.branch = branch
        self.operation = operation


class PushNotPermitted(GitError):
    """Push was attempted while policy forbids it (rules 5 and 6)."""


class WorktreePathRejected(GitError):
    """A worktree path fell outside the configured worktree root."""

    def __init__(self, path: object, root: object) -> None:
        super().__init__(f"Worktree path {path} is not inside {root}")
        self.path = path
        self.root = root


class MergeConflict(GitError):
    def __init__(self, path: object, paths: tuple[str, ...]) -> None:
        super().__init__(f"Unresolved conflicts in {path}: {', '.join(paths)}")
        self.path = path
        self.paths = paths


class NothingToCommit(GitError):
    """Commit was requested but the worktree matches HEAD."""


class WorktreeMissing(GitError):
    """A run's worktree was expected on disk and is not there.

    Distinct from ``WorktreePathRejected``: that one refuses a path, this one
    reports that a run cannot be resumed or delivered from where it worked.
    Recoverable by policy, not by retrying -- see ``services.recovery``.
    """
