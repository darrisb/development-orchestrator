from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ..db.models import ArtifactRow
from ..domain.models import Artifact
from .base import Repository


class ArtifactRepository(Repository[ArtifactRow, Artifact]):
    """Metadata for files on disk (build.md section 9).

    The database records the path and hash; the bytes live under
    ``ARTIFACT_ROOT``. Nothing large is ever stored in a row.
    """

    row_type = ArtifactRow

    def _to_domain(self, row: ArtifactRow) -> Artifact:
        return Artifact(
            id=row.id,
            task_run_id=row.task_run_id,
            kind=row.kind,
            path=row.path,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            created_at=row.created_at,
        )

    def add(self, artifact: Artifact) -> Artifact:
        row = ArtifactRow(
            id=artifact.id,
            task_run_id=artifact.task_run_id,
            kind=artifact.kind,
            path=artifact.path,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def record(self, artifact: Artifact) -> Artifact:
        """Add, or update the existing record for this run and kind.

        A run that is retried rewrites its artifacts; keeping one row per kind
        means the path and hash in the database always describe the file that
        is actually on disk.
        """
        existing = self.session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.task_run_id == artifact.task_run_id,
                ArtifactRow.kind == artifact.kind,
            )
        )
        if existing is None:
            return self.add(artifact)
        existing.path = artifact.path
        existing.sha256 = artifact.sha256
        existing.size_bytes = artifact.size_bytes
        self.session.flush()
        return self._to_domain(existing)

    def get(self, artifact_id: UUID) -> Artifact | None:
        row = self._get_row(artifact_id)
        return self._to_domain(row) if row else None

    def list_for_run(self, task_run_id: UUID) -> list[Artifact]:
        rows = self.session.scalars(
            select(ArtifactRow)
            .where(ArtifactRow.task_run_id == task_run_id)
            .order_by(ArtifactRow.kind)
        ).all()
        return [self._to_domain(row) for row in rows]
