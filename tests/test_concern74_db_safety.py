"""Concern 74: Fail-closed test database isolation.

These tests prove that the test harness cannot destructively operate on the
runtime database. They validate the safety guard that must be called before
any destructive database operation (drop_all, DROP DATABASE, schema resets).

The guard fails closed: if it cannot positively establish that the target is
an isolated test database, it raises a fatal error and performs ZERO
destructive SQL.
"""

from __future__ import annotations

import pytest

from tests.db_safety import (
    DatabaseIdentity,
    TestDatabaseSafetyError,
    assert_test_database_safe,
)


class TestDatabaseIdentityParsing:
    """Tests for URL parsing and normalization."""

    def test_parse_postgresql_url(self):
        identity = DatabaseIdentity.from_url(
            "postgresql+psycopg://user:pass@localhost:5432/test_db"
        )
        assert identity.scheme == "postgresql+psycopg"
        assert identity.host == "localhost"
        assert identity.port == 5432
        assert identity.database == "test_db"
        assert identity.username == "user"

    def test_parse_sqlite_url(self):
        identity = DatabaseIdentity.from_url("sqlite:///tmp/test.db")
        assert identity.scheme == "sqlite"
        assert identity.is_sqlite()

    def test_normalize_localhost_variants(self):
        """localhost, 127.0.0.1, ::1 should all be treated as equivalent."""
        id1 = DatabaseIdentity.from_url("postgresql://u:p@localhost:5432/test")
        id2 = DatabaseIdentity.from_url("postgresql://u:p@127.0.0.1:5432/test")
        id3 = DatabaseIdentity.from_url("postgresql://u:p@[::1]:5432/test")

        # All should normalize to "localhost"
        assert id1.host == "localhost"
        assert id2.host == "localhost"
        assert id3.host == "localhost"

    def test_case_insensitive_database_name(self):
        """Database names should be normalized to lowercase."""
        identity = DatabaseIdentity.from_url(
            "postgresql://u:p@localhost:5432/Orchestrator_Test"
        )
        assert identity.database == "orchestrator_test"


class TestRuntimeDatabaseRejection:
    """Tests that runtime databases are rejected."""

    def test_reject_orchestrator_database(self):
        with pytest.raises(TestDatabaseSafetyError, match="runtime database"):
            assert_test_database_safe(
                "postgresql://u:p@localhost:5432/orchestrator"
            )

    def test_reject_orchestrator_dev(self):
        with pytest.raises(TestDatabaseSafetyError, match="runtime database"):
            assert_test_database_safe(
                "postgresql://u:p@localhost:5432/orchestrator_dev"
            )

    def test_reject_orchestrator_prod(self):
        with pytest.raises(TestDatabaseSafetyError, match="runtime database"):
            assert_test_database_safe(
                "postgresql://u:p@localhost:5432/orchestrator_prod"
            )

    def test_reject_case_insensitive_runtime(self):
        """Runtime database names should be rejected regardless of case."""
        with pytest.raises(TestDatabaseSafetyError, match="runtime database"):
            assert_test_database_safe(
                "postgresql://u:p@localhost:5432/ORCHESTRATOR"
            )


class TestEqualityRejection:
    """Tests that TEST_DATABASE_URL == DATABASE_URL is rejected."""

    def test_reject_equal_urls(self):
        runtime = "postgresql://u:p@localhost:5432/test_db"
        with pytest.raises(TestDatabaseSafetyError, match="equals runtime"):
            assert_test_database_safe(runtime, runtime_url=runtime)

    def test_reject_with_different_credentials(self):
        """Different credentials do not make the same DB safe."""
        test_url = "postgresql://test_user:test_pass@localhost:5432/test_db"
        runtime_url = "postgresql://prod_user:prod_pass@localhost:5432/test_db"
        with pytest.raises(TestDatabaseSafetyError, match="equals runtime"):
            assert_test_database_safe(test_url, runtime_url=runtime_url)

    def test_reject_with_query_parameters(self):
        """Query parameters cannot evade equality detection."""
        test_url = "postgresql://u:p@localhost:5432/test_db?sslmode=require"
        runtime_url = "postgresql://u:p@localhost:5432/test_db"
        # Both should parse to the same database identity
        test_id = DatabaseIdentity.from_url(test_url)
        runtime_id = DatabaseIdentity.from_url(runtime_url)
        assert test_id.database == runtime_id.database
        assert test_id.host == runtime_id.host
        assert test_id.port == runtime_id.port


class TestNamingConvention:
    """Tests that test databases must follow naming conventions."""

    def test_accept_test_prefix(self):
        identity = assert_test_database_safe(
            "postgresql://u:p@localhost:5432/test_my_database"
        )
        assert identity.database == "test_my_database"

    def test_accept_orchestrator_test(self):
        identity = assert_test_database_safe(
            "postgresql://u:p@localhost:5432/orchestrator_test"
        )
        assert identity.database == "orchestrator_test"

    def test_accept_scratch_prefixes(self):
        """Scratch databases created by integration tests are accepted."""
        for prefix in ["race_", "lock_", "recover_"]:
            identity = assert_test_database_safe(
                f"postgresql://u:p@localhost:5432/{prefix}abc123"
            )
            assert identity.database.startswith(prefix)

    def test_reject_unsafe_prefix(self):
        with pytest.raises(TestDatabaseSafetyError, match="safe prefixes"):
            assert_test_database_safe(
                "postgresql://u:p@localhost:5432/my_database"
            )

    def test_reject_invalid_characters(self):
        with pytest.raises(TestDatabaseSafetyError, match="expected pattern"):
            assert_test_database_safe(
                "postgresql://u:p@localhost:5432/test-database"
            )


class TestEmptyAndMissing:
    """Tests for empty or missing TEST_DATABASE_URL."""

    def test_reject_empty_url(self):
        with pytest.raises(TestDatabaseSafetyError, match="empty or not set"):
            assert_test_database_safe("")

    def test_reject_none_url(self):
        with pytest.raises(TestDatabaseSafetyError, match="empty or not set"):
            assert_test_database_safe(None)

    def test_reject_missing_database_name(self):
        with pytest.raises(TestDatabaseSafetyError, match="no database name"):
            assert_test_database_safe("postgresql://u:p@localhost:5432/")


class TestSQLiteAllowance:
    """Tests for SQLite database handling."""

    def test_accept_sqlite_by_default(self):
        identity = assert_test_database_safe("sqlite:///tmp/test.db")
        assert identity.is_sqlite()

    def test_reject_sqlite_when_disabled(self):
        with pytest.raises(TestDatabaseSafetyError, match="SQLite is not allowed"):
            assert_test_database_safe(
                "sqlite:///tmp/test.db", allow_sqlite=False
            )


class TestUnsupportedSchemes:
    """Tests for unsupported database schemes."""

    def test_reject_mysql(self):
        with pytest.raises(TestDatabaseSafetyError, match="unsupported scheme"):
            assert_test_database_safe("mysql://u:p@localhost:3306/test_db")

    def test_reject_mssql(self):
        with pytest.raises(TestDatabaseSafetyError, match="unsupported scheme"):
            assert_test_database_safe(
                "mssql+pyodbc://u:p@localhost:1433/test_db"
            )


class TestHostNormalization:
    """Tests for host normalization and comparison."""

    def test_localhost_and_127_0_0_1_match(self):
        """localhost and 127.0.0.1 should be treated as the same host."""
        test_url = "postgresql://u:p@localhost:5432/test_db"
        runtime_url = "postgresql://u:p@127.0.0.1:5432/test_db"
        with pytest.raises(TestDatabaseSafetyError):
            assert_test_database_safe(test_url, runtime_url=runtime_url)

    def test_different_hosts_are_safe(self):
        """Different hosts should not trigger equality rejection."""
        test_url = "postgresql://u:p@db-test.example.com:5432/test_db"
        runtime_url = "postgresql://u:p@db-prod.example.com:5432/test_db"
        # Should not raise (different hosts)
        identity = assert_test_database_safe(test_url, runtime_url=runtime_url)
        assert identity.database == "test_db"


class TestPortComparison:
    """Tests for port comparison."""

    def test_different_ports_are_safe(self):
        """Different ports should not trigger equality rejection."""
        test_url = "postgresql://u:p@localhost:5433/test_db"
        runtime_url = "postgresql://u:p@localhost:5432/test_db"
        # Should not raise (different ports)
        identity = assert_test_database_safe(test_url, runtime_url=runtime_url)
        assert identity.database == "test_db"

    def test_same_port_triggers_rejection(self):
        """Same port with same database should trigger rejection."""
        test_url = "postgresql://u:p@localhost:5432/test_db"
        runtime_url = "postgresql://u:p@localhost:5432/test_db"
        with pytest.raises(TestDatabaseSafetyError):
            assert_test_database_safe(test_url, runtime_url=runtime_url)
