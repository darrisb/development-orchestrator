"""Stage 4 campaign autonomy against the real durable machinery.

Three things these cover that the unit tests structurally cannot:

* **The real workflow boundary.** ``CampaignRunner`` is handed the actual
  ``WorkflowRunner`` rather than a fake that writes ``COMPLETE`` itself, so the
  claim "campaign drives the existing task graph" is proven by the graph
  running -- code edits, verification, review, integration and worktree release
  all included -- and not by a stub standing where it would be. The providers
  are scripted; no model is called.

* **The real lock.** ``with_for_update()`` is a no-op on SQLite, so the
  concurrency claim is made against PostgreSQL with two real connections and a
  ``threading.Barrier``, following the concern 67 race tests. It is skipped,
  not passed, when no server is reachable.

* **Restart semantics.** What is actually durable after a crash between
  ``create_run`` and the workflow, and what the existing recovery
  classification says may be done with it.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import Complexity, TaskStatus, WorkerProfile
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.campaign import CampaignRunner, CampaignStatus
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.workflow import WorkflowRunner
from apps.orchestrator.workflow.recovery import (
    RecoveryDisposition,
    inspect_incomplete_runs,
)
from tests.conftest import run_git
from tests.db_safety import assert_test_database_safe, drop_test_database
from tests.integration.test_fix_loop import ScriptedModel, _review, reviewer

pytestmark = pytest.mark.integration


# --- the fixture project ------------------------------------------------------

#: Tolerant on purpose. Baseline certification runs this against the tree as it
#: is *before* a task's edit, so a check that demanded the post-edit content
#: would fail the baseline and the test would be about certification instead.
_TEST = """\
import os

for path, needle in (("src/nav.py", "return target"), ("src/greet.py", "hello")):
    if os.path.exists(path):
        source = open(path).read()
        if "TODO" not in source:
            assert needle in source, path
"""

_STUB_NAV = "def navigate(target):\n    pass  # TODO: TS-001\n"
_NAV = "def navigate(target):\n    return target\n"
_GREET = "def greet(name):\n    return f'hello {name}'\n"


def _edit(source: str, *, path: str, operation: str, summary: str) -> str:
    """One edit, wrapped the way a reasoning model wraps one."""
    payload = {
        "summary": summary,
        "edits": [{"path": path, "operation": operation, "content": source}],
        "requirementsMet": [summary],
        "testsAdded": [],
        "followUps": [],
        "deviationsFromPlan": [],
    }
    return "<think>Reading the task.</think>\n```json\n" + json.dumps(payload) + "\n```"


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "project"
    (repository / "src").mkdir(parents=True)
    (repository / "tools").mkdir()
    (repository / "src" / "nav.py").write_text(_STUB_NAV, encoding="utf-8")
    (repository / "tools" / "test.py").write_text(_TEST, encoding="utf-8")
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")
    return repository


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=60,
    )


def _seed(factory, repository: Path, *, dependent: bool) -> tuple[uuid.UUID, ...]:
    """One project, two tasks. ``dependent`` makes TS-002 wait on TS-001."""
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="fixture",
                repository_path=str(repository),
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=("python3 tools/test.py",)),
            )
        )
        first = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-001",
                title="implement navigation",
                section=1,
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
            )
        )
        second = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-002",
                title="implement greeting",
                section=2,
                complexity=Complexity.LOW,
                files_to_modify=["src/greet.py"],
                depends_on=["TS-001"] if dependent else [],
            )
        )
        return project.id, first.id, second.id


@dataclass
class Fixture:
    engine: Engine
    factory: sessionmaker
    settings: Settings
    repository: Path
    project_id: uuid.UUID
    first_id: uuid.UUID
    second_id: uuid.UUID


def _fixture(tmp_path: Path, *, dependent: bool = True) -> Fixture:
    repository = _repository(tmp_path)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'campaign.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    project_id, first_id, second_id = _seed(factory, repository, dependent=dependent)
    return Fixture(
        engine=engine,
        factory=factory,
        settings=_settings(tmp_path),
        repository=repository,
        project_id=project_id,
        first_id=first_id,
        second_id=second_id,
    )


def _runner(fixture: Fixture, *coder_answers: str, reviews: tuple[str, ...]) -> WorkflowRunner:
    return WorkflowRunner(
        fixture.factory,
        coder=ScriptedModel(*coder_answers),
        reviewer=reviewer(*reviews),
        settings=fixture.settings,
    )


def _campaign(
    fixture: Fixture, workflow: WorkflowRunner, *, transition_limit: int = 10
) -> CampaignRunner:
    """The campaign driving the *existing* workflow, with nothing in between.

    ``task_executor`` is ``WorkflowRunner.run`` and only that: the campaign owns
    selection and the stopping decision, and owns no part of code, fix,
    verification, review or integration.
    """

    async def execute(run_id: uuid.UUID) -> dict[str, object]:
        return dict(await workflow.run(run_id))

    return CampaignRunner(
        fixture.factory,
        task_executor=execute,
        transition_limit=transition_limit,
        settings=fixture.settings,
    )


def _status(fixture: Fixture, task_id: uuid.UUID) -> TaskStatus:
    with fixture.factory() as session:
        task = TaskRepository(session).get(task_id)
    assert task is not None
    return task.status


# --- 4. the real workflow boundary -------------------------------------------


@pytest.mark.asyncio
async def test_campaign_drives_two_real_workflows_to_completion(tmp_path: Path):
    """Requirements 1, 3, 4, 7, 19. The whole Stage 4 claim, end to end.

    Campaign selects TS-001, creates its run, hands it to the real graph, and
    the graph takes it to ``COMPLETE`` with its commit in the integration
    baseline. Campaign then re-reads durable state, sees TS-002's dependency
    satisfied -- which it was not when the campaign started -- selects it, runs
    the same graph, and reports ``COMPLETE``.
    """
    fixture = _fixture(tmp_path, dependent=True)
    workflow = _runner(
        fixture,
        _edit(_NAV, path="src/nav.py", operation="update", summary="navigate returns target"),
        _edit(_GREET, path="src/greet.py", operation="create", summary="greet returns a greeting"),
        reviews=(_review(taskId="TS-001"), _review(taskId="TS-002")),
    )
    try:
        report = await _campaign(fixture, workflow).advance(fixture.project_id)
    finally:
        await workflow.aclose()

    assert report.status is CampaignStatus.COMPLETE, report.stop_reason
    # Both tasks ran, in dependency order, each exactly once.
    assert report.tasks_completed_this_invocation == ("TS-001", "TS-002")
    assert len(report.run_ids) == 2
    assert report.resumed_run_ids == ()
    assert report.transitions_used == 2

    assert _status(fixture, fixture.first_id) is TaskStatus.COMPLETE
    assert _status(fixture, fixture.second_id) is TaskStatus.COMPLETE
    with fixture.factory() as session:
        tasks = TaskRepository(session).list_for_project(fixture.project_id)
        runs = TaskRunRepository(session)
        per_task = {task.external_task_id: runs.list_for_task(task.id) for task in tasks}
    # The durable integrated state, not the campaign's own bookkeeping.
    assert all(task.is_integrated for task in tasks)
    assert all(task.unintegrated_commit is None for task in tasks)
    assert [len(value) for value in per_task.values()] == [1, 1], per_task
    assert all(run.candidate_commit for value in per_task.values() for run in value)
    # Both tasks' work is in the integration baseline's committed tree, which
    # is what "integrated" has to mean. Read from the ref rather than the
    # working copy: integration advances the branch from the supervisor's own
    # worktree and never touches the source checkout's index.
    assert "hello" in run_git(
        fixture.repository, "show", f"{INTEGRATION_BRANCH}:src/greet.py"
    )
    assert "return target" in run_git(
        fixture.repository, "show", f"{INTEGRATION_BRANCH}:src/nav.py"
    )
    fixture.engine.dispose()


# --- 5. transition bound and resume ------------------------------------------


@pytest.mark.asyncio
async def test_transition_bound_resumes_without_rerunning_completed_tasks(tmp_path: Path):
    """Requirements 10, 11. The bound is a pause, not a restart.

    ``transition_limit=1`` with two eligible tasks, through the real workflow.
    The second advance is a *separate* ``CampaignRunner`` over the same
    database, so it has no in-memory memory of the first -- everything it does
    not rerun, it declines to rerun because of what is durably recorded.
    """
    fixture = _fixture(tmp_path, dependent=False)
    first_workflow = _runner(
        fixture,
        _edit(_NAV, path="src/nav.py", operation="update", summary="navigate returns target"),
        reviews=(_review(taskId="TS-001"),),
    )
    try:
        first = await _campaign(fixture, first_workflow, transition_limit=1).advance(
            fixture.project_id
        )
    finally:
        await first_workflow.aclose()

    assert first.status is CampaignStatus.CONTINUATION_REQUIRED, first.stop_reason
    assert first.autonomous_advance_possible is True
    assert first.tasks_completed_this_invocation == ("TS-001",)
    assert _status(fixture, fixture.first_id) is TaskStatus.COMPLETE
    assert _status(fixture, fixture.second_id) is not TaskStatus.COMPLETE

    second_workflow = _runner(
        fixture,
        _edit(_GREET, path="src/greet.py", operation="create", summary="greet returns a greeting"),
        reviews=(_review(taskId="TS-002"),),
    )
    try:
        second = await _campaign(fixture, second_workflow, transition_limit=10).advance(
            fixture.project_id
        )
    finally:
        await second_workflow.aclose()

    assert second.status is CampaignStatus.COMPLETE, second.stop_reason
    # The completed task was not rerun: it is reported as already complete and
    # is absent from what this invocation did.
    assert second.tasks_completed_this_invocation == ("TS-002",)
    assert "TS-001" in second.tasks_already_completed
    # The scripted coder was given exactly one answer, so a rerun of TS-001
    # would have raised rather than quietly re-asked a model.
    with fixture.factory() as session:
        tasks = TaskRepository(session).list_for_project(fixture.project_id)
        runs = TaskRunRepository(session)
        counts = {task.external_task_id: len(runs.list_for_task(task.id)) for task in tasks}
    assert counts == {"TS-001": 1, "TS-002": 1}
    fixture.engine.dispose()


# --- 6. failure and human stop -----------------------------------------------


@pytest.mark.asyncio
async def test_escalated_task_stops_campaign_and_holds_back_its_dependent(tmp_path: Path):
    """Requirements 12, 13, 20. The real workflow escalates; campaign stops.

    The reviewer rejects until the review-cycle budget is spent, so the stop is
    produced by the existing retry policy rather than by the campaign deciding
    to stop. TS-002 depends on TS-001, so the test also proves the dependent
    task is not started on an unintegrated dependency.
    """
    fixture = _fixture(tmp_path, dependent=True)
    rejection = _review(
        taskId="TS-001",
        decision="CHANGES_REQUESTED",
        issues=[
            {
                "severity": "HIGH",
                "category": "requirement",
                "file": "src/nav.py",
                "line": 2,
                "requirementId": "TS-001-R1",
                "problem": "A null target is returned instead of being rejected.",
                "requiredFix": "Raise when target is None.",
            }
        ],
    )
    edit = _edit(_NAV, path="src/nav.py", operation="update", summary="navigate returns target")
    workflow = _runner(fixture, *([edit] * 6), reviews=tuple([rejection] * 6))
    try:
        report = await _campaign(fixture, workflow).advance(fixture.project_id)
    finally:
        await workflow.aclose()

    assert report.status is CampaignStatus.HUMAN_ACTION_REQUIRED, report.stop_reason
    assert report.tasks_escalated == ("TS-001",)
    assert report.tasks_completed_this_invocation == ()
    # The dependent task was never started: no run row at all, and its status
    # was never promoted.
    assert _status(fixture, fixture.second_id) is not TaskStatus.COMPLETE
    with fixture.factory() as session:
        second_runs = TaskRunRepository(session).list_for_task(fixture.second_id)
        first = TaskRepository(session).get(fixture.first_id)
    assert second_runs == []
    # TS-001 is escalated, so its dependency is unsatisfied by the one rule the
    # scheduler uses: COMPLETE *and* integrated. ``is_integrated`` alone is
    # vacuously true here -- there is no accepted output to be missing.
    assert first is not None and first.status is not TaskStatus.COMPLETE
    assert first.status in {TaskStatus.HUMAN_REVIEW, TaskStatus.FAILED}
    # The integration baseline exists -- certification creates it at the
    # imported branch -- but nothing was integrated onto it, so it is still the
    # commit the fixture started from.
    assert run_git(
        fixture.repository, "rev-parse", INTEGRATION_BRANCH
    ).strip() == run_git(fixture.repository, "rev-parse", "main").strip()
    fixture.engine.dispose()


# --- 3. in-flight and restart semantics --------------------------------------


@pytest.mark.asyncio
async def test_crash_between_create_run_and_workflow_is_resumed_not_stranded(tmp_path: Path):
    """Requirement 8, against the real graph.

    The durable residue of that crash is asserted first, so what follows is
    known to be about the real restart shape: a ``PENDING`` run, generation 0,
    no owner, no worktree, and the task already promoted to ``READY``. The
    existing restart reconciliation calls it ``RESUMABLE``; the campaign
    resumes it through the same dispatch a fresh start would use, and no second
    run is created.
    """
    fixture = _fixture(tmp_path, dependent=True)
    # Exactly what CampaignRunner._select_or_resume commits before dispatching.
    with fixture.factory.begin() as session:
        from apps.orchestrator.services.scheduler import select_next_task

        ProjectRepository(session).lock(fixture.project_id)
        selection = select_next_task(session, fixture.project_id)
        assert selection.task is not None
        stranded = create_run(session, selection.task.id).id

    with fixture.factory() as session:
        run = TaskRunRepository(session).get(stranded)
        task = TaskRepository(session).get(fixture.first_id)
        dispositions = {
            candidate.run_id: candidate.disposition
            for candidate in inspect_incomplete_runs(session, settings=fixture.settings)
        }
    assert run is not None and task is not None
    assert run.status.value == "PENDING"
    assert run.execution_generation == 0
    assert run.execution_owner is None
    assert run.branch_name is None
    assert task.status is TaskStatus.READY
    assert dispositions == {stranded: RecoveryDisposition.RESUMABLE}

    workflow = _runner(
        fixture,
        _edit(_NAV, path="src/nav.py", operation="update", summary="navigate returns target"),
        _edit(_GREET, path="src/greet.py", operation="create", summary="greet returns a greeting"),
        reviews=(_review(taskId="TS-001"), _review(taskId="TS-002")),
    )
    try:
        report = await _campaign(fixture, workflow).advance(fixture.project_id)
    finally:
        await workflow.aclose()

    assert report.status is CampaignStatus.COMPLETE, report.stop_reason
    assert report.resumed_run_ids == (stranded,)
    assert report.run_ids[0] == stranded
    with fixture.factory() as session:
        first_runs = TaskRunRepository(session).list_for_task(fixture.first_id)
        resumed = TaskRunRepository(session).get(stranded)
    assert [item.id for item in first_runs] == [stranded], "the stranded run was duplicated"
    # The resume took ownership through the normal fence, so the dead executor
    # could no longer have persisted anything.
    assert resumed is not None and resumed.execution_generation > 0
    fixture.engine.dispose()


# --- 2. concurrency, against the database that can answer it ------------------

_DEFAULT_SERVER = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/postgres"


def _server_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if configured.startswith("postgresql"):
        return configured.rsplit("/", 1)[0] + "/postgres"
    return os.environ.get("TEST_POSTGRES_SERVER_URL", _DEFAULT_SERVER)


@contextlib.contextmanager
def _scratch_postgres() -> Iterator[str]:
    """A scratch database on the configured server, dropped afterwards."""
    server = _server_url()
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:  # pragma: no cover - no server available
        admin.dispose()
        pytest.skip(f"no PostgreSQL server for the campaign race test at {server}: {exc}")
    name = f"test_campaign_{uuid.uuid4().hex[:12]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.rsplit("/", 1)[0] + f"/{name}"
    finally:
        drop_test_database(server, name)
        admin.dispose()


@pytest.fixture
def pg_factory(tmp_path: Path) -> Iterator[sessionmaker]:
    with _scratch_postgres() as url:
        assert_test_database_safe(url)
        engine = create_db_engine(url)
        Base.metadata.create_all(engine)
        try:
            yield sessionmaker(bind=engine, expire_on_commit=False)
        finally:
            engine.dispose()


def _race(worker) -> list:
    """Two threads, one barrier, no sleeps.

    The barrier is what makes this deterministic rather than probabilistic:
    both threads are inside their own transaction and about to take the project
    lock before either is released, so the contention is real and does not
    depend on how the scheduler happened to interleave them.
    """
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def run(index: int) -> None:
        try:
            results[index] = worker(barrier, index)
        except Exception as error:  # captured, then asserted on
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a racing thread hung"
    return results


def _seed_bare(factory, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A project with one pending task. No repository: nothing here dispatches."""
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(name=name, repository_path=f"/nonexistent/{name}")
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-001",
                title="only task",
                section=1,
            )
        )
        return project.id, task.id


def _selection_worker(factory, project_id: uuid.UUID):
    """``CampaignRunner._select_or_resume``'s transaction, under a barrier.

    The real transaction boundary, the real ``SELECT ... FOR UPDATE`` on the
    project row, the real ``select_next_task`` predicate and the real
    ``create_run`` -- the step under test, with nothing stubbed. The campaign
    loop around it is not needed to answer the question and would require a
    worktree to dispatch into.
    """
    from apps.orchestrator.services.campaign import CampaignRunner

    def worker(barrier: threading.Barrier, index: int):
        runner = CampaignRunner(
            factory, task_executor=_unreachable, settings=Settings(_env_file=None)
        )
        barrier.wait(timeout=30)
        started = runner._select_or_resume(project_id)
        return (started.run_id, started.resumed, started.stop)

    return worker


async def _unreachable(run_id: uuid.UUID) -> dict[str, object]:  # pragma: no cover
    raise AssertionError("the race test must not dispatch a workflow")


@pytest.mark.integration
def test_two_concurrent_advances_create_exactly_one_run(pg_factory: sessionmaker):
    """Requirement 18, against the lock that actually holds.

    Both campaigns want the same single eligible task. The project row lock
    serializes them, and the loser's ``select_next_task`` -- re-evaluated after
    the wait, inside its own transaction -- sees the winner's committed run and
    refuses to start a second one. What the loser does with it is a separate
    question; what this pins is that there is one run.
    """
    project_id, task_id = _seed_bare(pg_factory, "racing")

    results = _race(_selection_worker(pg_factory, project_id))

    assert not any(isinstance(result, Exception) for result in results), results
    created = [result for result in results if result[0] is not None and not result[1]]
    assert len(created) == 1, f"both advances started independent work: {results}"
    with pg_factory() as session:
        runs = TaskRunRepository(session).list_for_task(task_id)
    assert len(runs) == 1, f"{len(runs)} runs were created for one selected task"
    # The loser did not fabricate a second run; it either resumed the winner's
    # one run or stopped. Either way the run identity is the same.
    loser = next(result for result in results if result is not created[0])
    assert loser[0] in (None, runs[0].id)


@pytest.mark.integration
def test_different_projects_do_not_share_a_campaign_lock(pg_factory: sessionmaker):
    """Requirement 17. The lock identity is the project row, so it cannot be global.

    Both threads hold their own project's lock simultaneously -- the barrier is
    released only once both have taken it -- and both proceed. A global campaign
    lock would deadlock this at the barrier and the join would time out.
    """
    first_project, first_task = _seed_bare(pg_factory, "project-one")
    second_project, second_task = _seed_bare(pg_factory, "project-two")
    projects = (first_project, second_project)

    def worker(barrier: threading.Barrier, index: int):
        with pg_factory.begin() as session:
            ProjectRepository(session).lock(projects[index])
            # Both locks are now held at once, or this never returns.
            barrier.wait(timeout=30)
            from apps.orchestrator.services.scheduler import select_next_task

            selection = select_next_task(session, projects[index])
            assert selection.task is not None
            return create_run(session, selection.task.id).id

    results = _race(worker)

    assert not any(isinstance(result, Exception) for result in results), results
    with pg_factory() as session:
        runs = TaskRunRepository(session)
        assert len(runs.list_for_task(first_task)) == 1
        assert len(runs.list_for_task(second_task)) == 1


@pytest.mark.integration
def test_the_project_lock_is_released_before_the_workflow_is_dispatched(
    pg_factory: sessionmaker,
):
    """The one ordering rule campaign has to obey, pinned against real locks.

    Delivery's transaction takes the **run** lock (``require_in_flight``) and
    then the **project** lock (``integrate_candidate``), in that order. Campaign
    takes the project lock first. Those two orders are only compatible because
    campaign's selection transaction *commits* before the workflow is
    dispatched -- a campaign that held the project lock across
    ``task_executor`` would be holding exactly what its own delivery is about
    to wait for, and every task would deadlock until ``lock_timeout``.

    So the executor here tries to take the project lock on its own connection
    with a short ``lock_timeout``. Getting it is the proof: nothing was holding
    it. The bound is what makes a regression a fast failure instead of a hang.
    """
    project_id, task_id = _seed_bare(pg_factory, "ordering")
    acquired: list[object] = []

    async def execute(run_id: uuid.UUID) -> dict[str, object]:
        # A separate session, as the workflow's own transactions are.
        with pg_factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            try:
                session.execute(
                    text("SELECT id FROM projects WHERE id = :id FOR UPDATE"),
                    {"id": project_id},
                )
                acquired.append(True)
            except Exception as error:  # the regression this exists for
                acquired.append(error)
        return {"run_id": str(run_id), "outcome": "COMPLETED"}

    runner = CampaignRunner(
        pg_factory,
        task_executor=execute,
        transition_limit=1,
        settings=Settings(_env_file=None),
    )
    import asyncio

    asyncio.run(runner.advance(project_id))

    assert acquired == [True], (
        "the campaign still held the project row lock while the workflow ran; "
        f"delivery would deadlock behind it: {acquired}"
    )
    with pg_factory() as session:
        assert len(TaskRunRepository(session).list_for_task(task_id)) == 1
