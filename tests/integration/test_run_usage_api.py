"""The usage endpoints (concern 78).

``GET /runs/{run_id}/usage`` and ``GET /projects/{project_id}/usage`` are the
read-only view of what was spent. The property these tests are mostly about is
serialisation: a cost is a decimal *string* on the wire, because the values are
fractions of a cent and a client sums many of them, and a float would
reintroduce the error the persisted column exists to avoid.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.session import get_db
from apps.orchestrator.domain.enums import (
    ModelPurpose,
    ModelRole,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.main import create_app
from apps.orchestrator.providers import ProviderConfig, TokenUsage
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services.model_providers import register_model
from apps.orchestrator.services.model_runs import record_model_call
from apps.orchestrator.services.runs import create_run

pytestmark = pytest.mark.integration

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


@pytest.fixture
def client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def project(session: Session):
    return ProjectRepository(session).add(
        Project(name="TraceStack", repository_path="/workspace/tracestack")
    )


@pytest.fixture
def run(session: Session, project) -> TaskRun:
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(project_id=project.id, external_task_id="TS-078", title="Navigation")
    )
    tasks.transition(task.id, TaskStatus.READY)
    return create_run(session, task.id)


def config_for(model) -> ProviderConfig:
    return ProviderConfig(
        provider_id=str(model.id),
        base_url=model.endpoint,
        model_name=model.model_name,
        role=model.role,
    )


def record_hybrid_run(session: Session, run: TaskRun) -> None:
    """The shape a real smoke run has: paid cloud coder, free local reviewer."""
    coder = register_model(
        session,
        provider="openai_compatible",
        model_name="gpt-5.3-codex",
        role=ModelRole.CODER,
        endpoint="https://api.openai.com/v1",
        metadata=PAID_PRICING,
    )
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
        duration_ms=18_432,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
        attempt=1,
    )
    record_model_call(
        session,
        task_run_id=run.id,
        config=config_for(reviewer),
        purpose=ModelPurpose.REVIEW,
        status=RunStatus.SUCCEEDED,
        duration_ms=7_104,
        usage=TokenUsage(input_tokens=2364, output_tokens=61),
        attempt=1,
        review_cycle=1,
    )


def test_run_usage_reports_each_call_and_the_totals(
    client: TestClient, session: Session, run: TaskRun
) -> None:
    record_hybrid_run(session, run)

    response = client.get(f"/runs/{run.id}/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run.id)
    assert body["totals"] == {
        "model_calls": 2,
        "input_tokens": 4462,
        "output_tokens": 498,
        "calls_without_usage": 0,
        "known_cost": "0.006992500000",
        "currency": "USD",
        "has_unknown_cost": False,
        "mixed_currencies": False,
        "known_cost_by_currency": {"USD": "0.006992500000"},
    }
    code, review = body["calls"]
    assert code["purpose"] == ModelPurpose.CODE.value
    assert code["model"] == "gpt-5.3-codex"
    assert code["input_tokens"] == 2098
    assert code["output_tokens"] == 437
    assert code["duration_ms"] == 18_432
    assert Decimal(code["cost"]) == Decimal("0.0069925")
    assert review["purpose"] == ModelPurpose.REVIEW.value
    assert Decimal(review["cost"]) == Decimal(0)
    assert (
        body["by_purpose"][ModelPurpose.REVIEW.value]["known_cost"]
        == "0.000000000000"
    )


def test_a_cost_is_serialised_as_a_string_and_not_as_a_float(
    client: TestClient, session: Session, run: TaskRun
) -> None:
    """Read from the raw body, before any JSON number could have been parsed
    into a binary float on either side of the wire."""
    record_hybrid_run(session, run)

    raw = client.get(f"/runs/{run.id}/usage").text

    assert '"known_cost":"0.006992500000"' in raw.replace(", ", ",").replace(": ", ":")
    body = json.loads(raw, parse_float=Decimal)
    assert isinstance(body["totals"]["known_cost"], str)
    assert Decimal(body["totals"]["known_cost"]) == Decimal("0.0069925")


def test_an_unpriced_call_is_reported_as_null_and_flagged(
    client: TestClient, session: Session, run: TaskRun
) -> None:
    """``null`` is the wire form of UNKNOWN. It is not ``"0"``, and the totals
    say so rather than publishing a partial sum as a complete one."""
    record_model_call(
        session,
        task_run_id=run.id,
        config=ProviderConfig(
            provider_id="env:local-coder",
            base_url="http://192.168.0.126:8080/v1",
            model_name="qwen-coder-30b",
            role=ModelRole.CODER,
        ),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage(input_tokens=2098, output_tokens=437),
    )

    body = client.get(f"/runs/{run.id}/usage").json()

    assert body["calls"][0]["cost"] is None
    assert body["calls"][0]["currency"] is None
    assert body["totals"]["has_unknown_cost"] is True
    assert body["totals"]["known_cost"] is None


def test_usage_for_an_unknown_run_is_a_404_not_an_empty_report(
    client: TestClient,
) -> None:
    """An empty report would be indistinguishable from a real run that made no
    model calls."""
    assert client.get(f"/runs/{uuid4()}/usage").status_code == 404


def test_project_usage_covers_the_projects_runs(
    client: TestClient, session: Session, project, run: TaskRun
) -> None:
    record_hybrid_run(session, run)

    response = client.get(f"/projects/{project.id}/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == str(project.id)
    assert body["totals"]["model_calls"] == 2
    assert Decimal(body["totals"]["known_cost"]) == Decimal("0.0069925")
    assert body["totals"]["has_unknown_cost"] is False


def test_project_usage_for_an_unknown_project_is_a_404(client: TestClient) -> None:
    assert client.get(f"/projects/{uuid4()}/usage").status_code == 404
