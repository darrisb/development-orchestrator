"""Concern 60: cumulative active runtime is durable and excludes idle lifetime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.repositories import ProjectRepository, TaskRepository, TaskRunRepository
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.runtime import begin_active_runtime, end_active_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_run(session: Session):
    project = ProjectRepository(session).add(
        Project(name="runtime", repository_path="/tmp/runtime")
    )
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="RT-1",
            title="runtime",
            limits=TaskLimits(max_runtime_minutes=1),
        )
    )
    task = TaskRepository(session).transition(task.id, TaskStatus.READY)
    return task, create_run(session, task.id)


def test_delay_before_first_execution_does_not_spend_runtime(session, runtime_run):
    task, run = runtime_run
    created = datetime(2026, 1, 1, tzinfo=UTC)
    active = created + timedelta(hours=3)
    TaskRunRepository(session).update_fields(run.id, started_at=created)

    budget = begin_active_runtime(
        session, run.id, task.limits, worker_timeout_seconds=300, now=active
    )

    assert budget.consumed_ms == 0
    assert budget.remaining_ms == 60_000
    assert budget.runtime_deadline == active + timedelta(minutes=1)


def test_workspace_failure_and_inactive_pause_leave_budget_untouched(session, runtime_run):
    task, run = runtime_run
    # No active boundary is opened during workspace preparation or a pause.
    TaskRunRepository(session).update_fields(
        run.id, started_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    recovered = datetime(2026, 1, 2, tzinfo=UTC)

    budget = begin_active_runtime(
        session, run.id, task.limits, worker_timeout_seconds=300, now=recovered
    )

    assert budget.remaining_ms == 60_000


def test_active_time_is_accumulated_and_same_run_gets_only_the_remainder(
    session, runtime_run
):
    task, run = runtime_run
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    begin_active_runtime(session, run.id, task.limits, worker_timeout_seconds=300, now=start)
    consumed = end_active_runtime(
        session,
        run.id,
        task.limits,
        worker_timeout_seconds=300,
        now=start + timedelta(seconds=25),
    )
    resumed = begin_active_runtime(
        session,
        run.id,
        task.limits,
        worker_timeout_seconds=300,
        now=start + timedelta(hours=1),
    )

    assert consumed == 25_000
    assert resumed.consumed_ms == 25_000
    assert resumed.remaining_ms == 35_000


def test_crash_recovery_charges_open_work_but_not_unbounded_inactive_time(
    session, runtime_run
):
    task, run = runtime_run
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    begin_active_runtime(session, run.id, task.limits, worker_timeout_seconds=30, now=start)

    recovered = begin_active_runtime(
        session,
        run.id,
        task.limits,
        worker_timeout_seconds=30,
        now=start + timedelta(hours=1),
    )

    assert recovered.consumed_ms == 30_000
    assert recovered.remaining_ms == 30_000


def test_repeated_restart_cannot_refresh_the_same_run(session, runtime_run):
    task, run = runtime_run
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    begin_active_runtime(session, run.id, task.limits, worker_timeout_seconds=20, now=moment)
    for seconds in (20, 40, 60):
        budget = begin_active_runtime(
            session,
            run.id,
            task.limits,
            worker_timeout_seconds=20,
            now=moment + timedelta(seconds=seconds),
        )

    assert budget.consumed_ms == 60_000
    assert budget.remaining_ms == 0
    assert budget.active_started_at is None


def test_retry_task_new_run_has_a_fresh_budget(session, runtime_run):
    task, first = runtime_run
    TaskRunRepository(session).update_fields(first.id, active_runtime_ms=60_000)
    second = create_run(session, task.id)

    budget = begin_active_runtime(
        session,
        second.id,
        task.limits,
        worker_timeout_seconds=300,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert second.id != first.id
    assert budget.consumed_ms == 0
    assert budget.remaining_ms == 60_000


def test_worker_timeout_is_per_invocation_not_the_cumulative_run_budget(
    session, runtime_run
):
    task, run = runtime_run
    TaskRunRepository(session).update_fields(run.id, active_runtime_ms=10_000)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    budget = begin_active_runtime(
        session, run.id, task.limits, worker_timeout_seconds=15, now=start
    )

    assert budget.runtime_deadline == start + timedelta(seconds=50)
    assert budget.worker_deadline == start + timedelta(seconds=15)
    assert budget.deadline == budget.worker_deadline
