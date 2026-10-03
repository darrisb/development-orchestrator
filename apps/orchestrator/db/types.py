"""Custom SQLAlchemy column types."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Numeric, String
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


class ExactDecimal(TypeDecorator):
    """A ``Decimal`` that survives the round trip on every supported backend.

    Monetary values must not pass through binary floating point, which rules
    out SQLAlchemy's plain ``Numeric`` here: PostgreSQL has a real ``NUMERIC``
    and keeps the value exactly, but SQLite has no decimal storage class and
    SQLAlchemy's fallback routes the value through a Python ``float`` -- which
    is precisely how a cost of ``0.006992500000`` comes back as something
    ending in ...4999. The tests run on SQLite, so a type that is only exact in
    production is a type whose exactness is never actually tested.

    So the value is stored as ``NUMERIC(precision, scale)`` where the backend
    has one and as a fixed-scale decimal *string* where it does not. Strings
    sort and compare correctly for this use only incidentally, so aggregation
    over these columns is done in Python (``ModelRunRepository``) rather than
    by asking the database to ``SUM`` them.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 20, scale: int = 12) -> None:
        self.decimal_precision = precision
        self.decimal_scale = scale
        super().__init__(precision=precision, scale=scale, asdecimal=True)

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            # Wide enough for the sign, the integer part at this precision, the
            # point and the full scale.
            return dialect.type_descriptor(String(self.decimal_precision + 3))
        return dialect.type_descriptor(
            Numeric(precision=self.decimal_precision, scale=self.decimal_scale)
        )

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-self.decimal_scale))
        return str(quantized) if dialect.name == "sqlite" else quantized

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))
