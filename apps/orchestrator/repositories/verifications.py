from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ..db.models import VerificationBaselineRow, VerificationRunRow
from ..domain.enums import VerificationStatus, VerificationType, WorkerProfile
from ..domain.models import VerificationBaseline, VerificationRun
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


class VerificationBaselineRepository(
    Repository[VerificationBaselineRow, VerificationBaseline]
):
    """The known state of a tree, per verification command (concern 78, stage 2).

    One row per ``(project, baseline_sha, verification_type, command)``, which
    is also the lookup. ``record`` replaces in place rather than appending, so
    a tree that is measured twice has one answer rather than two that a reader
    would have to choose between by timestamp.
    """

    row_type = VerificationBaselineRow

    def _to_domain(self, row: VerificationBaselineRow) -> VerificationBaseline:
        return VerificationBaseline(
            id=row.id,
            project_id=row.project_id,
            baseline_sha=row.baseline_sha,
            verification_type=row.verification_type,
            command=row.command,
            worker_profile=row.worker_profile,
            status=row.status,
            failure_identities=list(row.failure_identities or ()),
            failures_available=row.failures_available,
            extractor=row.extractor,
            exit_code=row.exit_code,
            stdout_artifact=row.stdout_artifact,
            source_task_run_id=row.source_task_run_id,
            created_at=row.created_at,
        )

    def record(self, baseline: VerificationBaseline) -> VerificationBaseline:
        """Write or replace the evidence for one command against one tree.

        Replacing rather than inserting is what makes the unique constraint a
        guarantee instead of an error to handle at every call site: a second
        measurement of the same tree is a *correction*, and keeping both would
        leave the lookup ambiguous.
        """
        row = self._find_row(
            baseline.project_id,
            baseline.baseline_sha,
            baseline.verification_type,
            baseline.command,
        )
        if row is None:
            row = VerificationBaselineRow(
                id=baseline.id,
                project_id=baseline.project_id,
                baseline_sha=baseline.baseline_sha,
                verification_type=baseline.verification_type,
                command=baseline.command,
                worker_profile=baseline.worker_profile,
                status=baseline.status,
            )
            self.session.add(row)
        row.worker_profile = baseline.worker_profile
        row.status = baseline.status
        row.failure_identities = sorted(baseline.failure_identities)
        row.failures_available = baseline.failures_available
        row.extractor = baseline.extractor
        row.exit_code = baseline.exit_code
        row.stdout_artifact = baseline.stdout_artifact
        row.source_task_run_id = baseline.source_task_run_id
        self.session.flush()
        return self._to_domain(row)

    def list_for_sha(
        self, project_id: UUID, baseline_sha: str
    ) -> list[VerificationBaseline]:
        rows = self.session.scalars(
            select(VerificationBaselineRow)
            .where(
                VerificationBaselineRow.project_id == project_id,
                VerificationBaselineRow.baseline_sha == baseline_sha,
            )
            .order_by(VerificationBaselineRow.command)
        ).all()
        return [self._to_domain(row) for row in rows]

    def find(
        self,
        project_id: UUID,
        baseline_sha: str,
        verification_type: VerificationType,
        command: str,
        worker_profile: WorkerProfile,
    ) -> VerificationBaseline | None:
        """The evidence for exactly this command against exactly this tree.

        Every argument is part of the provenance, and none of them is relaxed
        on a miss. A baseline for a different commit, for a command whose text
        has since changed, or for a different worker image is not a near-enough
        baseline; it is no baseline, and the caller must either recertify or
        classify the candidate as unclassified.
        """
        row = self._find_row(project_id, baseline_sha, verification_type, command)
        if row is None or row.worker_profile is not worker_profile:
            return None
        return self._to_domain(row)

    def _find_row(
        self,
        project_id: UUID,
        baseline_sha: str,
        verification_type: VerificationType,
        command: str,
    ) -> VerificationBaselineRow | None:
        return self.session.scalars(
            select(VerificationBaselineRow).where(
                VerificationBaselineRow.project_id == project_id,
                VerificationBaselineRow.baseline_sha == baseline_sha,
                VerificationBaselineRow.verification_type == verification_type,
                VerificationBaselineRow.command == command,
            )
        ).first()
