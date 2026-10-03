"""Persisting what a model call cost, and totalling it (concern 78).

Three properties are under test here, and all three are about history rather
than arithmetic:

*   The snapshot. A call is costed once, at the recording boundary, and the
    prices it was costed against are stored beside it -- so correcting a price
    tomorrow cannot reprice yesterday.
*   The shared path. PLAN, CODE, FIX and REVIEW are costed by the same code,
    because a comparison between purposes is only meaningful if nothing
    measured them differently.
*   The unknowns. A call nobody could price is excluded from a total and
    declared, never added as a zero.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import (
    ModelPurpose,
    ModelRole,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.providers import ProviderConfig, TokenUsage
from apps.orchestrator.repositories import (
    ModelRepository,
    ModelRunRepository,
    ProjectRepository,
    TaskRepository,
)
from apps.orchestrator.services import model_usage
from apps.orchestrator.services.errors import EntityConflict
from apps.orchestrator.services.model_providers import register_model
from apps.orchestrator.services.model_runs import record_model_call
from apps.orchestrator.services.runs import create_run

pytestmark = pytest.mark.integration

#: Illustrative operator configuration. Deliberately not any provider's real
#: published rate: no price is hard-coded anywhere in the implementation, and a
#: test that borrowed a real one would imply otherwise.
PAID_PRICING = {
    "pricing": {
        "currency": "USD",
        "input_per_million_tokens": "1.25",
        "output_per_million_tokens": "10.00",
    }
}
FREE_PRICING = {
    "pricing": {
        "currency": "USD",
        "input_per_million_tokens": 0,
        "output_per_million_tokens": 0,
    }
}
EURO_PRICING = {
    "pricing": {
        "currency": "EUR",
        "input_per_million_tokens": "1.00",
        "output_per_million_tokens": "2.00",
    }
}


@pytest.fixture
def project(session: Session):
    return ProjectRepository(session).add(
        Project(name="TraceStack", repository_path="/workspace/tracestack")
    )


@pytest.fixture
def run(session: Session, project) -> TaskRun:
    return _run_for(session, project, "TS-078")


def _run_for(session: Session, project, external_task_id: str) -> TaskRun:
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id=external_task_id,
            title="Navigation",
        )
    )
    tasks.transition(task.id, TaskStatus.READY)
    return create_run(session, task.id)


def paid_model(session: Session, *, name: str = "gpt-5.3-codex", **overrides):
    return register_model(
        session,
        provider="openai_compatible",
        model_name=name,
        role=overrides.pop("role", ModelRole.CODER),
        endpoint="https://api.openai.com/v1",
        metadata=overrides.pop("metadata", PAID_PRICING),
        **overrides,
    )


def config_for(model) -> ProviderConfig:
    return ProviderConfig(
        provider_id=str(model.id),
        base_url=model.endpoint,
        model_name=model.model_name,
        role=model.role,
    )


def unpriced_env_config(**overrides) -> ProviderConfig:
    """The default local coder, which arrives from the environment carrying no
    pricing at all -- the ordinary unknown-cost case."""
    return ProviderConfig(
        **{
            "provider_id": "env:local-coder",
            "base_url": "http://192.168.0.126:8080/v1",
            "model_name": "qwen-coder-30b",
            "role": ModelRole.CODER,
            **overrides,
        }
    )


# --- the snapshot on one call ------------------------------------------------


def test_a_paid_call_persists_its_cost_and_the_prices_it_used(
    session: Session, run: TaskRun
) -> None:
    model = paid_model(session)

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )

    stored = ModelRunRepository(session).get(recorded.model_run.id)
    assert stored is not None
    assert stored.pricing_currency == "USD"
    assert stored.input_price_per_million == Decimal("1.25")
    assert stored.output_price_per_million == Decimal("10.00")
    assert stored.input_cost == Decimal("0.0026225")
    assert stored.output_cost == Decimal("0.00437")
    assert stored.total_cost == Decimal("0.0069925")
    assert stored.has_known_cost


def test_a_fractional_cent_survives_the_database_round_trip(
    session: Session, run: TaskRun
) -> None:
    """The reason the column is not a float and not scaled to cents: a cost
    this small must come back as itself, not as something ending in ...4999."""
    model = paid_model(session)

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )
    session.expire_all()

    stored = ModelRunRepository(session).get(recorded.model_run.id)
    assert stored is not None
    assert stored.total_cost == Decimal("0.00001125")
    assert isinstance(stored.total_cost, Decimal)


def test_an_explicitly_free_model_records_zero_rather_than_unknown(
    session: Session, run: TaskRun
) -> None:
    reviewer = register_model(
        session,
        provider="openai_compatible",
        model_name="qwen2.5-coder-32b",
        role=ModelRole.REVIEWER,
        endpoint="http://192.168.0.126:8080/v1",
        metadata=FREE_PRICING,
    )

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(reviewer),
        purpose=ModelPurpose.REVIEW,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2364, output_tokens=61),
    )

    assert recorded.model_run.total_cost == Decimal(0)
    assert recorded.model_run.pricing_currency == "USD"
    assert recorded.model_run.has_known_cost


def test_a_model_without_pricing_records_unknown_and_not_zero(
    session: Session, run: TaskRun
) -> None:
    """An unpriced model is unpriced. Writing zero here would make every
    unconfigured installation look free."""
    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=unpriced_env_config(),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )

    stored = recorded.model_run
    assert stored.total_cost is None
    assert stored.input_cost is None
    assert stored.pricing_currency is None
    assert not stored.has_known_cost
    # The tokens are still recorded: what is unknown is the money, not the use.
    assert stored.input_tokens == 2098


def test_a_priced_model_with_no_reported_usage_is_unknown_cost(
    session: Session, run: TaskRun
) -> None:
    model = paid_model(session)

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=None,
    )

    stored = recorded.model_run
    assert stored.total_cost is None
    # The prices that applied are knowable even where the usage was not.
    assert stored.input_price_per_million == Decimal("1.25")
    assert stored.pricing_currency == "USD"


def test_changing_a_models_price_does_not_change_a_recorded_call(
    session: Session, run: TaskRun
) -> None:
    """The property the whole snapshot exists for."""
    model = paid_model(session)
    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )

    ModelRepository(session).update_fields(
        model.id,
        meta={
            "pricing": {
                "currency": "USD",
                "input_per_million_tokens": "99.00",
                "output_per_million_tokens": "99.00",
            }
        },
    )
    session.expire_all()

    unchanged = ModelRunRepository(session).get(recorded.model_run.id)
    assert unchanged is not None
    assert unchanged.input_price_per_million == Decimal("1.25")
    assert unchanged.total_cost == Decimal("0.0069925")
    # And a call made after the change is costed at the new price.
    after = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    )
    assert after.model_run.total_cost == Decimal("99.00")


def test_invalid_pricing_is_refused_at_registration(session: Session) -> None:
    """A typo is caught where an operator can still be told about it, rather
    than silently costing every later call as unknown."""
    with pytest.raises(EntityConflict):
        paid_model(
            session,
            metadata={"pricing": {"input_per_million_tokens": "free"}},
        )


def test_pricing_that_became_invalid_after_registration_is_unknown_not_fatal(
    session: Session, run: TaskRun
) -> None:
    """Recording happens after the endpoint has already answered. Refusing
    here would discard the audit row to report a configuration fault."""
    model = paid_model(session)
    ModelRepository(session).update_fields(model.id, meta={"pricing": "free"})
    session.expire_all()

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(ModelRepository(session).get(model.id)),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=10, output_tokens=10),
    )

    assert recorded.model_run.total_cost is None
    assert recorded.model_run.input_tokens == 10


# --- every purpose uses the same path ----------------------------------------


@pytest.mark.parametrize(
    "purpose",
    [ModelPurpose.PLAN, ModelPurpose.CODE, ModelPurpose.FIX, ModelPurpose.REVIEW],
)
def test_every_purpose_is_costed_by_the_shared_recording_path(
    session: Session, run: TaskRun, purpose: ModelPurpose
) -> None:
    model = paid_model(session)

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=purpose,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )

    assert recorded.model_run.purpose is purpose
    assert recorded.model_run.total_cost == Decimal("0.0069925")


# --- failed calls -------------------------------------------------------------


def test_a_failed_call_that_reported_usage_is_still_costed(
    session: Session, run: TaskRun
) -> None:
    """Status is not consulted when costing. An endpoint that charged for a
    prompt and then failed to produce a usable answer still charged."""
    model = paid_model(session)

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.FAILED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
        error_detail="ModelResponseInvalid: schema mismatch",
    )

    assert recorded.model_run.status is RunStatus.FAILED
    assert recorded.model_run.total_cost == Decimal("0.0069925")


def test_a_failed_call_without_usage_remains_unknown(
    session: Session, run: TaskRun
) -> None:
    """A timeout reports nothing, and nothing is what is recorded. No token
    count is fabricated to make the row costable."""
    model = paid_model(session)

    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(model),
        purpose=ModelPurpose.CODE,
        status=RunStatus.FAILED,
        duration_ms=600_000,
        error_detail="ReadTimeout: endpoint did not answer",
    )

    assert recorded.model_run.total_cost is None
    assert recorded.model_run.input_tokens is None


# --- aggregation --------------------------------------------------------------


def test_a_run_totals_the_costs_persisted_against_it(
    session: Session, run: TaskRun
) -> None:
    """The hybrid shape the orchestrator actually runs: a paid cloud coder and
    a declared-free local reviewer."""
    coder = paid_model(session)
    reviewer = register_model(
        session,
        provider="openai_compatible",
        model_name="qwen2.5-coder-32b",
        role=ModelRole.REVIEWER,
        endpoint="http://192.168.0.126:8080/v1",
        metadata=FREE_PRICING,
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(coder),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(reviewer),
        purpose=ModelPurpose.REVIEW,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2364, output_tokens=61),
    )

    summary = model_usage.usage_for_run(session, run.id)

    assert summary.model_calls == 2
    assert summary.input_tokens == 4462
    assert summary.output_tokens == 498
    assert summary.known_cost == Decimal("0.0069925")
    assert summary.has_unknown_cost is False
    assert summary.currency == "USD"


def test_a_run_breaks_its_usage_down_by_purpose_and_model(
    session: Session, run: TaskRun
) -> None:
    coder = paid_model(session)
    reviewer = register_model(
        session,
        provider="openai_compatible",
        model_name="qwen2.5-coder-32b",
        role=ModelRole.REVIEWER,
        endpoint="http://192.168.0.126:8080/v1",
        metadata=FREE_PRICING,
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(coder),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(reviewer),
        purpose=ModelPurpose.REVIEW,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2364, output_tokens=61),
    )

    summary = model_usage.usage_for_run(session, run.id)
    by_purpose = summary.by_purpose()

    assert by_purpose[ModelPurpose.CODE].known_cost == Decimal("0.0069925")
    assert by_purpose[ModelPurpose.REVIEW].known_cost == Decimal(0)
    assert summary.by_model()["gpt-5.3-codex"].input_tokens == 2098
    assert summary.by_model()["qwen2.5-coder-32b"].input_tokens == 2364
    code_call = next(
        call for call in summary.calls if call.purpose is ModelPurpose.CODE
    )
    assert code_call.model_name == "gpt-5.3-codex"


def test_a_run_with_an_unpriced_call_reports_no_complete_total(
    session: Session, run: TaskRun
) -> None:
    """The misleading answer this guards against is ``total_cost = 0.0069925``
    on a run where one of two calls was never priced at all."""
    coder = paid_model(session)
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(coder),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=unpriced_env_config(role=ModelRole.REVIEWER),
        purpose=ModelPurpose.REVIEW,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2364, output_tokens=61),
    )

    summary = model_usage.usage_for_run(session, run.id)

    assert summary.has_unknown_cost is True
    assert summary.known_cost == Decimal("0.0069925")
    assert summary.model_calls == 2
    # The unknown call's tokens are still counted; only its money is missing.
    assert summary.input_tokens == 4462


def test_calls_with_no_usage_at_all_are_counted_so_token_totals_are_honest(
    session: Session, run: TaskRun
) -> None:
    coder = paid_model(session)
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(coder),
        purpose=ModelPurpose.CODE,
        status=RunStatus.FAILED,
        error_detail="ReadTimeout",
    )

    summary = model_usage.usage_for_run(session, run.id)

    assert summary.calls_without_usage == 1
    assert summary.input_tokens == 0
    assert summary.has_unknown_cost is True


def test_unlike_currencies_are_grouped_rather_than_added(
    session: Session, run: TaskRun
) -> None:
    """No conversion happens: a rate needs a date, and inventing one would
    make an audit record into an estimate."""
    dollars = paid_model(session)
    euros = paid_model(
        session, name="euro-coder", role=ModelRole.PLANNER, metadata=EURO_PRICING
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(dollars),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(euros),
        purpose=ModelPurpose.PLAN,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    )

    summary = model_usage.usage_for_run(session, run.id)

    assert summary.mixed_currencies is True
    assert summary.currency is None
    assert summary.known_cost is None
    assert summary.known_cost_by_currency == {
        "USD": Decimal("1.25"),
        "EUR": Decimal("1.00"),
    }


def test_a_run_that_made_no_model_calls_totals_to_nothing_known(
    session: Session, run: TaskRun
) -> None:
    summary = model_usage.usage_for_run(session, run.id)

    assert summary.model_calls == 0
    assert summary.known_cost is None
    assert summary.has_unknown_cost is False


def test_a_project_totals_every_run_it_owns(session: Session, project) -> None:
    """Including the run that was retried: those calls were made and charged
    whatever the project went on to do."""
    coder = paid_model(session)
    for external_task_id in ("TS-078", "TS-079"):
        task_run = _run_for(session, project, external_task_id)
        record_model_call(
            session,
            task_run_id=task_run.id,
            config=config_for(coder),
            purpose=ModelPurpose.CODE,
            status=RunStatus.SUCCEEDED,
            usage=TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    summary = model_usage.usage_for_project(session, project.id)

    assert summary.model_calls == 2
    assert summary.known_cost == Decimal("22.50")
    assert summary.has_unknown_cost is False
