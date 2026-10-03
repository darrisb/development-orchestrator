"""Pricing configuration and the cost arithmetic (concern 78).

The distinction these tests exist to protect is between *unknown* and *zero*.
A model with no configured pricing has an unknown cost; a model an operator has
declared free has a cost of zero. Collapsing the two produces totals that look
complete and understate spending, which is the one failure mode nobody notices.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.orchestrator.domain.pricing import (
    CallCost,
    InvalidPricing,
    ModelPricing,
)


def pricing_metadata(
    input_price: object = "1.25", output_price: object = "10.00", **extra: object
) -> dict[str, object]:
    return {
        "pricing": {
            "input_per_million_tokens": input_price,
            "output_per_million_tokens": output_price,
            **extra,
        }
    }


# --- reading configuration ---------------------------------------------------


def test_pricing_is_read_from_model_metadata() -> None:
    pricing = ModelPricing.from_metadata(pricing_metadata(currency="usd"))

    assert pricing is not None
    assert pricing.input_per_million == Decimal("1.25")
    assert pricing.output_per_million == Decimal("10.00")
    assert pricing.currency == "USD"


def test_the_currency_defaults_to_usd_when_unstated() -> None:
    pricing = ModelPricing.from_metadata(pricing_metadata())

    assert pricing is not None
    assert pricing.currency == "USD"


def test_a_model_without_pricing_metadata_has_no_pricing() -> None:
    """Absent pricing is absent, not free. ``None`` is what makes the
    difference representable at every layer above this one."""
    assert ModelPricing.from_metadata({"source": "environment"}) is None
    assert ModelPricing.from_metadata({}) is None
    assert ModelPricing.from_metadata(None) is None


def test_a_float_price_is_not_read_through_binary_floating_point() -> None:
    """``Decimal(0.03)`` is 0.0299999...; a price means the number written."""
    pricing = ModelPricing.from_metadata(pricing_metadata(input_price=0.03))

    assert pricing is not None
    assert pricing.input_per_million == Decimal("0.03")


@pytest.mark.parametrize(
    "metadata",
    [
        {"pricing": "free"},
        {"pricing": {"input_per_million_tokens": "1.00"}},
        {"pricing": {"output_per_million_tokens": "1.00"}},
        pricing_metadata(input_price="cheap"),
        pricing_metadata(input_price=-1),
        pricing_metadata(input_price=True),
        pricing_metadata(input_price=None),
        pricing_metadata(currency=""),
        pricing_metadata(currency=7),
    ],
)
def test_unreadable_pricing_fails_closed(metadata: dict[str, object]) -> None:
    """Pricing that is present but malformed is a configuration fault. Treating
    it as absent would turn a typo into a silent run-wide unknown."""
    with pytest.raises(InvalidPricing):
        ModelPricing.from_metadata(metadata)


# --- costing a call ----------------------------------------------------------


def test_a_paid_model_costs_its_reported_usage() -> None:
    pricing = ModelPricing(Decimal("1.25"), Decimal("10.00"))

    cost = pricing.cost_for(input_tokens=2098, output_tokens=437)

    assert cost.input_cost == Decimal("0.0026225")
    assert cost.output_cost == Decimal("0.00437")
    assert cost.total_cost == Decimal("0.0069925")
    assert cost.total_cost == cost.input_cost + cost.output_cost
    assert cost.is_known


def test_a_fraction_of_a_cent_is_not_rounded_away() -> None:
    """A single call can cost a thousandth of a cent, and a run is the sum of
    many. Rounding each call to cents loses the run, not just the call."""
    pricing = ModelPricing(Decimal("0.15"), Decimal("0.60"))

    cost = pricing.cost_for(input_tokens=1, output_tokens=1)

    assert cost.input_cost == Decimal("0.00000015")
    assert cost.output_cost == Decimal("0.0000006")
    assert cost.total_cost == Decimal("0.00000075")
    # Not zero, and not rounded to a cent.
    assert cost.total_cost > 0


def test_an_explicitly_free_model_costs_exactly_zero() -> None:
    """Zero declared by an operator is a measurement, and is reported as a
    number so it can be added to a total."""
    pricing = ModelPricing(Decimal(0), Decimal(0))

    cost = pricing.cost_for(input_tokens=2364, output_tokens=61)

    assert pricing.is_free
    assert cost.total_cost == Decimal(0)
    assert cost.is_known


def test_missing_usage_is_unknown_even_when_pricing_is_known() -> None:
    """The prices still applied, so they are carried; the cost is not
    invented. A half-costed call is indistinguishable from a complete one."""
    pricing = ModelPricing(Decimal("1.25"), Decimal("10.00"))

    assert pricing.cost_for(None, None).total_cost is None
    partial = pricing.cost_for(2098, None)
    assert partial.total_cost is None
    assert partial.input_cost is None
    assert partial.pricing is pricing
    assert not partial.is_known


def test_an_uncosted_call_reports_neither_a_cost_nor_a_currency() -> None:
    unknown = CallCost()

    assert unknown.total_cost is None
    assert unknown.currency is None
    assert not unknown.is_known
