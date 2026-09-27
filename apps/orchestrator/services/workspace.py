"""Task workspace lifecycle (build.md sections 10 and 11).

One task run gets one isolated worktree on one orchestrator-named branch,
created from a recorded starting commit. This module is the only place that
decides when those things happen; ``GitService`` decides how.

Every function here is safe to call twice: preparation refuses to reuse a
directory, and release tolerates a worktree that is already gone. Cleanup runs
on failure paths, where raising a second error would bury the first.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import RunEventType
from ..domain.git import (
    DiffSummary,
    checkpoint_tag_name,
    task_branch_name,
    task_commit_message,
    worktree_dir_name,
)
from ..domain.models import Project, RunEvent, Task, TaskRun
from ..repositories import (
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from .errors import EntityNotFound
from .git_errors import GitError, WorktreeMissing
from .git_service import DIFF_TRUNCATION_MARKER, GitService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TaskWorkspace:
    """An isolated checkout prepared for one task run."""

    project_id: UUID
    task_id: UUID
    task_run_id: UUID
    external_task_id: str
    path: Path
    branch: str
    starting_commit: str
    git: GitService
    """Rooted at ``path``, not at the managed repository."""

    repository: GitService
    """Rooted at the managed repository, for branch and worktree bookkeeping."""


@dataclass(frozen=True, slots=True)
class DiffCapture:
    """A diff as both text (for the reviewer) and structure (for the guards)."""

    text: str
    summary: DiffSummary
    truncated: bool = False

    @property
    def files_changed(self) -> int:
        return self.summary.files_changed

    @property
    def line_count(self) -> int:
        return self.summary.line_count


def repository_service(project: Project, *, settings: Settings | None = None) -> GitService:
    """A ``GitService`` for a project's managed repository."""
    return GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=settings or get_settings(),
    )


def prepare_workspace(
    session: Session,
    task_run_id: UUID,
    *,
    settings: Settings | None = None,
    allow_dirty: bool | None = None,
) -> TaskWorkspace:
    """Create the branch and worktree for ``task_run_id``.

    Order matters and follows section 10: validate the managed repository is
    clean (rule 4), resolve and record the starting SHA *before* any work
    (rule 3), then create the branch and worktree the orchestrator named
    (rules 2 and 7).

    Raises:
        EntityNotFound: the run, its task, or its project is missing.
        DirtyWorktree: the managed repository has uncommitted changes.
        BranchAlreadyExists: a branch for this task and run already exists.
        WorktreePathRejected: the target path is outside ``WORKTREE_ROOT``.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, task_run_id)

    repository = repository_service(project, settings=config)
    repository.ensure_clean_worktree(allow_dirty=allow_dirty)

    starting_commit = repository.resolve_sha(project.default_branch)
    branch = task_branch_name(task.external_task_id, task.title)
    directory = worktree_dir_name(task.external_task_id, run.run_number)
    path = config.worktree_root / str(project.id) / directory

    worktree_git = repository.create_worktree(path, branch, start_point=starting_commit)
    _prepopulate_dependencies(project, path, worktree_git)

    TaskRunRepository(session).update_fields(
        run.id, branch_name=branch, starting_commit=starting_commit
    )
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.WORKSPACE_CREATED,
            attempt=run.attempt_number,
            payload={
                "branch": branch,
                "starting_commit": starting_commit,
                "worktree_path": str(path),
            },
        )
    )
    logger.info(
        "workspace_prepared",
        task=task.external_task_id,
        run_id=str(run.id),
        branch=branch,
        starting_commit=starting_commit,
        path=str(path),
    )
    return TaskWorkspace(
        project_id=project.id,
        task_id=task.id,
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        path=path,
        branch=branch,
        starting_commit=starting_commit,
        git=worktree_git,
        repository=repository,
    )


def _prepopulate_dependencies(
    project: Project, worktree: Path, worktree_git: GitService
) -> None:
    """Copy declared, Git-ignored dependencies into a networkless worktree."""
    repository = Path(project.repository_path).resolve()
    for declared in project.dependency_paths:
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"Unsafe dependency path {declared!r}")
        source = (repository / relative).resolve()
        if not source.is_relative_to(repository):
            raise ValueError(f"Dependency path escapes repository: {declared!r}")
        if not source.exists():
            raise ValueError(f"Declared dependency path does not exist: {declared!r}")
        normalized = relative.as_posix()
        # A directory-only `.gitignore` entry (`node_modules/`) does not match
        # the bare name when the path is absent from the worktree, which it
        # always is at this point: Git cannot tell an absent path is a
        # directory. The source is on disk, so ask with the form that matches.
        candidates = (normalized, f"{normalized}/") if source.is_dir() else (normalized,)
        if not any(worktree_git.is_ignored(form) for form in candidates):
            raise ValueError(
                f"Dependency path {declared!r} must be ignored by Git before it "
                "can be copied into a task worktree"
            )
        target = worktree / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)


def workspace_path(
    project_id: UUID, external_task_id: str, run_number: int, *, settings: Settings | None = None
) -> Path:
    """Where a run's worktree lives.

    Derived rather than stored, and that is the point: the same run always
    names the same directory, so a workflow resumed in a new process -- or a
    reaper looking for trees nobody released -- can find a worktree without a
    column that could disagree with the disk.
    """
    config = settings or get_settings()
    return config.worktree_root / str(project_id) / worktree_dir_name(external_task_id, run_number)


def attach_workspace(
    session: Session, task_run_id: UUID, *, settings: Settings | None = None
) -> TaskWorkspace:
    """Re-open the worktree an earlier call to ``prepare_workspace`` created.

    Needed wherever a run outlives the process that started it: a workflow
    resuming after a restart, a human accepting a candidate days later, the
    reaper releasing a tree whose run is over. Nothing is created and nothing
    is validated beyond existence -- what state the tree is in is the caller's
    question, not this function's.

    Raises:
        EntityNotFound: the run, its task, or its project is missing.
        WorktreeMissing: the run never had a worktree, or it is gone.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, task_run_id)
    if run.branch_name is None or run.starting_commit is None:
        raise WorktreeMissing(
            f"Run {task_run_id} has no prepared workspace to attach to"
        )
    path = workspace_path(project.id, task.external_task_id, run.run_number, settings=config)
    if not path.exists():
        raise WorktreeMissing(f"Worktree {path} is gone")

    repository = repository_service(project, settings=config)
    return TaskWorkspace(
        project_id=project.id,
        task_id=task.id,
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        path=path,
        branch=run.branch_name,
        starting_commit=run.starting_commit,
        git=repository.for_worktree(path),
        repository=repository,
    )


def capture_diff(workspace: TaskWorkspace, *, max_bytes: int | None = None) -> DiffCapture:
    """Diff the worktree against the run's starting commit.

    Uncommitted and untracked work is included: the coder writes files and the
    orchestrator, not the coder, decides whether they are committed.
    """
    text = workspace.git.get_diff(workspace.starting_commit, max_bytes=max_bytes)
    summary = workspace.git.get_diff_summary(workspace.starting_commit)
    return DiffCapture(
        text=text,
        summary=summary,
        truncated=max_bytes is not None and text.endswith(DIFF_TRUNCATION_MARKER),
    )


def commit_task_work(
    session: Session, workspace: TaskWorkspace, task: Task, *, allow_empty: bool = False
) -> str:
    """Commit the worktree as ``TS-004: title`` and record the candidate SHA.

    Raises:
        NothingToCommit: the worktree matches HEAD.
        MergeConflict: the worktree has unresolved paths.
    """
    sha = workspace.git.commit(
        task_commit_message(task.external_task_id, task.title), allow_empty=allow_empty
    )
    run = TaskRunRepository(session).update_fields(workspace.task_run_id, candidate_commit=sha)
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=workspace.task_run_id,
            project_id=workspace.project_id,
            task_id=workspace.task_id,
            event_type=RunEventType.COMMIT_CREATED,
            attempt=run.attempt_number,
            payload={"commit": sha, "branch": workspace.branch},
        )
    )
    return sha


def push_task_branch(session: Session, workspace: TaskWorkspace) -> bool:
    """Push the task branch if policy allows. Returns whether a push happened.

    Pushing is off by default and is never required for a task to complete, so
    a disabled push is a logged non-event rather than a failure.
    """
    if not workspace.repository.settings.git_push_enabled:
        logger.info("push_skipped", branch=workspace.branch, reason="GIT_PUSH_ENABLED=false")
        return False
    workspace.repository.push(workspace.branch)
    run = TaskRunRepository(session).get(workspace.task_run_id)
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=workspace.task_run_id,
            project_id=workspace.project_id,
            task_id=workspace.task_id,
            event_type=RunEventType.PUSH_COMPLETED,
            attempt=run.attempt_number if run else 1,
            payload={"branch": workspace.branch},
        )
    )
    return True


def rollback_workspace(workspace: TaskWorkspace) -> None:
    """Return the worktree to the run's starting commit (section 10 rule 8)."""
    workspace.git.reset_hard_to_sha(workspace.starting_commit)
    logger.info(
        "workspace_rolled_back",
        task=workspace.external_task_id,
        sha=workspace.starting_commit,
    )


def checkpoint_workspace(workspace: TaskWorkspace, attempt: int) -> str | None:
    """Tag the current worktree HEAD so an escalated attempt stays findable.

    Best-effort: a missing checkpoint tag must not mask the failure that
    prompted it, so a Git error here is logged and swallowed.
    """
    tag = checkpoint_tag_name(workspace.external_task_id, attempt)
    try:
        return workspace.git.tag_checkpoint(
            tag, message=f"{workspace.external_task_id} attempt {attempt}"
        )
    except GitError as exc:
        logger.warning("checkpoint_failed", tag=tag, error=str(exc))
        return None


def release_workspace(workspace: TaskWorkspace, *, delete_branch: bool = False) -> None:
    """Remove the worktree, optionally deleting its branch.

    The branch is kept by default: it is the run's audit trail (rule 7). Pass
    ``delete_branch`` only when the branch holds no commits worth keeping.
    """
    workspace.repository.remove_worktree(
        workspace.path, delete_branch=workspace.branch if delete_branch else None
    )


def load_run_context(session: Session, task_run_id: UUID) -> tuple[TaskRun, Task, Project]:
    """The run and the records it belongs to.

    Raises:
        EntityNotFound: the run, its task, or its project is missing.
    """
    run = TaskRunRepository(session).get(task_run_id)
    if run is None:
        raise EntityNotFound("Run", task_run_id)
    task = TaskRepository(session).get(run.task_id)
    if task is None:
        raise EntityNotFound("Task", run.task_id)
    project = ProjectRepository(session).get(task.project_id)
    if project is None:
        raise EntityNotFound("Project", task.project_id)
    return run, task, project
