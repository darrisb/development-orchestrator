from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from ..db.models import PauseRequestRow
from ..domain.models import PauseRequest
from .base import Repository


class PauseRequestRepository(Repository[PauseRequestRow, PauseRequest]):
    """Pause requests (build.md section 28).

    A request is in force until it is released, and releasing is the only way
    it ends: a workflow that stops for one does not clear it, because "this
    run stopped" and "work may start again" are different facts and only a
    person knows the second one.
    """

    row_type = PauseRequestRow

    def _to_domain(self, row: PauseRequestRow) -> PauseRequest:
        return PauseRequest(
            id=row.id,
            project_id=row.project_id,
            task_id=row.task_id,
            reason=row.reason,
            requested_by=row.requested_by,
            created_at=row.created_at,
            honoured_at=row.honoured_at,
            released_at=row.released_at,
        )

    def add(self, request: PauseRequest) -> PauseRequest:
        row = PauseRequestRow(
            id=request.id,
            project_id=request.project_id,
            task_id=request.task_id,
            reason=request.reason,
            requested_by=request.requested_by,
            honoured_at=request.honoured_at,
            released_at=request.released_at,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, request_id: UUID) -> PauseRequest | None:
        row = self._get_row(request_id)
        return self._to_domain(row) if row else None

    def _in_force(self):
        return select(PauseRequestRow).where(PauseRequestRow.released_at.is_(None))

    def in_force_for_task(self, project_id: UUID, task_id: UUID) -> PauseRequest | None:
        """The request that stops this task: its own, or its project's.

        A project-wide request is returned for every task in the project,
        which is what makes "pause project" mean what it says without writing
        a row per task.
        """
        row = self.session.scalars(
            self._in_force()
            .where(PauseRequestRow.project_id == project_id)
            .where(
                (PauseRequestRow.task_id == task_id) | (PauseRequestRow.task_id.is_(None))
            )
            # A task-scoped request first: it is the more specific answer to
            # "why did this stop", and it is the one an operator releases.
            .order_by(PauseRequestRow.task_id.is_(None), PauseRequestRow.created_at)
        ).first()
        return self._to_domain(row) if row else None

    def list_in_force(
        self, *, project_id: UUID | None = None, task_id: UUID | None = None
    ) -> list[PauseRequest]:
        statement = self._in_force()
        if project_id is not None:
            statement = statement.where(PauseRequestRow.project_id == project_id)
        if task_id is not None:
            statement = statement.where(PauseRequestRow.task_id == task_id)
        rows = self.session.scalars(statement.order_by(PauseRequestRow.created_at)).all()
        return [self._to_domain(row) for row in rows]

    def mark_honoured(self, request_id: UUID) -> PauseRequest:
        """Record that a run actually stopped for this request.

        Raises:
            LookupError: no such request.
        """
        row = self._get_row(request_id)
        if row is None:
            raise LookupError(f"Pause request {request_id} not found")
        if row.honoured_at is None:
            row.honoured_at = datetime.now(UTC)
        self.session.flush()
        return self._to_domain(row)

    def release(
        self, *, project_id: UUID, task_id: UUID | None = None, include_project: bool = False
    ) -> list[PauseRequest]:
        """Lift the matching requests and return the ones that were lifted.

        Resuming a *task* does not lift its project's pause unless asked:
        a project-wide stop is a bigger decision than the task in front of
        you, and quietly undoing it from a task endpoint would restart work
        an operator deliberately halted.
        """
        statement = self._in_force().where(PauseRequestRow.project_id == project_id)
        if task_id is not None and not include_project:
            statement = statement.where(PauseRequestRow.task_id == task_id)
        elif task_id is not None:
            statement = statement.where(
                (PauseRequestRow.task_id == task_id) | (PauseRequestRow.task_id.is_(None))
            )
        rows = list(self.session.scalars(statement).all())
        now = datetime.now(UTC)
        for row in rows:
            row.released_at = now
        self.session.flush()
        return [self._to_domain(row) for row in rows]
