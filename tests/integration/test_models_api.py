"""Model configuration and connection testing over the API (phase E, items 3-4)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.session import get_db
from apps.orchestrator.domain.enums import ModelRole
from apps.orchestrator.main import create_app
from apps.orchestrator.providers import ProviderConfig, ProviderNotConfigured
from apps.orchestrator.services import model_providers as provider_service

pytestmark = pytest.mark.integration


@pytest.fixture
def client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DEFAULT_LOCAL_MODEL_BASE_URL", "http://model-host:8080/v1")
    monkeypatch.setenv("DEFAULT_LOCAL_MODEL", "qwen-coder-30b")
    monkeypatch.setenv("LOCAL_MODEL_CONTEXT_WINDOW", "32768")
    # No reviewer configured: the default installation has a coder only.
    monkeypatch.setenv("REVIEW_BASE_URL", "")
    monkeypatch.setenv("REVIEW_MODEL", "")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_the_environment_coder_is_listed_without_a_database_row(
    client: TestClient,
) -> None:
    body = client.get("/models").json()

    assert len(body) == 1
    assert body[0]["source"] == "environment"
    assert body[0]["model_name"] == "qwen-coder-30b"
    assert body[0]["context_window"] == 32768
    assert body[0]["authenticated"] is False


def test_a_registered_model_is_listed_ahead_of_the_environment(
    client: TestClient,
) -> None:
    """Registration is the explicit choice; the environment is the default."""
    created = client.post(
        "/models",
        json={
            "provider": "openai_compatible",
            "model_name": "qwen-coder-7b",
            "role": "coder",
            "endpoint": "http://other-host:8080/v1",
            "context_window": 16384,
        },
    )
    assert created.status_code == 201, created.text

    body = client.get("/models").json()

    assert [entry["source"] for entry in body] == ["database", "environment"]
    assert body[0]["model_id"] == created.json()["model_id"]


def test_registering_the_same_model_twice_conflicts(client: TestClient) -> None:
    payload = {
        "model_name": "qwen-coder-7b",
        "role": "coder",
        "endpoint": "http://other-host:8080/v1",
    }
    assert client.post("/models", json=payload).status_code == 201
    assert client.post("/models", json=payload).status_code == 409


def test_a_credential_is_never_stored_only_its_variable_name(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_REVIEWER_TOKEN", "sk-secret-value")
    created = client.post(
        "/models",
        json={
            "model_name": "remote-reviewer",
            "role": "reviewer",
            "endpoint": "https://reviewer.example/v1",
            "metadata": {"api_key_env": "MY_REVIEWER_TOKEN"},
        },
    )
    assert created.status_code == 201

    listed = client.get("/models").json()
    entry = next(item for item in listed if item["model_name"] == "remote-reviewer")

    assert entry["authenticated"] is True
    assert "sk-secret-value" not in listed.__repr__()

    stored = provider_service.get_model(session, UUID(created.json()["model_id"]))
    assert stored.metadata == {"api_key_env": "MY_REVIEWER_TOKEN"}


def test_an_unknown_provider_id_is_reported_as_unavailable(client: TestClient) -> None:
    """Probing a provider that does not exist must not look like a pass."""
    response = client.post("/models/connection-test?provider_id=env:nonexistent")

    assert response.status_code == 503
    assert response.json()["error"] == "ProviderNotConfigured"


def test_the_connection_test_reports_a_reachable_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_build_provider(config: ProviderConfig, **_kwargs):
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"data": [{"id": config.model_name}]})
        )
        from apps.orchestrator.providers import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            config,
            client=httpx.AsyncClient(transport=transport, base_url=config.base_url),
        )

    monkeypatch.setattr(provider_service, "build_provider", fake_build_provider)

    body = client.post("/models/connection-test").json()

    assert len(body) == 1
    assert body[0]["reachable"] is True
    assert body[0]["model_available"] is True
    assert body[0]["healthy"] is True


def test_a_disabled_provider_is_reported_not_probed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_config: ProviderConfig, **_kwargs):
        raise AssertionError("a disabled provider must never be contacted")

    monkeypatch.setattr(provider_service, "build_provider", explode)
    client.post(
        "/models",
        json={
            "model_name": "retired-model",
            "role": "coder",
            "endpoint": "http://gone:8080/v1",
            "enabled": False,
        },
    )

    reports = client.post(
        "/models/connection-test?provider_id="
        + client.get("/models").json()[0]["provider_id"]
    ).json()

    assert reports[0]["reachable"] is False
    assert reports[0]["detail"] == "Provider is disabled"


def test_resolving_a_role_with_nothing_configured_raises(session: Session) -> None:
    """No fallback: an unconfigured reviewer stops the workflow (principle 10)."""
    from apps.orchestrator.config.settings import Settings

    empty = Settings(_env_file=None, default_local_model="", review_base_url="")

    with pytest.raises(ProviderNotConfigured, match="role 'reviewer'"):
        provider_service.resolve_for_role(session, ModelRole.REVIEWER, settings=empty)
