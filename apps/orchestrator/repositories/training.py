"""Training-example persistence (build.md section 34).

One row per accepted run, pointing at the directory of preserved artifacts. The
database "indexes the artifacts" as section 34 asks; the curation state lives
here too, because *do not automatically train on every accepted example* means
capture and selection must be separately visible.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ..db.models import TrainingExampleRow
from ..domain.enums import TrainingStatus
from ..domain.models import TrainingExample
from .base import Repository


class TrainingExampleRepository(Repository[TrainingExampleRow, TrainingExample]):
    row_type = TrainingExampleRow

    def _to_domain(self, row: TrainingExampleRow) -> TrainingExample:
        return TrainingExample(
            id=row.id,
            task_run_id=row.task_run_id,
            project_id=row.project_id,
            task_id=row.task_id,
            external_project_id=row.external_project_id,
            external_task_id=row.external_task_id,
            external_run_id=row.external_run_id,
            artifact_path=row.artifact_path,
            manifest_sha256=row.manifest_sha256,
            outcome=row.outcome,
            coder_model_id=row.coder_model_id,
            reviewer_model=row.reviewer_model,
            prompt_version=row.prompt_version,
            attempts=row.attempts,
            review_cycles=row.review_cycles,
            duration_ms=row.duration_ms,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            status=row.status,
            exclusion_reason=row.exclusion_reason,
            created_at=row.created_at,
        )

    def add(self, example: TrainingExample) -> TrainingExample:
        row = TrainingExampleRow(
            id=example.id,
            task_run_id=example.task_run_id,
            project_id=example.project_id,
            task_id=example.task_id,
            external_project_id=example.external_project_id,
            external_task_id=example.external_task_id,
            external_run_id=example.external_run_id,
            artifact_path=example.artifact_path,
            manifest_sha256=example.manifest_sha256,
            outcome=example.outcome,
            coder_model_id=example.coder_model_id,
            reviewer_model=example.reviewer_model,
            prompt_version=example.prompt_version,
            attempts=example.attempts,
            review_cycles=example.review_cycles,
            duration_ms=example.duration_ms,
            input_tokens=example.input_tokens,
            output_tokens=example.output_tokens,
            status=example.status,
            exclusion_reason=example.exclusion_reason,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, example_id: UUID) -> TrainingExample | None:
        row = self._get_row(example_id)
        return self._to_domain(row) if row else None

    def update_fields(self, example_id: UUID, **fields: object) -> TrainingExample:
        row = self._get_row(example_id)
        if row is None:
            raise LookupError(f"Training example {example_id} not found")
        for name, value in fields.items():
            if not hasattr(row, name):
                raise AttributeError(f"TrainingExampleRow has no column {name!r}")
            setattr(row, name, value)
        self.session.flush()
        return self._to_domain(row)

    def get_for_run(self, task_run_id: UUID) -> TrainingExample | None:
        """The example for a run, if it was captured.

        Capture is keyed on the run, so re-running capture after a restart
        updates one row rather than accumulating duplicates of the same
        accepted work.
        """
        row = self.session.scalar(
            select(TrainingExampleRow).where(
                TrainingExampleRow.task_run_id == task_run_id
            )
        )
        return self._to_domain(row) if row else None

    def list_for_project(
        self, project_id: UUID, *, limit: int = 100
    ) -> list[TrainingExample]:
        rows = self.session.scalars(
            select(TrainingExampleRow)
            .where(TrainingExampleRow.project_id == project_id)
            .order_by(TrainingExampleRow.created_at.desc(), TrainingExampleRow.id)
            .limit(limit)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_by_status(
        self, status: TrainingStatus, *, project_id: UUID | None = None, limit: int = 100
    ) -> list[TrainingExample]:
        """Examples in one curation state.

        ``project_id`` is optional here but every caller that knows the project
        passes it: an unscoped status query spans every project in the
        deployment, and a curation view that silently included another team's
        examples would be a data leak wearing the costume of a filter.
        """
        stmt = select(TrainingExampleRow).where(TrainingExampleRow.status == status)
        if project_id is not None:
            stmt = stmt.where(TrainingExampleRow.project_id == project_id)
        rows = self.session.scalars(
            stmt.order_by(TrainingExampleRow.created_at.desc(), TrainingExampleRow.id).limit(
                limit
            )
        ).all()
        return [self._to_domain(row) for row in rows]

    def set_status(
        self,
        example_id: UUID,
        status: TrainingStatus,
        *,
        reason: str | None = None,
    ) -> TrainingExample:
        """Move an example through curation.

        V1 never calls this with ``SELECTED``: selecting high-quality examples
        is the future process section 34 describes, and the transition exists
        so that process has somewhere to write rather than so it can be
        reached by accident.
        """
        row = self._get_row(example_id)
        if row is None:
            raise LookupError(f"Training example {example_id} not found")
        row.status = status
        row.exclusion_reason = reason if status is TrainingStatus.EXCLUDED else None
        self.session.flush()
        return self._to_domain(row)


__all__ = ["TrainingExampleRepository"]
