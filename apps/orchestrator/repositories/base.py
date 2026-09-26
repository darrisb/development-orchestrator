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

    def __init__(self, session: Session) -> None:
        self.session = session

    def _to_domain(self, row: RowT) -> DomainT:  # pragma: no cover - abstract
        raise NotImplementedError

    def _get_row(self, entity_id) -> RowT | None:  # type: ignore[no-untyped-def]
        return self.session.get(self.row_type, entity_id)
