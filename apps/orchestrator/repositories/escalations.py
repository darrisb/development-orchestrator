from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from ..db.models import HumanEscalationRow
from ..domain.enums import EscalationStatus
from ..domain.escalation import EscalationIntent, EscalationOption
from ..domain.models import HumanEscalation
from .base import Repository


class EscalationRepository(Repository[HumanEscalationRow, HumanEscalation]):
    """Human escalations (build.md section 24).

    An escalation is a record a person is meant to act on, so nothing here
    closes one automatically: ``resolve`` is called by an operator's decision
    reaching the API, never by a run deciding it no longer needs an answer.
    """

    row_type = HumanEscalationRow

    def _to_domain(self, row: HumanEscalationRow) -> HumanEscalation:
        return HumanEscalation(
            id=row.id,
            task_id=row.task_id,
            task_run_id=row.task_run_id,
            reason=row.reason,
            summary=row.summary,
            options=[EscalationOption.restore(raw) or str(raw) for raw in row.options or []],
            status=row.status,
            resolution=row.resolution,
            resolution_intent=(
                EscalationIntent(row.resolution_intent) if row.resolution_intent else None
            ),
            created_at=row.created_at,
            resolved_at=row.resolved_at,
        )

    def add(self, escalation: HumanEscalation) -> HumanEscalation:
        row = HumanEscalationRow(
            id=escalation.id,
            task_id=escalation.task_id,
            task_run_id=escalation.task_run_id,
            reason=escalation.reason,
            summary=escalation.summary,
            options=[
                option.describe() if isinstance(option, EscalationOption) else str(option)
                for option in escalation.options
            ],
            status=escalation.status,
            resolution=escalation.resolution,
            resolution_intent=(
                escalation.resolution_intent.value if escalation.resolution_intent else None
            ),
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, escalation_id: UUID) -> HumanEscalation | None:
        row = self._get_row(escalation_id)
        return self._to_domain(row) if row else None

    def list_open(self, *, task_id: UUID | None = None) -> list[HumanEscalation]:
        statement = select(HumanEscalationRow).where(
            HumanEscalationRow.status == EscalationStatus.OPEN
        )
        if task_id is not None:
            statement = statement.where(HumanEscalationRow.task_id == task_id)
        rows = self.session.scalars(
            statement.order_by(HumanEscalationRow.created_at)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_run(self, task_run_id: UUID) -> list[HumanEscalation]:
        rows = self.session.scalars(
            select(HumanEscalationRow)
            .where(HumanEscalationRow.task_run_id == task_run_id)
            .order_by(HumanEscalationRow.created_at)
        ).all()
        return [self._to_domain(row) for row in rows]

    def resolve(
        self,
        escalation_id: UUID,
        *,
        resolution: str,
        status: EscalationStatus = EscalationStatus.RESOLVED,
        intent: EscalationIntent | None = None,
    ) -> HumanEscalation:
        """Close an escalation with a human's answer.

        ``intent`` is the option they chose; a dismissal has none, because
        dismissing is an answer that asks for nothing to happen.

        Raises:
            LookupError: no such escalation.
        """
        row = self._get_row(escalation_id)
        if row is None:
            raise LookupError(f"Human escalation {escalation_id} not found")
        row.status = status
        row.resolution = resolution
        row.resolution_intent = intent.value if intent else None
        row.resolved_at = datetime.now(UTC)
        self.session.flush()
        return self._to_domain(row)

    def list_for_task(
        self, task_id: UUID, *, status: EscalationStatus | None = None
    ) -> list[HumanEscalation]:
        """Every escalation for a task, oldest first."""
        statement = select(HumanEscalationRow).where(HumanEscalationRow.task_id == task_id)
        if status is not None:
            statement = statement.where(HumanEscalationRow.status == status)
        rows = self.session.scalars(
            statement.order_by(HumanEscalationRow.created_at)
        ).all()
        return [self._to_domain(row) for row in rows]
