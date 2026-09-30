"""The cumulative accepted baseline (concern 51).

Task dependencies used to constrain *scheduling* and nothing else. Every task
worktree started from the imported branch, so a task that depended on another
one was scheduled after it and then handed a tree without its work in it. The
TraceStack run made the consequence concrete: TS-104 was told to reuse the
``find()`` that TS-103 had just added, could not see it, and correctly wrote its
own private one. Four individually verified candidates that would not merge
together.

This module is the missing half. One ref per project --
``domain.git.INTEGRATION_BRANCH`` -- holds the cumulative state of everything
that has been accepted *and* shown to work together, and new task worktrees
start from it.

Four properties, in the order they matter.

* **The imported branch is never advanced.** It stays exactly where the operator
  left it. `GitService` protects it and `force_branch` refuses it, so this
  module cannot move it even by mistake. Merging the orchestrator's work back
  into a project's own branch stays what it already was: a human's decision.
* **The ref moves last.** The integration worktree is always *detached*, so the
  merge and the verification both happen on a commit that no branch points at.
  The ref is moved only once both have passed, which is what makes a conflict or
  a failure a non-event rather than something to roll back. There is no window
  in which the baseline names a tree nobody has verified.
* **Verification runs over the merged tree, not over the candidate.** Two
  candidates can each pass on their own and fail together; that is the whole
  reason a cumulative baseline needs its own gate. The commands are the
  project's own profile, run in an ordinary worker under the ordinary worker
  policy -- nothing here relaxes the sandbox.
* **A blocked integration is loud, keeps the last good baseline, and stops the
  tasks that depend on it.** The candidate is already committed on its own branch
  with its own tag, and that record is untouched; what does not happen is the
  baseline moving. The next task therefore starts from the last state known to
  work -- but *not* a task that depended on this one, because the tree it would
  start from does not contain what it was told to build on. So the block is
  recorded on the task (``unintegrated_commit``), which readiness reads, and an
  escalation is opened for a person, which is the only thing that clears it. This
  is the half the first version of this module left open: it wrote the event and
  nothing read it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import (
    EscalationStatus,
    FailureReason,
    RunEventType,
    VerificationStatus,
    VerificationType,
)
from ..domain.escalation import (
    integration_escalation_options,
    render_integration_escalation,
)
from ..domain.git import INTEGRATION_BRANCH, INTEGRATION_WORKTREE_DIR
from ..domain.models import (
    HumanEscalation,
    Project,
    RunEvent,
    Task,
    TaskRun,
    VerificationRun,
)
from ..domain.verification import COMMAND_CATEGORIES
from ..repositories import (
    EscalationRepository,
    RunEventRepository,
    TaskRepository,
    VerificationRunRepository,
)
from . import artifact_store
from .command_execution import execute_commands
from .errors import EntityConflict
from .git_errors import GitError, MergeConflict, WorktreeUnusable
from .git_service import GitService
from .worker_service import worker_session

logger = get_logger(__name__)

#: Where the operator's page for a blocked integration is filed under the run.
INTEGRATION_ESCALATION_ARTIFACT = "integration/escalation.txt"

#: Cumulative check to candidate check, per category. Every category the gate
#: runs is mapped, including ``SECURITY``, which is in ``COMMAND_CATEGORIES``:
#: filing a security command as ``INTEGRATION_TESTS`` would be the one guess
#: here that could matter, and a default would have made that guess silently.
_INTEGRATION_TYPES: dict[VerificationType, VerificationType] = {
    VerificationType.BUILD: VerificationType.INTEGRATION_BUILD,
    VerificationType.LINT: VerificationType.INTEGRATION_LINT,
    VerificationType.TESTS: VerificationType.INTEGRATION_TESTS,
    VerificationType.SECURITY: VerificationType.INTEGRATION_SECURITY,
}


@dataclass(frozen=True, slots=True)
class Integration:
    """What integrating one accepted candidate did, or did not, do."""

    #: Whether the baseline now contains this candidate.
    advanced: bool
    #: The baseline before and after. Equal when nothing advanced.
    previous_sha: str
    baseline_sha: str
    #: The merge commit that was verified, when a merge got that far.
    merged_sha: str | None = None
    #: Why the baseline did not move. ``None`` when it did.
    blocked_reason: str | None = None
    #: Paths Git could not merge, when that is what stopped it.
    conflicts: tuple[str, ...] = ()
    #: Cumulative verification commands that failed, when that is what stopped it.
    failed_commands: tuple[str, ...] = ()
    #: How many commands ran over the merged tree. Zero means the project
    #: declares no verification profile, not that nothing was checked.
    commands_run: int = 0
    #: The escalation opened for a person, when the baseline did not move. The
    #: durable half of "this is blocked": the event says it happened, this is
    #: the thing somebody has to answer.
    escalation_id: UUID | None = None
    #: The commit that was integrated, which is the accepted candidate unless a
    #: person resolved a blockage on top of it (``retry_integration``).
    integrated_sha: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "advanced": self.advanced,
            "previous_sha": self.previous_sha,
            "baseline_sha": self.baseline_sha,
            "merged_sha": self.merged_sha,
            "blocked_reason": self.blocked_reason,
            "conflicts": list(self.conflicts),
            "failed_commands": list(self.failed_commands),
            "commands_run": self.commands_run,
            "escalation_id": str(self.escalation_id) if self.escalation_id else None,
            "integrated_sha": self.integrated_sha,
        }


def integration_worktree_path(project_id: UUID, *, settings: Settings | None = None) -> Path:
    """Where a project's integration worktree lives.

    Derived rather than stored, for the same reason a task worktree's path is:
    the same project always names the same directory, so a process that restarts
    can find it without a column that could disagree with the disk.
    """
    config = settings or get_settings()
    return config.worktree_root / str(project_id) / INTEGRATION_WORKTREE_DIR


def ensure_integration_branch(repository: GitService, project: Project) -> str:
    """The integration ref's current SHA, creating the ref if it is absent.

    A project that has never had an accepted task starts its baseline at the
    imported branch, which is the only moment the two are the same commit.
    """
    if repository.branch_exists(INTEGRATION_BRANCH):
        return repository.resolve_sha(INTEGRATION_BRANCH)
    start = repository.resolve_sha(project.default_branch)
    created = repository.force_branch(INTEGRATION_BRANCH, start)
    logger.info(
        "integration_branch_created",
        project_id=str(project.id),
        branch=INTEGRATION_BRANCH,
        sha=created,
        from_branch=project.default_branch,
    )
    return created


def integration_baseline(repository: GitService, project: Project) -> str:
    """The commit a new task worktree starts from.

    This is the whole of concern 51's read side: one function, called where the
    imported branch used to be resolved directly.
    """
    return ensure_integration_branch(repository, project)


def integrate_candidate(
    session: Session,
    project: Project,
    task: Task,
    run: TaskRun,
    candidate_sha: str,
    *,
    settings: Settings | None = None,
) -> Integration:
    """Merge an accepted candidate into the baseline, gated on the merged tree.

    Never raises for an outcome: a conflict and a cumulative verification
    failure are both *results*, because the candidate is already committed and
    the only question left is whether the baseline may include it.

    Raises:
        GitError: the repository itself could not be operated on -- a broken
            worktree, not a rejected merge.
    """
    config = settings or get_settings()
    repository = GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=config,
    )
    previous = ensure_integration_branch(repository, project)

    # Concern 76: idempotent integration. Git cannot share the delivery
    # transaction, so a crash after the ref moved but before the database
    # committed leaves a baseline that already carries this exact candidate while
    # the run row still says "delivery owed". Asking Git the same question
    # ``retry_integration`` asks -- is the work already in the baseline -- turns a
    # replayed delivery into a settle instead of a second, divergent merge. On the
    # ordinary path the candidate is a brand-new commit and is never contained, so
    # this short-circuit cannot fire except on a genuine replay.
    if repository.contains_commit(candidate_sha, ref=INTEGRATION_BRANCH):
        logger.info(
            "integration_candidate_already_in_baseline",
            task=task.external_task_id,
            run_id=str(run.id),
            candidate_sha=candidate_sha,
            baseline_sha=previous,
        )
        return _already_integrated(session, project, task, run, previous, candidate_sha)

    worktree = _integration_worktree(repository, project, previous, config=config)
    try:
        merged = worktree.merge(
            candidate_sha,
            message=(
                f"Integrate {task.external_task_id}: {task.title}\n\n"
                f"Candidate {candidate_sha} accepted by run {run.id}."
            ),
        )
    except MergeConflict as conflict:
        return _blocked(
            session, project, task, run, previous,
            candidate_sha=candidate_sha,
            reason=(
                "the accepted candidate does not merge into the current "
                "integration baseline"
            ),
            conflicts=conflict.paths,
            settings=config,
        )

    failed, ran = _verify_cumulative(
        session, project, run, worktree_path=worktree.path, settings=config
    )
    if failed:
        return _blocked(
            session, project, task, run, previous,
            candidate_sha=candidate_sha,
            reason=(
                "the merged tree failed the project's own verification, so the "
                "candidate works alone but not together with what came before"
            ),
            failed_commands=failed,
            merged_sha=merged,
            commands_run=ran,
            settings=config,
        )

    advanced = repository.force_branch(INTEGRATION_BRANCH, merged)
    # The task's output is in the baseline, so nothing of it is outstanding and
    # anything that depends on it may run (concern 51). Written here rather than
    # in `delivery` because this function is the only thing that knows whether
    # the ref moved, and the flag is a statement about the ref.
    TaskRepository(session).record_integration(task.id, unintegrated_commit=None)
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.INTEGRATION_ADVANCED,
            attempt=run.attempt_number,
            payload={
                "branch": INTEGRATION_BRANCH,
                "previous_sha": previous,
                "baseline_sha": advanced,
                "candidate_sha": candidate_sha,
                "integrated_sha": candidate_sha,
                "commands_run": ran,
            },
        )
    )
    logger.info(
        "integration_advanced",
        task=task.external_task_id,
        run_id=str(run.id),
        previous_sha=previous,
        baseline_sha=advanced,
        commands_run=ran,
    )
    return Integration(
        advanced=True,
        previous_sha=previous,
        baseline_sha=advanced,
        merged_sha=merged,
        commands_run=ran,
        integrated_sha=candidate_sha,
    )


def retry_integration(
    session: Session,
    task_run_id: UUID,
    *,
    settings: Settings | None = None,
) -> Integration:
    """Re-attempt the integration of an accepted candidate a person unblocked.

    The answer to the one option a blocked integration offers. What a person did
    in between is outside this system -- they merged the baseline into the task
    branch, or moved the baseline, or merged the candidate by hand -- so this
    does not trust any of it. It re-asks the same two deterministic questions:
    does it merge, and does the merged tree pass the project's own verification.

    Three cases, in the order they are checked.

    * **The baseline already contains the candidate.** Somebody merged it by
      hand. There is nothing to merge and nothing to verify that the baseline has
      not already been verified with, so the flag is cleared and the event says
      who did it. Asked of Git rather than taken on trust.
    * **The task branch has moved on top of the candidate.** This is the ordinary
      resolution: the conflict was resolved on the branch. The branch head is
      what gets integrated, and it is required to *contain* the accepted
      candidate -- a branch that no longer has the reviewed commit in its history
      is a different change, and integrating it would launder unreviewed work
      through an escalation answer.
    * **Neither.** The same commit is merged into the same baseline again, which
      is worth doing only because the baseline may have moved. Failing again is a
      result, not an error: it blocks again, and opens a new escalation.

    Raises:
        EntityConflict: the run never recorded a candidate commit, or its branch
            no longer contains the accepted one.
        GitError: the repository itself could not be operated on.
    """
    config = settings or get_settings()
    # Imported here for the reason `workspace` imports this module that way: the
    # two need each other and only one of them can win at module level.
    from .workspace import load_run_context

    run, task, project = load_run_context(session, task_run_id)
    if run.candidate_commit is None:
        raise EntityConflict(
            f"Run {run.id} recorded no candidate commit, so there is nothing to "
            "integrate"
        )
    accepted = run.candidate_commit
    repository = GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=config,
    )
    previous = ensure_integration_branch(repository, project)

    if repository.contains_commit(accepted, ref=INTEGRATION_BRANCH):
        return _already_integrated(session, project, task, run, previous, accepted)

    target = accepted
    if run.branch_name and repository.branch_exists(run.branch_name):
        head = repository.resolve_sha(run.branch_name)
        if head != accepted:
            if not repository.contains_commit(accepted, ref=head):
                raise EntityConflict(
                    f"Branch {run.branch_name} no longer contains the accepted "
                    f"candidate {accepted}; the reviewed work must stay in the "
                    "history that is integrated"
                )
            target = head

    logger.info(
        "integration_retried",
        task=task.external_task_id,
        run_id=str(run.id),
        baseline_sha=previous,
        accepted_sha=accepted,
        integrating_sha=target,
    )
    return integrate_candidate(session, project, task, run, target, settings=config)


def _already_integrated(
    session: Session,
    project: Project,
    task: Task,
    run: TaskRun,
    baseline: str,
    accepted: str,
) -> Integration:
    """Clear the block for a candidate an operator merged into the baseline.

    The ref is not touched: it already contains the work, and moving it would be
    this module re-deciding something a person has already done. What changes is
    only the orchestrator's record of it, which is what was wrong.
    """
    TaskRepository(session).record_integration(task.id, unintegrated_commit=None)
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.INTEGRATION_ADVANCED,
            attempt=run.attempt_number,
            payload={
                "branch": INTEGRATION_BRANCH,
                "previous_sha": baseline,
                "baseline_sha": baseline,
                "candidate_sha": accepted,
                "integrated_sha": accepted,
                "resolved_by": "operator",
                "commands_run": 0,
            },
        )
    )
    logger.info(
        "integration_resolved_by_operator",
        task=task.external_task_id,
        run_id=str(run.id),
        baseline_sha=baseline,
        candidate_sha=accepted,
    )
    return Integration(
        advanced=True,
        previous_sha=baseline,
        baseline_sha=baseline,
        merged_sha=None,
        integrated_sha=accepted,
    )


# --------------------------------------------------------------------- internals


def _integration_worktree(
    repository: GitService, project: Project, baseline: str, *, config: Settings
) -> GitService:
    """The project's integration worktree, detached at ``baseline``.

    Reused across runs and reset each time rather than created and destroyed: the
    dependency tree it needs for cumulative verification is expensive to copy,
    and nothing in it is worth keeping between integrations.
    """
    path = integration_worktree_path(project.id, settings=config)
    worktree = _usable_or_recreated_integration_worktree(
        repository, path, baseline
    )
    _copy_dependencies(project, path, worktree)
    return worktree


def _usable_or_recreated_integration_worktree(
    repository: GitService, path: Path, baseline: str
) -> GitService:
    """Return the supervisor-owned integration worktree at ``baseline``.

    The integration worktree is disposable supervisor state. If its linked
    worktree metadata was written in another namespace and is not usable here,
    remove exactly that worktree and recreate it from the expected baseline.
    Task run worktrees are deliberately not repaired this way: they may be the
    only copy of an approved or escalated candidate.
    """
    if (path / ".git").exists():
        worktree = repository.for_worktree(path)
        try:
            worktree.assert_usable_worktree()
            worktree.reset_hard_to_sha(baseline)
            worktree.checkout_detached(baseline)
            return worktree
        except WorktreeUnusable as exc:
            logger.warning(
                "integration_worktree_unusable_recreating",
                path=str(path),
                error=str(exc),
            )
            _remove_owned_integration_worktree(repository, path)
        except GitError:
            # Other Git failures (for example a dirty or conflicted but usable
            # tree) remain loud. Recreating on every GitError would turn an
            # operator problem into silent deletion of evidence.
            raise
    elif path.exists():
        shutil.rmtree(path)
    repository.prune_worktrees()
    recreated = repository.create_detached_worktree(path, baseline)
    recreated.assert_usable_worktree(expected_head=baseline)
    return recreated


def _remove_owned_integration_worktree(repository: GitService, path: Path) -> None:
    """Remove only the orchestrator-owned integration worktree path."""
    repository.remove_worktree(path)
    if path.exists():
        shutil.rmtree(path)


def _copy_dependencies(project: Project, worktree: Path, worktree_git: GitService) -> None:
    """Give the integration worktree the same declared dependencies a task gets.

    Cumulative verification runs the project's real commands, so it needs what
    they need (concern 12). Deliberately a copy of the same rule rather than a
    call into ``workspace``: that module is about a *run's* worktree and this one
    has no run.
    """
    repository_root = Path(project.repository_path).resolve()
    for declared in project.dependency_paths:
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"Unsafe dependency path {declared!r}")
        source = (repository_root / relative).resolve()
        if not source.is_relative_to(repository_root) or not source.exists():
            raise ValueError(f"Unusable dependency path {declared!r}")
        normalized = relative.as_posix()
        candidates = (normalized, f"{normalized}/") if source.is_dir() else (normalized,)
        if not any(worktree_git.is_ignored(form) for form in candidates):
            raise ValueError(
                f"Dependency path {declared!r} must be ignored by Git before it "
                "can be copied into the integration worktree"
            )
        target = worktree / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)


def _verify_cumulative(
    session: Session,
    project: Project,
    run: TaskRun,
    *,
    worktree_path: Path,
    settings: Settings,
) -> tuple[tuple[str, ...], int]:
    """Run the project's profile over the merged tree. Returns failures and count.

    The same commands, the same worker policy and the same per-command ceilings
    as an ordinary verification (section 17): what differs is only the tree they
    run against. Logs are filed under the run that triggered the integration, so
    the evidence sits with the run whose acceptance caused it.

    Each command is also written to ``verification_runs``, with a
    ``verification_type`` of its own. This is the gap that made the cumulative
    gate unauditable: the run's verification history showed the candidate's own
    checks -- eight green rows in the TraceStack run -- and nothing about the
    merged tree that actually decided whether the baseline moved. A reader asking
    "was the cumulative gate really run?" had only a log directory and an event
    payload to answer with, and the event was written by the *other* branch of
    this function, so a crash between the two left no row at all.

    The type is what distinguishes them. ``INTEGRATION_BUILD``/``LINT``/``TESTS``
    rather than ``BUILD``/``LINT``/``TESTS`` on purpose: same table, same
    repository, same ``VerificationRun`` model as a candidate check, and a
    question that reads the history can tell "this passed on its own" from "this
    passed together with what came before" without guessing from the command
    text. No new table, no new model, no second gate.
    """
    profile = project.verification
    if profile.is_empty:
        logger.warning(
            "cumulative_verification_skipped",
            project_id=str(project.id),
            run_id=str(run.id),
            detail="the project declares no verification commands",
        )
        return (), 0

    types = {
        category: _INTEGRATION_TYPES.get(category, VerificationType.INTEGRATION_TESTS)
        for category in COMMAND_CATEGORIES
    }
    verifications = VerificationRunRepository(session)
    failures: list[str] = []
    ran = 0
    with worker_session(
        worktree_path, profile=project.worker_profile, settings=settings
    ) as worker:
        for category in COMMAND_CATEGORIES:
            commands = profile.commands_for(category)
            if not commands:
                continue
            executions = execute_commands(
                session,
                worker,
                run.id,
                commands,
                category=f"integration-{category.value.casefold()}",
                prefix="integration/",
                settings=settings,
                stop_on_failure=True,
                record_logs=True,
            )
            ran += len(executions)
            for execution in executions:
                # Written per command as it finishes rather than per category at
                # the end: a kill between two commands in a profile must not
                # erase the checks that already passed.
                verifications.add(
                    VerificationRun(
                        task_run_id=run.id,
                        verification_type=types[category],
                        command=execution.result.command.source,
                        status=(
                            VerificationStatus.PASSED
                            if execution.succeeded
                            else VerificationStatus.FAILED
                        ),
                        exit_code=execution.result.exit_code,
                        stdout_artifact=execution.log_artifact,
                        duration_ms=execution.result.duration_ms,
                    )
                )
            failures.extend(
                execution.result.command.source
                for execution in executions
                if not execution.succeeded
            )
            if failures:
                # Same rule as the ordinary pipeline: a tree that does not build
                # has nothing useful to say about its own tests.
                break
    # The gate is what a person reads when the baseline refused to move, and it
    # is the evidence the escalation points at, so it is written before the
    # branch that decides anything.
    session.flush()
    return tuple(failures), ran


def _blocked(
    session: Session,
    project: Project,
    task: Task,
    run: TaskRun,
    previous: str,
    *,
    candidate_sha: str,
    reason: str,
    settings: Settings,
    conflicts: tuple[str, ...] = (),
    failed_commands: tuple[str, ...] = (),
    merged_sha: str | None = None,
    commands_run: int = 0,
) -> Integration:
    """Leave the baseline where it is, and make the consequence durable.

    Three things happen, and the first one is the fix to what concern 51 left
    open. The event was always written; what it could not do is stop anything,
    because an event is a record and scheduling reads state.

    * The task is marked as holding an **unintegrated commit**. The task stays
      ``COMPLETE`` -- it is, a reviewer accepted it, and marking delivered work
      FAILED would be a lie that also discards it -- but from this moment its
      dependents are not eligible, because the tree they would start from does
      not contain the thing they were told to build on.
    * An **escalation** is opened, with the one option the orchestrator can
      deterministically carry out. This is what makes the condition
      operator-visible and answerable rather than merely logged.
    * The **baseline does not move**, which is the part that needs no code: the
      integration worktree is detached, so nothing was pointing at the merge.
    """
    tasks = TaskRepository(session)
    tasks.record_integration(task.id, unintegrated_commit=candidate_sha)
    dependents = tuple(
        sorted(
            other.external_task_id
            for other in tasks.list_for_project(project.id)
            if task.external_task_id in other.depends_on
        )
    )
    escalation = _escalate_integration(
        session,
        project,
        task,
        run,
        previous,
        candidate_sha=candidate_sha,
        reason=reason,
        conflicts=conflicts,
        failed_commands=failed_commands,
        dependents=dependents,
        settings=settings,
    )
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.INTEGRATION_BLOCKED,
            attempt=run.attempt_number,
            payload={
                "branch": INTEGRATION_BRANCH,
                "baseline_sha": previous,
                "reason": reason,
                "conflicts": list(conflicts),
                "failed_commands": list(failed_commands),
                "candidate_sha": candidate_sha,
                "escalation_id": str(escalation.id),
                "blocked_dependents": list(dependents),
            },
        )
    )
    logger.warning(
        "integration_blocked",
        task=task.external_task_id,
        run_id=str(run.id),
        baseline_sha=previous,
        reason=reason,
        conflicts=list(conflicts),
        failed_commands=list(failed_commands),
        escalation_id=str(escalation.id),
        blocked_dependents=list(dependents),
    )
    return Integration(
        advanced=False,
        previous_sha=previous,
        baseline_sha=previous,
        merged_sha=merged_sha,
        blocked_reason=reason,
        conflicts=conflicts,
        failed_commands=failed_commands,
        commands_run=commands_run,
        escalation_id=escalation.id,
    )


def _escalate_integration(
    session: Session,
    project: Project,
    task: Task,
    run: TaskRun,
    baseline_sha: str,
    *,
    candidate_sha: str,
    reason: str,
    conflicts: tuple[str, ...],
    failed_commands: tuple[str, ...],
    dependents: tuple[str, ...],
    settings: Settings,
) -> HumanEscalation:
    """Open the escalation a blocked integration needs (section 24).

    A fresh one every time, including after a failed ``retry_integration``: an
    escalation is the record of one question asked once, and answering it is what
    closes it. A condition that is still true after an answer is a new question,
    and re-opening the answered row would erase the fact that somebody tried.
    """
    options = integration_escalation_options()
    summary = render_integration_escalation(
        external_task_id=task.external_task_id,
        branch=run.branch_name or "(the run recorded no branch)",
        candidate_commit=candidate_sha,
        baseline_sha=baseline_sha,
        integration_branch=INTEGRATION_BRANCH,
        blocker=reason,
        conflicts=conflicts,
        failed_commands=failed_commands,
        dependents=dependents,
        options=options,
    )
    try:
        artifact_store.write_text(
            session,
            run.id,
            INTEGRATION_ESCALATION_ARTIFACT,
            summary,
            kind=INTEGRATION_ESCALATION_ARTIFACT,
            settings=settings,
        )
    except Exception as error:  # noqa: BLE001 - the row is the record that matters
        logger.warning(
            "integration_escalation_artifact_failed",
            run_id=str(run.id),
            task=task.external_task_id,
            error=str(error),
        )
    escalation = EscalationRepository(session).add(
        HumanEscalation(
            task_id=task.id,
            task_run_id=run.id,
            reason=FailureReason.INTEGRATION_BLOCKED.value,
            summary=summary,
            options=list(options),
            status=EscalationStatus.OPEN,
        )
    )
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.HUMAN_REVIEW_REQUIRED,
            attempt=run.attempt_number,
            payload={
                "escalation_id": str(escalation.id),
                "failure_reason": FailureReason.INTEGRATION_BLOCKED.value,
                "candidate_sha": candidate_sha,
                "blocked_dependents": list(dependents),
            },
        )
    )
    return escalation


def integrate_human_commit(
    session: Session,
    project: Project,
    task: Task,
    commit_sha: str,
    *,
    task_run_id: UUID | None = None,
    settings: Settings | None = None,
) -> Integration:
    """Merge a human-produced commit into the baseline (concern 73).

    Used when an operator resolves a ``COMPLETED_BY_HAND`` escalation with a
    Git commit. The commit is validated, merged, and verified through the same
    cumulative gate as an automated candidate. If the merge or verification
    fails, the function raises so the caller can roll back the escalation
    resolution.

    Raises:
        EntityNotFound: the commit does not exist in the repository.
        MergeConflict: the commit cannot be merged into the baseline.
        EntityConflict: cumulative verification failed.
        GitError: the repository itself could not be operated on.
    """
    from .errors import EntityNotFound

    config = settings or get_settings()
    repository = GitService(
        project.repository_path,
        default_branch=project.default_branch,
        settings=config,
    )

    try:
        repository.resolve_sha(commit_sha)
    except Exception as exc:
        raise EntityNotFound(
            "Commit", f"{commit_sha} in repository {project.repository_path}"
        ) from exc

    previous = ensure_integration_branch(repository, project)

    if repository.contains_commit(commit_sha, ref=INTEGRATION_BRANCH):
        TaskRepository(session).record_integration(task.id, unintegrated_commit=None)
        if task_run_id is not None:
            RunEventRepository(session).append(
                RunEvent(
                    task_run_id=task_run_id,
                    project_id=project.id,
                    task_id=task.id,
                    event_type=RunEventType.INTEGRATION_ADVANCED,
                    attempt=1,
                    payload={
                        "branch": INTEGRATION_BRANCH,
                        "previous_sha": previous,
                        "baseline_sha": previous,
                        "candidate_sha": commit_sha,
                        "integrated_sha": commit_sha,
                        "resolved_by": "operator",
                        "provenance": "human",
                        "commands_run": 0,
                    },
                )
            )
        logger.info(
            "human_commit_already_integrated",
            task=task.external_task_id,
            commit_sha=commit_sha,
            baseline_sha=previous,
        )
        return Integration(
            advanced=True,
            previous_sha=previous,
            baseline_sha=previous,
            merged_sha=None,
            integrated_sha=commit_sha,
        )

    worktree = _integration_worktree(repository, project, previous, config=config)
    try:
        merged = worktree.merge(
            commit_sha,
            message=(
                f"Integrate {task.external_task_id}: {task.title}\n\n"
                f"Human commit {commit_sha} integrated by operator."
            ),
        )
    except MergeConflict as conflict:
        raise MergeConflict(conflict.path, conflict.paths) from None

    failed, ran = _verify_cumulative_human(
        session,
        project,
        task,
        task_run_id,
        worktree_path=worktree.path,
        settings=config,
    )
    if failed:
        raise EntityConflict(
            f"Human commit {commit_sha} failed cumulative verification: "
            f"{', '.join(failed)}"
        )

    advanced = repository.force_branch(INTEGRATION_BRANCH, merged)
    TaskRepository(session).record_integration(task.id, unintegrated_commit=None)
    if task_run_id is not None:
        RunEventRepository(session).append(
            RunEvent(
                task_run_id=task_run_id,
                project_id=project.id,
                task_id=task.id,
                event_type=RunEventType.INTEGRATION_ADVANCED,
                attempt=1,
                payload={
                    "branch": INTEGRATION_BRANCH,
                    "previous_sha": previous,
                    "baseline_sha": advanced,
                    "candidate_sha": commit_sha,
                    "integrated_sha": commit_sha,
                    "provenance": "human",
                    "commands_run": ran,
                },
            )
        )
    logger.info(
        "human_commit_integrated",
        task=task.external_task_id,
        commit_sha=commit_sha,
        previous_sha=previous,
        baseline_sha=advanced,
        commands_run=ran,
    )
    return Integration(
        advanced=True,
        previous_sha=previous,
        baseline_sha=advanced,
        merged_sha=merged,
        commands_run=ran,
        integrated_sha=commit_sha,
    )


def _verify_cumulative_human(
    session: Session,
    project: Project,
    task: Task,
    task_run_id: UUID | None,
    *,
    worktree_path: Path,
    settings: Settings,
) -> tuple[tuple[str, ...], int]:
    """Run cumulative verification for a human commit.

    Similar to ``_verify_cumulative`` but does not require a TaskRun. If
    ``task_run_id`` is None, verification runs are not recorded but the
    commands still execute.
    """
    profile = project.verification
    if profile.is_empty:
        logger.warning(
            "cumulative_verification_skipped",
            project_id=str(project.id),
            task=task.external_task_id,
            detail="the project declares no verification commands",
        )
        return (), 0

    types = {
        category: _INTEGRATION_TYPES.get(category, VerificationType.INTEGRATION_TESTS)
        for category in COMMAND_CATEGORIES
    }
    verifications = VerificationRunRepository(session) if task_run_id else None
    failures: list[str] = []
    ran = 0
    with worker_session(
        worktree_path, profile=project.worker_profile, settings=settings
    ) as worker:
        for category in COMMAND_CATEGORIES:
            commands = profile.commands_for(category)
            if not commands:
                continue
            executions = execute_commands(
                session,
                worker,
                task_run_id,
                commands,
                category=f"integration-{category.value.casefold()}",
                prefix="integration/",
                settings=settings,
                stop_on_failure=True,
                record_logs=True,
            )
            ran += len(executions)
            for execution in executions:
                if verifications is not None:
                    verifications.add(
                        VerificationRun(
                            task_run_id=task_run_id,
                            verification_type=types[category],
                            command=execution.result.command.source,
                            status=(
                                VerificationStatus.PASSED
                                if execution.succeeded
                                else VerificationStatus.FAILED
                            ),
                            exit_code=execution.result.exit_code,
                            stdout_artifact=execution.log_artifact,
                            duration_ms=execution.result.duration_ms,
                        )
                    )
                if not execution.succeeded:
                    failures.append(execution.result.command.source)
            if failures:
                break
    session.flush()
    return tuple(failures), ran


__all__ = [
    "INTEGRATION_ESCALATION_ARTIFACT",
    "Integration",
    "ensure_integration_branch",
    "integrate_candidate",
    "integrate_human_commit",
    "integration_baseline",
    "integration_worktree_path",
    "retry_integration",
]
