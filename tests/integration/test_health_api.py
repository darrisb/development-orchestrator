from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.orchestrator.config import get_settings
from apps.orchestrator.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(engine: Engine, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr("apps.orchestrator.services.health.get_engine", lambda: engine)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    # The worker-backend check probes the host's container runtime. These tests
    # are about the endpoint, not about whether this developer has Docker.
    monkeypatch.setenv("WORKER_BACKEND", "subprocess")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_health_reports_ok_when_components_are_up(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {component["name"] for component in body["components"]} == {
        "database",
        "artifact_root",
        "worker_backend",
    }


def test_health_returns_503_when_the_database_is_unreachable(client: TestClient, monkeypatch):
    from apps.orchestrator.db.session import create_db_engine

    broken = create_db_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("apps.orchestrator.services.health.get_engine", lambda: broken)
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    database = next(c for c in body["components"] if c["name"] == "database")
    assert database["healthy"] is False


def test_health_returns_503_when_the_database_is_reachable_but_unmigrated(
    client: TestClient, monkeypatch, tmp_path
):
    from apps.orchestrator.db.session import create_db_engine

    empty = create_db_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr("apps.orchestrator.services.health.get_engine", lambda: empty)

    response = client.get("/health")

    assert response.status_code == 503
    database = next(c for c in response.json()["components"] if c["name"] == "database")
    assert database["healthy"] is False
    assert "schema missing tables" in database["detail"]
    empty.dispose()


def test_health_reports_an_unusable_worker_backend(client: TestClient, monkeypatch):
    """Section 47: a host that cannot run a verification command should say so
    on /health, not on the first task."""
    monkeypatch.setenv("WORKER_BACKEND", "docker")
    monkeypatch.setenv("DOCKER_BINARY", "definitely-not-a-container-runtime")
    get_settings.cache_clear()

    response = client.get("/health")

    assert response.status_code == 503
    worker = next(c for c in response.json()["components"] if c["name"] == "worker_backend")
    assert worker["healthy"] is False
    assert "not on PATH" in worker["detail"]


def test_the_subprocess_backend_is_healthy_but_says_what_it_is(client: TestClient):
    response = client.get("/health")

    worker = next(c for c in response.json()["components"] if c["name"] == "worker_backend")
    assert worker["healthy"] is True
    assert "weaker isolation" in worker["detail"]


def test_lifespan_creates_the_artifact_directories(client: TestClient):
    settings = get_settings()
    for directory in (settings.runs_dir, settings.training_dir, settings.artifacts_dir):
        assert directory.is_dir()
