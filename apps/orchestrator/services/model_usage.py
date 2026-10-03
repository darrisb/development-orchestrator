"""What a run spent: tokens, time and money, per call and in total.

The figures are read, never recomputed. Every cost in here was calculated once
when its call was recorded (``services.model_runs``) and persisted beside it,
so an aggregate is a sum of history rather than a re-pricing of it. Change a
model's configured price today and nothing in this module's answer about
yesterday moves.

Two things this module refuses to do, both of them for the same reason -- a
number that is wrong is worse than a number that is missing, because only the
missing one admits it:

*   **It does not read NULL as zero.** A call with no pricing configured, or
    with no reported usage, has an unknown cost. It is excluded from
    ``known_cost`` and raises ``has_unknown_cost``, so a caller can never
    mistake "the part we could price" for "the total".
*   **It does not add unlike currencies.** Costs are grouped by the currency
    they were recorded in. Nothing here converts between currencies; a
    conversion needs a rate, a rate needs a date, and inventing either would
    make an audit record into an estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from ..domain.enums import ModelPurpose, RunStatus
from ..domain.models import Model, ModelRun
from ..repositories import ModelRepository, ModelRunRepository


@dataclass(frozen=True, slots=True)
class ModelCallUsage:
    """One recorded model call, as the usage view sees it.

    ``cost`` is ``None`` for an unknown cost and ``Decimal("0")`` for a model
    whose pricing declares zero. The two are different answers and this type
    keeps them different.
    """

    model_run_id: UUID
    model_id: UUID
    model_name: str
    provider: str
    purpose: ModelPurpose
    status: RunStatus
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    cost: Decimal | None
    currency: str | None
    attempt: int | None
    review_cycle: int | None

    @property
    def has_known_cost(self) -> bool:
        return self.cost is not None


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """Totals over a set of calls, plus the per-call breakdown.

    ``known_cost`` and ``currency`` are populated only when every priced call
    shares one currency, which is the ordinary case. When they do not,
    ``known_cost_by_currency`` carries the separate totals and the two
    single-currency fields are ``None`` -- a caller that reads only
    ``known_cost`` therefore sees "no single total" rather than a sum of
    dollars and euros.
    """

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: Calls that reported no token count at all, so the token totals above are
    #: a floor rather than the full figure.
    calls_without_usage: int = 0
    known_cost_by_currency: dict[str, Decimal] = field(default_factory=dict)
    has_unknown_cost: bool = False
    calls: tuple[ModelCallUsage, ...] = ()

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted(self.known_cost_by_currency))

    @property
    def mixed_currencies(self) -> bool:
        return len(self.known_cost_by_currency) > 1

    @property
    def currency(self) -> str | None:
        currencies = self.currencies
        return currencies[0] if len(currencies) == 1 else None

    @property
    def known_cost(self) -> Decimal | None:
        """The total of the priced calls, when one currency can express it.

        ``None`` under two different circumstances, and a caller that needs to
        tell them apart reads ``has_unknown_cost`` and ``mixed_currencies``:
        nothing was priced, or what was priced was priced in several
        currencies.
        """
        currency = self.currency
        return self.known_cost_by_currency[currency] if currency else None

    def by_purpose(self) -> dict[ModelPurpose, UsageSummary]:
        """The same summary per purpose, so CODE and REVIEW are separable."""
        return {
            purpose: summarize(
                [call for call in self.calls if call.purpose is purpose]
            )
            for purpose in dict.fromkeys(call.purpose for call in self.calls)
        }

    def by_model(self) -> dict[str, UsageSummary]:
        """The same summary per model name."""
        return {
            name: summarize([call for call in self.calls if call.model_name == name])
            for name in dict.fromkeys(call.model_name for call in self.calls)
        }


def summarize(calls: list[ModelCallUsage] | tuple[ModelCallUsage, ...]) -> UsageSummary:
    """Total a set of calls without re-pricing any of them."""
    by_currency: dict[str, Decimal] = {}
    has_unknown = False
    input_tokens = 0
    output_tokens = 0
    without_usage = 0
    for call in calls:
        input_tokens += call.input_tokens or 0
        output_tokens += call.output_tokens or 0
        if call.input_tokens is None and call.output_tokens is None:
            without_usage += 1
        if call.cost is None or call.currency is None:
            has_unknown = True
            continue
        by_currency[call.currency] = by_currency.get(call.currency, Decimal(0)) + call.cost
    return UsageSummary(
        model_calls=len(calls),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calls_without_usage=without_usage,
        known_cost_by_currency=by_currency,
        has_unknown_cost=has_unknown,
        calls=tuple(calls),
    )


def usage_for_run(session: Session, task_run_id: UUID) -> UsageSummary:
    """Every model call made on one task run (concern 78)."""
    runs = ModelRunRepository(session).list_for_run(task_run_id)
    return summarize(_as_calls(session, runs))


def usage_for_project(session: Session, project_id: UUID) -> UsageSummary:
    """Every model call made on a project's tasks, across all of its runs.

    Reached through ``tasks`` by ``ModelRunRepository.list_for_project``, so a
    project's total covers runs that were abandoned or retried as well as the
    ones that delivered: what the project cost includes the attempts that did
    not work.
    """
    runs = ModelRunRepository(session).list_for_project(project_id)
    return summarize(_as_calls(session, runs))


def _as_calls(session: Session, runs: list[ModelRun]) -> list[ModelCallUsage]:
    models = ModelRepository(session)
    cache: dict[UUID, Model | None] = {}

    def model_for(model_id: UUID) -> Model | None:
        if model_id not in cache:
            cache[model_id] = models.get(model_id)
        return cache[model_id]

    calls = []
    for run in runs:
        model = model_for(run.model_id)
        calls.append(
            ModelCallUsage(
                model_run_id=run.id,
                model_id=run.model_id,
                # A ``models`` row cannot be deleted while calls reference it
                # (the foreign key is ON DELETE RESTRICT), so the fallback is
                # unreachable in practice and present so that a usage report
                # degrades rather than raises if it ever is not.
                model_name=model.model_name if model else str(run.model_id),
                provider=model.provider if model else "unknown",
                purpose=run.purpose,
                status=run.status,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                duration_ms=run.duration_ms,
                cost=run.total_cost,
                currency=run.pricing_currency,
                attempt=run.attempt,
                review_cycle=run.review_cycle,
            )
        )
    return calls


__all__ = [
    "ModelCallUsage",
    "UsageSummary",
    "summarize",
    "usage_for_project",
    "usage_for_run",
]
