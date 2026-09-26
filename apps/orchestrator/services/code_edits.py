"""Applying a coder's edits to a worktree (build.md phase G item 4).

This is the only code in the system that writes a file on a model's behalf, so
it is the boundary where the scope allowance stops being advice. Every edit
passes three gates before a byte is written:

1.  **Structural** -- the path must be repository-relative: no absolute path,
    no ``~``, no ``..`` segment. Refused, never normalised.
2.  **Policy** -- the scope guard (``domain.scope``) must permit a write there:
    not protected, not declared inspect-only, inside the task's allowance.
3.  **Filesystem** -- the resolved path must still be inside the worktree, and
    neither it nor any parent may be a symbolic link. A symlink is how an
    otherwise blameless relative path reaches ``/etc`` or the managed
    repository, and a checkout can contain one legitimately.

Refusals are recorded, not raised: they belong in the completion report as
evidence, and the coder is told exactly which edit was refused and why so the
next attempt can be different. A refusal for a *scope* reason is a different
matter and the caller escalates it -- see ``EditApplication.scope_refusals``.

Nothing here executes anything. No command, no hook, no shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config.logging import get_logger
from ..domain.completion import RejectedEdit
from ..domain.edits import CodeChangeSet, EditOperation, FileEdit
from ..domain.scope import ScopeFindingKind, ScopePolicy, check_write_path, is_within_repository

logger = get_logger(__name__)

#: Refusal kinds that mean the coder went outside its allowance rather than
#: made a mistake about the tree. These are scope violations, and the caller
#: rolls the attempt back instead of sending it on to verification.
_SCOPE_REFUSALS: frozenset[ScopeFindingKind] = frozenset(
    {
        ScopeFindingKind.PROTECTED_PATH,
        ScopeFindingKind.OUTSIDE_ALLOWANCE,
        ScopeFindingKind.INSPECT_ONLY,
        ScopeFindingKind.TOO_MANY_FILES,
    }
)


@dataclass(frozen=True, slots=True)
class EditApplication:
    """What was written, what was refused, and what to do about it."""

    written: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    rejected: tuple[RejectedEdit, ...] = ()
    #: Refusal kinds encountered, so the caller can tell a scope violation from
    #: an edit that simply did not match the tree.
    refusal_kinds: tuple[ScopeFindingKind, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def applied_paths(self) -> tuple[str, ...]:
        """Every path this application changed, written or deleted."""
        return tuple(dict.fromkeys([*self.written, *self.deleted]))

    @property
    def scope_refusals(self) -> tuple[RejectedEdit, ...]:
        """Refusals that mean the coder left its allowance."""
        if not set(self.refusal_kinds) & _SCOPE_REFUSALS:
            return ()
        return self.rejected

    @property
    def changed_anything(self) -> bool:
        return bool(self.written or self.deleted)

    def describe(self) -> dict[str, object]:
        return {
            "written": list(self.written),
            "deleted": list(self.deleted),
            "rejected": [edit.describe() for edit in self.rejected],
            "warnings": list(self.warnings),
        }


def apply_change_set(
    change_set: CodeChangeSet, *, root: Path, policy: ScopePolicy
) -> EditApplication:
    """Write the permitted edits of ``change_set`` into ``root``.

    ``root`` must be the run's worktree, never the managed repository: this
    function does not check which one it was handed, because by the time an
    edit reaches it the decision of where a run may write has already been made
    by ``services.workspace``.

    Edits are applied in the order the coder gave them, and a refusal does not
    stop the ones after it: a change set is not a transaction, and the
    completion report plus the captured diff describe exactly what the tree
    now holds.
    """
    resolved_root = root.expanduser().resolve()
    written: list[str] = []
    deleted: list[str] = []
    rejected: list[RejectedEdit] = []
    refusal_kinds: list[ScopeFindingKind] = []
    warnings: list[str] = []

    if len(change_set.edits) > policy.max_files_changed:
        # Refused as a whole rather than clipped: applying the first twelve of
        # thirty edits produces a half-implemented candidate that would waste a
        # verification cycle proving it does not work.
        reason = (
            f"the change set edits {len(change_set.edits)} files; the task allows "
            f"{policy.max_files_changed}"
        )
        logger.warning("edits_refused_wholesale", root=str(resolved_root), reason=reason)
        return EditApplication(
            rejected=tuple(
                RejectedEdit(path=edit.path, operation=edit.operation.value, reason=reason)
                for edit in change_set.edits
            ),
            refusal_kinds=(ScopeFindingKind.TOO_MANY_FILES,),
        )

    for edit in change_set.edits:
        outcome = _apply_one(edit, root=resolved_root, policy=policy)
        if outcome.rejection is not None:
            rejected.append(outcome.rejection)
            if outcome.kind is not None:
                refusal_kinds.append(outcome.kind)
            continue
        if outcome.warning:
            warnings.append(outcome.warning)
        if edit.operation is EditOperation.DELETE:
            deleted.append(edit.path)
        else:
            written.append(edit.path)

    logger.info(
        "edits_applied",
        root=str(resolved_root),
        written=len(written),
        deleted=len(deleted),
        rejected=len(rejected),
    )
    return EditApplication(
        written=tuple(written),
        deleted=tuple(deleted),
        rejected=tuple(rejected),
        refusal_kinds=tuple(dict.fromkeys(refusal_kinds)),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class _Outcome:
    rejection: RejectedEdit | None = None
    kind: ScopeFindingKind | None = None
    warning: str | None = None


def _apply_one(edit: FileEdit, *, root: Path, policy: ScopePolicy) -> _Outcome:
    def refuse(reason: str, kind: ScopeFindingKind | None = None) -> _Outcome:
        logger.warning(
            "edit_refused",
            path=edit.path,
            operation=edit.operation.value,
            reason=reason,
        )
        return _Outcome(
            rejection=RejectedEdit(
                path=edit.path, operation=edit.operation.value, reason=reason
            ),
            kind=kind,
        )

    if not is_within_repository(edit.path):
        return refuse("the path is not repository-relative")

    finding = check_write_path(edit.path, policy)
    if finding is not None:
        return refuse(finding.detail, finding.kind)

    target = root / edit.path
    containment = _containment_problem(target, root)
    if containment is not None:
        return refuse(containment)

    if edit.operation is EditOperation.DELETE:
        if not target.is_file():
            return refuse("the file does not exist, so there is nothing to delete")
        target.unlink()
        return _Outcome()

    if edit.operation is EditOperation.UPDATE and not target.is_file():
        return refuse(
            "the file does not exist; an update must name a file that is already "
            "in the repository, and a new file needs operation 'create'"
        )

    warning: str | None = None
    if edit.operation is EditOperation.CREATE and target.is_file():
        # Not a refusal: the path is inside the allowance either way, and a
        # coder calling an overwrite a "create" is a labelling slip, not an
        # attempt to reach somewhere it should not be.
        warning = f"{edit.path} was created over an existing file"

    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" would pass "\r\n" through from a model that emitted Windows
    # line endings; writing text normally keeps the file in the repository's
    # own convention and keeps the diff readable.
    target.write_text(edit.content or "", encoding="utf-8")
    return _Outcome(warning=warning)


def _containment_problem(target: Path, root: Path) -> str | None:
    """Why ``target`` may not be written, or ``None`` when it may be.

    Checked against the *existing* part of the path: a file that does not exist
    yet cannot be resolved, but every directory leading to it can, and a
    symbolic link anywhere along that chain is what turns a relative path into
    an escape route.
    """
    if not _is_inside(target, root):
        return "the path resolves outside the worktree"

    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return (
                f"{_relative(current, root)} is a symbolic link, "
                f"which may not be written through"
            )
        if not current.exists():
            break

    deepest_existing = current if current.exists() else current.parent
    if not _is_inside(deepest_existing.resolve(), root):
        return "the path resolves outside the worktree"
    return None


def _is_inside(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = ["EditApplication", "apply_change_set"]
