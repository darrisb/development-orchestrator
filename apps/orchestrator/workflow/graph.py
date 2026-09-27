"""The Phase K LangGraph: coordination around existing deterministic services.

Nodes contain no coding, review, Git or state-machine policy. They open one
short transaction, call the service that owns the operation, and return small
serializable facts used only for conditional edges.
"""

from __future__ import annotations

from datetime import datetime
from typing import NotRequired, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from ..agents.fix_loop import LoopOutcome, run_fix_loop
from ..config.settings import Settings, get_settings
from ..domain.enums import EscalationStatus, ModelRole, RunStatus, TaskStatus
from ..domain.escalation import EscalationIntent
from ..domain.workflow import WorkflowOutcome, WorkflowPhase, run_deadline
from ..providers import ModelProvider, build_provider, build_review_provider
from ..providers.review import ReviewProvider
from ..repositories import (
    EscalationRepository,
    PauseRequestRepository,
    TaskRepository,
    TaskRunRepository,
)
from ..services.delivery import deliver_candidate
from ..services.errors import EntityConflict, EntityNotFound
from ..services.model_providers import resolve_for_role
from ..services.runs import create_run
from ..services.scheduler import Selection, select_next_task
from ..services.workspace import attach_workspace, load_run_context, prepare_workspace
from ..services.worktrees import release_for_run
from .checkpoints import SqlAlchemyCheckpointSaver
from .recovery import RecoveryDisposition, inspect_incomplete_runs


class WorkflowState(TypedDict):
    project_id: str
    task_id: str
    run_id: str
    phase: str
    outcome: NotRequired[str]
    task_status: NotRequired[str]
    loop_outcome: NotRequired[str]
    deadline: NotRequired[str]
    escalation_id: NotRequired[str]
    commit_sha: NotRequired[str]
    worktree_released: NotRequired[bool]
    #: The task completed, but its accepted work is not in the cumulative
    #: integration baseline and a person has to resolve that (concern 51).
    #: Reported rather than turned into a failed outcome: the task really is
    #: complete, and what is blocked is everything that depends on it.
    integration_blocked: NotRequired[bool]
    pause_request_id: NotRequired[str]
    resume_status: NotRequired[str]
    error: NotRequired[str]


class WorkflowRunner:
    """Compile and invoke the one-task-at-a-time V1 graph."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        coder: ModelProvider,
        reviewer: ReviewProvider,
        settings: Settings | None = None,
        checkpointer: SqlAlchemyCheckpointSaver | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.coder = coder
        self.reviewer = reviewer
        self.settings = settings or get_settings()
        self.checkpointer = checkpointer or SqlAlchemyCheckpointSaver(session_factory)
        self.graph = self._build_graph()

    @classmethod
    def configured(
        cls,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings | None = None,
    ) -> WorkflowRunner:
        """Build the runner from the configured coder and reviewer roles."""
        config = settings or get_settings()
        with session_factory() as session:
            coder_config = resolve_for_role(
                session, ModelRole.CODER, settings=config
            )
            reviewer_config = resolve_for_role(
                session, ModelRole.REVIEWER, settings=config
            )
        return cls(
            session_factory,
            coder=build_provider(coder_config),
            reviewer=build_review_provider(reviewer_config, settings=config),
            settings=config,
        )

    async def aclose(self) -> None:
        """Release provider transports owned by this runner."""
        await self.coder.aclose()
        await self.reviewer.aclose()

    async def run_next(self, project_id: UUID) -> tuple[Selection, WorkflowState | None]:
        """Select, create and execute the project's next eligible task."""
        existing_run: UUID | None = None
        existing_task = None
        with self.session_factory() as session:
            for candidate in inspect_incomplete_runs(session, settings=self.settings):
                run, task, project = load_run_context(session, candidate.run_id)
                if project.id != project_id:
                    continue
                pause = PauseRequestRepository(session).in_force_for_task(
                    project.id, task.id
                )
                if pause is None and candidate.disposition in {
                    RecoveryDisposition.RESUMABLE,
                    RecoveryDisposition.PAUSED,
                }:
                    existing_run = run.id
                    existing_task = task
                break
        if existing_run is not None and existing_task is not None:
            state = (
                await self.resume(existing_run)
                if existing_task.status is TaskStatus.PAUSED
                else await self.run(existing_run)
            )
            return Selection(task=existing_task), state

        with self.session_factory.begin() as session:
            selection = select_next_task(session, project_id)
            if selection.task is None:
                return selection, None
            if PauseRequestRepository(session).in_force_for_task(
                project_id, selection.task.id
            ):
                return selection, None
            run = create_run(session, selection.task.id)
        return selection, await self.run(run.id)

    async def run_task(self, task_id: UUID) -> WorkflowState:
        """Create a run for one READY task and execute it through the graph."""
        with self.session_factory.begin() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise EntityNotFound("Task", task_id)
            if PauseRequestRepository(session).in_force_for_task(
                task.project_id, task.id
            ):
                raise EntityConflict(f"Task {task.external_task_id} is paused")
            run = create_run(session, task.id)
        return await self.run(run.id)

    async def run(self, run_id: UUID) -> WorkflowState:
        """Start or recover a specific run using its durable thread id."""
        with self.session_factory() as session:
            run, task, project = load_run_context(session, run_id)
            state: WorkflowState = {
                "project_id": str(project.id),
                "task_id": str(task.id),
                "run_id": str(run.id),
                "phase": WorkflowPhase.LOADING.value,
            }
        config = {"configurable": {"thread_id": str(run_id)}}
        return await self.graph.ainvoke(state, config=config)

    async def resume(self, run_id: UUID) -> WorkflowState:
        """Release task-scoped pauses and resume at a safe boundary."""
        with self.session_factory.begin() as session:
            run, task, project = load_run_context(session, run_id)
            PauseRequestRepository(session).release(
                project_id=project.id, task_id=task.id
            )
            if task.status is TaskStatus.PAUSED:
                target = (
                    TaskStatus.CHANGES_REQUESTED
                    if run.review_cycle or run.attempt_number > 1
                    else TaskStatus.READY
                )
                TaskRepository(session).transition(task.id, target)
        return await self.run(run_id)

    async def recover_incomplete(self) -> dict[UUID, WorkflowState | str]:
        """Resume safe runs and report the ones requiring an operator."""
        with self.session_factory() as session:
            candidates = inspect_incomplete_runs(session, settings=self.settings)
        recovered: dict[UUID, WorkflowState | str] = {}
        for candidate in candidates:
            if candidate.disposition is RecoveryDisposition.RESUMABLE:
                recovered[candidate.run_id] = await self.run(candidate.run_id)
            else:
                recovered[candidate.run_id] = candidate.disposition.value
        return recovered

    def _build_graph(self):
        builder = StateGraph(WorkflowState)
        builder.add_node("load_task", self._load_task)
        builder.add_node("prepare_workspace", self._prepare_workspace)
        builder.add_node("pause", self._pause)
        builder.add_node("execute", self._execute)
        builder.add_node("deliver", self._deliver)
        builder.add_node("release", self._release)
        builder.add_node("terminal", self._terminal)
        builder.add_edge(START, "load_task")
        builder.add_conditional_edges(
            "load_task",
            self._after_load,
            {
                "prepare": "prepare_workspace",
                "pause": "pause",
                "terminal": "terminal",
            },
        )
        builder.add_conditional_edges(
            "prepare_workspace",
            self._after_prepare,
            {"pause": "pause", "execute": "execute", "deliver": "deliver"},
        )
        builder.add_conditional_edges(
            "execute",
            self._after_execute,
            {"deliver": "deliver", "release": "release", "terminal": "terminal"},
        )
        builder.add_edge("deliver", END)
        builder.add_edge("release", END)
        builder.add_edge("pause", END)
        builder.add_edge("terminal", END)
        return builder.compile(checkpointer=self.checkpointer)

    def _load_task(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            run, task, project = load_run_context(session, UUID(state["run_id"]))
            pause = PauseRequestRepository(session).in_force_for_task(
                project.id, task.id
            )
            deadline = run_deadline(run.started_at or datetime.now().astimezone(), task.limits)
            return {
                "phase": WorkflowPhase.LOADING.value,
                "task_status": task.status.value,
                "deadline": deadline.isoformat(),
                "pause_request_id": str(pause.id) if pause else "",
            }

    def _after_load(self, state: WorkflowState) -> str:
        status = TaskStatus(state["task_status"])
        if state.get("pause_request_id") or status is TaskStatus.PAUSED:
            return "pause"
        if status in {TaskStatus.COMPLETE, TaskStatus.HUMAN_REVIEW, TaskStatus.FAILED}:
            return "terminal"
        return "prepare"

    def _prepare_workspace(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            run, task, project = load_run_context(session, UUID(state["run_id"]))
            if run.branch_name:
                attach_workspace(session, run.id, settings=self.settings)
            else:
                prepare_workspace(session, run.id, settings=self.settings)
            if run.status is RunStatus.PENDING:
                TaskRunRepository(session).update_fields(run.id, status=RunStatus.RUNNING)
            pause = PauseRequestRepository(session).in_force_for_task(project.id, task.id)
            return {
                "phase": WorkflowPhase.PREPARING_WORKSPACE.value,
                "task_status": task.status.value,
                "pause_request_id": str(pause.id) if pause else "",
            }

    def _after_prepare(self, state: WorkflowState) -> str:
        if state.get("pause_request_id"):
            return "pause"
        return "deliver" if state["task_status"] == TaskStatus.APPROVED.value else "execute"

    def _pause(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            _, task, _ = load_run_context(session, UUID(state["run_id"]))
            if state.get("pause_request_id"):
                PauseRequestRepository(session).mark_honoured(
                    UUID(state["pause_request_id"])
                )
            resume_status = (
                TaskStatus.CHANGES_REQUESTED
                if task.status not in {TaskStatus.READY, TaskStatus.PENDING}
                else task.status
            )
            if task.status is not TaskStatus.PAUSED:
                TaskRepository(session).transition(task.id, TaskStatus.PAUSED)
        return {
            "outcome": WorkflowOutcome.PAUSED.value,
            "resume_status": resume_status.value,
            "task_status": TaskStatus.PAUSED.value,
        }

    async def _execute(self, state: WorkflowState) -> dict[str, object]:
        session = self.session_factory()
        try:
            run, task, _ = load_run_context(session, UUID(state["run_id"]))
            if task.status is TaskStatus.APPROVED:
                return {"loop_outcome": LoopOutcome.APPROVED.value}
            if task.status is TaskStatus.HUMAN_REVIEW:
                return {"loop_outcome": LoopOutcome.ESCALATED.value}
            if task.status is TaskStatus.FAILED:
                return {"loop_outcome": LoopOutcome.FAILED.value}
            workspace = attach_workspace(session, run.id, settings=self.settings)
            result = await run_fix_loop(
                session,
                workspace,
                coder=self.coder,
                reviewer=self.reviewer,
                settings=self.settings,
                initial_feedback=self._human_feedback(session, task.id, run.started_at),
                deadline=datetime.fromisoformat(state["deadline"]),
                checkpoint_turn=session.commit,
            )
            session.commit()
            return {
                "phase": WorkflowPhase.EXECUTING.value,
                "loop_outcome": result.outcome.value,
                "task_status": result.task_status.value,
                "escalation_id": str(result.escalation.id) if result.escalation else "",
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _after_execute(self, state: WorkflowState) -> str:
        outcome = LoopOutcome(state["loop_outcome"])
        if outcome is LoopOutcome.APPROVED:
            return "deliver"
        if outcome is LoopOutcome.FAILED:
            return "release"
        return "terminal"

    def _deliver(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            workspace = attach_workspace(
                session, UUID(state["run_id"]), settings=self.settings
            )
            delivered = deliver_candidate(session, workspace, settings=self.settings)
            integration = delivered.integration
            blocked = integration is not None and not integration.advanced
            state_update: dict[str, object] = {
                "phase": WorkflowPhase.DONE.value,
                "outcome": WorkflowOutcome.COMPLETED.value,
                "task_status": TaskStatus.COMPLETE.value,
                "commit_sha": delivered.commit_sha,
                "worktree_released": delivered.released,
                "integration_blocked": blocked,
            }
            if blocked and integration.escalation_id is not None:
                state_update["escalation_id"] = str(integration.escalation_id)
            return state_update

    def _release(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            released = release_for_run(
                session, UUID(state["run_id"]), settings=self.settings
            )
        return {
            "phase": WorkflowPhase.DONE.value,
            "outcome": WorkflowOutcome.FAILED.value,
            "worktree_released": released,
        }

    def _terminal(self, state: WorkflowState) -> dict[str, object]:
        status = TaskStatus(state["task_status"])
        if status is TaskStatus.COMPLETE:
            outcome = WorkflowOutcome.COMPLETED
        elif (
            status is TaskStatus.HUMAN_REVIEW
            or state.get("loop_outcome") == LoopOutcome.ESCALATED
        ):
            outcome = WorkflowOutcome.ESCALATED
        elif status is TaskStatus.PAUSED:
            outcome = WorkflowOutcome.PAUSED
        else:
            outcome = WorkflowOutcome.FAILED
        return {"phase": WorkflowPhase.DONE.value, "outcome": outcome.value}

    @staticmethod
    def _human_feedback(
        session: Session, task_id: UUID, run_started_at: datetime | None
    ) -> str | None:
        escalations = EscalationRepository(session).list_for_task(
            task_id, status=EscalationStatus.RESOLVED
        )
        for escalation in reversed(escalations):
            if escalation.resolution_intent is not EscalationIntent.REQUEST_CHANGES:
                continue
            if (
                run_started_at
                and escalation.resolved_at
                and escalation.resolved_at > run_started_at
            ):
                continue
            return escalation.resolution
        return None


__all__ = ["WorkflowRunner", "WorkflowState"]
