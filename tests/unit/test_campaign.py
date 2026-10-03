from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import TaskRow
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    FailureReason,
    ProjectStatus,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.models import PauseRequest, Project, Task
from apps.orchestrator.repositories import (
    PauseRequestRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.campaign import CampaignRunner, CampaignStatus
from apps.orchestrator.services.runs import create_run


def _settings() -> Settings:
    """Isolated from the developer's ``.env``.

    ``inspect_incomplete_runs`` resolves worktree paths from settings, so a real
    ``WORKTREE_ROOT`` would let a leftover directory decide a disposition.
    """
    return Settings(_env_file=None, _env_file_encoding=None)


@pytest.fixture
def campaign_factory(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'campaign.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def _project(factory) -> UUID:
    with factory.begin() as session:
        return ProjectRepository(session).add(
            Project(name="campaign", repository_path="/tmp/campaign")
        ).id


def _task(factory, project_id: UUID, external_id: str, *, section: int, depends=()):
    with factory.begin() as session:
        return TaskRepository(session).add(
            Task(
                project_id=project_id,
                external_task_id=external_id,
                title=external_id,
                section=section,
                depends_on=list(depends),
            )
        )


def _set_task_status(factory, task_id: UUID, status: TaskStatus) -> None:
    with factory.begin() as session:
        session.execute(update(TaskRow).where(TaskRow.id == task_id).values(status=status))


def _integrated_executor(factory, order: list[str], *, commit_prefix: str = "commit"):
    async def execute(run_id: UUID) -> dict[str, object]:
        with factory.begin() as session:
            run = TaskRunRepository(session).get(run_id)
            assert run is not None
            task = TaskRepository(session).get(run.task_id)
            assert task is not None
            order.append(task.external_task_id)
            TaskRunRepository(session).finish(run_id, RunStatus.SUCCEEDED)
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task.id)
                .values(status=TaskStatus.COMPLETE, unintegrated_commit=None)
            )
        return {
            "run_id": str(run_id),
            "outcome": "COMPLETED",
            "commit_sha": f"{commit_prefix}-{len(order)}",
        }

    return execute


@pytest.mark.asyncio
async def test_three_independent_tasks_execute_without_manual_selection(campaign_factory):
    project_id = _project(campaign_factory)
    for section, external_id in enumerate(("T-003", "T-001", "T-002"), start=1):
        _task(campaign_factory, project_id, external_id, section=section)
    order: list[str] = []

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        transition_limit=10,
    ).advance(project_id)

    assert report.status is CampaignStatus.COMPLETE
    assert order == ["T-003", "T-001", "T-002"]
    assert report.tasks_completed_this_invocation == ("T-003", "T-001", "T-002")
    assert len(report.run_ids) == 3


@pytest.mark.asyncio
async def test_deterministic_order_uses_section_then_external_id(campaign_factory):
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "B", section=2)
    _task(campaign_factory, project_id, "A", section=2)
    _task(campaign_factory, project_id, "C", section=1)
    order: list[str] = []

    await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        transition_limit=10,
    ).advance(project_id)

    assert order == ["C", "A", "B"]


@pytest.mark.asyncio
async def test_dependency_chain_executes_in_dependency_order(campaign_factory):
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=3)
    _task(campaign_factory, project_id, "B", section=2, depends=("A",))
    _task(campaign_factory, project_id, "C", section=1, depends=("B",))
    order: list[str] = []

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        transition_limit=10,
    ).advance(project_id)

    assert report.status is CampaignStatus.COMPLETE
    assert order == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_dependent_task_waits_for_successful_integration(campaign_factory):
    project_id = _project(campaign_factory)
    task_a = _task(campaign_factory, project_id, "A", section=1)
    _task(campaign_factory, project_id, "B", section=2, depends=("A",))
    _set_task_status(campaign_factory, task_a.id, TaskStatus.COMPLETE)
    with campaign_factory.begin() as session:
        TaskRepository(session).record_integration(
            task_a.id, unintegrated_commit="abc123"
        )

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
    ).advance(project_id)

    assert report.status is CampaignStatus.BLOCKED
    assert report.run_ids == ()
    assert "A" in report.tasks_blocked


@pytest.mark.asyncio
async def test_already_completed_tasks_are_not_rerun_and_resume_continues(campaign_factory):
    project_id = _project(campaign_factory)
    done = _task(campaign_factory, project_id, "A", section=1)
    _task(campaign_factory, project_id, "B", section=2)
    _set_task_status(campaign_factory, done.id, TaskStatus.COMPLETE)
    order: list[str] = []

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
    ).advance(project_id)

    assert report.status is CampaignStatus.COMPLETE
    assert order == ["B"]
    assert report.tasks_already_completed == ("A", "B")


@pytest.mark.asyncio
async def test_state_is_reloaded_between_task_transitions(campaign_factory):
    project_id = _project(campaign_factory)
    task_a = _task(campaign_factory, project_id, "A", section=1)
    _task(campaign_factory, project_id, "B", section=2, depends=("A",))
    order: list[str] = []

    async def execute(run_id: UUID) -> dict[str, object]:
        with campaign_factory.begin() as session:
            run = TaskRunRepository(session).get(run_id)
            assert run is not None
            task = TaskRepository(session).get(run.task_id)
            assert task is not None
            order.append(task.external_task_id)
            TaskRunRepository(session).finish(run_id, RunStatus.SUCCEEDED)
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task.id)
                .values(status=TaskStatus.COMPLETE)
            )
            if task.id == task_a.id:
                TaskRepository(session).record_integration(
                    task.id, unintegrated_commit="needs-human-merge"
                )
        return {"run_id": str(run_id), "outcome": "COMPLETED"}

    report = await CampaignRunner(campaign_factory, task_executor=execute).advance(project_id)

    assert order == ["A"]
    assert report.status is CampaignStatus.BLOCKED
    assert "B" in report.tasks_blocked


@pytest.mark.asyncio
async def test_active_in_flight_task_is_not_duplicated(campaign_factory):
    project_id = _project(campaign_factory)
    task = _task(campaign_factory, project_id, "A", section=1)
    _set_task_status(campaign_factory, task.id, TaskStatus.CODING)

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
    ).advance(project_id)

    assert report.status is CampaignStatus.BLOCKED
    assert report.run_ids == ()


@pytest.mark.asyncio
async def test_orphan_pending_run_is_resumed_and_never_duplicated(campaign_factory):
    """Requirement 8. An interrupted campaign is resumed, not stranded.

    The durable residue of a crash between ``create_run`` and the workflow is a
    ``PENDING`` run at generation 0 with no owner, and the existing restart
    reconciliation calls exactly that ``RESUMABLE``. The invariant worth
    pinning is that advancement produces no *second* run row -- not that it
    refuses, which would strand recoverable work no operator knows about.
    """
    project_id = _project(campaign_factory)
    task = _task(campaign_factory, project_id, "A", section=1)
    with campaign_factory.begin() as session:
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        first_run = create_run(session, task.id)
    order: list[str] = []

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        settings=_settings(),
    ).advance(project_id)

    assert report.status is CampaignStatus.COMPLETE
    assert order == ["A"]
    assert report.run_ids == (first_run.id,)
    assert report.resumed_run_ids == (first_run.id,)
    with campaign_factory() as session:
        runs = TaskRunRepository(session).list_for_task(task.id)
    assert [run.id for run in runs] == [first_run.id], "a second run row was created"


@pytest.mark.asyncio
async def test_owned_in_flight_run_is_fail_closed_not_resumed(campaign_factory):
    """Requirement 8, the other half. An owner stamp is not campaign's to clear.

    ``execution_owner`` is set either by a dispatch that is genuinely running or
    by one that was killed holding it, and the durable record cannot tell those
    apart -- which is why ``override_active_owner`` exists and why it is an
    operator decision with an audit event. Campaign advancement stops.
    """
    project_id = _project(campaign_factory)
    task = _task(campaign_factory, project_id, "A", section=1)
    with campaign_factory.begin() as session:
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        run = create_run(session, task.id)
        acquired = TaskRunRepository(session).acquire_execution(run.id, owner="dead-dispatch")
        assert acquired is not None

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
        settings=_settings(),
    ).advance(project_id)

    assert report.status is CampaignStatus.BLOCKED
    assert report.run_ids == ()
    assert "dead-dispatch" in report.stop_reason
    assert "override_active_owner" in report.stop_reason


@pytest.mark.asyncio
async def test_in_flight_run_under_pause_blocks_without_resuming(campaign_factory):
    """Requirement 15. A pause in force outranks resumability."""
    project_id = _project(campaign_factory)
    task = _task(campaign_factory, project_id, "A", section=1)
    with campaign_factory.begin() as session:
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        create_run(session, task.id)
        PauseRequestRepository(session).add(
            PauseRequest(project_id=project_id, task_id=task.id, requested_by="operator")
        )

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
        settings=_settings(),
    ).advance(project_id)

    assert report.status is CampaignStatus.BLOCKED
    assert report.run_ids == ()
    assert "pause" in report.stop_reason


@pytest.mark.asyncio
async def test_in_flight_run_awaiting_human_escalates_rather_than_blocks(campaign_factory):
    """Requirement 12. An in-flight run on an escalated task needs a person."""
    project_id = _project(campaign_factory)
    task = _task(campaign_factory, project_id, "A", section=1)
    with campaign_factory.begin() as session:
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        create_run(session, task.id)
    _set_task_status(campaign_factory, task.id, TaskStatus.HUMAN_REVIEW)

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
        settings=_settings(),
    ).advance(project_id)

    assert report.status is CampaignStatus.HUMAN_ACTION_REQUIRED
    assert report.run_ids == ()


@pytest.mark.asyncio
async def test_in_flight_run_of_another_project_does_not_block_this_one(campaign_factory):
    """Requirement 17. Campaigns are per project, and so is this decision."""
    other_project = _project(campaign_factory)
    other_task = _task(campaign_factory, other_project, "X", section=1)
    with campaign_factory.begin() as session:
        TaskRepository(session).transition(other_task.id, TaskStatus.READY)
        create_run(session, other_task.id)

    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=1)
    order: list[str] = []

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        settings=_settings(),
    ).advance(project_id)

    assert report.status is CampaignStatus.COMPLETE
    assert order == ["A"]


@pytest.mark.asyncio
async def test_human_review_and_retry_exhaustion_stop_campaign(campaign_factory):
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=1)

    async def human_review(run_id: UUID) -> dict[str, object]:
        with campaign_factory.begin() as session:
            run = TaskRunRepository(session).get(run_id)
            assert run is not None
            TaskRunRepository(session).finish(
                run_id, RunStatus.FAILED, FailureReason.RETRY_EXHAUSTED.value
            )
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == run.task_id)
                .values(status=TaskStatus.HUMAN_REVIEW)
            )
        return {"run_id": str(run_id), "outcome": "ESCALATED"}

    report = await CampaignRunner(campaign_factory, task_executor=human_review).advance(project_id)

    assert report.status is CampaignStatus.HUMAN_ACTION_REQUIRED
    assert report.tasks_escalated == ("A",)


@pytest.mark.asyncio
async def test_pending_tasks_with_unsatisfied_dependencies_are_blocked(campaign_factory):
    project_id = _project(campaign_factory)
    task_a = _task(campaign_factory, project_id, "A", section=1)
    _task(campaign_factory, project_id, "B", section=2, depends=("A",))
    _set_task_status(campaign_factory, task_a.id, TaskStatus.PAUSED)

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
    ).advance(project_id)

    assert report.status is CampaignStatus.BLOCKED
    assert report.run_ids == ()


@pytest.mark.asyncio
async def test_transition_bound_returns_continuation_and_next_call_completes(campaign_factory):
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=1)
    _task(campaign_factory, project_id, "B", section=2)
    order: list[str] = []

    first = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        transition_limit=1,
    ).advance(project_id)
    second = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, order),
        transition_limit=10,
    ).advance(project_id)

    assert first.status is CampaignStatus.CONTINUATION_REQUIRED
    assert first.autonomous_advance_possible is True
    assert second.status is CampaignStatus.COMPLETE
    assert order == ["A", "B"]


@pytest.mark.asyncio
async def test_failed_task_stops_without_campaign_owning_fix_logic(campaign_factory):
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=1)

    async def exhausted(run_id: UUID) -> dict[str, object]:
        with campaign_factory.begin() as session:
            run = TaskRunRepository(session).get(run_id)
            assert run is not None
            TaskRunRepository(session).finish(
                run_id, RunStatus.FAILED, FailureReason.RETRY_EXHAUSTED.value
            )
            session.execute(
                update(TaskRow).where(TaskRow.id == run.task_id).values(status=TaskStatus.FAILED)
            )
        return {
            "run_id": str(run_id),
            "outcome": "ESCALATED",
            "failure_reason": FailureReason.RETRY_EXHAUSTED.value,
        }

    report = await CampaignRunner(campaign_factory, task_executor=exhausted).advance(project_id)

    assert report.status is CampaignStatus.HUMAN_ACTION_REQUIRED
    assert report.tasks_escalated == ("A",)


@pytest.mark.asyncio
async def test_report_lists_completed_escalated_and_blocked_tasks(campaign_factory):
    project_id = _project(campaign_factory)
    completed = _task(campaign_factory, project_id, "A", section=1)
    escalated = _task(campaign_factory, project_id, "B", section=2)
    blocked = _task(campaign_factory, project_id, "C", section=3, depends=("B",))
    _set_task_status(campaign_factory, completed.id, TaskStatus.COMPLETE)
    _set_task_status(campaign_factory, escalated.id, TaskStatus.HUMAN_REVIEW)
    _set_task_status(campaign_factory, blocked.id, TaskStatus.BLOCKED)

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
    ).advance(project_id)

    assert report.status is CampaignStatus.HUMAN_ACTION_REQUIRED
    assert report.tasks_already_completed == ("A",)
    assert report.tasks_escalated == ("B",)
    assert report.tasks_blocked == ("C",)


@pytest.mark.asyncio
async def test_paused_project_is_blocked_without_creating_a_run(campaign_factory):
    """Requirement 16. Project runnability is the scheduler's rule, not a new one."""
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=1)
    with campaign_factory.begin() as session:
        ProjectRepository(session).set_status(project_id, ProjectStatus.PAUSED)

    report = await CampaignRunner(
        campaign_factory,
        task_executor=_integrated_executor(campaign_factory, []),
        settings=_settings(),
    ).advance(project_id)

    assert report.status is CampaignStatus.BLOCKED
    assert report.run_ids == ()
    assert "runnable" in report.stop_reason


@pytest.mark.asyncio
async def test_executor_failure_stops_the_campaign_as_failed(campaign_factory):
    """Requirement 14. An executor that raises is not a reason to keep going.

    The run was already created and committed, so the campaign cannot know what
    became of it; continuing would select the next task while an unknown run is
    outstanding. It stops, and the second task is never started.
    """
    project_id = _project(campaign_factory)
    _task(campaign_factory, project_id, "A", section=1)
    _task(campaign_factory, project_id, "B", section=2)

    async def exploding(run_id: UUID) -> dict[str, object]:
        raise RuntimeError("the worker host went away")

    report = await CampaignRunner(
        campaign_factory, task_executor=exploding, settings=_settings()
    ).advance(project_id)

    assert report.status is CampaignStatus.FAILED
    assert len(report.run_ids) == 1
    assert report.transitions_used == 1
    with campaign_factory() as session:
        tasks = {
            task.external_task_id: task
            for task in TaskRepository(session).list_for_project(project_id)
        }
        runs = TaskRunRepository(session)
        assert runs.list_for_task(tasks["B"].id) == [], "B was started after a failure"


def test_a_transition_limit_below_one_is_refused(campaign_factory):
    """Requirement 10's bound must actually bound something."""
    with pytest.raises(ValueError, match="transition_limit"):
        CampaignRunner(
            campaign_factory,
            task_executor=_integrated_executor(campaign_factory, []),
            transition_limit=0,
        )
