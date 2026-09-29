"""Concern 74: Fail-closed test database isolation.

The historical orchestrator database was lost because the test harness could
destructively operate on the runtime database. This module provides a
centralized safety validator that must be called before ANY destructive
database operation (drop_all, DROP DATABASE, schema resets, etc.).

The validator fails closed: if it cannot positively establish that the target
is an isolated test database, it raises a fatal error and performs ZERO
destructive SQL.

Safety contract:
- TEST_DATABASE_URL must be explicitly set for PostgreSQL testing
- TEST_DATABASE_URL must NOT equal DATABASE_URL (runtime database)
- TEST_DATABASE_URL must NOT target the production 'orchestrator' database
- TEST_DATABASE_URL must target a database explicitly marked for testing
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse


class TestDatabaseSafetyError(Exception):
    """Raised when a test database target cannot be verified as safe."""


@dataclass(frozen=True)
class DatabaseIdentity:
    """Normalized database connection identity."""

    scheme: str
    host: str
    port: int | None
    database: str
    username: str | None

    @classmethod
    def from_url(cls, url: str) -> DatabaseIdentity:
        """Parse a database URL into its normalized identity.

        Raises ValueError if the URL cannot be parsed.
        """
        parsed = urlparse(url)
        if not parsed.scheme:
            raise ValueError(f"URL missing scheme: {url}")

        scheme = parsed.scheme.lower()
        host = _normalize_host(parsed.hostname or "")
        port = parsed.port
        database = parsed.path.lstrip("/") if parsed.path else ""
        username = parsed.username

        return cls(
            scheme=scheme,
            host=host,
            port=port,
            database=database.lower(),
            username=username,
        )

    def is_sqlite(self) -> bool:
        """Return True if this is a SQLite database (file-based, isolated)."""
        return self.scheme.startswith("sqlite")

    def is_postgresql(self) -> bool:
        """Return True if this is a PostgreSQL database."""
        return self.scheme.startswith("postgresql")


_RUNTIME_DATABASE_NAMES = frozenset({
    "orchestrator",
    "orchestrator_dev",
    "orchestrator_prod",
    "orchestrator_production",
})


def _normalize_host(host: str) -> str:
    """Normalize hostname for comparison (localhost variants)."""
    if not host:
        return ""
    host = host.lower().strip()
    # Remove brackets from IPv6 addresses
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    # Normalize localhost variants
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return "localhost"
    return host


def _hosts_match(host1: str, host2: str) -> bool:
    """Check if two hosts refer to the same location."""
    return _normalize_host(host1) == _normalize_host(host2)


def assert_test_database_safe(
    test_url: str,
    *,
    runtime_url: str | None = None,
    allow_sqlite: bool = True,
) -> DatabaseIdentity:
    """Assert that a test database URL is safe for destructive operations.

    This function MUST be called before any destructive database operation
    (drop_all, DROP DATABASE, schema reset, etc.).

    Args:
        test_url: The TEST_DATABASE_URL to validate.
        runtime_url: The runtime DATABASE_URL to compare against.
            If None, reads from environment variable DATABASE_URL.
        allow_sqlite: Whether to allow SQLite databases (default True).
            SQLite is file-based and inherently isolated.

    Returns:
        The validated DatabaseIdentity if safe.

    Raises:
        TestDatabaseSafetyError: If the target cannot be verified as safe.
    """
    if not test_url:
        raise TestDatabaseSafetyError(
            "TEST_DATABASE_URL is empty or not set. "
            "Refusing to perform destructive operations without an explicit test target."
        )

    try:
        test_identity = DatabaseIdentity.from_url(test_url)
    except ValueError as exc:
        raise TestDatabaseSafetyError(
            f"Cannot parse TEST_DATABASE_URL: {exc}. "
            "Refusing to perform destructive operations on an unparseable URL."
        ) from exc

    if test_identity.is_sqlite():
        if allow_sqlite:
            return test_identity
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL targets SQLite ({test_url}), but SQLite is not allowed. "
            "Set allow_sqlite=True if this is intentional."
        )

    if not test_identity.is_postgresql():
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL has unsupported scheme: {test_identity.scheme}. "
            "Only PostgreSQL and SQLite are supported for testing."
        )

    if not test_identity.database:
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL has no database name: {test_url}. "
            "Refusing to perform destructive operations without a specific database target."
        )

    if test_identity.database in _RUNTIME_DATABASE_NAMES:
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL targets runtime database '{test_identity.database}': {test_url}. "
            "Refusing to perform destructive operations on the runtime database. "
            "Use a dedicated test database like 'orchestrator_test'."
        )

    if not re.match(r"^[a-z_][a-z0-9_]*$", test_identity.database):
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL database name '{test_identity.database}' does not match "
            "expected pattern for test databases (lowercase alphanumeric and underscores). "
            "Refusing to perform destructive operations on a suspicious database name."
        )

    # Accept test databases with clear naming conventions:
    # - test_* (explicit test prefix)
    # - orchestrator_test (dedicated test database)
    # - race_*, lock_*, recover_* (scratch databases created by integration tests)
    # - c64_*, c65_*, c67_*, c68_* (concern-specific scratch databases)
    # These patterns are used by the test harness for isolated scratch databases.
    safe_prefixes = (
        "test_",
        "orchestrator_test",
        "race_",
        "lock_",
        "recover_",
        "settle_",
        "verify_",
        "c64_",
        "c65_",
        "c67_",
        "c68_",
    )
    if not test_identity.database.startswith(safe_prefixes):
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL database name '{test_identity.database}' does not start with "
            f"one of the safe prefixes: {safe_prefixes}. "
            "Test databases must have a clear naming convention to distinguish them from "
            "runtime databases. Use 'orchestrator_test' or a name starting with 'test_'."
        )

    if runtime_url is None:
        runtime_url = os.environ.get("DATABASE_URL", "")

    if runtime_url:
        try:
            runtime_identity = DatabaseIdentity.from_url(runtime_url)
        except ValueError:
            runtime_identity = None

        if runtime_identity is not None:
            if (
                _hosts_match(test_identity.host, runtime_identity.host)
                and test_identity.port == runtime_identity.port
                and test_identity.database == runtime_identity.database
            ):
                raise TestDatabaseSafetyError(
                    f"TEST_DATABASE_URL ({test_url}) equals runtime DATABASE_URL "
                    f"({runtime_url}). "
                    "Refusing to perform destructive operations on the runtime database. "
                    "Set TEST_DATABASE_URL to a dedicated test database."
                )

            if (
                _hosts_match(test_identity.host, runtime_identity.host)
                and test_identity.port == runtime_identity.port
                and test_identity.database == runtime_identity.database
                and test_identity.username == runtime_identity.username
            ):
                raise TestDatabaseSafetyError(
                    f"TEST_DATABASE_URL ({test_url}) targets the same database as "
                    f"runtime DATABASE_URL ({runtime_url}). "
                    "Different credentials do not make the same database safe for testing."
                )

    test_marker = os.environ.get("ORCHESTRATOR_TEST_DATABASE")
    if test_marker and test_marker != test_identity.database:
        raise TestDatabaseSafetyError(
            f"TEST_DATABASE_URL database '{test_identity.database}' does not match "
            f"ORCHESTRATOR_TEST_DATABASE marker '{test_marker}'. "
            "The test database must match the explicitly approved marker."
        )

    return test_identity


def drop_all_tables_for_test(engine, database_url: str | None = None) -> None:
    """Drop all mapped tables after revalidating the actual teardown target."""
    from apps.orchestrator.db.base import Base

    if database_url is None:
        database_url = engine.url.render_as_string(hide_password=False)
    assert_test_database_safe(database_url)
    Base.metadata.drop_all(engine)


def get_safe_test_database_url(
    *,
    default_name: str = "orchestrator_test",
    host: str = "localhost",
    port: int = 5432,
    username: str = "orchestrator",
    password: str = "orchestrator",
) -> str:
    """Generate a safe test database URL.

    This function returns a URL for a dedicated test database that is
    guaranteed to pass the safety validator.

    Args:
        default_name: The database name to use (default: orchestrator_test).
        host: PostgreSQL host (default: localhost).
        port: PostgreSQL port (default: 5432).
        username: PostgreSQL username (default: orchestrator).
        password: PostgreSQL password (default: orchestrator).

    Returns:
        A PostgreSQL URL for the test database.
    """
    return f"postgresql+psycopg://{username}:{password}@{host}:{port}/{default_name}"


def create_test_database_if_not_exists(
    server_url: str,
    database_name: str,
    *,
    safety_check: bool = True,
) -> str:
    """Create a test database if it does not exist.

    This function uses an administrative connection to create a database.
    It validates that the target database name is safe before creation.

    Args:
        server_url: PostgreSQL server URL (without database name, e.g.,
            postgresql+psycopg://user:pass@host:port/postgres).
        database_name: Name of the database to create.
        safety_check: Whether to validate the database name (default True).

    Returns:
        The full URL to the created database.

    Raises:
        TestDatabaseSafetyError: If the database name is not safe.
    """
    from sqlalchemy import create_engine, text

    if safety_check:
        test_url = server_url.rsplit("/", 1)[0] + f"/{database_name}"
        assert_test_database_safe(test_url)

    if not re.match(r"^[a-z_][a-z0-9_]*$", database_name):
        raise TestDatabaseSafetyError(
            f"Database name '{database_name}' does not match expected pattern. "
            "Only lowercase alphanumeric and underscores are allowed."
        )

    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            )
            if not result.scalar():
                conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        admin.dispose()

    return server_url.rsplit("/", 1)[0] + f"/{database_name}"


def drop_test_database(
    server_url: str,
    database_name: str,
    *,
    safety_check: bool = True,
) -> None:
    """Drop a test database.

    This function uses an administrative connection to drop a database.
    It validates that the target database name is safe before dropping.

    Args:
        server_url: PostgreSQL server URL (without database name).
        database_name: Name of the database to drop.
        safety_check: Whether to validate the database name (default True).

    Raises:
        TestDatabaseSafetyError: If the database name is not safe.
    """
    from sqlalchemy import create_engine, text

    if safety_check:
        test_url = server_url.rsplit("/", 1)[0] + f"/{database_name}"
        assert_test_database_safe(test_url)

    if not re.match(r"^[a-z_][a-z0-9_]*$", database_name):
        raise TestDatabaseSafetyError(
            f"Database name '{database_name}' does not match expected pattern."
        )

    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name"
                ),
                {"name": database_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        admin.dispose()
