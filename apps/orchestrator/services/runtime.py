"""Durable active-execution accounting for task runs.

Run creation, workspace preparation, queues, pauses and escalations are not
active execution.  The workflow opens an interval immediately before the fix
loop and closes it at the invocation boundary.  An interval left open by a
crash is charged on recovery, but never beyond that invocation's worker safety
timeout.  Thus a restart cannot reset work already consumed and post-crash idle
time cannot consume an unbounded run budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from ..domain.models import TaskLimits
from ..domain.workflow import run_deadline
from ..repositories import TaskRunRepository


@dataclass(frozen=True, slots=True)
class RuntimeBudget:
    configured_ms: int
    consumed_ms: int
    remaining_ms: int
    active_started_at: datetime | None
    runtime_deadline: datetime
    deadline: datetime
    worker_deadline: datetime


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def configured_runtime_ms(limits: TaskLimits) -> int:
    return max(1, limits.max_runtime_minutes) * 60_000


def begin_active_runtime(
    session: Session,
    run_id: UUID,
    limits: TaskLimits,
    *,
    worker_timeout_seconds: int,
    now: datetime | None = None,
) -> RuntimeBudget:
    """Open an invocation and return its cumulative-run and worker deadlines."""
    moment = _aware(now or datetime.now(UTC))
    runs = TaskRunRepository(session)
    run = runs.get(run_id)
    if run is None:
        raise LookupError(f"Task run {run_id} not found")

    consumed = max(0, run.active_runtime_ms)
    if run.active_started_at is not None:
        stale_start = _aware(run.active_started_at)
        stale_end = min(
            moment,
            stale_start + timedelta(seconds=max(1, worker_timeout_seconds)),
        )
        consumed += max(0, int((stale_end - stale_start).total_seconds() * 1000))

    configured = configured_runtime_ms(limits)
    consumed = min(configured, consumed)
    remaining = max(0, configured - consumed)
    worker_deadline = moment + timedelta(seconds=max(1, worker_timeout_seconds))
    runtime_deadline = run_deadline(
        moment, limits, consumed_runtime_ms=consumed
    )
    active_start = moment if remaining else None
    runs.update_fields(
        run_id,
        active_runtime_ms=consumed,
        active_started_at=active_start,
    )
    return RuntimeBudget(
        configured_ms=configured,
        consumed_ms=consumed,
        remaining_ms=remaining,
        active_started_at=active_start,
        runtime_deadline=runtime_deadline,
        deadline=min(runtime_deadline, worker_deadline),
        worker_deadline=worker_deadline,
    )


def end_active_runtime(
    session: Session,
    run_id: UUID,
    limits: TaskLimits,
    *,
    worker_timeout_seconds: int,
    now: datetime | None = None,
) -> int:
    """Close the current interval and return cumulative active milliseconds."""
    moment = _aware(now or datetime.now(UTC))
    runs = TaskRunRepository(session)
    run = runs.get(run_id)
    if run is None:
        raise LookupError(f"Task run {run_id} not found")
    consumed = max(0, run.active_runtime_ms)
    if run.active_started_at is not None:
        active_start = _aware(run.active_started_at)
        active_end = min(
            moment,
            active_start + timedelta(seconds=max(1, worker_timeout_seconds)),
        )
        consumed += max(0, int((active_end - active_start).total_seconds() * 1000))
    consumed = min(configured_runtime_ms(limits), consumed)
    runs.update_fields(run_id, active_runtime_ms=consumed, active_started_at=None)
    return consumed


__all__ = [
    "RuntimeBudget",
    "begin_active_runtime",
    "configured_runtime_ms",
    "end_active_runtime",
]
