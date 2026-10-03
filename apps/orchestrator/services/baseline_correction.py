"""Operator corrections to the project specification (lifecycle gap, UI-001).

Task workspaces start from ``agent/integration``, the cumulative *accepted*
baseline, and never from the project's own branch. That is deliberate: a run
must build on the tree the previous accepted work left behind. It also means
the baseline is the only tree a task ever sees, and therefore the only place a
corrected ``build.tasks.yaml`` can reach one.

UI-001 is what exposed the gap. Its manifest wrongly declared ``.gitignore``
writable; an operator fixed ``build.tasks.yaml`` and committed on ``main``;
``POST /projects/{id}/import-tasks`` synchronised the specification into the
database while correctly preserving the FAILED runtime status; and
``POST /tasks/{id}/retry`` moved the task to READY. Every one of those did its
job, and the project was still wrong: ``agent/integration`` still held the old
manifest, so the next workspace would have been cut from a tree whose manifest
disagreed with the database that was about to drive it.

**Why this is not an integration.** ``integrate_candidate`` and
``integrate_human_commit`` both mean "a task produced this, and it is done".
They mark a task COMPLETE, clear its ``unintegrated_commit``, and write
task-completion provenance. A specification correction asserts none of that: no
task produced it, no task is finished by it, and recording it as though one were
would put a lie in the audit trail that every later reader would believe. So
this is a separate project-level operation with its own event type, and it
touches no task's runtime state at all.

**Why the policy is this narrow.** The operation merges an operator-authored
commit into the accepted baseline, which is the most trusted tree in the
system. A general "merge any operator commit" mechanism would be a way around
every scope, review and verification guard the orchestrator has. So the commit
is required to be a single-parent commit whose own diff touches nothing but the
manifest (see :data:`ALLOWED_CORRECTION_PATHS`), built directly on history the
baseline already contains. Widening that set is a deliberate, reviewable edit
to one constant, not a request parameter.

**Why no verification runs.** A specification correction is not a claim that
the project builds (requirement 12). Greenfield projects legitimately declare
``npm test`` before anything implements it, and demanding a passing cumulative
gate here would make the mechanism unusable exactly when a manifest is most
likely to need fixing. Baseline certification is untouched: the next task's
workspace preparation certifies the new baseline by the existing rule, which
records what *does* fail without claiming the project passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import RunEventType
from ..domain.git import INTEGRATION_BRANCH
from ..domain.models import Project, RunEvent
from ..repositories import ProjectRepository, RunEventRepository
from .errors import EntityConflict, EntityNotFound
from .git_service import GitService
from .integration import (
    _usable_or_recreated_integration_worktree,
    ensure_integration_branch,
    integration_worktree_path,
)
from .manifest_loader import MANIFEST_FILENAME, load_manifest
from .task_importer import ImportReport, import_manifest

logger = get_logger(__name__)

#: The only paths a baseline correction may touch.
#:
#: One entry, on purpose. This mechanism merges into the accepted baseline
#: without review, scope-guard or verification, so what it is allowed to carry
#: has to be the smallest thing that solves the problem it was built for: the
#: project's own task specification. A correction that touches source code is
#: not a specification correction -- it is task work, and task work goes
#: through a run.
#:
#: Adding to this set widens an unreviewed path into the most trusted tree in
#: the system. It is deliberately a constant and not a parameter so that any
#: widening is a code change somebody has to approve.
ALLOWED_CORRECTION_PATHS: frozenset[str] = frozenset({MANIFEST_FILENAME})


@dataclass(frozen=True, slots=True)
class BaselineCorrection:
    """What applying one operator correction did, or did not, do."""

    project_id: UUID
    #: The baseline now contains the correction because *this call* put it there.
    applied: bool
    #: The baseline already contained it; this call changed nothing (replay).
    already_applied: bool
    #: The baseline before and after. Equal when nothing moved.
    previous_sha: str
    baseline_sha: str
    #: The operator commit this call was asked to incorporate.
    correction_sha: str
    #: The merge commit the baseline was moved to. ``None`` on a replay.
    merged_sha: str | None
    #: What the correction commit changed, relative to its own parent.
    changed_paths: tuple[str, ...]
    reason: str
    requested_by: str
    #: The provenance record. ``None`` on a replay, which writes none.
    event_id: UUID | None = None
    #: What re-synchronising the corrected manifest changed. ``None`` on a
    #: replay, which re-synchronises nothing.
    import_report: ImportReport | None = None

    def describe(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "applied": self.applied,
            "already_applied": self.already_applied,
            "previous_sha": self.previous_sha,
            "baseline_sha": self.baseline_sha,
            "correction_sha": self.correction_sha,
            "merged_sha": self.merged_sha,
            "changed_paths": list(self.changed_paths),
            "reason": self.reason,
            "requested_by": self.requested_by,
            "event_id": str(self.event_id) if self.event_id else None,
        }


def apply_baseline_correction(
    session: Session,
    project_id: UUID,
    *,
    commit_sha: str,
    reason: str,
    requested_by: str,
    settings: Settings | None = None,
) -> BaselineCorrection:
    """Merge an operator's specification correction into the accepted baseline.

    The order of the last three steps is the whole of this function's crash
    safety, so it is worth stating plainly. The manifest is re-synchronised and
    the provenance event appended **in the caller's transaction**, and the Git
    ref is moved **last**:

    * any database failure -- an invalid manifest reaching the importer, a
      command the worker policy forbids, a constraint -- rolls the caller's
      transaction back, and the ref was never touched;
    * a Git failure moving the ref propagates, and the caller's rollback undoes
      the specification sync with it.

    Doing it the other way round -- ref first -- would leave an advanced
    baseline behind every failed import, which is precisely the half-applied
    state requirement 14 forbids.

    Args:
        commit_sha: the operator's correction commit. Any revision Git
            resolves; the full SHA is what gets recorded.
        reason: required operator explanation, recorded in the provenance.
        requested_by: required operator identity, recorded in the provenance.

    Returns:
        The correction, including the truthful already-applied reading when the
        baseline already contains the commit.

    Raises:
        ValueError: ``reason`` or ``requested_by`` is empty or whitespace.
        EntityNotFound: no such project, or the commit is not in the repository.
        EntityConflict: the commit is a merge or root commit, is not based on
            history the baseline already contains, or touches a path this
            mechanism does not allow.
        MergeConflict: the correction cannot be merged into the baseline.
        ManifestError: the corrected manifest does not parse or validate, or
            declares a command no worker may run.
        LockWaitTimeout: another integration or certification holds the
            project's integration worktree.
        GitError: the repository itself could not be operated on.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    if not requested_by or not requested_by.strip():
        raise ValueError("requested_by is required")

    config = settings or get_settings()
    projects = ProjectRepository(session)
    project = projects.get(project_id)
    if project is None:
        raise EntityNotFound("Project", project_id)

    repository = GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=config,
    )
    try:
        correction = repository.resolve_sha(commit_sha)
    except Exception as exc:
        raise EntityNotFound(
            "Commit", f"{commit_sha} in repository {project.repository_path}"
        ) from exc

    # The project row lock, taken before anything reads or writes the shared
    # integration worktree or ref -- the fourth participant in it, alongside
    # baseline certification, candidate integration and human-commit
    # integration. Everything below is read *after* the lock, so the baseline
    # this correction is validated against is the one it is merged onto.
    projects.lock(project.id)
    previous = ensure_integration_branch(repository, project)

    # Replay. Asked first, because every check below would otherwise be
    # answering about a correction that has already landed -- and the honest
    # answer to "apply this again" is that there is nothing to apply.
    if repository.contains_commit(correction, ref=INTEGRATION_BRANCH):
        logger.info(
            "baseline_correction_already_applied",
            project_id=str(project.id),
            correction_sha=correction,
            baseline_sha=previous,
            requested_by=requested_by,
        )
        return BaselineCorrection(
            project_id=project.id,
            applied=False,
            already_applied=True,
            previous_sha=previous,
            baseline_sha=previous,
            correction_sha=correction,
            merged_sha=None,
            changed_paths=(),
            reason=reason,
            requested_by=requested_by,
        )

    changed = _assert_correctable(repository, project, correction, baseline=previous)

    worktree = _usable_or_recreated_integration_worktree(
        repository,
        integration_worktree_path(project.id, settings=config),
        previous,
    )
    # Deliberately no dependency prepopulation: nothing here runs a command, so
    # the expensive tree a cumulative gate would need is not required, and a
    # dependency failure must not be able to stop a specification fix.
    merged = worktree.merge(
        correction,
        message=(
            f"Apply project specification correction {correction[:12]}\n\n"
            f"Requested by {requested_by}: {reason}\n"
            f"Paths: {', '.join(changed)}\n"
        ),
    )

    # The corrected manifest has to be valid *as merged*, not as it sits on the
    # operator's branch: the merged tree is what the baseline is about to
    # become and what the next workspace would be cut from. Raises
    # ManifestError before the ref moves.
    manifest = load_manifest(Path(worktree.path) / MANIFEST_FILENAME)

    # Runtime task status is preserved here by task_importer's own rules -- the
    # same rules POST /import-tasks uses. This operation adds no status
    # handling of its own, so a FAILED task stays FAILED and an active one is
    # skipped rather than rewritten.
    report = import_manifest(session, manifest, project_id=project.id)

    event = RunEventRepository(session).append(
        RunEvent(
            # No run and no task: this belongs to the project. See
            # RunEventType.BASELINE_CORRECTION_APPLIED.
            task_run_id=None,
            task_id=None,
            project_id=project.id,
            event_type=RunEventType.BASELINE_CORRECTION_APPLIED,
            payload={
                "branch": INTEGRATION_BRANCH,
                "previous_sha": previous,
                "correction_sha": correction,
                "merged_sha": merged,
                "integrated_sha": merged,
                "changed_paths": list(changed),
                "reason": reason,
                "requested_by": requested_by,
                "provenance": "operator_specification_correction",
                "created": list(report.created),
                "updated": list(report.updated),
                "skipped_active": list(report.skipped_active),
                "orphaned": list(report.orphaned),
            },
        )
    )

    # Last, so that every failure above leaves the baseline where it was.
    advanced = repository.force_branch(INTEGRATION_BRANCH, merged)
    logger.info(
        "baseline_correction_applied",
        project_id=str(project.id),
        previous_sha=previous,
        correction_sha=correction,
        baseline_sha=advanced,
        changed_paths=list(changed),
        requested_by=requested_by,
        updated=len(report.updated),
    )
    return BaselineCorrection(
        project_id=project.id,
        applied=True,
        already_applied=False,
        previous_sha=previous,
        baseline_sha=advanced,
        correction_sha=correction,
        merged_sha=merged,
        changed_paths=changed,
        reason=reason,
        requested_by=requested_by,
        event_id=event.id,
        import_report=report,
    )


def _assert_correctable(
    repository: GitService, project: Project, correction: str, *, baseline: str
) -> tuple[str, ...]:
    """The correction's changed paths, or a refusal saying why there are none.

    Three questions, in the order that makes each one meaningful: what this
    commit *is*, whether it is based on something the baseline already has, and
    only then what it touches.
    """
    parents = repository.commit_parents(correction)
    if len(parents) != 1:
        kind = "a root commit" if not parents else f"a merge commit ({len(parents)} parents)"
        raise EntityConflict(
            f"Correction {correction[:12]} is {kind}. A baseline correction is "
            "scoped to one commit's own diff, which only a single-parent commit "
            "has; a merge would carry whatever else its other side contained."
        )
    parent = parents[0]

    # Staleness, stated as a property of Git rather than of a timestamp: the
    # commit must sit directly on top of history the baseline already contains.
    # That is what makes the merge below introduce exactly this commit's diff
    # and nothing else, and it is what refuses a correction built on a branch
    # point the baseline never had -- including an unrelated history, whose
    # parent is in no baseline.
    if not repository.contains_commit(parent, ref=INTEGRATION_BRANCH):
        raise EntityConflict(
            f"Correction {correction[:12]} is based on {parent[:12]}, which the "
            f"integration baseline {baseline[:12]} does not contain. Rebase the "
            "correction onto the accepted baseline; merging it as it stands "
            "would bring unaccepted history with it."
        )

    changed = repository.list_changed_paths(parent, correction)
    if not changed:
        raise EntityConflict(
            f"Correction {correction[:12]} changes nothing. There is no "
            "specification correction to apply."
        )
    disallowed = sorted(set(changed) - ALLOWED_CORRECTION_PATHS)
    if disallowed:
        allowed = ", ".join(sorted(ALLOWED_CORRECTION_PATHS))
        raise EntityConflict(
            f"Correction {correction[:12]} changes {', '.join(disallowed)}. A "
            f"baseline correction may only change: {allowed}. Changes to "
            "anything else are task work and go through a run, where scope, "
            "review and verification apply."
        )
    logger.info(
        "baseline_correction_validated",
        project_id=str(project.id),
        correction_sha=correction,
        parent_sha=parent,
        changed_paths=list(changed),
    )
    return changed


__all__ = [
    "ALLOWED_CORRECTION_PATHS",
    "BaselineCorrection",
    "apply_baseline_correction",
]
