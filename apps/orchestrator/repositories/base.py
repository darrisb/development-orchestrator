"""Repository base.

Repositories are the only place that translates between SQLAlchemy rows and
domain dataclasses. Services and workflow nodes see domain objects only.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.orm import Session

RowT = TypeVar("RowT")
DomainT = TypeVar("DomainT")


class Repository(Generic[RowT, DomainT]):
    row_type: type[RowT]
    #: How this repository names itself in a "not found" error. The message is
    #: the same shape everywhere so a caller can rely on it.
    label: str = "Row"

    def __init__(self, session: Session) -> None:
        self.session = session

    def _to_domain(self, row: RowT) -> DomainT:  # pragma: no cover - abstract
        raise NotImplementedError

    def _get_row(self, entity_id) -> RowT | None:  # type: ignore[no-untyped-def]
        return self.session.get(self.row_type, entity_id)

    def _require_row(self, entity_id) -> RowT:  # type: ignore[no-untyped-def]
        """The row, or ``LookupError`` naming what was missing."""
        row = self._get_row(entity_id)
        if row is None:
            raise LookupError(f"{self.label} {entity_id} not found")
        return row
