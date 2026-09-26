"""Alembic migrations must apply and reverse cleanly (Phase A exit condition)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine

pytestmark = pytest.mark.integration

EXPECTED_TABLES = set(Base.metadata.tables)


@pytest.fixture
def alembic_config(tmp_path, monkeypatch) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'migrations.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    yield config, url
    get_settings.cache_clear()


def test_upgrade_head_creates_every_mapped_table(alembic_config):
    config, url = alembic_config
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert tables == EXPECTED_TABLES
    finally:
        engine.dispose()


def test_downgrade_base_removes_every_table(alembic_config):
    config, url = alembic_config
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = create_db_engine(url)
    try:
        tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert tables == set()
    finally:
        engine.dispose()


def test_upgrade_head_matches_the_mapped_task_columns(alembic_config):
    """A mapping added without a revision would only fail against a real database."""
    config, url = alembic_config
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
        assert columns == set(Base.metadata.tables["tasks"].columns.keys())
        assert {"files_to_inspect", "files_to_modify", "files_to_create"} <= columns
    finally:
        engine.dispose()


def test_upgrade_head_matches_the_mapped_project_columns(alembic_config):
    """The verification profile (section 18) is a column, not only a mapping."""
    config, url = alembic_config
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("projects")}
        assert columns == set(Base.metadata.tables["projects"].columns.keys())
        assert "verification_profile" in columns
    finally:
        engine.dispose()


def test_upgrade_head_matches_the_mapped_lesson_foreign_keys(alembic_config):
    """Phase L's evidence columns are references, and the mapping says so.

    ``lessons`` is an existing table, so its two run references are added as
    columns and constrained separately -- an easy place to end up with a mapping
    promising a foreign key the database never enforces, which no column
    comparison would notice.
    """
    config, url = alembic_config
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        inspector = inspect(engine)
        actual = {
            tuple(key["constrained_columns"])
            for key in inspector.get_foreign_keys("lessons")
        }
        mapped = {
            tuple(column.name for column in constraint.columns)
            for constraint in Base.metadata.tables["lessons"].foreign_key_constraints
        }
        assert actual == mapped
        assert ("source_run_id",) in actual
        assert ("last_seen_run_id",) in actual
    finally:
        engine.dispose()
