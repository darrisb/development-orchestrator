from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config.settings import Settings

# Import for the side effect of registering every table on Base.metadata.
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """SQLite by default; set TEST_DATABASE_URL to run against PostgreSQL."""
    configured = os.environ.get("TEST_DATABASE_URL")
    if configured:
        return configured
    db_path: Path = tmp_path_factory.mktemp("db") / "test.db"
    return f"sqlite:///{db_path}"


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_db_engine(database_url)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Each test runs in a transaction that is rolled back afterwards."""
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def manifest_document() -> dict:
    """The manifest from build.md section 5, trimmed to two tasks."""
    return {
        "version": 1,
        "project": {
            "id": "tracestack",
            "name": "TraceStack",
            "repository": "/workspace/tracestack",
            "default_branch": "main",
        },
        "runtime": {"worker_profile": "node", "max_parallel_tasks": 1},
        "model_policy": {
            "default_coder": "qwen-coder-14b",
            "high_complexity_coder": "qwen-coder-30b",
            "reviewer": "primary-reviewer",
        },
        "verification": {
            "build": ["npm run compile"],
            "lint": ["npm run lint"],
            "tests": ["npm test"],
            "milestone_interval": 5,
        },
        "protected_paths": [".git/**", ".env", "secrets/**"],
        "tasks": [
            {
                "id": "TS-001",
                "section": 1,
                "title": "Scaffold extension",
                "status": "pending",
                "complexity": "low",
                "depends_on": [],
                "limits": {
                    "max_attempts": 3,
                    "max_review_cycles": 3,
                    "max_runtime_minutes": 30,
                    "max_files_changed": 12,
                    "max_diff_lines": 1200,
                },
                "verify": ["npm run compile", "npm test"],
            },
            {
                "id": "TS-002",
                "section": 2,
                "title": "Navigation core",
                "status": "pending",
                "complexity": "medium",
                "depends_on": ["TS-001"],
                "verify": ["npm run compile", "npm test"],
            },
        ],
    }


@pytest.fixture
def manifest_file(tmp_path: Path, manifest_document: dict) -> Path:
    """``manifest_document`` written where a managed repository would keep it."""
    import yaml

    repository = tmp_path / "tracestack"
    repository.mkdir()
    manifest_document["project"]["repository"] = str(repository)
    path = repository / "build.tasks.yaml"
    path.write_text(yaml.safe_dump(manifest_document, sort_keys=False), encoding="utf-8")
    return path


# --- Git fixtures (build.md section 46: "Git fixture repository") ------------


def run_git(repository: Path, *args: str) -> str:
    """Run Git in ``repository`` for test setup only.

    Tests build fixture state with raw Git deliberately: asserting that
    ``GitService`` behaves correctly is worth little if the state it is
    asserted against was produced by the same code under test.
    """
    completed = subprocess.run(
        ("git", *args),
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@localhost",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@localhost",
            "LC_ALL": "C",
        },
    )
    return completed.stdout


@pytest.fixture
def git_settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's ``.env``.

    ``_env_file=None`` matters: a real ``.env`` with ``GIT_PUSH_ENABLED=true``
    would otherwise make the push-policy tests pass for the wrong reason.
    """
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
    )


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny repository on ``main`` with one commit, mirroring a managed project."""
    repository = tmp_path / "fixture-repo"
    repository.mkdir()
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    (repository / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repository / "src").mkdir()
    (repository / "src" / "app.js").write_text("export const answer = 41;\n", encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "Initial commit")
    return repository
