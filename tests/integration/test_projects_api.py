"""Project and task API (build.md section 39) and the Phase B exit condition."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.session import get_db
from apps.orchestrator.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    app = create_app()
    # Every request shares the test transaction, which is rolled back afterwards.
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def _register(client: TestClient, repository: Path) -> dict:
    response = client.post(
        "/projects",
        json={"name": "TraceStack", "repository_path": str(repository)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_manifest_import_yields_the_correct_next_task(
    client: TestClient, manifest_file: Path
):
    """Phase B exit condition, end to end over the API."""
    project = _register(client, manifest_file.parent)

    imported = client.post(f"/projects/{project['id']}/import-tasks")
    assert imported.status_code == 200, imported.text
    report = imported.json()
    assert report["created"] == ["TS-001", "TS-002"]
    assert report["ready"] == ["TS-001"]

    tasks = client.get(f"/projects/{project['id']}/tasks").json()
    assert [task["external_task_id"] for task in tasks] == ["TS-001", "TS-002"]
    assert [task["status"] for task in tasks] == ["READY", "PENDING"]

    next_task = client.get(f"/projects/{project['id']}/next-task").json()
    assert next_task["task"]["external_task_id"] == "TS-001"
    assert next_task["reason"] is None
    assert next_task["readiness"]["waiting"] == {"TS-002": ["TS-001"]}


def test_a_project_is_created_and_listed(client: TestClient, tmp_path: Path):
    created = _register(client, tmp_path)

    assert created["status"] == "REGISTERED"
    assert created["default_branch"] == "main"
    assert created["worker_profile"] == "node"

    listed = client.get("/projects").json()
    assert [project["id"] for project in listed] == [created["id"]]
    assert client.get(f"/projects/{created['id']}").json() == created


def test_a_duplicate_external_project_id_is_a_conflict(client: TestClient, tmp_path: Path):
    payload = {
        "name": "TraceStack",
        "repository_path": str(tmp_path),
        "external_project_id": "tracestack",
    }
    assert client.post("/projects", json=payload).status_code == 201
    conflict = client.post("/projects", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "EntityConflict"


def test_an_unknown_field_is_rejected(client: TestClient, tmp_path: Path):
    response = client.post(
        "/projects",
        json={"name": "X", "repository_path": str(tmp_path), "worker": "node"},
    )
    assert response.status_code == 422


def test_an_unknown_project_is_a_404(client: TestClient):
    response = client.get(f"/projects/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"] == "EntityNotFound"


def test_an_invalid_manifest_is_a_422(client: TestClient, tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "build.tasks.yaml").write_text("version: 1\ntasks: []\n", encoding="utf-8")
    project = _register(client, repository)

    response = client.post(f"/projects/{project['id']}/import-tasks")

    assert response.status_code == 422
    assert response.json()["error"] == "ManifestError"


def test_a_missing_manifest_is_a_422(client: TestClient, tmp_path: Path):
    project = _register(client, tmp_path)
    response = client.post(f"/projects/{project['id']}/import-tasks")
    assert response.status_code == 422
    assert "Manifest not found" in response.json()["detail"]


def test_a_manifest_path_can_be_given_explicitly(client: TestClient, manifest_file: Path):
    project = _register(client, Path("/nonexistent"))

    response = client.post(
        f"/projects/{project['id']}/import-tasks",
        json={"manifest_path": str(manifest_file)},
    )

    assert response.status_code == 200
    assert response.json()["created"] == ["TS-001", "TS-002"]


def test_tasks_can_be_filtered_by_status(client: TestClient, manifest_file: Path):
    project = _register(client, manifest_file.parent)
    client.post(f"/projects/{project['id']}/import-tasks")

    tasks = client.get(f"/projects/{project['id']}/tasks", params={"status": "READY"}).json()

    assert [task["external_task_id"] for task in tasks] == ["TS-001"]


def test_a_task_and_its_runs_are_exposed(client: TestClient, manifest_file: Path):
    project = _register(client, manifest_file.parent)
    client.post(f"/projects/{project['id']}/import-tasks")
    task = client.get(f"/projects/{project['id']}/tasks").json()[0]

    detail = client.get(f"/tasks/{task['id']}").json()
    assert detail["verify_commands"] == ["npm run compile", "npm test"]
    assert detail["limits"]["max_attempts"] == 3
    assert client.get(f"/tasks/{task['id']}/runs").json() == []


def test_pausing_a_project_stops_selection_and_resuming_restores_it(
    client: TestClient, manifest_file: Path
):
    project = _register(client, manifest_file.parent)
    client.post(f"/projects/{project['id']}/import-tasks")

    assert client.post(f"/projects/{project['id']}/pause").json()["status"] == "PAUSED"
    paused = client.get(f"/projects/{project['id']}/next-task").json()
    assert paused["task"] is None
    assert paused["reason"] == "PROJECT_NOT_RUNNABLE"

    assert client.post(f"/projects/{project['id']}/resume").json()["status"] == "RUNNING"
    resumed = client.get(f"/projects/{project['id']}/next-task").json()
    assert resumed["task"]["external_task_id"] == "TS-001"


def test_resuming_a_project_that_is_not_paused_is_a_conflict(client: TestClient, tmp_path: Path):
    project = _register(client, tmp_path)
    response = client.post(f"/projects/{project['id']}/resume")
    assert response.status_code == 409
