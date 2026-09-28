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
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Model, Project, Task, TaskLimits
from apps.orchestrator.repositories import (
    ModelRepository,
    ProjectRepository,
    TaskRepository,
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


def test_upgrade_head_matches_the_mapped_task_run_columns(alembic_config):
    config, url = alembic_config
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("task_runs")}
        assert columns == set(Base.metadata.tables["task_runs"].columns.keys())
        assert {"active_runtime_ms", "active_started_at"} <= columns
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
    previous = ScriptDirectory.from_config(config).get_revision(
        "b7c41d90e2a5"
    ).down_revision
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
                session.execute(
                    text(
                        "INSERT INTO task_runs "
                        "(id, task_id, run_number, attempt_number, review_cycle, status) "
                        "VALUES (:id, :task_id, 1, 1, 0, 'RUNNING')"
                    ),
                    {"id": uuid.uuid4().hex, "task_id": task.id.hex},
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


def test_populated_task_runs_gain_conservative_runtime_accounting(alembic_config):
    """Historical wall time is not fabricated as active execution time."""
    config, url = alembic_config
    command.upgrade(config, "b7c41d90e2a5")
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            factory = sessionmaker(bind=connection, expire_on_commit=False)
            with factory.begin() as session:
                project = ProjectRepository(session).add(
                    Project(name="runtime", repository_path="/tmp/runtime")
                )
                task = TaskRepository(session).add(
                    Task(project_id=project.id, external_task_id="T-1", title="runtime")
                )
                session.execute(
                    text(
                        "INSERT INTO task_runs "
                        "(id, task_id, run_number, attempt_number, review_cycle, "
                        "status, started_at) "
                        "VALUES (:id, :task_id, 1, 1, 0, 'RUNNING', CURRENT_TIMESTAMP)"
                    ),
                    {"id": uuid.uuid4().hex, "task_id": task.id.hex},
                )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT active_runtime_ms, active_started_at FROM task_runs")
            ).one()
        assert row == (0, None)
    finally:
        engine.dispose()


def test_populated_task_runs_gain_unowned_generation_zero(alembic_config):
    """Concern 67's columns say the truthful thing about a pre-existing run.

    Generation 0 and no owner. Not "owned by whoever started it": nothing on
    the old row could say who that was, and a migration that guessed would
    either strand every historical run behind an owner nothing will ever clear
    or claim an executor that is long gone. Zero and NULL mean "no dispatch
    holds this", and the first acquisition takes generation 1 -- which is what
    fences the run's original executor whether or not it is still out there.
    """
    config, url = alembic_config
    command.upgrade(config, "e2d6b79a4f10")
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            factory = sessionmaker(bind=connection, expire_on_commit=False)
            with factory.begin() as session:
                project = ProjectRepository(session).add(
                    Project(name="ownership", repository_path="/tmp/ownership")
                )
                task = TaskRepository(session).add(
                    Task(project_id=project.id, external_task_id="T-67", title="own")
                )
                session.execute(
                    text(
                        "INSERT INTO task_runs "
                        "(id, task_id, run_number, attempt_number, review_cycle, "
                        "status, active_runtime_ms, started_at) "
                        "VALUES (:id, :task_id, 1, 1, 0, 'RUNNING', 0, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"id": uuid.uuid4().hex, "task_id": task.id.hex},
                )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT execution_generation, execution_owner, "
                    "execution_started_at FROM task_runs"
                )
            ).one()
        assert row == (0, None, None)
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
