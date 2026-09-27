"""Worktree lifecycle beyond a single run (build.md section 10, concern 34).

``release_workspace`` has existed since phase C and nothing called it, for a
reason that was right in each case and wrong in aggregate: an escalated run's
worktree is the only live copy of the candidate a person is being asked to
judge, and an approved one's still holds work nothing had committed. Deleting
either would have destroyed something, so nothing deleted anything and
``WORKTREE_ROOT`` grew by one checkout of the repository per run, for ever.

Phase K can answer it, because the rule was never "release on exit". It is
**release once the run's outcome no longer needs the tree**:

* delivered -- the commit is on the branch, so the tree is a copy (``delivery``
  releases it itself);
* rolled back -- the tree holds nothing the diff artifact does not;
* answered -- a person has decided, so the candidate they were shown is no
  longer evidence anyone is waiting on.

What is *not* released is the case the concern was protecting: a run sitting in
``HUMAN_REVIEW``, and a run still in flight. Those two exclusions are the whole
of the reaper's caution, and they are expressed as a list of statuses rather
than as a heuristic about the directory, because the database knows what a
worktree is for and the filesystem does not.

Directories with no run behind them are counted and reported, never deleted.
An unknown checkout under ``WORKTREE_ROOT`` is somebody's, and disk is cheaper
than the one time it is not ours to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..db.models import TaskRow, TaskRunRow
from ..domain.enums import RunStatus, TaskStatus
from ..domain.git import INTEGRATION_WORKTREE_DIR
from .errors import EntityNotFound
from .git_errors import GitError, WorktreeMissing
from .workspace import attach_workspace, release_workspace, workspace_path

logger = get_logger(__name__)

#: Run states in which no further work will happen, so the tree is finished
#: being written to.
_FINISHED_RUNS: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABANDONED}
)

#: Task states whose worktree is still evidence somebody needs. ``APPROVED``
#: is here as well as ``HUMAN_REVIEW``: an approved candidate that has not been
#: delivered yet is uncommitted work, and the tree is the only copy of it.
_KEEP_FOR: frozenset[TaskStatus] = frozenset(
    {TaskStatus.HUMAN_REVIEW, TaskStatus.APPROVED, TaskStatus.PAUSED}
)


@dataclass(frozen=True, slots=True)
class WorktreeCensus:
    """What is on disk under ``WORKTREE_ROOT`` right now."""

    root: Path
    #: Every worktree directory found, whether or not a run claims it.
    total: int = 0
    #: Directories belonging to a run that has finished and is not waiting on
    #: a person: these are what a reap would remove.
    releasable: int = 0
    #: Directories matching no run in the database.
    unclaimed: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "total": self.total,
            "releasable": self.releasable,
            "unclaimed": list(self.unclaimed),
        }


@dataclass(frozen=True, slots=True)
class ReapReport:
    """What a reaping pass did."""

    released: tuple[str, ...] = ()
    #: Runs whose tree could not be removed, with the reason. A failure here
    #: is never fatal: the next pass will try again.
    failed: tuple[tuple[str, str], ...] = ()
    kept: int = 0
    dry_run: bool = False

    def describe(self) -> dict[str, object]:
        return {
            "released": list(self.released),
            "failed": [{"run": run, "error": error} for run, error in self.failed],
            "kept": self.kept,
            "dry_run": self.dry_run,
        }


@dataclass(slots=True)
class _Candidate:
    run_id: UUID
    path: Path
    external_task_id: str
    releasable: bool
    keep_reason: str | None = None
    extras: dict[str, object] = field(default_factory=dict)


def census(session: Session, *, settings: Settings | None = None) -> WorktreeCensus:
    """Count the worktrees on disk and say how many could go.

    Exposed on ``/health`` so the growth concern 34 describes is visible
    before it is a full disk rather than after: a machine with forty live
    worktrees and two releasable ones has a reaper that is not running, and a
    machine with forty unclaimed ones has a directory somebody should look at.
    """
    config = settings or get_settings()
    root = config.worktree_root
    on_disk = _directories(root)
    claimed = {candidate.path: candidate for candidate in _candidates(session, config)}
    return WorktreeCensus(
        root=root,
        total=len(on_disk),
        releasable=sum(
            1 for path in on_disk if path in claimed and claimed[path].releasable
        ),
        unclaimed=tuple(sorted(str(path) for path in on_disk if path not in claimed)),
    )


def reap(
    session: Session, *, settings: Settings | None = None, dry_run: bool = False
) -> ReapReport:
    """Release every worktree whose run no longer needs it.

    Safe to run at any time, including while a run is in flight: a run that
    has not finished is never a candidate. Safe to run twice: a tree that is
    already gone is not an error.
    """
    config = settings or get_settings()
    released: list[str] = []
    failed: list[tuple[str, str]] = []
    kept = 0

    for candidate in _candidates(session, config):
        if not candidate.releasable:
            kept += 1
            continue
        if not candidate.path.exists():
            continue
        if dry_run:
            released.append(str(candidate.path))
            continue
        try:
            release_for_run(session, candidate.run_id, settings=config)
        except (GitError, EntityNotFound) as exc:
            failed.append((str(candidate.run_id), str(exc)))
            continue
        released.append(str(candidate.path))

    report = ReapReport(
        released=tuple(released), failed=tuple(failed), kept=kept, dry_run=dry_run
    )
    if released or failed:
        logger.info("worktrees_reaped", **report.describe())
    return report


def release_for_run(
    session: Session,
    task_run_id: UUID,
    *,
    settings: Settings | None = None,
    delete_branch: bool = False,
) -> bool:
    """Remove one run's worktree. Returns whether there was one to remove.

    The branch is kept unless asked otherwise: it is the run's audit trail,
    and after a delivery it is where the commit lives.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        GitError: Git refused to remove the worktree.
    """
    config = settings or get_settings()
    try:
        workspace = attach_workspace(session, task_run_id, settings=config)
    except WorktreeMissing:
        return False
    release_workspace(workspace, delete_branch=delete_branch)
    logger.info(
        "worktree_released",
        run_id=str(task_run_id),
        task=workspace.external_task_id,
        path=str(workspace.path),
    )
    return True


def _candidates(session: Session, settings: Settings) -> list[_Candidate]:
    """Every run that has a worktree path, with the verdict on releasing it."""
    rows = session.execute(
        select(TaskRunRow, TaskRow)
        .join(TaskRow, TaskRow.id == TaskRunRow.task_id)
        .where(TaskRunRow.branch_name.is_not(None))
    ).all()
    candidates: list[_Candidate] = []
    for run, task in rows:
        keep_reason: str | None = None
        if run.status not in _FINISHED_RUNS:
            keep_reason = f"run is {run.status.value}"
        elif task.status in _KEEP_FOR:
            keep_reason = f"task is {task.status.value}"
        candidates.append(
            _Candidate(
                run_id=run.id,
                path=workspace_path(
                    task.project_id, task.external_task_id, run.run_number, settings=settings
                ),
                external_task_id=task.external_task_id,
                releasable=keep_reason is None,
                keep_reason=keep_reason,
            )
        )
    return candidates


def _directories(root: Path) -> list[Path]:
    """Worktree directories on disk: one level of project, one of run.

    The project's integration worktree is not one of these (concern 51). It
    belongs to no run, so it would be counted as unclaimed for ever, and
    "unclaimed" is the census's way of saying a human should look at something.
    It is also not reapable: ``reap`` works from runs, and this tree has none.
    """
    if not root.exists():
        return []
    return sorted(
        child
        for project_dir in root.iterdir()
        if project_dir.is_dir()
        for child in project_dir.iterdir()
        if child.is_dir() and child.name != INTEGRATION_WORKTREE_DIR
    )


__all__ = ["ReapReport", "WorktreeCensus", "census", "reap", "release_for_run"]
