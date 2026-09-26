from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ..db.models import VerificationRunRow
from ..domain.enums import VerificationStatus, VerificationType
from ..domain.models import VerificationRun
from .base import Repository


class VerificationRunRepository(Repository[VerificationRunRow, VerificationRun]):
    """What the orchestrator executed, and what it returned (section 7).

    One row per check, written whether it passed or failed and whether it was
    a command or a policy decision. The row is the answer to "was this really
    run?", which is the question section 17 exists to make answerable, so a
    skipped category is recorded as ``SKIPPED`` rather than left out.
    """

    row_type = VerificationRunRow

    def _to_domain(self, row: VerificationRunRow) -> VerificationRun:
        return VerificationRun(
            id=row.id,
            task_run_id=row.task_run_id,
            verification_type=row.verification_type,
            command=row.command,
            status=row.status,
            exit_code=row.exit_code,
            stdout_artifact=row.stdout_artifact,
            stderr_artifact=row.stderr_artifact,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )

    def add(self, verification: VerificationRun) -> VerificationRun:
        row = VerificationRunRow(
            id=verification.id,
            task_run_id=verification.task_run_id,
            verification_type=verification.verification_type,
            command=verification.command,
            status=verification.status,
            exit_code=verification.exit_code,
            stdout_artifact=verification.stdout_artifact,
            stderr_artifact=verification.stderr_artifact,
            duration_ms=verification.duration_ms,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, verification_id: UUID) -> VerificationRun | None:
        row = self._get_row(verification_id)
        return self._to_domain(row) if row else None

    def list_for_run(self, task_run_id: UUID) -> list[VerificationRun]:
        rows = self.session.scalars(
            select(VerificationRunRow)
            .where(VerificationRunRow.task_run_id == task_run_id)
            .order_by(VerificationRunRow.created_at, VerificationRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_failures(self, task_run_id: UUID) -> list[VerificationRun]:
        """Everything that did not pass, for an escalation or a fix prompt."""
        rows = self.session.scalars(
            select(VerificationRunRow)
            .where(
                VerificationRunRow.task_run_id == task_run_id,
                VerificationRunRow.status.in_(
                    [
                        VerificationStatus.FAILED,
                        VerificationStatus.ERROR,
                        VerificationStatus.TIMEOUT,
                    ]
                ),
            )
            .order_by(VerificationRunRow.created_at, VerificationRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def latest_for_type(
        self, task_run_id: UUID, verification_type: VerificationType
    ) -> VerificationRun | None:
        row = self.session.scalars(
            select(VerificationRunRow)
            .where(
                VerificationRunRow.task_run_id == task_run_id,
                VerificationRunRow.verification_type == verification_type,
            )
            .order_by(VerificationRunRow.created_at.desc(), VerificationRunRow.id.desc())
        ).first()
        return self._to_domain(row) if row else None
