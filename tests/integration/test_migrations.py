"""Alembic migrations must apply and reverse cleanly (Phase A exit condition)."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    ModelRole,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Model, Project, Task, TaskLimits, TaskRun
from apps.orchestrator.repositories import (
    ModelRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)

pytestmark = pytest.mark.integration

EXPECTED_TABLES = set(Base.metadata.tables)

ENDPOINT = "http://x/v1"


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


def test_upgrade_head_matches_the_mapped_model_run_columns(alembic_config):
    """The model call audit fields are in the revision, not only the mapping.

    A call that failed left no row, and the row that did survive could not say
    how long it waited; both are fixed in the mapping and in the recording. A
    mapping without a revision fixes them for a fresh test database and not for
    the running one, and every new deployment would find out the first time a
    timeout happened.
    """
    config, url = alembic_config
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        columns = {column["name"]: column for column in inspect(engine).get_columns("model_runs")}
        assert set(columns) == set(Base.metadata.tables["model_runs"].columns.keys())
        assert {"error_detail", "attempt", "review_cycle"} <= set(columns)
        # Nullable, so every row written before the revision keeps the meaning
        # it already had: a call that succeeded, or one recorded before this.
        assert all(
            columns[name]["nullable"] for name in ("error_detail", "attempt", "review_cycle")
        )
    finally:
        engine.dispose()


def test_a_recorded_call_survives_the_audit_columns_revision(alembic_config):
    """The upgrade runs over a table that has rows in it, and keeps them.

    An empty database is the easy case and the one a fresh test suite always
    takes. This starts from the revision before the audit fields, writes a call
    the way the app before that revision wrote it, and then applies the upgrade to
    a table with data in it: the new columns have to be added around those rows
    rather than by recreating an empty table beside them.
    """
    config, url = alembic_config
    previous = ScriptDirectory.from_config(config).get_revision("head").down_revision
    command.upgrade(config, previous)
    engine = create_db_engine(url)
    try:
        # A call the way the app wrote it before this revision. The parents go
        # in through the repositories, and the call itself goes in as a raw
        # insert, because the mapping now names columns the database at this
        # revision does not have -- which is the whole point of the test. The
        # ids are read back rather than typed, because the app's UUID type
        # stores them in a form no reader would guess.
        with engine.connect() as connection:
            factory = sessionmaker(bind=connection, expire_on_commit=False)
            with factory.begin() as session:
                project = ProjectRepository(session).add(
                    Project(name="T", repository_path="/tmp/t", default_branch="main",
                            worker_profile=WorkerProfile.PYTHON)
                )
                tasks = TaskRepository(session)
                task = tasks.add(
                    Task(project_id=project.id, external_task_id="TS-001", title="t",
                         instructions="i", complexity=Complexity.LOW, files_to_modify=["a.py"],
                         limits=TaskLimits(max_files_changed=3, max_diff_lines=200))
                )
                tasks.transition(task.id, TaskStatus.READY)
                TaskRunRepository(session).add(
                    TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
                )
                ModelRepository(session).add(
                    Model(provider="openai-compatible", model_name="coder-test",
                          role=ModelRole.CODER, endpoint=ENDPOINT)
                )
        with engine.begin() as connection:
            run_id = connection.exec_driver_sql("SELECT id FROM task_runs").scalar_one()
            model_id = connection.exec_driver_sql("SELECT id FROM models").scalar_one()
            connection.exec_driver_sql(
                "INSERT INTO model_runs (id, task_run_id, model_id, purpose, status,"
                " duration_ms) VALUES (?, ?, ?, 'CODE', 'SUCCEEDED', 12)",
                (uuid.uuid4().hex, run_id, model_id),
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT purpose, status, duration_ms, error_detail, attempt, review_cycle"
                     " FROM model_runs")
            ).one()
        assert row == ("CODE", "SUCCEEDED", 12, None, None, None), row
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
