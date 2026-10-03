from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from ..config.settings import Settings
from ..domain.enums import RunStatus, TaskStatus
from ..repositories import (
    PauseRequestRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from ..workflow.recovery import RecoveryDisposition, inspect_incomplete_runs
from .runs import create_run
from .scheduler import NoTaskReason, Selection, select_next_task
from .workspace import load_run_context


class CampaignStatus(StrEnum):
    COMPLETE = "COMPLETE"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CONTINUATION_REQUIRED = "CONTINUATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class CampaignTaskSummary:
    external_task_id: str
    task_id: UUID
    status: TaskStatus
    integrated: bool
    run_ids: tuple[UUID, ...] = ()
    latest_run_status: RunStatus | None = None
    latest_run_failure_reason: str | None = None
    latest_candidate_commit: str | None = None
    unintegrated_commit: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignReport:
    project_id: UUID
    status: CampaignStatus
    tasks_considered: tuple[str, ...]
    tasks_completed_this_invocation: tuple[str, ...]
    tasks_already_completed: tuple[str, ...]
    tasks_escalated: tuple[str, ...]
    tasks_blocked: tuple[str, ...]
    current_task: str | None = None
    current_run_id: UUID | None = None
    last_task: str | None = None
    last_run_id: UUID | None = None
    run_ids: tuple[UUID, ...] = ()
    resumed_run_ids: tuple[UUID, ...] = ()
    integrated_commits: tuple[str, ...] = ()
    stop_reason: str = ""
    autonomous_advance_possible: bool = False
    transition_limit: int = 0
    transitions_used: int = 0
    tasks: tuple[CampaignTaskSummary, ...] = ()


TaskExecutor = Callable[[UUID], Awaitable[dict[str, object]]]


@dataclass(slots=True)
class _CampaignProgress:
    completed: list[str] = field(default_factory=list)
    run_ids: list[UUID] = field(default_factory=list)
    resumed_run_ids: list[UUID] = field(default_factory=list)
    integrated_commits: list[str] = field(default_factory=list)
    last_task: str | None = None
    last_run_id: UUID | None = None


class CampaignRunner:
    """Bounded multi-task campaign loop around the existing task workflow."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        task_executor: TaskExecutor,
        transition_limit: int = 10,
        settings: Settings | None = None,
    ) -> None:
        if transition_limit < 1:
            raise ValueError("transition_limit must be >= 1")
        self.session_factory = session_factory
        self.task_executor = task_executor
        self.transition_limit = transition_limit
        self.settings = settings

    async def advance(self, project_id: UUID) -> CampaignReport:
        progress = _CampaignProgress()
        transitions = 0

        while transitions < self.transition_limit:
            started = self._select_or_resume(project_id)
            if started.stop is not None:
                status, stop_reason = started.stop
                return self._report(
                    project_id,
                    status,
                    stop_reason,
                    progress,
                    transitions_used=transitions,
                )
            if started.selection.task is None:
                return self._report_for_selection(
                    project_id,
                    started.selection,
                    progress,
                    transitions_used=transitions,
                )

            task = started.selection.task
            run_id = started.run_id
            progress.last_task = task.external_task_id
            progress.last_run_id = run_id
            progress.run_ids.append(run_id)
            if started.resumed:
                progress.resumed_run_ids.append(run_id)
            transitions += 1

            try:
                state = await self.task_executor(run_id)
            except Exception:
                return self._report(
                    project_id,
                    CampaignStatus.FAILED,
                    "Campaign task executor failed before a durable campaign "
                    "continuation decision could be made.",
                    progress,
                    transitions_used=transitions,
                )

            self._record_successful_transition(project_id, task.external_task_id, state, progress)
            terminal = self._terminal_after_task(project_id, progress, transitions)
            if terminal is not None:
                return terminal

        return self._report(
            project_id,
            CampaignStatus.CONTINUATION_REQUIRED,
            "Transition safety bound reached; invoke campaign advancement again.",
            progress,
            transitions_used=transitions,
            autonomous_advance_possible=True,
        )

    def _select_or_resume(self, project_id: UUID) -> _StartedTask:
        """Decide what to execute next, under the project lock.

        One transaction, and the project row lock is taken first, because the
        decision and the ``create_run`` that follows from it have to be one
        step: two campaigns that both read "no run in flight" would both create
        one. ``select_next_task`` is the predicate, and after the lock it is
        authoritative -- the loser of the race re-reads it and sees the
        winner's run.

        A run already in flight is **not** automatically a refusal. An
        interrupted campaign leaves a durable ``PENDING`` run behind, and the
        existing restart reconciliation already classifies exactly that run as
        ``RESUMABLE``; ``WorkflowRunner.run_next`` resumes it rather than
        stranding it. Campaign advancement reuses the same boundary, so a crash
        between ``create_run`` and the workflow does not need an operator.
        Resuming is the same dispatch call as starting, and no second run row is
        created, so this cannot duplicate work.
        """
        with self.session_factory.begin() as session:
            ProjectRepository(session).lock(project_id)
            existing = self._existing_run(session, project_id)
            if existing is not None:
                return existing
            selection = select_next_task(session, project_id)
            if selection.task is None:
                return _StartedTask(selection=selection)
            run = create_run(session, selection.task.id)
            return _StartedTask(selection=selection, run_id=run.id)

    def _existing_run(self, session: Session, project_id: UUID) -> _StartedTask | None:
        """This project's in-flight run and what may be done with it, if any.

        ``None`` means there is nothing in flight and selection may proceed.
        Every other answer is read from the existing recovery machinery rather
        than decided here: the disposition comes from
        ``inspect_incomplete_runs`` and the ownership question from the run's
        own ``execution_owner`` stamp, which is the same fence
        ``WorkflowRunner._acquire_execution`` applies.
        """
        for candidate in inspect_incomplete_runs(session, settings=self.settings):
            run, task, project = load_run_context(session, candidate.run_id)
            if project.id != project_id:
                continue
            if run.execution_owner is not None:
                # A dispatch is recorded as holding this run. Either it is
                # genuinely executing -- and a second executor is exactly what
                # the generation fence exists to prevent -- or it was killed
                # mid-dispatch and left a stamp that nothing clears. Those two
                # are indistinguishable from the durable record, so this stays
                # fail-closed: taking the run anyway is
                # ``override_active_owner``, which is an operator decision with
                # an audit event, and campaign advancement does not make it.
                return _StartedTask(
                    selection=Selection(reason=NoTaskReason.TASK_IN_FLIGHT),
                    stop=(
                        CampaignStatus.BLOCKED,
                        f"Run {run.external_run_id or run.id} of task "
                        f"{task.external_task_id} is held by dispatch "
                        f"{run.execution_owner}. A live executor must not be "
                        "duplicated, and a dispatch killed mid-execution leaves "
                        "the same stamp behind, so operator recovery with "
                        "override_active_owner is required to tell them apart.",
                    ),
                )
            pause = PauseRequestRepository(session).in_force_for_task(project.id, task.id)
            if pause is not None:
                return _StartedTask(
                    selection=Selection(reason=NoTaskReason.TASK_IN_FLIGHT),
                    stop=(
                        CampaignStatus.BLOCKED,
                        f"Task {task.external_task_id} has a run in flight and a "
                        "pause request in force; release the pause to continue.",
                    ),
                )
            if candidate.disposition is RecoveryDisposition.RESUMABLE:
                return _StartedTask(
                    selection=Selection(task=task), run_id=run.id, resumed=True
                )
            if candidate.disposition is RecoveryDisposition.WAITING_FOR_HUMAN:
                return _StartedTask(
                    selection=Selection(reason=NoTaskReason.TASK_IN_FLIGHT),
                    stop=(
                        CampaignStatus.HUMAN_ACTION_REQUIRED,
                        f"Task {task.external_task_id} has a run in flight that "
                        f"cannot continue: {candidate.detail}.",
                    ),
                )
            return _StartedTask(
                selection=Selection(reason=NoTaskReason.TASK_IN_FLIGHT),
                stop=(
                    CampaignStatus.BLOCKED,
                    f"Task {task.external_task_id} has a run in flight that "
                    f"campaign advancement cannot resume: {candidate.detail}.",
                ),
            )
        return None

    def _record_successful_transition(
        self,
        project_id: UUID,
        external_task_id: str,
        state: dict[str, object],
        progress: _CampaignProgress,
    ) -> None:
        with self.session_factory() as session:
            tasks = {task.external_task_id: task for task in _tasks(session, project_id)}
            task = tasks.get(external_task_id)
            if task is not None and task.status is TaskStatus.COMPLETE:
                progress.completed.append(task.external_task_id)
                if task.unintegrated_commit is None:
                    commit = state.get("commit_sha")
                    if isinstance(commit, str) and commit:
                        progress.integrated_commits.append(commit)

    def _terminal_after_task(
        self,
        project_id: UUID,
        progress: _CampaignProgress,
        transitions: int,
    ) -> CampaignReport | None:
        with self.session_factory() as session:
            tasks = _tasks(session, project_id)
        if any(task.status is TaskStatus.HUMAN_REVIEW for task in tasks):
            return self._report(
                project_id,
                CampaignStatus.HUMAN_ACTION_REQUIRED,
                "At least one task is waiting for human review.",
                progress,
                transitions_used=transitions,
            )
        if any(task.status is TaskStatus.FAILED for task in tasks):
            return self._report(
                project_id,
                CampaignStatus.HUMAN_ACTION_REQUIRED,
                "At least one task exhausted autonomous retry or failed and needs "
                "an operator decision.",
                progress,
                transitions_used=transitions,
            )
        return None

    def _report_for_selection(
        self,
        project_id: UUID,
        selection: Selection,
        progress: _CampaignProgress,
        *,
        transitions_used: int,
    ) -> CampaignReport:
        if selection.reason is NoTaskReason.ALL_TASKS_COMPLETE:
            with self.session_factory() as session:
                tasks = _tasks(session, project_id)
            if any(not task.is_integrated for task in tasks):
                return self._report(
                    project_id,
                    CampaignStatus.BLOCKED,
                    "All tasks have terminal status, but at least one accepted output "
                    "is not in the integration baseline.",
                    progress,
                    transitions_used=transitions_used,
                )
            return self._report(
                project_id,
                CampaignStatus.COMPLETE,
                "All tasks are complete.",
                progress,
                transitions_used=transitions_used,
            )
        if selection.reason is NoTaskReason.TASK_IN_FLIGHT:
            return self._report(
                project_id,
                CampaignStatus.BLOCKED,
                "A task already has active work in flight; campaign will not duplicate it.",
                progress,
                transitions_used=transitions_used,
            )
        if selection.reason is NoTaskReason.PROJECT_NOT_RUNNABLE:
            return self._report(
                project_id,
                CampaignStatus.BLOCKED,
                "Project is not in a runnable state.",
                progress,
                transitions_used=transitions_used,
            )
        with self.session_factory() as session:
            tasks = _tasks(session, project_id)
        if any(task.status in {TaskStatus.HUMAN_REVIEW, TaskStatus.FAILED} for task in tasks):
            return self._report(
                project_id,
                CampaignStatus.HUMAN_ACTION_REQUIRED,
                "No autonomous task is eligible because unresolved work needs human action.",
                progress,
                transitions_used=transitions_used,
            )
        return self._report(
            project_id,
            CampaignStatus.BLOCKED,
            "Pending work remains, but no task is eligible under current durable dependency state.",
            progress,
            transitions_used=transitions_used,
        )

    def _report(
        self,
        project_id: UUID,
        status: CampaignStatus,
        stop_reason: str,
        progress: _CampaignProgress,
        *,
        transitions_used: int,
        autonomous_advance_possible: bool | None = None,
    ) -> CampaignReport:
        with self.session_factory() as session:
            tasks = _tasks(session, project_id)
            runs = TaskRunRepository(session)
            summaries = tuple(_summarize_task(runs, task) for task in tasks)
        completed = tuple(
            task.external_task_id
            for task in tasks
            if task.status is TaskStatus.COMPLETE and task.is_integrated
        )
        escalated = tuple(
            task.external_task_id
            for task in tasks
            if task.status in {TaskStatus.HUMAN_REVIEW, TaskStatus.FAILED}
        )
        blocked = tuple(
            task.external_task_id
            for task in tasks
            if task.status is TaskStatus.BLOCKED or task.unintegrated_commit is not None
        )
        if autonomous_advance_possible is None:
            autonomous_advance_possible = status is CampaignStatus.CONTINUATION_REQUIRED
        return CampaignReport(
            project_id=project_id,
            status=status,
            tasks_considered=tuple(task.external_task_id for task in tasks),
            tasks_completed_this_invocation=tuple(progress.completed),
            tasks_already_completed=completed,
            tasks_escalated=escalated,
            tasks_blocked=blocked,
            current_task=(
                progress.last_task if status is CampaignStatus.CONTINUATION_REQUIRED else None
            ),
            current_run_id=progress.last_run_id
            if status is CampaignStatus.CONTINUATION_REQUIRED
            else None,
            last_task=progress.last_task,
            last_run_id=progress.last_run_id,
            run_ids=tuple(progress.run_ids),
            resumed_run_ids=tuple(progress.resumed_run_ids),
            integrated_commits=tuple(progress.integrated_commits),
            stop_reason=stop_reason,
            autonomous_advance_possible=autonomous_advance_possible,
            transition_limit=self.transition_limit,
            transitions_used=transitions_used,
            tasks=summaries,
        )


@dataclass(frozen=True, slots=True)
class _StartedTask:
    selection: Selection
    run_id: UUID | None = None
    #: This run already existed and is being resumed, not created.
    resumed: bool = False
    #: A terminal campaign answer the selection step reached on its own.
    stop: tuple[CampaignStatus, str] | None = None


def _tasks(session: Session, project_id: UUID):
    return TaskRepository(session).list_for_project(project_id)


def _summarize_task(runs: TaskRunRepository, task) -> CampaignTaskSummary:
    task_runs = runs.list_for_task(task.id)
    latest = task_runs[-1] if task_runs else None
    return CampaignTaskSummary(
        external_task_id=task.external_task_id,
        task_id=task.id,
        status=task.status,
        integrated=task.is_integrated,
        run_ids=tuple(run.id for run in task_runs),
        latest_run_status=latest.status if latest is not None else None,
        latest_run_failure_reason=latest.failure_reason if latest is not None else None,
        latest_candidate_commit=latest.candidate_commit if latest is not None else None,
        unintegrated_commit=task.unintegrated_commit,
    )


__all__ = [
    "CampaignReport",
    "CampaignRunner",
    "CampaignStatus",
    "CampaignTaskSummary",
]
