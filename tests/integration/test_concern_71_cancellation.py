"""Concern 71: request cancellation must not strand durable workflow state.

An HTTP client that disconnects cancels the coroutine the orchestrator is
running. ``asyncio.CancelledError`` is a ``BaseException``, so it is not an
``Exception``, and a workflow that only handles ``Exception`` unwinds straight
past its own settlement: the run stays ``RUNNING``, the task stays ``CODING``,
and nothing is left that will ever close either. The client is gone; the
durable records are not.

The contract these tests pin is narrow on purpose:

* the run is settled to ``FAILED`` with the machine reason
  ``WORKFLOW_CANCELLED``, and the run's own history carries exactly one
  ``RUN_CANCELLED`` event;
* the task is moved to ``FAILED``;
* execution ownership is released, once;
* the cancellation is then propagated, unchanged;
* and none of it is allowed to make another model call, start another attempt,
  reset the attempt budget, produce a candidate, review anything, or move the
  integration baseline.

Two shapes of test are here, and the distinction matters.

**The handler is named, not broad.** ``SystemExit``, ``KeyboardInterrupt`` and
``GeneratorExit`` are ``BaseException``\\ s too. Catching them broadly would
record a Ctrl-C on a run as ``WORKFLOW_CANCELLED`` -- a statement about a
request that was never made -- and would stand between the interpreter and its
own shutdown. The handler has to be ``except asyncio.CancelledError`` and
nothing else, and a provider that raises each of the other three is the test of
that.

**Synchronous on purpose.** The settlement is reached by unwinding a cancelled
task, so every ``await`` inside it is a place a *second* cancellation could be
delivered. The settlement contains none, which is why a second cancellation
arriving while its transaction is open cannot abandon it half-written. That is
a structural property, and it is tested by delivering one at the statement
where the transaction is open but not yet committed.

Every test here synchronises with an ``asyncio.Event`` or a SQLAlchemy cursor
hook, never with a sleep: the point of each test is a state that exists at one
particular instant, and a sleep would be a guess about when that instant is.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    IN_FLIGHT_RUN_STATUSES,
    Complexity,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.providers import (
    ConnectionReport,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
)
from apps.orchestrator.providers.errors import ModelTimeout
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.abandon import abandon_run
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import load_run_context
from apps.orchestrator.workflow import WorkflowRunner
from apps.orchestrator.workflow.graph import CANCELLATION_FAILURE_REASON
from apps.orchestrator.workflow.recovery import (
    RecoveryDisposition,
    inspect_incomplete_runs,
)
from tests.conftest import run_git
from tests.integration.test_fix_loop import (
    BROKEN,
    STUB,
    ScriptedModel,
    _code,
    _review,
    reviewer,
)

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ doubles


class GatedModel:
    """A coder that announces it is waiting, and then waits.

    ``generate`` sets ``entered`` the moment it is called and then suspends
    forever. A test that awaits ``entered`` knows the provider call is in
    flight, with no sleep and no polling: the event is set by the code under
    test, on the line where the wait begins.

    ``preceded`` lets the earlier turns of a run be scripted normally, so a
    test can be about what happens *after* a recorded failure rather than
    about the failure itself.
    """

    def __init__(self, *preceded: str | Exception) -> None:
        self.entered = asyncio.Event()
        self.calls = 0
        self.inner = ScriptedModel(*preceded)
        self.config = ProviderConfig(
            provider_id="gated-coder",
            base_url="http://stub/v1",
            model_name="gated-coder-test",
            role=ModelRole.CODER,
            context_window=32768,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.inner.answers:
            return await self.inner.generate(request)
        self.entered.set()
        await asyncio.Event().wait()  # left only by cancellation, never released
        raise AssertionError("unreachable: the gate is only left by cancellation")

    async def check_connection(self) -> ConnectionReport:
        return ConnectionReport(provider_id=self.config.provider_id, reachable=True)

    async def aclose(self) -> None:
        pass


class RaisingModel(ScriptedModel):
    """A coder that raises one chosen exception and nothing else."""

    def __init__(self, raised: BaseException) -> None:
        super().__init__()
        self.raised = raised

    async def generate(self, request: ModelRequest) -> ModelResponse:  # noqa: ARG002
        raise self.raised


class RecordingFactory(sessionmaker):  # type: ignore[type-arg]
    """A sessionmaker that remembers every Session it hands out.

    The concern 66 invariant -- no database transaction open across a model
    call -- is a statement about *the workflow's own* session, so the test
    needs a handle on it. Nothing in the production code is instrumented to
    provide one.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sessions: list[Session] = []

    def __call__(self, **kwargs: Any) -> Session:  # type: ignore[override]
        session = super().__call__(**kwargs)
        self.sessions.append(session)
        return session

    def workflow_session(self) -> Session:
        """The session the running workflow is using right now."""
        live = [session for session in self.sessions if session.is_active]
        assert live, "the workflow has not opened a session"
        return live[-1]


@dataclass
class World:
    """One task, one run, one repository, and the runner under test."""

    engine: Engine
    factory: RecordingFactory
    settings: Settings
    repository: Path
    task_id: UUID
    run_id: UUID
    runner: WorkflowRunner

    def read(self) -> dict[str, Any]:
        """Everything the invariants are stated about, in one read."""
        with self.factory() as session:
            run, task, _ = load_run_context(session, self.run_id)
            cancellations = [
                stored
                for stored in RunEventRepository(session).list_for_run(self.run_id)
                if stored.event_type == RunEventType.RUN_CANCELLED
            ]
            return {
                "run": run,
                "task": task,
                "cancellations": cancellations,
                "model_calls": ModelRunRepository(session).list_for_run(self.run_id),
                "reviews": ReviewRepository(session).list_for_run(self.run_id),
                "integrated": [
                    stored
                    for stored in RunEventRepository(session).list_for_run(self.run_id)
                    if stored.event_type
                    in {RunEventType.INTEGRATION_ADVANCED, RunEventType.COMMIT_CREATED}
                ],
                "recoverable": [
                    candidate
                    for candidate in inspect_incomplete_runs(
                        session, settings=self.settings
                    )
                    if candidate.run_id == self.run_id
                ],
            }

    def integration_sha(self) -> str | None:
        """The cumulative baseline, or ``None`` before one branch exists.

        ``agent/integration`` is created by workspace preparation, so it does
        not exist for a run that was never dispatched. What matters is that a
        run which was dispatched leaves it exactly where preparation found it.
        """
        try:
            return run_git(
                self.repository, "rev-parse", "--verify", "agent/integration"
            ).strip()
        except subprocess.CalledProcessError:
            return None


def _world(
    tmp_path: Path,
    coder: Any,
    *,
    limits: TaskLimits | None = None,
) -> World:
    """A project whose verification command really runs and can really fail."""
    repository = tmp_path / "project"
    (repository / "src").mkdir(parents=True)
    (repository / "tools").mkdir()
    (repository / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (repository / "tools" / "test.py").write_text(
        "assert 'return target' in open('src/nav.py').read()\n", encoding="utf-8"
    )
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")

    engine = create_db_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
    Base.metadata.create_all(engine)
    factory = RecordingFactory(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="fixture",
                repository_path=str(repository),
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=("python3 tools/test.py",)),
            )
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-004",
                title="implement navigation",
                complexity=Complexity.LOW,
                limits=limits or TaskLimits(max_attempts=3),
                files_to_modify=["src/nav.py"],
            )
        )
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        run = create_run(session, task.id)

    runner = WorkflowRunner(
        factory,
        coder=coder,
        reviewer=reviewer(_review(taskId="TS-004")),
        settings=settings,
    )
    return World(
        engine=engine,
        factory=factory,
        settings=settings,
        repository=repository,
        task_id=task.id,
        run_id=run.id,
        runner=runner,
    )


# --------------------------------------------------------------------- A


@pytest.mark.asyncio
async def test_cancellation_during_the_provider_wait_settles_the_run(tmp_path: Path):
    """A: the client disappears while the coder is being waited on.

    This is the shape RUN-000008 had, without RUN-000008's 600 seconds: the
    run is in flight, the task is CODING, a provider call is outstanding, and
    the request that started it is cancelled.
    """
    coder = GatedModel()
    world = _world(tmp_path, coder)
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        # The provider set this event; the wait below it is the wait under test.
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        # Checked here, while the workflow is still parked on the provider:
        # concern 66's invariant is about the transaction that is open at the
        # boundary, and after the run has unwound the session is closed either
        # way, so a later check could not tell a rollback from a close.
        assert not world.factory.workflow_session().in_transaction(), (
            "the provider boundary was crossed inside a database transaction"
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = world.read()
        run, stored_task = state["run"], state["task"]

        # Terminal, once, with the machine reason, and the history agrees.
        assert run.status is RunStatus.FAILED
        assert run.failure_reason == CANCELLATION_FAILURE_REASON
        assert run.completed_at is not None
        assert len(state["cancellations"]) == 1
        assert state["cancellations"][0].payload["failure_reason"] == (
            CANCELLATION_FAILURE_REASON
        )
        assert stored_task.status is TaskStatus.FAILED

        # Ownership released, once, and no run left in flight without one.
        assert run.execution_owner is None
        assert run.execution_started_at is None
        assert run.active_started_at is None
        assert state["recoverable"] == []

        # And nothing a cancelled run must not do.
        assert run.candidate_commit is None
        assert state["reviews"] == []
        assert not state["integrated"]
        baseline = world.integration_sha()
        assert baseline == run.starting_commit, "the integration baseline moved"
        assert run.attempt_number == 1
        # A cancelled call is not an endpoint that failed, so it is not on
        # the model-call record at all: the run made the call and the answer
        # is the question that was never answered.
        assert coder.calls == 1
        assert state["model_calls"] == []
        assert task.cancelled()
    finally:
        world.engine.dispose()


# --------------------------------------------------------------------- B


@pytest.mark.asyncio
async def test_cancellation_after_a_durable_provider_failure_does_not_bypass_settlement(
    tmp_path: Path,
):
    """B: a recorded failure, and a cancellation before the next attempt.

    Attempt 1's answer parses as JSON and is not a usable change set, so the
    loop records it and routes the attempt to a second one. The cancellation
    lands while the second attempt is waiting on its provider: the run has a
    durable record of a failed attempt and no settlement of its own.
    """
    coder = GatedModel('{"summary": "nothing to apply", "edits": []}')
    world = _world(tmp_path, coder)
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = world.read()
        run, stored_task = state["run"], state["task"]

        assert run.status is RunStatus.FAILED
        assert run.failure_reason == CANCELLATION_FAILURE_REASON
        assert stored_task.status is TaskStatus.FAILED
        assert len(state["cancellations"]) == 1

        # The first attempt is on the record, and the cancelled one is not: a
        # cancellation is not an endpoint that failed, and a FAILED row for it
        # would put a lie into section 35's arithmetic.
        assert [call.attempt for call in state["model_calls"]] == [1]
        assert [call.status for call in state["model_calls"]] == [RunStatus.SUCCEEDED]
        assert coder.calls == 2
        # The attempt number advanced to the attempt being made, and the
        # settlement started no third.
        assert run.attempt_number == 2
        assert state["recoverable"] == []
    finally:
        world.engine.dispose()


# --------------------------------------------------------------------- C


@pytest.mark.asyncio
async def test_cancellation_with_attempts_remaining_does_not_reset_the_budget(
    tmp_path: Path,
):
    """C: two attempts left of three, cancelled during the second."""
    coder = GatedModel(_code(BROKEN))
    world = _world(tmp_path, coder, limits=TaskLimits(max_attempts=3))
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = world.read()
        run = state["run"]
        assert run.status is RunStatus.FAILED
        assert run.failure_reason == CANCELLATION_FAILURE_REASON
        assert state["task"].status is TaskStatus.FAILED
        # One attempt finished and one was in flight; the remaining allowance
        # is not spent, and nothing starts a third.
        assert run.attempt_number == 2
        assert coder.calls == 2
        assert [call.attempt for call in state["model_calls"]] == [1]
        assert state["recoverable"] == []
    finally:
        world.engine.dispose()


# --------------------------------------------------------------------- D


@pytest.mark.asyncio
async def test_cancellation_on_the_final_attempt_settles_the_same_way(tmp_path: Path):
    """D: the same run, with no attempt left after the one being cancelled."""
    coder = GatedModel(_code(BROKEN))
    world = _world(tmp_path, coder, limits=TaskLimits(max_attempts=2))
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = world.read()
        run, stored_task = state["run"], state["task"]
        assert run.status is RunStatus.FAILED
        assert run.failure_reason == CANCELLATION_FAILURE_REASON
        assert stored_task.status is TaskStatus.FAILED
        assert run.attempt_number == 2
        assert coder.calls == 2
        assert [call.attempt for call in state["model_calls"]] == [1]
        assert len(state["cancellations"]) == 1
        # Nothing was escalated and nothing was retried: a cancellation is not
        # a retry, and the run's own reason says which of the two it was.
        assert run.failure_reason != "RETRY_EXHAUSTED"
        assert state["recoverable"] == []
    finally:
        world.engine.dispose()


# --------------------------------------------------------------------- E


@pytest.mark.asyncio
async def test_a_second_cancellation_cannot_interrupt_the_settlement(tmp_path: Path):
    """E: a second cancellation arrives while the settlement transaction is open.

    The settlement is reached by unwinding a cancelled task, so any ``await``
    inside it is a place a second cancellation could land and abandon a
    half-written transaction. There is none: the second cancellation is
    delivered at the next suspension point, which is after the commit, and the
    run is already terminal when it is finally raised.
    """
    coder = GatedModel()
    world = _world(tmp_path, coder)
    fired: list[str] = []
    held: list[bool] = []
    armed = False
    workflow_session: Session | None = None

    def on_settlement_write(
        _conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any
    ) -> None:
        # Fires between the settlement's UPDATE and its COMMIT, with the
        # transaction open: the exact window the property is about. The reason
        # is a bound parameter, so it is in ``parameters`` and not in the SQL.
        if (
            armed
            and "UPDATE task_runs" in statement
            and "failure_reason" in statement
            and CANCELLATION_FAILURE_REASON in str(parameters)
        ):
            fired.append(statement)
            # Whether the interrupted workflow still held a transaction at the
            # moment the settlement reached for the same rows. It must not: on
            # a real server the settlement would be waiting on -- or losing a
            # race for -- a lock this very workflow is holding, and a lost race
            # is a settlement that never happened.
            held.append(bool(workflow_session and workflow_session.in_transaction()))
            task.cancel()  # second cancellation, from inside the open transaction

    event.listen(world.engine, "before_cursor_execute", on_settlement_write)
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        workflow_session = world.factory.workflow_session()
        armed = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(fired) == 1, "the settlement's terminal write was not observed"
        assert held == [False], (
            "the interrupted workflow still held a transaction while the "
            "settlement was writing"
        )
        state = world.read()
        run, stored_task = state["run"], state["task"]
        assert run.status is RunStatus.FAILED
        assert run.failure_reason == CANCELLATION_FAILURE_REASON
        assert run.completed_at is not None
        assert stored_task.status is TaskStatus.FAILED
        assert len(state["cancellations"]) == 1, "the settlement ran twice"
        assert run.execution_owner is None
        assert state["recoverable"] == []
    finally:
        event.remove(world.engine, "before_cursor_execute", on_settlement_write)
        world.engine.dispose()


# --------------------------------------------------------------------- F


@pytest.mark.asyncio
async def test_ownership_is_released_exactly_once_when_cancelled_during_the_release(
    tmp_path: Path,
):
    """F: the cancellation is re-raised while ownership is being handed back.

    The release is the last thing a dispatch does, and it is synchronous for
    the same reason the settlement is: a cancellation delivered into the middle
    of it would leave a run nobody owns, which is the other half of the strand.
    """
    coder = GatedModel()
    world = _world(tmp_path, coder)
    releases: list[str] = []
    armed = False

    def on_release(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        # The release is the *clearing* write, which is the one that has to
        # happen exactly once. Arming matters: the acquisition writes the same
        # column, and a cancellation delivered into that one would be a
        # different thing entirely.
        if armed and "UPDATE task_runs" in statement and "execution_owner" in statement:
            releases.append(statement)
            task.cancel()

    event.listen(world.engine, "before_cursor_execute", on_release)
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        armed = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(releases) == 1, f"ownership was released {len(releases)} times"
        state = world.read()
        run = state["run"]
        assert run.execution_owner is None
        assert run.execution_started_at is None
        # The settlement ran before the release, and both are durable.
        assert run.status is RunStatus.FAILED
        assert run.failure_reason == CANCELLATION_FAILURE_REASON
        assert len(state["cancellations"]) == 1
        assert state["recoverable"] == []
    finally:
        event.remove(world.engine, "before_cursor_execute", on_release)
        world.engine.dispose()


# ------------------------------------------- the handler is named, not broad


@pytest.mark.asyncio
async def test_a_generator_exit_is_not_recorded_as_a_cancellation(tmp_path: Path):
    """``GeneratorExit`` is a ``BaseException`` and is not a cancellation.

    Routing it through the settlement would write ``WORKFLOW_CANCELLED`` onto a
    run for something nobody asked for, and would stand between a closing
    coroutine and the event loop that is closing it.
    """
    world = _world(tmp_path, RaisingModel(GeneratorExit()))
    try:
        unwound: BaseException | None = None
        try:
            await world.runner.run(world.run_id)
        except BaseException as error:  # noqa: BLE001 - that is the assertion
            unwound = error
        assert type(unwound) is GeneratorExit, f"{unwound!r} is not a GeneratorExit"

        state = world.read()
        assert state["cancellations"] == []
        assert state["run"].failure_reason != CANCELLATION_FAILURE_REASON
        assert state["run"].status in IN_FLIGHT_RUN_STATUSES, (
            "a BaseException that is not a cancellation must not be recorded "
            "as a settlement"
        )
    finally:
        world.engine.dispose()


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(KeyboardInterrupt, id="KeyboardInterrupt"),
        pytest.param(SystemExit, id="SystemExit"),
    ],
)
# LangGraph's error callback builds a coroutine while the loop that is being
# torn down by the interrupt is already gone, and the interpreter notices the
# coroutine that never ran. That is the boundary working, not a defect in it.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_an_interrupt_or_exit_is_not_recorded_as_a_cancellation(
    tmp_path: Path, raised: type[BaseException]
):
    """``KeyboardInterrupt`` and ``SystemExit`` unwind through, untouched.

    Same claim as the ``GeneratorExit`` test, and it needs its own process
    boundary: ``asyncio.Task.__step`` re-raises these two out of the event loop
    whatever the coroutine does with them, so a test that ran the workflow in
    this loop could not tell "the handler let it through" from "the loop tore
    the run in half on its way out". A thread with its own loop is the smallest
    boundary that makes the difference visible.
    """
    world = _world(tmp_path, RaisingModel(raised()))
    try:
        unwound = _run_isolated(world.runner.run(world.run_id))
        assert type(unwound) is raised, f"{unwound!r} is not a {raised.__name__}"

        state = world.read()
        assert state["cancellations"] == []
        assert state["run"].failure_reason != CANCELLATION_FAILURE_REASON
        assert state["run"].status in IN_FLIGHT_RUN_STATUSES
    finally:
        world.engine.dispose()


def _run_isolated(awaitable: Any) -> BaseException | None:
    """Run one awaitable on its own loop in a thread; report what escaped.

    The workflow is the only user of its database while this blocks, so a
    second thread is enough: the point is the interpreter's own handling of
    ``KeyboardInterrupt``, not concurrency.
    """
    escaped: list[BaseException] = []

    def target() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(awaitable)
        except BaseException as error:  # noqa: BLE001 - reporting it is the job
            escaped.append(error)
            # One more turn, so the graph's own error reporting finishes on a
            # live loop instead of leaving a coroutine behind for the garbage
            # collector to find after the interpreter has moved on.
            with contextlib.suppress(BaseException):
                loop.run_until_complete(asyncio.sleep(0))
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    thread = threading.Thread(target=target, name="c71-interrupt")
    thread.start()
    thread.join(timeout=120)
    assert not thread.is_alive(), "the isolated run did not finish"
    return escaped[0] if escaped else None


@pytest.mark.asyncio
async def test_a_provider_failure_is_not_a_cancellation(tmp_path: Path):
    """Concern 67's behaviour is unchanged: an endpoint that timed out strands.

    A provider failure is an ``Exception`` and has always unwound through the
    ordinary handler, leaving the run in flight for an operator to recover --
    concern 67, and the test that pins it. The cancellation settlement must
    not quietly take that over: the two conditions need different answers, and
    the difference is that one has a person coming to it and the other does not.
    """
    coder = GatedModel(
        _code(BROKEN),
        ModelTimeout("coder request timed out after 600s", timeout_seconds=600.0),
    )
    world = _world(tmp_path, coder)
    try:
        with pytest.raises(ModelTimeout):
            await world.runner.run(world.run_id)

        state = world.read()
        run, stored_task = state["run"], state["task"]
        assert run.status is RunStatus.RUNNING
        assert run.failure_reason is None
        assert state["cancellations"] == []
        assert stored_task.status is TaskStatus.CODING
        assert run.execution_owner is None
        assert [call.status for call in state["model_calls"]] == [
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
        ]
        assert state["recoverable"] and (
            state["recoverable"][0].disposition is RecoveryDisposition.RESUMABLE
        )
    finally:
        world.engine.dispose()


# ------------------------------------------------ the settlement's own edges


def test_a_task_the_state_machine_will_not_move_does_not_strand_the_run(
    tmp_path: Path,
):
    """A task already ``FAILED`` cannot move, and that must not undo the run.

    ``TaskRepository.transition`` raises for a move the state machine refuses,
    and a raise inside the settlement transaction rolls the run's settlement
    back with it. So the task move is conditional, and the run is still
    terminal afterwards. Reached through the settlement itself rather than
    through a workflow, because the graph never dispatches a ``FAILED`` task to
    ``execute``: stating the property needs the settlement on its own.
    """
    world = _world(tmp_path, GatedModel())
    try:
        with world.factory.begin() as session:
            TaskRepository(session).transition(world.task_id, TaskStatus.FAILED)
        world.runner._settle_cancelled_run({"run_id": str(world.run_id)})
        state = world.read()
        assert state["run"].status is RunStatus.FAILED
        assert state["run"].failure_reason == CANCELLATION_FAILURE_REASON
        assert state["task"].status is TaskStatus.FAILED
        assert len(state["cancellations"]) == 1
    finally:
        world.engine.dispose()


@pytest.mark.asyncio
async def test_a_cancellation_never_overwrites_an_operator_abandonment(tmp_path: Path):
    """Concern 64 still wins: an abandoned run stays abandoned.

    The settlement re-reads the run and writes only while it is in flight, and
    the write itself is concern 64's compare-and-swap. An operator's decision,
    committed while the provider was being waited on, is not something a
    disconnected client gets to overwrite.
    """
    coder = GatedModel()
    world = _world(tmp_path, coder)
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)
        with world.factory.begin() as session:
            abandon_run(session, world.run_id, reason="operator stopped this")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = world.read()
        run = state["run"]
        assert run.status is RunStatus.ABANDONED
        assert run.failure_reason == "OPERATOR_ABANDONED"
        assert state["cancellations"] == []
        assert state["task"].status is TaskStatus.FAILED
        assert run.execution_owner is None
    finally:
        world.engine.dispose()


# ------------------------------- a cancellation from a superseded executor


@pytest.mark.asyncio
async def test_a_superseded_dispatch_cannot_settle_a_recovered_run(tmp_path: Path):
    """Concern 66's fence, on the settlement: a stale executor settles nothing.

    A recovery resumes the *same* run, so a dispatch that was superseded is
    not writing to a run nobody else wants -- it is writing over the work that
    replaced it. Its cancellation is a real signal about a request that is
    already gone; it is not a decision about the run's successor, which now has
    a live owner. Settling that run FAILED from under its owner would end work
    in progress and leave the owner holding a terminal run.

    The settlement therefore quotes the generation it was dispatched at and lets
    the database decide, the way concern 64's abandonment and concern 66's
    acquisition already do. Taken over here through ``acquire_execution``,
    which is the call ``recover_run`` itself makes, so the generation, the owner
    stamp and the event are the production ones.
    """
    coder = GatedModel()
    world = _world(tmp_path, coder)
    try:
        task = asyncio.create_task(world.runner.run(world.run_id))
        await asyncio.wait_for(coder.entered.wait(), timeout=60)

        with world.factory.begin() as session:
            superseded = TaskRunRepository(session).acquire_execution(
                world.run_id,
                owner="recovery-owner",
                expected_generation=1,
                # ``recover_run``'s override_active_owner=True: the run is held
                # by a dispatch that is no longer answering, which is the whole
                # reason an operator is allowed to take it.
                require_unowned=False,
            )
        assert superseded is not None, "the test never actually superseded anything"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        state = world.read()
        run = state["run"]
        assert run.status is RunStatus.RUNNING, (
            "a superseded dispatch settled the run its successor was working on"
        )
        assert run.execution_owner == "recovery-owner", "the successor lost its run"
        assert run.execution_generation == 2
        assert run.failure_reason is None
        assert state["cancellations"] == [], "a stale executor wrote to the history"
        assert state["task"].status is TaskStatus.CODING
        assert state["recoverable"], "a run with a live owner is not recoverable"
    finally:
        world.engine.dispose()

