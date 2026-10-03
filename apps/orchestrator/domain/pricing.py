"""What a model call cost, and the pricing it was costed against.

Cost is not a property of a model; it is a property of a *call*, fixed at the
moment the call was made. An operator who corrects a mistyped price tomorrow,
or whose provider raises its rates next quarter, must not thereby rewrite what
last month's runs cost. So nothing here is a live lookup: a call is costed once
against the pricing then in force, and both the arithmetic and the prices it
used are persisted beside the call (``ModelRun``'s snapshot fields).

Two absences are deliberately different, and keeping them apart is most of the
reason this module exists:

*   **Unknown.** No pricing is configured for the model, or the endpoint
    reported no token usage. Cost is ``None``. It is not zero, and an
    aggregation must not add it as though it were -- a total that silently
    treated every unpriced call as free would be wrong in the direction that
    looks reassuring.
*   **Free.** An operator has declared ``input_per_million_tokens = 0`` and
    ``output_per_million_tokens = 0``, which is the honest configuration for a
    model served on hardware the operator already owns. Cost is exactly zero,
    and that zero is a measurement.

Money is ``Decimal`` throughout. A single call can cost a small fraction of a
cent, and a run is the sum of many of them; rounding each call to cents as it
is recorded would make the sum disagree with the invoice by more than the
figures it is made of.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

#: Prices are quoted per million tokens because that is how every provider
#: publishes them; costing a call divides by this.
TOKENS_PER_PRICING_UNIT = Decimal(1_000_000)

#: Decimal places kept on a persisted cost, matching the database column's
#: scale. Twelve because a price quoted to six decimal places divided by a
#: million needs twelve to stay exact: at this scale the arithmetic below is a
#: representation of the cost rather than a rounding of it.
COST_SCALE = Decimal("0.000000000001")

#: The key a model's metadata carries its pricing under.
PRICING_METADATA_KEY = "pricing"

DEFAULT_CURRENCY = "USD"


class InvalidPricing(ValueError):
    """A model declares pricing that cannot be read.

    Raised rather than ignored. Pricing that is present but unreadable is a
    configuration fault, and treating it as absent would turn a typo into a
    run-wide "unknown cost" that nobody would think to look for.
    """


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Operator-configured prices for one model.

    Never inferred from a model name and never fetched from a provider: an
    orchestrator that guessed prices would produce figures that look
    authoritative and are not.
    """

    input_per_million: Decimal
    output_per_million: Decimal
    currency: str = DEFAULT_CURRENCY

    @property
    def is_free(self) -> bool:
        """Whether the operator declared this model free, explicitly."""
        return not self.input_per_million and not self.output_per_million

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None) -> ModelPricing | None:
        """Read a model's pricing, or ``None`` if it declares none.

        Raises:
            InvalidPricing: a ``pricing`` key is present but is not a mapping,
                omits a price, or carries one that is not a non-negative
                number.
        """
        if not metadata:
            return None
        if PRICING_METADATA_KEY not in metadata:
            return None
        pricing = metadata[PRICING_METADATA_KEY]
        if not isinstance(pricing, Mapping):
            raise InvalidPricing(
                f"Model metadata '{PRICING_METADATA_KEY}' must be an object, "
                f"got {type(pricing).__name__}"
            )
        currency = pricing.get("currency", DEFAULT_CURRENCY)
        if not isinstance(currency, str) or not currency.strip():
            raise InvalidPricing("Model pricing 'currency' must be a non-empty string")
        return cls(
            input_per_million=_price(pricing, "input_per_million_tokens"),
            output_per_million=_price(pricing, "output_per_million_tokens"),
            currency=currency.strip().upper(),
        )

    def describe(self) -> dict[str, str]:
        """The pricing as JSON-safe exact strings, for API and log output."""
        return {
            "currency": self.currency,
            "input_per_million_tokens": str(self.input_per_million),
            "output_per_million_tokens": str(self.output_per_million),
        }

    def cost_for(
        self, input_tokens: int | None, output_tokens: int | None
    ) -> CallCost:
        """Cost this pricing assigns to one call's reported usage.

        Both token counts are required. A call that reported only one of them
        is costed as unknown rather than half-costed: a partial total is the
        one answer that cannot be told apart from a complete one downstream.
        The pricing is still carried through, because *which prices applied*
        is knowable even when the usage was not.
        """
        if input_tokens is None or output_tokens is None:
            return CallCost(pricing=self)
        input_cost = _quantize(
            Decimal(input_tokens) / TOKENS_PER_PRICING_UNIT * self.input_per_million
        )
        output_cost = _quantize(
            Decimal(output_tokens) / TOKENS_PER_PRICING_UNIT * self.output_per_million
        )
        return CallCost(
            pricing=self,
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=input_cost + output_cost,
        )


@dataclass(frozen=True, slots=True)
class CallCost:
    """The financial snapshot of one model call.

    ``pricing`` is ``None`` when the model had none configured; the costs are
    ``None`` whenever the cost is unknown for either reason. The object is
    self-describing on that point so no caller has to reconstruct the
    distinction from a combination of nulls.
    """

    pricing: ModelPricing | None = None
    input_cost: Decimal | None = None
    output_cost: Decimal | None = None
    total_cost: Decimal | None = None

    @property
    def is_known(self) -> bool:
        return self.total_cost is not None

    @property
    def currency(self) -> str | None:
        return self.pricing.currency if self.pricing else None


def _price(pricing: Mapping[str, Any], key: str) -> Decimal:
    if key not in pricing:
        raise InvalidPricing(f"Model pricing is missing required field '{key}'")
    value = pricing[key]
    # bool is an int in Python, and `True` as a price is a mistake, not a 1.
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise InvalidPricing(f"Model pricing '{key}' must be a number, got {value!r}")
    try:
        # Via str() even for a float: Decimal(0.03) is 0.0299999... and a
        # price read from JSON must mean the number that was written.
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidPricing(f"Model pricing '{key}' is not a number: {value!r}") from exc
    if not price.is_finite() or price < 0:
        raise InvalidPricing(f"Model pricing '{key}' must be non-negative, got {value!r}")
    return price


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(COST_SCALE, rounding=ROUND_HALF_UP)


__all__ = [
    "COST_SCALE",
    "DEFAULT_CURRENCY",
    "PRICING_METADATA_KEY",
    "TOKENS_PER_PRICING_UNIT",
    "CallCost",
    "InvalidPricing",
    "ModelPricing",
]
