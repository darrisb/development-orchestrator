"""Git naming rules and change descriptions (build.md section 10).

Framework-free by design: the orchestrator -- never a model -- chooses branch
names and commit messages, so the rules live in the domain where they can be
tested without a repository on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Branch prefix reserved for orchestrator-owned work. Anything outside it is
#: treated as human-owned and never written to.
AGENT_BRANCH_PREFIX = "agent/"

#: The orchestrator's cumulative accepted state (concern 51). Under the agent
#: prefix because it is orchestrator-owned by the same convention every task
#: branch is, and *not* the project's own branch: the imported branch stays
#: exactly where the operator left it, and integration never advances it.
#:
#: Task worktrees start from this ref rather than from the imported branch, so a
#: task that depends on another one sees the work that other one had accepted.
INTEGRATION_BRANCH = f"{AGENT_BRANCH_PREFIX}integration"

#: Directory name of the project's integration worktree, a sibling of the task
#: worktrees under ``WORKTREE_ROOT/<project>/``. The leading underscore keeps it
#: out of the namespace ``worktree_dir_name`` generates for tasks.
INTEGRATION_WORKTREE_DIR = "_integration"

#: Longest slug appended to a branch name. Git allows far more; a cap keeps
#: worktree directory names and log lines readable.
MAX_SLUG_LENGTH = 48

_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")

#: Refname characters Git rejects outright, plus the ones that make shell and
#: path handling ambiguous. Used to validate ids the manifest supplied.
_UNSAFE_REF_CHARS = re.compile(r"[\s~^:?*\[\]\\\x00-\x1f\x7f]")


class ChangeType(StrEnum):
    """Git's porcelain status letters, named."""

    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPE_CHANGED = "T"
    UNTRACKED = "?"
    UNMERGED = "U"


@dataclass(frozen=True, slots=True)
class FileChange:
    """One path in a diff.

    ``insertions``/``deletions`` are ``None`` for binary files, which Git
    reports as ``-`` in ``--numstat``. That is not zero, and the scope guard
    (section 20) must be able to tell the difference.
    """

    path: str
    change_type: ChangeType = ChangeType.MODIFIED
    insertions: int | None = None
    deletions: int | None = None
    original_path: str | None = None

    @property
    def is_binary(self) -> bool:
        return self.insertions is None and self.deletions is None

    @property
    def line_count(self) -> int:
        return (self.insertions or 0) + (self.deletions or 0)


@dataclass(frozen=True, slots=True)
class StatusEntry:
    """One line of ``git status --porcelain``: staged code, worktree code, path."""

    path: str
    index_status: str = " "
    worktree_status: str = " "
    original_path: str | None = None

    @property
    def is_untracked(self) -> bool:
        return self.index_status == "?" or self.worktree_status == "?"

    @property
    def is_unmerged(self) -> bool:
        """A conflict: Git marks both sides, or either side, with ``U``."""
        codes = {self.index_status, self.worktree_status}
        return "U" in codes or codes in ({"A"}, {"D"})


@dataclass(frozen=True, slots=True)
class DiffSummary:
    """Structured view of a diff, independent of the diff text itself."""

    files: tuple[FileChange, ...] = ()

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def insertions(self) -> int:
        return sum(change.insertions or 0 for change in self.files)

    @property
    def deletions(self) -> int:
        return sum(change.deletions or 0 for change in self.files)

    @property
    def line_count(self) -> int:
        """Insertions plus deletions -- the number the diff-size limit bounds."""
        return self.insertions + self.deletions

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.files)

    @property
    def deleted_paths(self) -> tuple[str, ...]:
        return tuple(
            change.path for change in self.files if change.change_type is ChangeType.DELETED
        )

    @property
    def binary_paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.files if change.is_binary)


def slugify(text: str, *, max_length: int = MAX_SLUG_LENGTH) -> str:
    """Lowercase, hyphen-separated, ASCII-only fragment of ``text``.

    Truncation stops at a hyphen where possible so a branch name ends on a
    whole word rather than mid-syllable.
    """
    slug = _SLUG_SEPARATORS.sub("-", text.strip().lower()).strip("-")
    if len(slug) <= max_length:
        return slug
    clipped = slug[:max_length]
    head, separator, _ = clipped.rpartition("-")
    return (head if separator and head else clipped).strip("-")


def assert_safe_ref_component(component: str, *, label: str = "ref component") -> str:
    """Raise ``ValueError`` unless ``component`` is safe inside a refname.

    Task ids come from a manifest, which is a hand-edited file rather than a
    trusted source. Rejecting a bad id here keeps it out of every later branch
    name, worktree path and command line.
    """
    if not component:
        raise ValueError(f"{label} must not be empty")
    if _UNSAFE_REF_CHARS.search(component):
        raise ValueError(f"{label} {component!r} contains characters Git forbids in a ref")
    if component.startswith("-") or ".." in component or component.endswith((".", ".lock", "/")):
        raise ValueError(f"{label} {component!r} is not a valid ref component")
    return component


def task_branch_name(
    external_task_id: str, title: str, *, prefix: str = AGENT_BRANCH_PREFIX
) -> str:
    """``agent/TS-004-navigation-tree``.

    The task id is preserved verbatim so a branch is always traceable to its
    task (section 10 rule 7); only the title is slugified.
    """
    assert_safe_ref_component(external_task_id, label="task id")
    slug = slugify(title)
    stem = f"{external_task_id}-{slug}" if slug else external_task_id
    return f"{prefix}{stem}"


def task_commit_message(external_task_id: str, title: str) -> str:
    """``TS-004: implement navigation tree`` (section 10 rule 9)."""
    assert_safe_ref_component(external_task_id, label="task id")
    return f"{external_task_id}: {title.strip()}"


def checkpoint_tag_name(external_task_id: str, attempt: int) -> str:
    """Tag marking the state a failed attempt can be reset to."""
    assert_safe_ref_component(external_task_id, label="task id")
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return f"checkpoint/{external_task_id}/attempt-{attempt}"


def worktree_dir_name(external_task_id: str, run_number: int) -> str:
    """Directory name for a task run's worktree.

    Includes the run number so a retried task never collides with the
    leftovers of an earlier run that failed to clean up.
    """
    assert_safe_ref_component(external_task_id, label="task id")
    if run_number < 1:
        raise ValueError("run_number must be >= 1")
    return f"{slugify(external_task_id)}-run{run_number}"
