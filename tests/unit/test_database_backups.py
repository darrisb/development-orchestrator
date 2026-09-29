from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.services import backups
from apps.orchestrator.services.backups import BackupError
from tests.db_safety import TestDatabaseSafetyError

RUNTIME_URL = "postgresql+psycopg://user:secret@localhost:5432/orchestrator"
TEST_URL = "postgresql+psycopg://user:secret@localhost:5432/orchestrator_test"


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, database_url=RUNTIME_URL, artifact_root=tmp_path)


def _ok_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if command[0] == "pg_dump":
        Path(command[command.index("--file") + 1]).write_bytes(b"backup")
    return subprocess.CompletedProcess(command, 0, "", "")


def test_backup_targets_configured_runtime_database(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _ok_runner(command, **kwargs)

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(backups, "_read_alembic_revision", lambda _url: "head")
    monkeypatch.setattr(backups, "_read_postgres_version", lambda _url: "PostgreSQL 16")
    monkeypatch.setattr(backups, "_table_counts", lambda _url: {"tasks": 0})

    result = backups.backup_database(runner=runner, now=dt.datetime(2026, 1, 2, tzinfo=dt.UTC))

    assert commands[0][-1].endswith("/orchestrator")
    assert result.metadata["source_database"]["database"] == "orchestrator"
    assert result.metadata["source_counts"] == {"tasks": 0}


def test_password_is_not_exposed_in_pg_dump_command(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return _ok_runner(command, **kwargs)

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(backups, "_read_alembic_revision", lambda _url: "head")
    monkeypatch.setattr(backups, "_read_postgres_version", lambda _url: "PostgreSQL 16")
    monkeypatch.setattr(backups, "_table_counts", lambda _url: {})

    backups.backup_database(runner=runner)

    assert "secret" not in " ".join(seen["command"])
    assert seen["env"]["PGPASSWORD"] == "secret"


def test_failed_pg_dump_produces_no_success_record(monkeypatch, tmp_path):
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(command[command.index("--file") + 1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, "", "boom")

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(backups, "_read_alembic_revision", lambda _url: "head")
    monkeypatch.setattr(backups, "_read_postgres_version", lambda _url: "PostgreSQL 16")
    monkeypatch.setattr(backups, "_table_counts", lambda _url: {})

    with pytest.raises(BackupError, match="pg_dump failed"):
        backups.backup_database(runner=runner)

    assert list((tmp_path / "backups").glob("*")) == []


def test_zero_output_rejected(monkeypatch, tmp_path):
    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(command[command.index("--file") + 1]).write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(backups, "_read_alembic_revision", lambda _url: "head")
    monkeypatch.setattr(backups, "_read_postgres_version", lambda _url: "PostgreSQL 16")
    monkeypatch.setattr(backups, "_table_counts", lambda _url: {})

    with pytest.raises(BackupError, match="empty backup"):
        backups.backup_database(runner=runner)


def test_checksum_and_metadata_are_written(monkeypatch, tmp_path):
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(backups, "_read_alembic_revision", lambda _url: "abc123")
    monkeypatch.setattr(backups, "_read_postgres_version", lambda _url: "PostgreSQL 16")
    monkeypatch.setattr(backups, "_table_counts", lambda _url: {"projects": 2})
    monkeypatch.setattr(backups._build_meta, "SOURCE_REVISION", "source-sha")

    result = backups.backup_database(runner=_ok_runner)
    metadata = json.loads(result.metadata_path.read_text())

    assert len(metadata["sha256"]) == 64
    assert metadata["source_database"]["database"] == "orchestrator"
    assert metadata["alembic_revision"] == "abc123"
    assert metadata["source_revision"] == "source-sha"


def test_verify_restore_uses_isolated_database(monkeypatch, tmp_path):
    backup, metadata = _backup_pair(tmp_path, verified=False)
    created: list[str] = []
    dropped: list[str] = []

    def create(_server: str, name: str) -> str:
        created.append(name)
        return f"postgresql+psycopg://u:p@localhost:5432/{name}"

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(backups, "create_test_database_if_not_exists", create)
    monkeypatch.setattr(backups, "drop_test_database", lambda _server, name: dropped.append(name))
    monkeypatch.setattr(backups, "_verify_restored_database", lambda _url, _meta: {"ok": True})

    result = backups.verify_restore(backup, metadata_path=metadata, runner=_ok_runner)

    assert created[0].startswith("verify_")
    assert result["verification_database"].startswith("verify_")
    assert dropped == created


def test_verify_restore_refuses_runtime_database(monkeypatch, tmp_path):
    backup, metadata = _backup_pair(tmp_path, verified=False)

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(
        backups,
        "create_test_database_if_not_exists",
        lambda _server, _name: RUNTIME_URL,
    )

    with pytest.raises(TestDatabaseSafetyError, match="runtime database"):
        backups.verify_restore(backup, metadata_path=metadata, runner=_ok_runner)


def test_restore_failure_reported_and_cleanup_guarded(monkeypatch, tmp_path):
    backup, metadata = _backup_pair(tmp_path, verified=False)
    dropped: list[str] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "restore failed")

    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(
        backups,
        "create_test_database_if_not_exists",
        lambda _server, name: f"postgresql+psycopg://u:p@localhost:5432/{name}",
    )
    monkeypatch.setattr(backups, "drop_test_database", lambda _server, name: dropped.append(name))

    with pytest.raises(BackupError, match="pg_restore failed"):
        backups.verify_restore(backup, metadata_path=metadata, runner=runner)

    assert dropped and dropped[0].startswith("verify_")


def test_schema_verification_detects_missing_tables(monkeypatch):
    monkeypatch.setattr(backups, "create_engine", lambda _url: _FakeEngine(tables=set()))
    monkeypatch.setattr(backups, "inspect", lambda connection: _FakeInspector(connection.tables))

    with pytest.raises(BackupError, match="missing tables"):
        backups._verify_restored_database(TEST_URL, {"alembic_revision": "head"})


def test_alembic_mismatch_detected(monkeypatch):
    monkeypatch.setattr(
        backups,
        "create_engine",
        lambda _url: _FakeEngine(tables=set(backups.Base.metadata.tables), revision="wrong"),
    )
    monkeypatch.setattr(backups, "inspect", lambda connection: _FakeInspector(connection.tables))

    with pytest.raises(BackupError, match="does not match"):
        backups._verify_restored_database(TEST_URL, {"alembic_revision": "head"})


def test_checksum_mismatch_rejected_before_restore(monkeypatch, tmp_path):
    backup, metadata = _backup_pair(tmp_path, verified=False)
    backup.write_bytes(b"changed")
    monkeypatch.setattr(backups, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(
        backups,
        "create_test_database_if_not_exists",
        lambda _server, name: f"postgresql+psycopg://u:p@localhost:5432/{name}",
    )
    monkeypatch.setattr(backups, "drop_test_database", lambda _server, _name: None)
    monkeypatch.setattr(backups, "_verify_restored_database", lambda _url, _meta: {"ok": True})

    with pytest.raises(BackupError, match="checksum"):
        backups.verify_restore(backup, metadata_path=metadata, runner=_ok_runner)


def test_retention_does_not_delete_last_verified_backup(tmp_path):
    backup, metadata = _backup_pair(tmp_path, verified=True, name="only")

    backups._apply_retention(tmp_path, retain_verified=1)

    assert backup.exists()
    assert metadata.exists()


def test_unverified_backup_does_not_evict_known_good(tmp_path):
    good_backup, good_metadata = _backup_pair(tmp_path, verified=True, name="good")
    new_backup, new_metadata = _backup_pair(tmp_path, verified=False, name="new")

    backups._apply_retention(tmp_path, retain_verified=1)

    assert good_backup.exists()
    assert good_metadata.exists()
    assert new_backup.exists()
    assert new_metadata.exists()


def _backup_pair(tmp_path: Path, *, verified: bool, name: str = "backup") -> tuple[Path, Path]:
    backup = tmp_path / f"{name}.dump"
    backup.write_bytes(b"backup")
    metadata = tmp_path / f"{name}.json"
    payload = {
        "backup_filename": backup.name,
        "sha256": backups._sha256_file(backup),
        "alembic_revision": "head",
        "source_counts": {},
        "verified_at": "20260101T000000Z" if verified else None,
    }
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    return backup, metadata


class _FakeEngine:
    def __init__(self, *, tables: set[str], revision: str = "head") -> None:
        self.tables = tables
        self.revision = revision

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self.tables, self.revision)

    def dispose(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, tables: set[str], revision: str) -> None:
        self.tables = tables
        self.revision = revision

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeScalar:
        sql = str(statement)
        if "alembic_version" in sql:
            return _FakeScalar(self.revision)
        return _FakeScalar(0)


class _FakeInspector:
    def __init__(self, tables: set[str]) -> None:
        self.tables = tables

    def get_table_names(self) -> list[str]:
        return list(self.tables)


class _FakeScalar:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value
