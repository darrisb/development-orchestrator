from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from ..db.models import ModelRow, ModelRunRow, TaskRow, TaskRunRow
from ..domain.enums import ModelPurpose, ModelRole, RunStatus
from ..domain.models import Model, ModelRun
from .base import Repository


class ModelRepository(Repository[ModelRow, Model]):
    row_type = ModelRow

    def _to_domain(self, row: ModelRow) -> Model:
        return Model(
            id=row.id,
            provider=row.provider,
            model_name=row.model_name,
            external_model_id=row.external_model_id,
            role=row.role,
            endpoint=row.endpoint,
            enabled=row.enabled,
            timeout_seconds=row.timeout_seconds,
            context_window=row.context_window,
            metadata=dict(row.meta or {}),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def add(self, model: Model) -> Model:
        row = ModelRow(
            id=model.id,
            provider=model.provider,
            model_name=model.model_name,
            external_model_id=model.external_model_id,
            role=model.role,
            endpoint=model.endpoint,
            enabled=model.enabled,
            timeout_seconds=model.timeout_seconds,
            context_window=model.context_window,
            meta=dict(model.metadata),
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, model_id: UUID) -> Model | None:
        row = self._get_row(model_id)
        return self._to_domain(row) if row else None

    def get_by_identity(
        self, provider: str, model_name: str, role: ModelRole
    ) -> Model | None:
        """Find a model by its natural key -- the table's unique constraint."""
        row = self.session.scalar(
            select(ModelRow).where(
                ModelRow.provider == provider,
                ModelRow.model_name == model_name,
                ModelRow.role == role,
            )
        )
        return self._to_domain(row) if row else None

    def update_fields(self, model_id: UUID, **fields: object) -> Model:
        row = self._get_row(model_id)
        if row is None:
            raise LookupError(f"Model {model_id} not found")
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"Model has no field {key!r}")
            setattr(row, key, value)
        self.session.flush()
        return self._to_domain(row)

    def list(self, role: ModelRole | None = None, enabled_only: bool = False) -> list[Model]:
        stmt = select(ModelRow)
        if role is not None:
            stmt = stmt.where(ModelRow.role == role)
        if enabled_only:
            stmt = stmt.where(ModelRow.enabled.is_(True))
        rows = self.session.scalars(stmt.order_by(ModelRow.model_name)).all()
        return [self._to_domain(row) for row in rows]


class ModelRunRepository(Repository[ModelRunRow, ModelRun]):
    """One row per model call (build.md section 7).

    Sections 34 and 35 -- training capture and model evaluation -- are
    arithmetic over this table, so a call that is not recorded here is a call
    that never happened as far as either is concerned. Every model the
    orchestrator calls therefore needs a ``models`` row to point at, including
    the ones configured from the environment; ``services.model_runs`` is what
    makes sure one exists.
    """

    row_type = ModelRunRow

    def _to_domain(self, row: ModelRunRow) -> ModelRun:
        return ModelRun(
            id=row.id,
            task_run_id=row.task_run_id,
            model_id=row.model_id,
            purpose=row.purpose,
            status=row.status,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            duration_ms=row.duration_ms,
            prompt_artifact=row.prompt_artifact,
            response_artifact=row.response_artifact,
            error_detail=row.error_detail,
            attempt=row.attempt,
            review_cycle=row.review_cycle,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    def add(self, model_run: ModelRun) -> ModelRun:
        row = ModelRunRow(
            id=model_run.id,
            task_run_id=model_run.task_run_id,
            model_id=model_run.model_id,
            purpose=model_run.purpose,
            status=model_run.status,
            input_tokens=model_run.input_tokens,
            output_tokens=model_run.output_tokens,
            duration_ms=model_run.duration_ms,
            prompt_artifact=model_run.prompt_artifact,
            response_artifact=model_run.response_artifact,
            error_detail=model_run.error_detail,
            attempt=model_run.attempt,
            review_cycle=model_run.review_cycle,
            started_at=model_run.started_at,
            completed_at=model_run.completed_at or datetime.now(UTC),
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, model_run_id: UUID) -> ModelRun | None:
        row = self._get_row(model_run_id)
        return self._to_domain(row) if row else None

    def list_for_run(self, task_run_id: UUID) -> list[ModelRun]:
        rows = self.session.scalars(
            select(ModelRunRow)
            .where(ModelRunRow.task_run_id == task_run_id)
            .order_by(ModelRunRow.started_at, ModelRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_purpose(self, task_run_id: UUID, purpose: ModelPurpose) -> list[ModelRun]:
        rows = self.session.scalars(
            select(ModelRunRow)
            .where(
                ModelRunRow.task_run_id == task_run_id,
                ModelRunRow.purpose == purpose,
            )
            .order_by(ModelRunRow.started_at, ModelRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_all(self) -> list[ModelRun]:
        """Every recorded call, oldest first.

        Section 35's model comparison spans the whole installation: the question
        it answers is which registered model is cheaper per accepted task, and
        that is not a per-project question.
        """
        rows = self.session.scalars(
            select(ModelRunRow).order_by(ModelRunRow.started_at, ModelRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_project(self, project_id: UUID) -> list[ModelRun]:
        """Every call made on a project's tasks.

        Reached through ``tasks`` rather than by iterating the project's runs:
        the per-run totals in ``RunMetrics`` already cover a project's own spend,
        and this is the cross-check, so it deliberately takes a different path
        through the schema and the two disagreeing would be worth knowing.
        """
        rows = self.session.scalars(
            select(ModelRunRow)
            .join(TaskRunRow, ModelRunRow.task_run_id == TaskRunRow.id)
            .join(TaskRow, TaskRunRow.task_id == TaskRow.id)
            .where(TaskRow.project_id == project_id)
            .order_by(ModelRunRow.started_at, ModelRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def tokens_for_run(self, task_run_id: UUID) -> tuple[int, int]:
        """Input and output tokens across a run, counting only what was reported.

        An endpoint that reports no usage contributes zero rather than an
        estimate: section 34 wants an unknown recorded as unknown, and a
        guessed token count in a training set is worse than a missing one.
        """
        runs = self.list_for_run(task_run_id)
        return (
            sum(run.input_tokens or 0 for run in runs),
            sum(run.output_tokens or 0 for run in runs),
        )

    def finish(
        self, model_run_id: UUID, status: RunStatus, **fields: object
    ) -> ModelRun:
        row = self._get_row(model_run_id)
        if row is None:
            raise LookupError(f"Model run {model_run_id} not found")
        row.status = status
        row.completed_at = datetime.now(UTC)
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"ModelRun has no field {key!r}")
            setattr(row, key, value)
        self.session.flush()
        return self._to_domain(row)
