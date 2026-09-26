"""Custom SQLAlchemy column types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


class StrEnumType(TypeDecorator):
    """Store a ``StrEnum`` as its string value.

    Chosen over ``sa.Enum`` deliberately: native database enum types require a
    migration for every new member, and the workflow gains states often. The
    trade-off is that validation happens here rather than in the database.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], length: int = 50) -> None:
        self.enum_class = enum_class
        super().__init__(length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Reject unknown strings rather than writing an unreadable row.
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self.enum_class(value)
