"""Bounded lock acquisition (concern 57).

The defect these cover cannot be reproduced against SQLite and cannot be
reproduced with the locking mechanism mocked: it *is* PostgreSQL's row-lock
wait, and what was wrong was that the wait had no bound. So these tests take
two real connections to a real server, contend on a real ``task_runs`` row with
the real statement from the incident -- ``UPDATE task_runs SET external_run_id``
-- and one of them kills a real child process.

The scratch database is created and dropped here, so nothing touches a managed
project's data.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.db import models  # noqa: F401
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.repositories import (
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.errors import LockWaitTimeout

pytestmark = pytest.mark.integration

#: Short enough to keep the suite quick, long enough that a wait of this length
#: cannot be mistaken for an ordinary slow statement.
LOCK_TIMEOUT_SECONDS = 2.0
IDLE_TIMEOUT_SECONDS = 5.0

_DEFAULT_SERVER = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/postgres"


def _server_url() -> str:
    configured = os.environ.get("TEST_DATABASE_URL", "")
    if configured.startswith("postgresql"):
        return configured.rsplit("/", 1)[0] + "/postgres"
    return os.environ.get("TEST_POSTGRES_SERVER_URL", _DEFAULT_SERVER)


@pytest.fixture(scope="module")
def lock_settings() -> Settings:
    return Settings(
        _env_file=None,
        db_lock_timeout_seconds=LOCK_TIMEOUT_SECONDS,
        db_idle_in_transaction_timeout_seconds=IDLE_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope="module")
def lock_database(lock_settings: Settings) -> Iterator[str]:
    """A scratch database on the configured server, dropped afterwards."""
    server = _server_url()
    # CREATE DATABASE cannot run inside a transaction, so the admin engine
    # autocommits. Connectivity is probed separately: only an unreachable server
    # is a reason to skip, and anything else should fail loudly rather than look
    # like an environment without PostgreSQL.
    # Plain create_engine, not the application's factory: this is scaffolding
    # that needs autocommit from the first statement, and pool_pre_ping would
    # open a transaction before the isolation level could be set.
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:  # pragma: no cover - no server available
        admin.dispose()
        pytest.skip(f"no PostgreSQL server for the lock tests at {server}: {exc}")

    name = f"lock_{uuid.uuid4().hex[:12]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = server.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def lock_engine(
    lock_database: str, lock_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Engine]:
    monkeypatch.setattr("apps.orchestrator.db.session.get_settings", lambda: lock_settings)
    engine = create_db_engine(lock_database)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def locked_run(lock_engine: Engine) -> uuid.UUID:
    """A committed run whose row the tests contend over."""
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(name="TraceStack", repository_path="/workspace/tracestack")
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-101",
                title="Report how many entries the stack holds",
                instructions="Add size().",
            )
        )
        run = TaskRunRepository(session).add(
            TaskRun(task_id=task.id, run_number=1, attempt_number=1)
        )
        return run.id


def _allocate(session: Session, run_id: uuid.UUID, external_run_id: str) -> None:
    """The statement from the incident, through the repository that issues it."""
    TaskRunRepository(session).update_fields(run_id, external_run_id=external_run_id)


def test_the_timeouts_are_set_on_every_connection(lock_engine: Engine):
    """A per-statement bound would leave every new write unbounded until someone
    remembered it, so the bound is on the connection."""
    with lock_engine.connect() as connection:
        assert connection.execute(text("show lock_timeout")).scalar() == "2s"
        assert (
            connection.execute(text("show idle_in_transaction_session_timeout")).scalar()
            == "5s"
        )


def test_an_uncontended_write_still_succeeds(lock_engine: Engine, locked_run: uuid.UUID):
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    with factory.begin() as session:
        _allocate(session, locked_run, "RUN-20260927-000001")
    with factory() as session:
        assert (
            TaskRunRepository(session).get(locked_run).external_run_id
            == "RUN-20260927-000001"
        )


def test_a_contended_write_fails_in_bounded_time_and_says_why(
    lock_engine: Engine, locked_run: uuid.UUID
):
    """The incident, reproduced: a transaction that wrote the row and then went
    idle -- an abandoned request -- and a second writer behind it.

    Before the fix the second writer waited for as long as the first existed;
    the orchestrator's own health check queued behind it and a restart was the
    only way out.
    """
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    holder = factory()
    _allocate(holder, locked_run, "RUN-HOLDER")
    holder.flush()
    # The holder's *last* statement is a read, which is why pg_stat_activity
    # showed a SELECT for a session that was holding a write lock.
    holder.execute(text("SELECT 1"))

    outcome: dict[str, object] = {}

    def second_writer() -> None:
        started = time.monotonic()
        session = factory()
        try:
            _allocate(session, locked_run, "RUN-SECOND")
            session.commit()
            outcome["error"] = None
        except BaseException as exc:  # noqa: BLE001 - the type is the assertion
            session.rollback()
            outcome["error"] = exc
        finally:
            session.close()
            outcome["waited"] = time.monotonic() - started

    thread = threading.Thread(target=second_writer)
    thread.start()
    thread.join(timeout=LOCK_TIMEOUT_SECONDS + 20)

    assert not thread.is_alive(), "the second writer waited without a bound"
    assert isinstance(outcome["error"], LockWaitTimeout), outcome["error"]
    # Diagnosable: it names the wait and what it was waiting for.
    assert "waiting for a database lock" in str(outcome["error"])
    assert LOCK_TIMEOUT_SECONDS <= outcome["waited"] < LOCK_TIMEOUT_SECONDS + 15

    # Mutual exclusion was not weakened: the holder still has the row, and the
    # writer that timed out wrote nothing.
    assert holder.get(models.TaskRunRow, locked_run).external_run_id == "RUN-HOLDER"
    holder.rollback()
    holder.close()


def test_the_holder_keeps_its_protection_while_it_is_active(
    lock_engine: Engine, locked_run: uuid.UUID
):
    """A transaction that keeps working is not interrupted by either timeout."""
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    holder = factory()
    _allocate(holder, locked_run, "RUN-ACTIVE")
    holder.flush()

    deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS * 1.5
    while time.monotonic() < deadline:
        holder.execute(text("SELECT 1"))
        time.sleep(0.25)

    holder.commit()
    holder.close()
    with factory() as session:
        assert TaskRunRepository(session).get(locked_run).external_run_id == "RUN-ACTIVE"


def test_release_lets_the_next_writer_through(lock_engine: Engine, locked_run: uuid.UUID):
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    holder = factory()
    _allocate(holder, locked_run, "RUN-FIRST")
    holder.commit()
    holder.close()

    with factory.begin() as session:
        _allocate(session, locked_run, "RUN-SECOND")
    with factory() as session:
        assert TaskRunRepository(session).get(locked_run).external_run_id == "RUN-SECOND"


def test_an_abandoned_transaction_is_ended_by_the_server(
    lock_engine: Engine, locked_run: uuid.UUID
):
    """The other half of the fix. ``lock_timeout`` stops a waiter from hanging;
    this is what stops the holder from existing indefinitely in the first place.

    An abandoned request leaves a *live* connection idle inside a transaction.
    PostgreSQL will not reclaim it on its own -- that is why a restart was the
    only remedy -- so the server is told to end it.
    """
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    holder = factory()
    _allocate(holder, locked_run, "RUN-ABANDONED")
    holder.flush()

    time.sleep(IDLE_TIMEOUT_SECONDS + 2)

    with pytest.raises(Exception):  # noqa: B017 - the driver's own disconnect error
        holder.execute(text("SELECT 1"))
    holder.close()

    # The abandoned write was rolled back with its transaction, and the row is
    # free again.
    with factory.begin() as session:
        _allocate(session, locked_run, "RUN-AFTER")
    with factory() as session:
        assert TaskRunRepository(session).get(locked_run).external_run_id == "RUN-AFTER"


def test_a_holder_whose_process_dies_releases_the_row(
    lock_engine: Engine, lock_database: str, locked_run: uuid.UUID
):
    """Process death was never the failing case -- PostgreSQL rolls back a
    backend whose client is gone -- and this pins that, because it is what makes
    the incident's diagnosis specific: the holder there was a live process with
    an orphaned session, not a dead one."""
    script = (
        "import sys, time\n"
        "from sqlalchemy import create_engine, text\n"
        f"engine = create_engine({lock_database!r})\n"
        "conn = engine.connect()\n"
        "conn.execute(text(\"UPDATE task_runs SET external_run_id='RUN-DOOMED' "
        "WHERE id = :id\"), {'id': sys.argv[1]})\n"
        "print('locked', flush=True)\n"
        "time.sleep(120)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(locked_run)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        child.kill()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover - only on an assertion failure
            child.kill()

    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    started = time.monotonic()
    with factory.begin() as session:
        _allocate(session, locked_run, "RUN-AFTER-DEATH")
    assert time.monotonic() - started < LOCK_TIMEOUT_SECONDS
    with factory() as session:
        assert (
            TaskRunRepository(session).get(locked_run).external_run_id
            == "RUN-AFTER-DEATH"
        )


def test_a_retry_after_a_timeout_does_not_create_a_second_owner(
    lock_engine: Engine, locked_run: uuid.UUID
):
    """A bounded failure is only safe if retrying it cannot end up with two
    writers believing they hold the row. The timed-out writer holds nothing, so
    its retry either waits again or takes the row once the holder is gone."""
    factory = sessionmaker(bind=lock_engine, expire_on_commit=False)
    holder = factory()
    _allocate(holder, locked_run, "RUN-OWNER")
    holder.flush()

    retried = factory()
    with pytest.raises(LockWaitTimeout):
        _allocate(retried, locked_run, "RUN-RETRY")
    retried.rollback()

    # Still exactly one owner while the holder lives.
    assert holder.get(models.TaskRunRow, locked_run).external_run_id == "RUN-OWNER"
    holder.commit()
    holder.close()

    # And the retry succeeds once it is released, without a second attempt
    # having partially applied.
    _allocate(retried, locked_run, "RUN-RETRY")
    retried.commit()
    retried.close()
    with factory() as session:
        assert TaskRunRepository(session).get(locked_run).external_run_id == "RUN-RETRY"
