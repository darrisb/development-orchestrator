"""Landing an approved candidate (build.md sections 10, 27 and 29, phase K).

Phase J stopped at ``APPROVED`` on purpose, and concern 25 is what that cost:
a reviewer's acceptance left the work uncommitted in a worktree, the run row
``RUNNING`` with no ``completed_at``, and nothing on the task branch. This
module is the other end of the loop -- the four steps section 27 names after
``route_review``:

```text
commit_candidate -> push_candidate -> complete_task -> (release the worktree)
```

Two rules shape it.

* **Nothing lands that was not approved.** The caller has to have an approval;
  this module checks the task is in ``APPROVED`` before it commits, because a
  function that will commit whatever it is pointed at is one mistake away from
  committing a rejected candidate.
* **The steps are ordered by what is recoverable.** The commit happens first
  and is the only irreversible part; a push that fails leaves a committed
  branch an operator can push by hand, and a failed release leaves a directory,
  not a lost change. So a later step never undoes an earlier one, and a failure
  in one is reported rather than rolled back.

Pushing stays off unless an operator turned it on (section 10 rule 5), and a
disabled push is a non-event rather than a failure: a task completes on a
machine with no remote.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import RunEventType, RunStatus, TaskStatus
from ..domain.git import checkpoint_tag_name
from ..domain.models import RunEvent, Task, TaskRun
from ..domain.state_machine import assert_transition
from ..repositories import RunEventRepository, TaskRepository, TaskRunRepository
from .errors import EntityConflict
from .git_errors import GitError
from .workspace import (
    TaskWorkspace,
    commit_task_work,
    load_run_context,
    push_task_branch,
    release_workspace,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Delivery:
    """What actually landed, step by step.

    Every field is a fact about the repository rather than an intention, so a
    caller writing the run's history does not have to infer what happened from
    the absence of an exception.
    """

    task_run_id: UUID
    external_task_id: str
    branch: str
    commit_sha: str
    pushed: bool
    released: bool
    tag: str | None = None
    #: Why the push did not happen, when it did not. ``None`` when it did.
    push_skipped_reason: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "task_run_id": str(self.task_run_id),
            "external_task_id": self.external_task_id,
            "branch": self.branch,
            "commit": self.commit_sha,
            "tag": self.tag,
            "pushed": self.pushed,
            "push_skipped_reason": self.push_skipped_reason,
            "worktree_released": self.released,
        }


def deliver_candidate(
    session: Session,
    workspace: TaskWorkspace,
    *,
    settings: Settings | None = None,
    tag: bool = True,
    release: bool = True,
) -> Delivery:
    """Commit, tag, push and complete an approved run.

    Args:
        tag: mark the delivered commit with the run's checkpoint tag, so the
            work stays findable after the branch is merged or the worktree is
            gone. Best-effort, like every other checkpoint.
        release: remove the worktree afterwards (concern 34). The branch is
            always kept -- it is the run's audit trail and now holds the
            commit.

    Returns:
        What landed.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        EntityConflict: the task is not ``APPROVED``, so there is nothing this
            function is allowed to land.
        NothingToCommit: the worktree matches the starting commit.
        MergeConflict: the worktree has unresolved paths.
        GitError: the commit itself failed.
    """
    config = settings or get_settings()
    return _deliver(
        session,
        workspace,
        allowed_status=TaskStatus.APPROVED,
        settings=config,
        tag=tag,
        release=release,
    )


def deliver_escalated_candidate(
    session: Session,
    workspace: TaskWorkspace,
    *,
    settings: Settings | None = None,
    tag: bool = True,
    release: bool = True,
) -> Delivery:
    """Land a candidate a human explicitly accepted from ``HUMAN_REVIEW``."""
    return _deliver(
        session,
        workspace,
        allowed_status=TaskStatus.HUMAN_REVIEW,
        settings=settings or get_settings(),
        tag=tag,
        release=release,
    )


def _deliver(
    session: Session,
    workspace: TaskWorkspace,
    *,
    allowed_status: TaskStatus,
    settings: Settings,
    tag: bool,
    release: bool,
) -> Delivery:
    run, task, project = load_run_context(session, workspace.task_run_id)
    if task.status is not allowed_status:
        raise EntityConflict(
            f"Task {task.external_task_id} is {task.status}; expected {allowed_status}"
        )

    sha = _commit_or_reconcile(session, workspace, task, run.candidate_commit)

    tag_name: str | None = None
    if tag:
        tag_name = _tag_delivery(workspace, run.attempt_number)

    pushed = False
    push_skipped: str | None = None
    try:
        pushed = push_task_branch(session, workspace)
        if not pushed:
            push_skipped = "GIT_PUSH_ENABLED is false"
    except GitError as exc:
        # The commit is already made and is the part that mattered. A remote
        # that refused the branch is an operator's problem with an operator's
        # fix, and failing the task here would mark delivered work as failed.
        push_skipped = str(exc)
        logger.warning(
            "push_failed_after_commit",
            run_id=str(run.id),
            task=task.external_task_id,
            branch=workspace.branch,
            error=str(exc),
        )

    _complete(session, task, run_id=run.id)
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.TASK_COMPLETED,
            attempt=run.attempt_number,
            payload={
                "commit": sha,
                "branch": workspace.branch,
                "tag": tag_name,
                "pushed": pushed,
                "push_skipped_reason": push_skipped,
            },
        )
    )

    released = False
    if release:
        released = _release(workspace)

    _capture_experience(session, run, task, settings=settings)

    delivery = Delivery(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        branch=workspace.branch,
        commit_sha=sha,
        pushed=pushed,
        released=released,
        tag=tag_name,
        push_skipped_reason=push_skipped,
    )
    logger.info("candidate_delivered", **delivery.describe())
    return delivery


def _capture_experience(
    session: Session, run: TaskRun, task: Task, *, settings: Settings
) -> None:
    """Everything phase L collects from a run that just landed.

    Three things, in this order, and the order matters twice over: the outcome
    file is written first so the copy inside the training example is the same
    artefact rather than a second rendering; the training example is captured
    while the run's artifacts are still on disk; and lessons are proposed last,
    because a lesson may only come from findings the fix loop already confirmed
    as addressed, and that is true here but is not something this function
    decides.

    Every step is best-effort *after* the commit has landed. The work is in the
    repository; a failure to index it is a gap in the orchestrator's memory of
    what it did, and turning that into a failed delivery would discard good work
    to fix a bookkeeping problem. The failure is logged loudly instead, because
    the alternative -- a silently incomplete history -- is what phase L exists
    to end.
    """
    try:
        from .training import capture_accepted_run

        capture_accepted_run(session, run.id, settings=settings)
    except Exception as error:  # noqa: BLE001 - bookkeeping must not fail delivery
        logger.error(
            "training_capture_failed",
            run_id=str(run.id),
            task=task.external_task_id,
            error=str(error),
            exc_info=True,
        )
    try:
        from .lessons import propose_lessons_for_run

        proposed = propose_lessons_for_run(session, run.id)
        logger.info(
            "lessons_proposed_for_delivered_run",
            run_id=str(run.id),
            task=task.external_task_id,
            proposed=len(proposed),
        )
    except Exception as error:  # noqa: BLE001 - bookkeeping must not fail delivery
        logger.error(
            "lesson_proposal_failed",
            run_id=str(run.id),
            task=task.external_task_id,
            error=str(error),
            exc_info=True,
        )


def _commit_or_reconcile(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    recorded_sha: str | None,
) -> str:
    """Commit once, or recover a commit made just before a process exit.

    Git and PostgreSQL cannot share a transaction. If Git committed and the
    process stopped before the run row was flushed, a resumed delivery sees a
    clean tree whose HEAD is newer than ``starting_commit``. Recording that
    existing HEAD is the only idempotent answer; trying to commit again would
    fail with ``NothingToCommit`` (recovery reconciliation, phase K item 7).
    """
    head = workspace.git.get_head_sha()
    if recorded_sha:
        if head != recorded_sha:
            raise EntityConflict(
                f"Run records candidate {recorded_sha}, but worktree HEAD is {head}"
            )
        return recorded_sha
    if workspace.git.is_clean() and head != workspace.starting_commit:
        TaskRunRepository(session).update_fields(
            workspace.task_run_id, candidate_commit=head
        )
        RunEventRepository(session).append(
            RunEvent(
                task_run_id=workspace.task_run_id,
                project_id=workspace.project_id,
                task_id=workspace.task_id,
                event_type=RunEventType.COMMIT_CREATED,
                payload={
                    "commit": head,
                    "branch": workspace.branch,
                    "reconciled": True,
                },
            )
        )
        return head
    return commit_task_work(session, workspace, task)


def complete_by_hand(session: Session, task: Task, *, task_run_id: UUID | None = None) -> Task:
    """Mark a task complete without committing anything (section 24).

    For the escalation answer that says a person did the work themselves. The
    orchestrator has nothing to land and must not pretend otherwise: no
    commit, no candidate SHA, and the run stays exactly as it ended.
    """
    return _complete(session, task, run_id=task_run_id, finish_run=False)


def _complete(
    session: Session, task: Task, *, run_id: UUID | None, finish_run: bool = True
) -> Task:
    """Move the task to ``COMPLETE`` and close the run.

    The transition is asserted rather than logged-and-skipped, unlike the ones
    inside the agents: this runs after the work has landed, so a task that
    cannot legally complete means the caller delivered something it should not
    have, and that must not pass quietly.
    """
    assert_transition(task.status, TaskStatus.COMPLETE)
    updated = TaskRepository(session).transition(task.id, TaskStatus.COMPLETE)
    if finish_run and run_id is not None:
        TaskRunRepository(session).finish(run_id, RunStatus.SUCCEEDED)
    return updated


def _tag_delivery(workspace: TaskWorkspace, attempt: int) -> str | None:
    """Best-effort tag. A missing tag never fails delivered work."""
    name = checkpoint_tag_name(workspace.external_task_id, attempt)
    try:
        workspace.git.tag_checkpoint(name, message=f"{workspace.external_task_id} delivered")
    except GitError as exc:
        logger.warning("delivery_tag_failed", tag=name, error=str(exc))
        return None
    return name


def _release(workspace: TaskWorkspace) -> bool:
    """Remove the worktree, keeping the branch. Never fails the delivery."""
    try:
        release_workspace(workspace)
    except GitError as exc:
        logger.warning(
            "worktree_release_failed", path=str(workspace.path), error=str(exc)
        )
        return False
    return True


__all__ = [
    "Delivery",
    "complete_by_hand",
    "deliver_candidate",
    "deliver_escalated_candidate",
]
