"""Runtime database backup and verified restore support."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from apps.orchestrator import _build_meta
from apps.orchestrator.config import Settings, get_settings
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from tests.db_safety import (
    DatabaseIdentity,
    TestDatabaseSafetyError,
    assert_test_database_safe,
    create_test_database_if_not_exists,
    drop_test_database,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class BackupError(RuntimeError):
    """Backup or restore verification failed."""


@dataclass(frozen=True)
class BackupResult:
    backup_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def default_backup_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.artifact_root / "backups"


def backup_database(
    *,
    database_url: str | None = None,
    destination: Path | None = None,
    retention: int = 7,
    runner: Runner = subprocess.run,
    now: dt.datetime | None = None,
) -> BackupResult:
    """Create a read-only custom-format pg_dump and write metadata atomically."""
    settings = get_settings()
    database_url = database_url or settings.database_url
    destination = destination or default_backup_dir(settings)
    destination.mkdir(parents=True, exist_ok=True)

    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup_path = destination / f"orchestrator-{stamp}-{uuid.uuid4().hex[:8]}.dump"
    metadata_path = backup_path.with_suffix(".json")

    identity = DatabaseIdentity.from_url(database_url)
    alembic_revision = _read_alembic_revision(database_url)
    postgres_version = _read_postgres_version(database_url)
    source_counts = _table_counts(database_url)

    with tempfile.NamedTemporaryFile(
        prefix=f".{backup_path.name}.",
        suffix=".tmp",
        dir=destination,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    command = ["pg_dump", "--format=custom", "--file", str(tmp_path), _pg_url(database_url)]
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=_pg_env(database_url),
        )
    except FileNotFoundError as exc:
        _unlink_missing_ok(tmp_path)
        raise BackupError("pg_dump was not found on PATH") from exc

    if completed.returncode != 0:
        _unlink_missing_ok(tmp_path)
        raise BackupError(_command_failure("pg_dump failed", completed))

    if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        _unlink_missing_ok(tmp_path)
        raise BackupError("pg_dump produced an empty backup")

    checksum = _sha256_file(tmp_path)
    tmp_path.replace(backup_path)

    metadata = {
        "created_at": stamp,
        "verified_at": None,
        "source_database": _identity_payload(identity),
        "source_revision": _build_meta.SOURCE_REVISION,
        "source_dirty": _build_meta.SOURCE_DIRTY,
        "alembic_revision": alembic_revision,
        "postgres_version": postgres_version,
        "backup_filename": backup_path.name,
        "format": "postgresql-custom",
        "size_bytes": backup_path.stat().st_size,
        "sha256": checksum,
        "source_counts": source_counts,
    }
    _write_json_atomic(metadata_path, metadata)
    _apply_retention(destination, retain_verified=retention)
    return BackupResult(backup_path=backup_path, metadata_path=metadata_path, metadata=metadata)


def verify_restore(
    backup_path: Path,
    *,
    metadata_path: Path | None = None,
    server_url: str | None = None,
    runner: Runner = subprocess.run,
    keep_database: bool = False,
) -> dict[str, Any]:
    """Restore a backup into a disposable database and verify it is usable."""
    backup_path = backup_path.resolve()
    metadata_path = metadata_path or backup_path.with_suffix(".json")
    metadata = _load_metadata(metadata_path)

    expected_checksum = metadata.get("sha256")
    actual_checksum = _sha256_file(backup_path)
    if expected_checksum != actual_checksum:
        raise BackupError("backup checksum does not match metadata")

    server_url = server_url or _server_url_from_database_url(get_settings().database_url)
    verify_name = f"verify_{uuid.uuid4().hex[:12]}"
    verify_url = create_test_database_if_not_exists(server_url, verify_name)
    identity = assert_test_database_safe(verify_url, allow_sqlite=False)
    if identity.database == "orchestrator":
        raise BackupError("restore verification target resolved to runtime database")

    restored = False
    try:
        command = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            _pg_url(verify_url),
            str(backup_path),
        ]
        try:
            completed = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=_pg_env(verify_url),
            )
        except FileNotFoundError as exc:
            raise BackupError("pg_restore was not found on PATH") from exc
        if completed.returncode != 0:
            raise BackupError(_command_failure("pg_restore failed", completed))
        restored = True

        checks = _verify_restored_database(verify_url, metadata)
        verified_at = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        metadata["verified_at"] = verified_at
        metadata["verified_database"] = _identity_payload(identity)
        metadata["restore_checks"] = checks
        _write_json_atomic(metadata_path, metadata)
        _apply_retention(metadata_path.parent)
        return {
            "backup_path": str(backup_path),
            "metadata_path": str(metadata_path),
            "verification_database": identity.database,
            "restored": restored,
            "checks": checks,
        }
    finally:
        if not keep_database:
            drop_test_database(server_url, verify_name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up and verify Orchestrator PostgreSQL")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="create a runtime database backup")
    backup.add_argument("--destination", type=Path, default=None)
    backup.add_argument("--retain-verified", type=int, default=7)

    verify = sub.add_parser("verify-restore", help="verify a backup by restoring it")
    verify.add_argument("backup", type=Path)
    verify.add_argument("--metadata", type=Path, default=None)
    verify.add_argument("--server-url", default=None)
    verify.add_argument("--keep-database", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_database(
                destination=args.destination,
                retention=args.retain_verified,
            )
            print(json.dumps(_public_result(result), indent=2, sort_keys=True))
        else:
            result = verify_restore(
                args.backup,
                metadata_path=args.metadata,
                server_url=args.server_url,
                keep_database=args.keep_database,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
    except (BackupError, TestDatabaseSafetyError, SQLAlchemyError) as exc:
        print(f"backup error: {exc}", file=os.sys.stderr)
        return 1
    return 0


def _public_result(result: BackupResult) -> dict[str, Any]:
    return {
        "backup_path": str(result.backup_path),
        "metadata_path": str(result.metadata_path),
        "size_bytes": result.metadata["size_bytes"],
        "sha256": result.metadata["sha256"],
        "source_database": result.metadata["source_database"],
        "alembic_revision": result.metadata["alembic_revision"],
        "source_revision": result.metadata["source_revision"],
        "postgres_version": result.metadata["postgres_version"],
    }


def _pg_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    scheme = parsed.scheme.split("+", 1)[0]
    user = parsed.username or ""
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = f"{user}@" if user else ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit(SplitResult(scheme, f"{auth}{host}{port}", parsed.path, parsed.query, ""))


def _pg_env(database_url: str) -> dict[str, str]:
    parsed = urlsplit(database_url)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return env


def _identity_payload(identity: DatabaseIdentity) -> dict[str, Any]:
    return {
        "scheme": identity.scheme,
        "host": identity.host,
        "port": identity.port,
        "database": identity.database,
        "username": identity.username,
    }


def _read_alembic_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            if "alembic_version" not in inspect(connection).get_table_names():
                return None
            return connection.execute(text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()


def _read_postgres_version(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("select version()")).scalar_one())
    finally:
        engine.dispose()


def _table_counts(database_url: str) -> dict[str, int]:
    engine = create_engine(database_url)
    counts: dict[str, int] = {}
    try:
        with engine.connect() as connection:
            present = set(inspect(connection).get_table_names())
            for table in sorted(set(Base.metadata.tables) & present):
                count = connection.execute(text(f'select count(*) from "{table}"')).scalar_one()
                counts[table] = int(count)
    finally:
        engine.dispose()
    return counts


def _verify_restored_database(database_url: str, metadata: dict[str, Any]) -> dict[str, Any]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            present = set(inspect(connection).get_table_names())
            missing = sorted(set(Base.metadata.tables) - present)
            if missing:
                raise BackupError(f"restored schema missing tables: {', '.join(missing[:5])}")

            restored_revision = connection.execute(
                text("select version_num from alembic_version")
            ).scalar()
            expected_revision = metadata.get("alembic_revision")
            if restored_revision != expected_revision:
                raise BackupError(
                    f"restored Alembic revision {restored_revision!r} "
                    f"does not match backup {expected_revision!r}"
                )

            counts: dict[str, int] = {}
            for table in sorted(Base.metadata.tables):
                counts[table] = int(
                    connection.execute(text(f'select count(*) from "{table}"')).scalar_one()
                )
    finally:
        engine.dispose()

    expected_counts = metadata.get("source_counts") or {}
    count_mismatches = {
        table: {"backup": expected, "restore": counts.get(table)}
        for table, expected in expected_counts.items()
        if counts.get(table) != expected
    }
    if count_mismatches:
        raise BackupError(f"restored row counts differ: {count_mismatches}")

    return {
        "schema_tables": len(Base.metadata.tables),
        "alembic_revision": restored_revision,
        "row_counts": counts,
        "source_counts_compared": bool(expected_counts),
    }


def _server_url_from_database_url(database_url: str) -> str:
    url = make_url(database_url)
    return str(url.set(database="postgres").render_as_string(hide_password=False))


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BackupError(f"could not read backup metadata {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BackupError(f"backup metadata is not valid JSON: {path}") from exc


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_failure(prefix: str, completed: subprocess.CompletedProcess[str]) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    detail = stderr or stdout or f"exit code {completed.returncode}"
    return f"{prefix}: {detail}"


def _apply_retention(destination: Path, *, retain_verified: int = 7) -> None:
    if retain_verified < 1:
        retain_verified = 1
    metadata_files = sorted(destination.glob("*.json"), key=lambda path: path.stat().st_mtime)
    verified = [
        path for path in metadata_files if (_safe_load_json(path) or {}).get("verified_at")
    ]
    if len(verified) <= retain_verified:
        return
    for metadata_path in verified[: len(verified) - retain_verified]:
        remaining = [path for path in verified if path != metadata_path and path.exists()]
        if not remaining:
            continue
        backup_name = (_safe_load_json(metadata_path) or {}).get("backup_filename")
        backup_path = (
            metadata_path.with_name(backup_name)
            if backup_name
            else metadata_path.with_suffix(".dump")
        )
        _unlink_missing_ok(backup_path)
        _unlink_missing_ok(metadata_path)


def _safe_load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _unlink_missing_ok(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def expected_alembic_head() -> str:
    config = Config("alembic.ini")
    return ScriptDirectory.from_config(config).get_current_head()


def pg_tools_available() -> bool:
    return shutil.which("pg_dump") is not None and shutil.which("pg_restore") is not None
