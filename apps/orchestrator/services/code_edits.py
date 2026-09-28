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

**Targeted edits and the absence of partial application (concern 70).** A
change set containing a targeted ``replace`` is planned in full before any
byte is written, and if any edit in it cannot be applied then *none* of it is.
The reason is specific to this operation rather than a general tightening: a
``replace`` is only meaningful against one known state of a file, so applying
the first two of three edits to a file the third one does not match produces a
candidate that is not the candidate the model described and that no reviewer
can read. A whole-file edit does not have this property -- it applies to
whatever the file happens to hold -- so a change set made only of those keeps
the original per-edit semantics, where a refusal does not stop the edits after
it.

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
    #: Paths changed by a targeted ``replace`` rather than by rewriting the
    #: file (concern 70). Recorded so a reviewer and the completion report can
    #: tell a three-line edit from a rewritten file that happens to have the
    #: same resulting content.
    targeted: tuple[str, ...] = ()
    #: Whether the response was refused whole because one of its edits could
    #: not be applied, leaving the worktree exactly as it was.
    refused_whole: bool = False

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
            "targeted": list(self.targeted),
            "refused_whole": self.refused_whole,
        }


def apply_change_set(
    change_set: CodeChangeSet,
    *,
    root: Path,
    policy: ScopePolicy,
    result_max_bytes: int | None = None,
) -> EditApplication:
    """Write the permitted edits of ``change_set`` into ``root``.

    ``root`` must be the run's worktree, never the managed repository: this
    function does not check which one it was handed, because by the time an
    edit reaches it the decision of where a run may write has already been made
    by ``services.workspace``.

    Args:
        result_max_bytes: ceiling on the *resulting* size of any file a targeted
            ``replace`` produces. This is the safety bound that a targeted
            edit does not get from the per-path output allowance, because that
            allowance measures what the model sent and a targeted edit sends
            only its region. The caller passes the largest file the context
            builder will read into a prompt at all, so the bound is a number
            the system already uses rather than a new allowance. ``None``
            applies no separate ceiling, which is only correct for a whole-file
            change set -- those are already bounded by their parsed content.

    Edits are applied in the order the coder gave them. A change set that
    contains a targeted edit is planned in full first and refused whole if any
    part of it cannot be applied; see the module docstring for why that
    boundary is drawn there and not everywhere.
    """
    resolved_root = root.expanduser().resolve()

    # Distinct paths, not edit entries: a change set of three targeted edits to
    # one file is one file, and refusing it against max_files_changed would be
    # measuring the model's chosen representation rather than the change it
    # proposes. For a change set of whole-file edits this is the old count,
    # because one path may appear only once there.
    proposed_paths = tuple(dict.fromkeys(edit.path for edit in change_set.edits))
    if len(proposed_paths) > policy.max_files_changed:
        # Refused as a whole rather than clipped: applying the first twelve of
        # thirty edits produces a half-implemented candidate that would waste a
        # verification cycle proving it does not work.
        reason = (
            f"the change set edits {len(proposed_paths)} files; the task allows "
            f"{policy.max_files_changed}"
        )
        logger.warning("edits_refused_wholesale", root=str(resolved_root), reason=reason)
        return EditApplication(
            rejected=tuple(
                RejectedEdit(path=edit.path, operation=edit.operation.value, reason=reason)
                for edit in change_set.edits
            ),
            refusal_kinds=(ScopeFindingKind.TOO_MANY_FILES,),
            refused_whole=True,
        )

    atomic = any(edit.is_targeted for edit in change_set.edits)
    planned: list[_Planned] = []
    rejected: list[RejectedEdit] = []
    refusal_kinds: list[ScopeFindingKind] = []
    warnings: list[str] = []
    # Content staged by an earlier edit in this same change set, so a targeted
    # edit that follows a create or a previous replace matches the text the
    # model was looking at when it wrote it.
    staged: dict[str, str] = {}

    for edit in change_set.edits:
        outcome = _plan_one(
            edit,
            root=resolved_root,
            policy=policy,
            staged=staged,
            result_max_bytes=result_max_bytes,
        )
        if outcome.rejection is not None:
            rejected.append(outcome.rejection)
            if outcome.kind is not None:
                refusal_kinds.append(outcome.kind)
            if atomic:
                return _refused_whole(
                    change_set,
                    root=resolved_root,
                    rejected=rejected,
                    refusal_kinds=refusal_kinds,
                    warnings=warnings,
                )
            continue
        if outcome.warning:
            warnings.append(outcome.warning)
        if outcome.planned is not None:
            planned.append(outcome.planned)

    final = _final_per_path(planned)
    for item in final.values():
        _write(item)

    written = tuple(
        path for path, item in final.items() if item.edit.operation is not EditOperation.DELETE
    )
    deleted = tuple(
        path for path, item in final.items() if item.edit.operation is EditOperation.DELETE
    )
    targeted = tuple(
        dict.fromkeys(item.edit.path for item in planned if item.edit.is_targeted)
    )

    logger.info(
        "edits_applied",
        root=str(resolved_root),
        written=len(written),
        deleted=len(deleted),
        rejected=len(rejected),
        targeted=len(targeted),
    )
    return EditApplication(
        written=written,
        deleted=deleted,
        rejected=tuple(rejected),
        refusal_kinds=tuple(dict.fromkeys(refusal_kinds)),
        warnings=tuple(warnings),
        targeted=targeted,
    )


def _final_per_path(planned: list[_Planned]) -> dict[str, _Planned]:
    """The last planned edit for each path, in first-mention order.

    Several targeted edits to one file are the normal way to write one, so the
    planned list holds one entry per edit while the filesystem is written once
    per path. Every entry already carries the complete file as it stood after
    that edit, so the last one for a path is its final state -- which is the
    same answer as replaying the edits in order, arrived at without touching
    the disk between them.
    """
    return {item.edit.path: item for item in planned}


def _refused_whole(
    change_set: CodeChangeSet,
    *,
    root: Path,
    rejected: list[RejectedEdit],
    refusal_kinds: list[ScopeFindingKind],
    warnings: list[str],
) -> EditApplication:
    """Nothing is written, and every edit in the response is accounted for.

    The failure that stopped the response is reported with its own reason; the
    edits that would have succeeded get the reason they were not applied. Both
    matter to the coder -- one is the thing to fix, the other is the assurance
    that nothing was half-done -- and listing only the first would make a
    refusal look like a smaller failure than it is.
    """
    failed_paths = {item.path for item in rejected}
    companion = RejectedEdit(
        path="",
        operation="",
        reason=(
            "not applied, because another edit in this response could not be "
            "applied, so the whole response was refused and nothing was written"
        ),
    )
    every = [
        *rejected,
        *(
            RejectedEdit(
                path=edit.path, operation=edit.operation.value, reason=companion.reason
            )
            for edit in change_set.edits
            if edit.path not in failed_paths
        ),
    ]
    logger.warning(
        "edits_refused_wholesale",
        root=str(root),
        reason=f"{rejected[-1].path}: {rejected[-1].reason}",
    )
    return EditApplication(
        rejected=tuple(every),
        refusal_kinds=tuple(dict.fromkeys(refusal_kinds)),
        warnings=tuple(warnings),
        refused_whole=True,
    )


@dataclass(frozen=True, slots=True)
class _Outcome:
    rejection: RejectedEdit | None = None
    kind: ScopeFindingKind | None = None
    warning: str | None = None
    planned: _Planned | None = None


@dataclass(frozen=True, slots=True)
class _Planned:
    """One edit that passed every gate, resolved to the bytes it will write."""

    edit: FileEdit
    target: Path
    #: ``None`` for a delete; otherwise the complete resulting file contents.
    content: str | None


def _plan_one(
    edit: FileEdit,
    *,
    root: Path,
    policy: ScopePolicy,
    staged: dict[str, str],
    result_max_bytes: int | None,
) -> _Outcome:
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
        if edit.path not in staged and not target.is_file():
            return refuse("the file does not exist, so there is nothing to delete")
        staged.pop(edit.path, None)
        return _Outcome(planned=_Planned(edit=edit, target=target, content=None))

    warning: str | None = None
    if edit.operation is EditOperation.REPLACE:
        content, reason = _resolve_targeted(
            edit, target=target, staged=staged, result_max_bytes=result_max_bytes
        )
        if reason is not None:
            return refuse(reason)
        staged[edit.path] = content
    else:
        existing = edit.path in staged or target.is_file()
        if edit.operation is EditOperation.UPDATE and not existing:
            return refuse(
                "the file does not exist; an update must name a file that is already "
                "in the repository, and a new file needs operation 'create'"
            )
        if edit.operation is EditOperation.CREATE and existing:
            # Not a refusal: the path is inside the allowance either way, and a
            # coder calling an overwrite a "create" is a labelling slip, not an
            # attempt to reach somewhere it should not be.
            warning = f"{edit.path} was created over an existing file"
        content = edit.content or ""
        staged[edit.path] = content

    return _Outcome(warning=warning, planned=_Planned(edit=edit, target=target, content=content))


def _resolve_targeted(
    edit: FileEdit, *, target: Path, staged: dict[str, str], result_max_bytes: int | None
) -> tuple[str, str | None]:
    """``(resulting content, refusal reason)`` for one targeted edit.

    Exactly one match or nothing (concern 70). Zero means the model's copy of
    the file is not the file in the tree, and multiple means there is no
    defensible choice between them -- picking the first would make the outcome
    depend on an unrelated copy of the same text elsewhere, which is how a
    targeted edit becomes a patch with all the failure modes a patch has. Both
    are refusals, and no fuzzy matching is offered as a way out: a near match
    applied where the model did not mean is a silent corruption, while a near
    match refused is one line of feedback the next attempt can act on.

    A tuple rather than a bare string, and a reason that may be ``None``, so
    that a replacement which legitimately empties a file is a result rather
    than a failure. When the reason is set the content is ``""`` and is not
    written.
    """
    if edit.old_text is None or edit.new_text is None:
        return "", "a replace needs both 'oldText' and 'newText' and neither was supplied"

    if edit.path in staged:
        base = staged[edit.path]
    elif target.is_file():
        base = target.read_text(encoding="utf-8")
    else:
        return "", (
            "the file does not exist, so there is no text in it to replace; "
            "a new file needs operation 'create'"
        )

    occurrences = base.count(edit.old_text)
    if occurrences == 0:
        return "", (
            f"'oldText' does not occur in {edit.path}. Copy it from the file's "
            f"current contents exactly, including indentation and line endings."
        )
    if occurrences > 1:
        return "", (
            f"'oldText' occurs {occurrences} times in {edit.path}. Widen it with "
            f"surrounding context so it matches exactly one place, or split this "
            f"into one edit per place."
        )

    result = base.replace(edit.old_text, edit.new_text, 1)
    if result_max_bytes is not None:
        result_bytes = len(result.encode())
        if result_bytes > result_max_bytes:
            return "", (
                f"the replacement would make {edit.path} {result_bytes} bytes, "
                f"over the {result_max_bytes}-byte limit for one file"
            )
    return result, None


def _write(item: _Planned) -> None:
    if item.content is None:
        item.target.unlink()
        return
    item.target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" would pass "\r\n" through from a model that emitted Windows
    # line endings; writing text normally keeps the file in the repository's
    # own convention and keeps the diff readable.
    item.target.write_text(item.content, encoding="utf-8")


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
