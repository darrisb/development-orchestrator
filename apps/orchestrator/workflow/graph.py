"""The Phase K LangGraph: coordination around existing deterministic services.

Nodes contain no coding, review, Git or state-machine policy. They open one
short transaction, call the service that owns the operation, and return small
serializable facts used only for conditional edges.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import NotRequired, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from ..agents.fix_loop import LoopOutcome, durable_checkpoint, run_fix_loop
from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..db.session import rollback_preserving_original
from ..domain.enums import (
    IN_FLIGHT_RUN_STATUSES,
    Complexity,
    EscalationStatus,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from ..domain.errors import AbandonedRunError, RunOwnershipLostError
from ..domain.escalation import EscalationIntent
from ..domain.model_policy import ModelPolicy
from ..domain.models import Project, RunEvent, Task
from ..domain.state_machine import can_transition
from ..domain.workflow import WorkflowOutcome, WorkflowPhase
from ..providers import (
    ModelProvider,
    build_provider,
    build_review_provider,
)
from ..providers.review import ReviewProvider
from ..repositories import (
    EscalationRepository,
    PauseRequestRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from ..services.delivery import deliver_candidate
from ..services.errors import EntityConflict, EntityNotFound
from ..services.model_providers import resolve_roles
from ..services.runs import create_run
from ..services.runtime import begin_active_runtime, end_active_runtime
from ..services.scheduler import Selection, select_next_task
from ..services.workspace import attach_workspace, load_run_context, prepare_workspace
from ..services.worktrees import release_for_run
from .checkpoints import SqlAlchemyCheckpointSaver
from .recovery import RecoveryDisposition, inspect_incomplete_runs

logger = get_logger(__name__)

#: The machine code written to ``task_runs.failure_reason`` when a run is
#: closed because the request that started it was cancelled (concern 71).
#:
#: A code rather than prose, like every other value on that column, so a
#: reader can group cancellations without matching on English. The prose
#: reason belongs in the event, which is written next to it in the same
#: transaction.
CANCELLATION_FAILURE_REASON = "WORKFLOW_CANCELLED"


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
    #: integration baseline (concern 51).
    #: Reported rather than turned into a failed outcome: the task really is
    #: complete, and what is blocked is everything that depends on it.
    integration_blocked: NotRequired[bool]
    pause_request_id: NotRequired[str]
    resume_status: NotRequired[str]
    error: NotRequired[str]
    #: Concern 64: set when the run was abandoned by an operator. The workflow
    #: stops rather than proceeding with any consequential transition.
    #:
    #: This flag is an optimization, not the guarantee. It is computed from a
    #: status read in one node and acted on in the next, so an operator's
    #: transaction can commit in the gap. The guarantees that do not have that
    #: gap are the compare-and-swap in ``TaskRunRepository.finish`` (which
    #: refuses to write any status to an abandoned run) and the fence in
    #: ``services.delivery._deliver`` (which refuses before the commit). These
    #: checks exist to avoid starting work that is already pointless, and they
    #: are checked again where it counts.
    run_abandoned: NotRequired[bool]
    #: Concern 67: the execution generation this dispatch acquired. Carried in
    #: the graph state rather than in a module variable because it has to
    #: survive the same thing everything else here survives -- the process --
    #: and because a node that persists anything has to be able to quote it
    #: back to the database. Absent when the dispatch holds no ownership,
    #: which happens only for a run that was already terminal when it was
    #: dispatched and will walk straight to the terminal node.
    execution_generation: NotRequired[int]


@dataclass(frozen=True, slots=True)
class _TaskProviders:
    """The providers one task's roles run on, and who owns their transports.

    ``owned`` is the whole point: a routed task gets instances built for it
    and closes them when it is done, while an unrouted one borrows the
    runner's and must not close anything -- the runner's providers outlive
    every task and are released by ``WorkflowRunner.aclose``.
    """

    coder: ModelProvider
    planner: ModelProvider
    reviewer: ReviewProvider
    owned: bool

    async def aclose(self) -> None:
        if not self.owned:
            return
        await self.coder.aclose()
        # A planner that fell back to the coder *is* the coder: closing it
        # again would close the same transport twice.
        if self.planner is not self.coder:
            await self.planner.aclose()
        await self.reviewer.aclose()


class WorkflowRunner:
    """Compile and invoke the one-task-at-a-time V1 graph."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        coder: ModelProvider,
        planner: ModelProvider | None = None,
        reviewer: ReviewProvider,
        settings: Settings | None = None,
        checkpointer: SqlAlchemyCheckpointSaver | None = None,
        route_by_project_policy: bool = False,
        ) -> None:
        self.session_factory = session_factory
        self.coder = coder
        self.planner = planner or coder
        self._planner_is_coder = planner is None
        self.reviewer = reviewer
        #: Whether a task's roles are re-resolved from its project's model
        #: policy (section 31). True for a runner built by ``configured``,
        #: which resolved the providers above itself and can resolve others
        #: the same way. False when they were handed in: a caller that chose
        #: the providers explicitly gets the providers it chose, and the
        #: runner has no configuration from which to build a substitute.
        self._routes_by_project_policy = route_by_project_policy
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
        """Build the runner from the configured coder and reviewer roles.

        These are the installation's providers, resolved with no project
        policy: the runner serves many projects and is built before it knows
        which task it will run. A project that declared a policy has its
        task's roles re-resolved in ``_execute``.
        """
        config = settings or get_settings()
        with session_factory() as session:
            selection = resolve_roles(
                session, ModelPolicy(), Complexity.MEDIUM, settings=config
            )
        return cls(
            session_factory,
            coder=build_provider(selection.coder),
            planner=None
            if selection.planner is None
            else build_provider(selection.planner),
            reviewer=build_review_provider(selection.reviewer, settings=config),
            settings=config,
            route_by_project_policy=True,
        )

    async def aclose(self) -> None:
        """Release provider transports owned by this runner."""
        await self.coder.aclose()
        if not self._planner_is_coder:
            await self.planner.aclose()
        await self.reviewer.aclose()

    async def _task_providers(
        self, session: Session, task: Task, project: Project
    ) -> _TaskProviders:
        """The providers this task's roles run on (section 31).

        The runner's own providers unless this runner resolves its own *and*
        the project declared a policy. A project with no policy therefore
        behaves exactly as it did before policies existed, down to using the
        same provider instances and opening no additional transport.

        Raises:
            ProviderNotConfigured: the policy named a model that is not
                registered and enabled for the role it was named for. Nothing
                is built before resolution finishes, so a failure here leaks
                nothing.
        """
        policy = project.model_policy
        if not self._routes_by_project_policy or policy.is_empty:
            return _TaskProviders(self.coder, self.planner, self.reviewer, owned=False)
        selection = resolve_roles(session, policy, task.complexity, settings=self.settings)
        built: list[ModelProvider] = []
        try:
            coder = build_provider(selection.coder)
            built.append(coder)
            # No planner registered means planning runs on *this task's*
            # coder, not on whichever coder the runner was built with.
            planner = coder
            if selection.planner is not None:
                planner = build_provider(selection.planner)
                built.append(planner)
            reviewer = build_review_provider(selection.reviewer, settings=self.settings)
        except BaseException:
            # A later role failing to build must not strand the transports the
            # earlier ones already opened.
            for provider in built:
                await provider.aclose()
            raise
        return _TaskProviders(coder, planner, reviewer, owned=True)

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

    async def run(
        self, run_id: UUID, *, acquired: tuple[str, int] | None = None
    ) -> WorkflowState:
        """Start or recover a specific run using its durable thread id.

        Dispatch is where execution ownership is taken (concern 67). Acquiring
        it here rather than in each caller is deliberate: ``run_next``,
        ``run_task``, ``resume``, ``recover_incomplete`` and the operator
        recovery endpoint all end up here, and an ownership rule that each of
        them had to remember to apply is an ownership rule that one of them
        would eventually not.

        The acquisition increments the run's ``execution_generation``, and the
        new number is carried in the graph state and quoted back to the
        database at every durable checkpoint. So a second dispatch of the same
        run does not merely fail to start -- if it somehow did start, the older
        one could no longer persist.

        Args:
            acquired: ``(owner, generation)`` when the caller already acquired
                ownership in its own transaction. The operator recovery path
                does that, because for it the acquisition *is* the operation
                and it has to be committed with the audit event, not separately
                afterwards.

        Raises:
            EntityConflict: another dispatch holds this run.
        """
        owner: str | None
        if acquired is not None:
            owner, generation = acquired
            # Ownership belongs to the caller's transaction; releasing it is
            # still this dispatch's job, because this is what is executing.
            release = True
        else:
            owner, generation = self._acquire_execution(run_id)
            release = owner is not None
        try:
            with self.session_factory() as session:
                run, task, project = load_run_context(session, run_id)
                state: WorkflowState = {
                    "project_id": str(project.id),
                    "task_id": str(task.id),
                    "run_id": str(run.id),
                    "phase": WorkflowPhase.LOADING.value,
                }
                if generation is not None:
                    state["execution_generation"] = generation
            config = {"configurable": {"thread_id": str(run_id)}}
            return await self.graph.ainvoke(state, config=config)
        finally:
            if release and owner is not None:
                self._release_execution(run_id, owner)

    def _acquire_execution(self, run_id: UUID) -> tuple[str | None, int | None]:
        """Take ownership of an in-flight run, or explain why not.

        A run that is already terminal is dispatched without ownership rather
        than refused: the graph's job for such a run is to walk to its terminal
        node and report, it persists nothing that needs fencing, and refusing
        would change the behaviour of every caller that dispatches a run to
        find out what became of it.
        """
        with self.session_factory.begin() as session:
            runs = TaskRunRepository(session)
            run = runs.get(run_id)
            if run is None:
                raise EntityNotFound("Run", run_id)
            if run.status not in IN_FLIGHT_RUN_STATUSES:
                return None, None
            owner = uuid.uuid4().hex
            acquired = runs.acquire_execution(run_id, owner=owner)
            if acquired is None:
                session.expire_all()
                current = runs.get(run_id)
                if current is not None and current.execution_owner is not None:
                    raise EntityConflict(
                        f"Run {current.external_run_id or current.id} is already "
                        f"held by dispatch {current.execution_owner} since "
                        f"{current.execution_started_at}; a run has one executor"
                    )
                raise EntityConflict(
                    f"Run {run_id} could not be dispatched; it is no longer in "
                    "flight"
                )
            return owner, acquired.execution_generation

    def _release_execution(self, run_id: UUID, owner: str) -> None:
        """Give the run back, if this dispatch still holds it.

        Best effort on purpose. A dispatch that was fenced by a recovery no
        longer matches the owner predicate and clears nothing, and a release
        that fails outright must not replace the workflow's own outcome or
        exception -- the generation, not this stamp, is what keeps the run
        safe.
        """
        try:
            with self.session_factory.begin() as session:
                TaskRunRepository(session).release_execution(run_id, owner=owner)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "execution_ownership_release_failed",
                run_id=str(run_id),
                execution_owner=owner,
            )

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
            # Concern 64: fencing check. If the run was abandoned by an operator,
            # do not proceed. The run is terminal.
            if run.status is RunStatus.ABANDONED:
                return {
                    "phase": WorkflowPhase.LOADING.value,
                    "task_status": task.status.value,
                    "run_abandoned": True,
                }
            pause = PauseRequestRepository(session).in_force_for_task(
                project.id, task.id
            )
            return {
                "phase": WorkflowPhase.LOADING.value,
                "task_status": task.status.value,
                "pause_request_id": str(pause.id) if pause else "",
            }

    def _after_load(self, state: WorkflowState) -> str:
        # Concern 64: if the run was abandoned, go to terminal.
        if state.get("run_abandoned"):
            return "terminal"
        status = TaskStatus(state["task_status"])
        if state.get("pause_request_id") or status is TaskStatus.PAUSED:
            return "pause"
        if status in {TaskStatus.COMPLETE, TaskStatus.HUMAN_REVIEW, TaskStatus.FAILED}:
            return "terminal"
        return "prepare"

    def _prepare_workspace(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            run, task, project = load_run_context(session, UUID(state["run_id"]))
            # Concern 64: fencing check. If the run was abandoned by an operator,
            # do not proceed with workspace preparation.
            if run.status is RunStatus.ABANDONED:
                return {
                    "phase": WorkflowPhase.PREPARING_WORKSPACE.value,
                    "task_status": task.status.value,
                    "run_abandoned": True,
                }
            # Concern 67: this node writes -- it moves PENDING to RUNNING and it
            # creates or attaches a worktree -- so it re-asks the ownership
            # question under the row lock rather than trusting the read above.
            self._require_ownership(session, run.id, state)
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
        # Concern 64: if the run was abandoned, go to terminal.
        if state.get("run_abandoned"):
            return "terminal"
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
            run, task, project = load_run_context(session, UUID(state["run_id"]))
            # Concern 64: fencing check. If the run was abandoned by an operator,
            # do not proceed with execution.
            if run.status is RunStatus.ABANDONED:
                return {
                    "phase": WorkflowPhase.EXECUTING.value,
                    "loop_outcome": LoopOutcome.FAILED.value,
                    "task_status": task.status.value,
                    "run_abandoned": True,
                }
            if task.status is TaskStatus.APPROVED:
                return {"loop_outcome": LoopOutcome.APPROVED.value}
            if task.status is TaskStatus.HUMAN_REVIEW:
                return {"loop_outcome": LoopOutcome.ESCALATED.value}
            if task.status is TaskStatus.FAILED:
                return {"loop_outcome": LoopOutcome.FAILED.value}
            workspace = attach_workspace(session, run.id, settings=self.settings)
            budget = begin_active_runtime(
                session,
                run.id,
                task.limits,
                worker_timeout_seconds=self.settings.worker_timeout_seconds,
            )
            # This boundary must survive a provider failure, transaction
            # rollback, or process death.  All loop work happens after it.
            session.commit()
            # Section 31: which models this task runs on is the project's
            # declaration, read per task because complexity is a task fact.
            # A policy naming a model nobody registered raises here, before
            # any turn: the run fails the way any provider failure does and
            # is never quietly served by a different model.
            providers = await self._task_providers(session, task, project)
            try:
                result = await run_fix_loop(
                    session,
                    workspace,
                    coder=providers.coder,
                    planner=providers.planner,
                    reviewer=providers.reviewer,
                    settings=self.settings,
                    initial_feedback=self._human_feedback(
                        session, task.id, run.started_at
                    ),
                    deadline=budget.runtime_deadline,
                    worker_deadline=budget.worker_deadline,
                    runtime_budget=budget,
                    # Concern 64: the turn's commit is behind the run row's lock,
                    # so an operator's transaction that committed while this call
                    # was in flight stops the turn instead of being overwritten by
                    # it. The fences in the graph above are reads and cannot do
                    # this; see agents.fix_loop.durable_checkpoint.
                    checkpoint_turn=durable_checkpoint(
                        session,
                        run.id,
                        session.commit,
                        # Concern 67: the token this dispatch acquired. Every turn
                        # boundary quotes it back to the database, so an executor
                        # recovered out from under itself cannot commit what it
                        # was doing.
                        expected_generation=state.get("execution_generation"),
                    ),
                )
            finally:
                # Exactly once, however the loop ended: a task that owns its
                # providers must not leave an httpx client behind, and one that
                # borrowed the runner's must not close them.
                await providers.aclose()
            end_active_runtime(
                session,
                run.id,
                task.limits,
                worker_timeout_seconds=self.settings.worker_timeout_seconds,
            )
            session.commit()
            return {
                "phase": WorkflowPhase.EXECUTING.value,
                "loop_outcome": result.outcome.value,
                "task_status": result.task_status.value,
                "escalation_id": str(result.escalation.id) if result.escalation else "",
            }
        except Exception as original:
            rollback_preserving_original(session, original)
            # Runtime cleanup must not depend on the Session which handled the
            # external call.  If PostgreSQL invalidated that connection, a new
            # Session gives recovery metadata an independent transaction.
            try:
                with self.session_factory.begin() as recovery_session:
                    run, task, _ = load_run_context(
                        recovery_session, UUID(state["run_id"])
                    )
                    end_active_runtime(
                        recovery_session,
                        run.id,
                        task.limits,
                        worker_timeout_seconds=self.settings.worker_timeout_seconds,
                    )
            except Exception:
                # Preserve the workflow failure.  Recovery accounting can be
                # reconstructed from the active interval if this independent
                # transaction also fails.
                logger.exception(
                    "workflow_runtime_cleanup_failed",
                    run_id=state["run_id"],
                    original_exception=type(original).__name__,
                )
            raise
        except asyncio.CancelledError as cancellation:
            # Concern 71. CancelledError is a BaseException, so the handler
            # above never sees it: an HTTP client that disconnects while this
            # run waits on a model provider unwinds straight past the workflow
            # and leaves a run RUNNING, a task mid-flight, and nothing left
            # that will ever settle either. The client is gone; the durable
            # records are not.
            #
            # Named rather than caught as BaseException. The settlement below
            # is only correct for a cancellation, and a broad handler would
            # route SystemExit, KeyboardInterrupt and GeneratorExit through it
            # as well: a Ctrl-C would be recorded on the run as
            # WORKFLOW_CANCELLED, which is a statement about a request nobody
            # made. Those keep unwinding untouched, as they did before.
            #
            # The rollback comes first and for the same reason it does in the
            # Exception handler: this session may still hold the transaction
            # that was open when the cancellation arrived, and settlement runs
            # on a second connection. Leaving it open would have the settlement
            # wait on -- or lose the race for -- a lock this very workflow is
            # holding, and a lost race is a settlement that never happened.
            rollback_preserving_original(session, cancellation)
            self._settle_cancelled_run(state)
            raise
        finally:
            session.close()

    def _settle_cancelled_run(self, state: WorkflowState) -> None:
        """Close the run a cancellation interrupted, deterministically.

        Concern 71. The run, its task, the reason and the runtime accounting
        are written together or not at all, so there is no interleaving in which
        the run is terminal and the task is not. The reason is a machine code
        like every other one on the column, and the event carries the same fact
        on the run's own history, exactly once, next to the attempt that was
        interrupted.

        The task is moved only when the state machine permits it. A task that
        is already FAILED -- the loop's own settlement can land first -- has
        nowhere to go, and raising here would roll back the run's settlement
        with it and strand the run for the sake of a bookkeeping move that had
        nothing left to say. A COMPLETE task is left alone for the same reason
        concern 64 leaves it: its work is delivered and in the baseline.

        The write is fenced by the execution generation this dispatch took, so
        an executor that has been superseded settles nothing at all -- not the
        run, not the event, not the runtime interval. Concern 64's abandonment
        and concern 66's fence are the two things that can invalidate a
        cancellation's settlement, and a run that is neither still in flight nor
        still ours to close is not this method's to close.

        The whole method is synchronous on purpose. It is reached by unwinding
        a cancelled task, and an ``await`` anywhere in it would be a place a
        *second* cancellation could be delivered -- which would abandon a
        half-written settlement and leave exactly the strand this closes. With
        no await there is nowhere for a second cancellation to land until the
        transaction has committed.

        Best effort, like the cleanup above it: a settlement that fails must
        not replace the cancellation the caller is owed. It is logged, because
        the alternative is a strand nobody can see.
        """
        try:
            with self.session_factory.begin() as settlement:
                run, task, project = load_run_context(
                    settlement, UUID(state["run_id"])
                )
                if run.status in IN_FLIGHT_RUN_STATUSES:
                    # Concern 64's compare-and-swap still guards this write, so
                    # a run an operator abandoned while the provider was being
                    # waited on stays ABANDONED. That refusal is not a failure
                    # of the settlement: the run is already terminal, and only
                    # the accounting below is left to do.
                    #
                    # The generation rides along on the same statement, for the
                    # reason concern 66 gave the column: a recovery resumes the
                    # *same* run, so an executor that has been superseded is not
                    # writing to a run nobody wants, it is writing over the work
                    # that replaced it. Its cancellation is a true statement
                    # about a request that has gone; it is not a decision about
                    # the successor's run, and settling that FAILED would end
                    # live work and leave its owner holding a terminal row.
                    try:
                        TaskRunRepository(settlement).finish(
                            run.id,
                            RunStatus.FAILED,
                            CANCELLATION_FAILURE_REASON,
                            expected_generation=state.get("execution_generation"),
                        )
                    except AbandonedRunError:
                        logger.info(
                            "workflow_cancellation_found_abandoned_run",
                            run_id=str(run.id),
                        )
                    except RunOwnershipLostError as lost:
                        # Superseded. The successor owns every remaining write
                        # on this run -- the terminal status, the event, and the
                        # runtime interval -- and a stale executor writing to
                        # any of them is the same defect concern 66 fenced.
                        logger.info(
                            "workflow_cancellation_found_superseded_run",
                            run_id=str(run.id),
                            held_generation=lost.held,
                            current_generation=lost.current,
                        )
                        return
                    else:
                        if task.status is not TaskStatus.FAILED and can_transition(
                            task.status, TaskStatus.FAILED
                        ):
                            TaskRepository(settlement).transition(
                                task.id, TaskStatus.FAILED
                            )
                        RunEventRepository(settlement).append(
                            RunEvent(
                                task_run_id=run.id,
                                project_id=project.id,
                                task_id=task.id,
                                event_type=RunEventType.RUN_CANCELLED,
                                attempt=run.attempt_number,
                                payload={
                                    "failure_reason": CANCELLATION_FAILURE_REASON,
                                    "attempt": run.attempt_number,
                                    "task_status": task.status.value,
                                    "execution_generation": (
                                        state.get("execution_generation")
                                    ),
                                },
                            )
                        )
                end_active_runtime(
                    settlement,
                    run.id,
                    task.limits,
                    worker_timeout_seconds=self.settings.worker_timeout_seconds,
                )
        except Exception:
            logger.exception(
                "workflow_cancellation_settlement_failed",
                run_id=state["run_id"],
            )

    def _after_execute(self, state: WorkflowState) -> str:
        # Concern 64: if the run was abandoned, go to terminal.
        if state.get("run_abandoned"):
            return "terminal"
        outcome = LoopOutcome(state["loop_outcome"])
        if outcome is LoopOutcome.APPROVED:
            return "deliver"
        if outcome is LoopOutcome.FAILED:
            return "release"
        return "terminal"

    def _deliver(self, state: WorkflowState) -> dict[str, object]:
        with self.session_factory.begin() as session:
            # Concern 64: do not deliver a candidate for an abandoned run. This
            # is the one place in the graph where a fence and a guarantee
            # overlap, so it is worth being precise about which is which: the
            # check here avoids entering delivery at all, and
            # services.delivery._deliver raises AbandonedRunError if the
            # operator commits after this read but before the commit. Removing
            # this check does not by itself let a candidate land -- the service
            # fence does that -- so the candidate/integration regression test
            # pins the service fence, not this node.
            run, _, _ = load_run_context(session, UUID(state["run_id"]))
            if run.status is RunStatus.ABANDONED:
                return {
                    "phase": WorkflowPhase.DONE.value,
                    "outcome": WorkflowOutcome.FAILED.value,
                    "run_abandoned": True,
                }
            # Concern 67: delivery is the most consequential write a run makes
            # -- a commit, and the integration baseline moving. A dispatch that
            # no longer owns the run may not make it, and the question is asked
            # under the row lock in the same transaction as the delivery.
            self._require_ownership(session, run.id, state)
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

    @staticmethod
    def _require_ownership(
        session: Session, run_id: UUID, state: WorkflowState
    ) -> None:
        """Refuse to write unless this dispatch still owns the run.

        A no-op for a dispatch that holds no generation, which is only ever a
        run that was terminal when it was dispatched and therefore has nothing
        here to protect.
        """
        generation = state.get("execution_generation")
        if generation is None:
            return
        TaskRunRepository(session).require_in_flight(
            run_id, expected_generation=generation
        )

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
        # Concern 64: if the run was abandoned, report it as abandoned.
        if state.get("run_abandoned"):
            return {
                "phase": WorkflowPhase.DONE.value,
                "outcome": WorkflowOutcome.FAILED.value,
            }
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
