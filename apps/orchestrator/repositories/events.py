from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from ..db.models import RunEventRow
from ..domain.models import RunEvent
from .base import Repository


class RunEventRepository(Repository[RunEventRow, RunEvent]):
    """Append-only event log (build.md section 40).

    **Every event is stamped when it is appended, not when its transaction
    started** (concern 49). The column's ``server_default`` is PostgreSQL's
    ``now()``, which returns the *transaction* timestamp: it does not advance
    within a transaction, so every event written during one loop turn would
    share the moment that turn began. Concern 36 made each turn a single
    transaction, which is correct for recovery and makes this worse for timing
    -- a longer transaction collapses more events onto one instant. Observed on
    the first real run: ``TESTS_PASSED`` was stamped 79 seconds before the tests
    ran.

    ``sequence`` establishes order independently and always did, so what this
    fixes is elapsed time, not ordering. The clock is the application's because
    ``clock_timestamp()`` is PostgreSQL-only and this runs on SQLite too.
    """

    row_type = RunEventRow

    def _to_domain(self, row: RunEventRow) -> RunEvent:
        return RunEvent(
            id=row.id,
            task_run_id=row.task_run_id,
            sequence=row.sequence,
            project_id=row.project_id,
            task_id=row.task_id,
            event_type=row.event_type,
            attempt=row.attempt,
            worker_id=row.worker_id,
            model_id=row.model_id,
            payload=dict(row.payload or {}),
            created_at=row.created_at,
        )

    def _next_sequence(self, task_run_id: UUID | None) -> int:
        current = self.session.scalar(
            select(func.max(RunEventRow.sequence)).where(
                RunEventRow.task_run_id == task_run_id
            )
        )
        return (current or 0) + 1

    def append(self, event: RunEvent) -> RunEvent:
        row = RunEventRow(
            id=event.id,
            sequence=self._next_sequence(event.task_run_id),
            # An event carrying its own timestamp keeps it; see the class
            # docstring for why the default cannot be relied on.
            created_at=event.created_at or datetime.now(UTC),
            task_run_id=event.task_run_id,
            project_id=event.project_id,
            task_id=event.task_id,
            event_type=str(event.event_type),
            attempt=event.attempt,
            worker_id=event.worker_id,
            model_id=event.model_id,
            payload=dict(event.payload),
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def list_for_run(self, task_run_id: UUID) -> list[RunEvent]:
        rows = self.session.scalars(
            select(RunEventRow)
            .where(RunEventRow.task_run_id == task_run_id)
            .order_by(RunEventRow.sequence)
        ).all()
        return [self._to_domain(row) for row in rows]
