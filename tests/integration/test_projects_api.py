"""Project and task API (build.md section 39) and the Phase B exit condition."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.session import get_db
from apps.orchestrator.main import create_app
from apps.orchestrator.services import projects as project_service
from apps.orchestrator.services.git_errors import (
    BranchAlreadyExists,
    DirtyWorktree,
    GitCommandTimeout,
    ProtectedBranch,
    WorktreeMissing,
)
from apps.orchestrator.services.scheduler import NoTaskReason, Selection

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


def test_run_project_closes_preflight_session_before_workflow(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from apps.orchestrator.api import projects as projects_api

    class RecordingSession(Session):
        instances: list[RecordingSession] = []

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.closed_for_test = False
            self.instances.append(self)

        def close(self) -> None:
            self.closed_for_test = True
            super().close()

    class Runner:
        async def run_next(self, project_id):
            observed["all_preflight_sessions_closed"] = all(
                session.closed_for_test for session in RecordingSession.instances
            )
            return Selection(reason=NoTaskReason.NO_TASKS), None

        async def aclose(self):
            observed["runner_closed"] = True

    observed: dict[str, bool] = {}
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        class_=RecordingSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        with factory.begin() as setup:
            project = project_service.create_project(
                setup, name="TraceStack", repository_path=str(tmp_path)
            )
            project_id = project.id

        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
        get_settings.cache_clear()
        monkeypatch.setattr(projects_api, "get_session_factory", lambda: factory)
        monkeypatch.setattr(
            projects_api.WorkflowRunner,
            "configured",
            staticmethod(lambda session_factory: Runner()),
        )
        app = create_app()
        with TestClient(app) as test_client:
            response = test_client.post(f"/projects/{project_id}/run")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "run_id": None,
            "outcome": None,
            "state": None,
            "no_task_reason": "NO_TASKS",
        }
        assert observed == {
            "all_preflight_sessions_closed": True,
            "runner_closed": True,
        }
    finally:
        get_settings.cache_clear()
        transaction.rollback()
        connection.close()


def test_run_project_preserves_project_not_found_before_workflow(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from apps.orchestrator.api import projects as projects_api

    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    configured_called = False

    def configured(session_factory):
        nonlocal configured_called
        configured_called = True
        raise AssertionError("runner should not be built for an unknown project")

    try:
        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
        get_settings.cache_clear()
        monkeypatch.setattr(projects_api, "get_session_factory", lambda: factory)
        monkeypatch.setattr(
            projects_api.WorkflowRunner, "configured", staticmethod(configured)
        )
        app = create_app()
        with TestClient(app) as test_client:
            response = test_client.post(f"/projects/{uuid4()}/run")

        assert response.status_code == 404
        assert response.json()["error"] == "EntityNotFound"
        assert configured_called is False
    finally:
        get_settings.cache_clear()
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (DirtyWorktree("/repo", ("uncommitted.txt",)), 409),
        (WorktreeMissing("the run's worktree is gone"), 409),
        (ProtectedBranch("main", "write"), 409),
        (GitCommandTimeout(("git", "fetch"), 120.0), 503),
        # Deliberately not translated: after concern 59 a branch collision means
        # a run identity is wrong, and a bug should arrive as a bug rather than
        # as a tidy 409 that reads like an operator problem.
        (BranchAlreadyExists("agent/TS-001-x-run1"), None),
    ],
)
def test_git_preparation_failures_are_classified_rather_than_opaque(
    error: Exception, expected_status: int | None, tmp_path: Path, monkeypatch
):
    """Concern 59's second half. The branch collision surfaced as an
    unclassified 500, which is what made it slow to diagnose: the taxonomy in
    ``services.git_errors`` already distinguishes an operator problem from a
    bug, and the API was not reading it.

    Driven through the real app and its registered handlers rather than by
    reading the table, and it asserts the *absence* of a mapping too -- that is
    the half that stops this from becoming a blanket ``GitError`` catch.
    """
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    app = create_app()

    @app.get("/_raise_for_test")
    def _raise() -> None:
        raise error

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_raise_for_test")

    if expected_status is None:
        assert response.status_code == 500
    else:
        assert response.status_code == expected_status, response.text
        assert response.json()["error"] == type(error).__name__
    get_settings.cache_clear()
