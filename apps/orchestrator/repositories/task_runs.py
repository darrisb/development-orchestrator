from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from ..db.models import TaskRow, TaskRunRow
from ..domain.enums import RunStatus
from ..domain.models import TaskRun
from .base import Repository


class TaskRunRepository(Repository[TaskRunRow, TaskRun]):
    row_type = TaskRunRow

    def _to_domain(self, row: TaskRunRow) -> TaskRun:
        return TaskRun(
            id=row.id,
            task_id=row.task_id,
            run_number=row.run_number,
            attempt_number=row.attempt_number,
            review_cycle=row.review_cycle,
            status=row.status,
            external_run_id=row.external_run_id,
            coder_model_id=row.coder_model_id,
            worker_image=row.worker_image,
            starting_commit=row.starting_commit,
            candidate_commit=row.candidate_commit,
            branch_name=row.branch_name,
            context_hash=row.context_hash,
            prompt_version=row.prompt_version,
            failure_reason=row.failure_reason,
            artifact_path=row.artifact_path,
            started_at=row.started_at,
            completed_at=row.completed_at,
            active_runtime_ms=row.active_runtime_ms,
            active_started_at=row.active_started_at,
        )

    def next_run_number(self, task_id: UUID) -> int:
        current = self.session.scalar(
            select(func.max(TaskRunRow.run_number)).where(TaskRunRow.task_id == task_id)
        )
        return (current or 0) + 1

    def add(self, run: TaskRun) -> TaskRun:
        row = TaskRunRow(
            id=run.id,
            task_id=run.task_id,
            run_number=run.run_number,
            attempt_number=run.attempt_number,
            review_cycle=run.review_cycle,
            status=run.status,
            external_run_id=run.external_run_id,
            coder_model_id=run.coder_model_id,
            worker_image=run.worker_image,
            starting_commit=run.starting_commit,
            candidate_commit=run.candidate_commit,
            branch_name=run.branch_name,
            context_hash=run.context_hash,
            prompt_version=run.prompt_version,
            failure_reason=run.failure_reason,
            artifact_path=run.artifact_path,
            started_at=run.started_at,
            completed_at=run.completed_at,
            active_runtime_ms=run.active_runtime_ms,
            active_started_at=run.active_started_at,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, run_id: UUID) -> TaskRun | None:
        row = self._get_row(run_id)
        return self._to_domain(row) if row else None

    def list_for_task(self, task_id: UUID) -> list[TaskRun]:
        rows = self.session.scalars(
            select(TaskRunRow)
            .where(TaskRunRow.task_id == task_id)
            .order_by(TaskRunRow.run_number)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_project(self, project_id: UUID) -> list[TaskRun]:
        """Every run of a project, oldest first.

        Phase L is arithmetic over this set: section 35's metrics, the review
        history and the training index all need "all the runs of this project"
        in one query rather than a lookup per task.
        """
        rows = self.session.scalars(
            select(TaskRunRow)
            .join(TaskRow, TaskRunRow.task_id == TaskRow.id)
            .where(TaskRow.project_id == project_id)
            .order_by(TaskRunRow.started_at, TaskRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_terminal(self, project_id: UUID | None = None) -> list[TaskRun]:
        """Runs that have finished, whatever the outcome (section 35).

        Both terminal states, deliberately: a failed run is the half of the
        evidence that makes a success rate a measurement rather than a count
        of what went right.
        """
        stmt = select(TaskRunRow).where(
            TaskRunRow.status.in_(
                [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABANDONED]
            )
        )
        if project_id is not None:
            stmt = stmt.join(TaskRow, TaskRunRow.task_id == TaskRow.id).where(
                TaskRow.project_id == project_id
            )
        rows = self.session.scalars(stmt.order_by(TaskRunRow.started_at, TaskRunRow.id)).all()
        return [self._to_domain(row) for row in rows]

    def list_incomplete(self) -> list[TaskRun]:
        """Runs that were in flight when the orchestrator stopped (section 28)."""
        rows = self.session.scalars(
            select(TaskRunRow).where(
                TaskRunRow.status.in_([RunStatus.PENDING, RunStatus.RUNNING])
            )
        ).all()
        return [self._to_domain(row) for row in rows]

    def finish(
        self, run_id: UUID, status: RunStatus, failure_reason: str | None = None
    ) -> TaskRun:
        row = self._get_row(run_id)
        if row is None:
            raise LookupError(f"Task run {run_id} not found")
        row.status = status
        row.failure_reason = failure_reason
        row.completed_at = datetime.now(UTC)
        self.session.flush()
        return self._to_domain(row)

    def update_fields(self, run_id: UUID, **fields: object) -> TaskRun:
        row = self._get_row(run_id)
        if row is None:
            raise LookupError(f"Task run {run_id} not found")
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"TaskRun has no field {key!r}")
            setattr(row, key, value)
        self.session.flush()
        return self._to_domain(row)
