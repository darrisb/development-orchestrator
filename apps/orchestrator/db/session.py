"""Engine and session management.

PostgreSQL is the production target. SQLite is supported so that Phase A unit
and integration tests run without a database server; it is not a supported
runtime backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from ..services.errors import LockWaitTimeout

#: PostgreSQL ``lock_not_available``: a statement gave up waiting for a lock
#: because ``lock_timeout`` expired. Matched on SQLSTATE rather than on message
#: text, which is localised.
_LOCK_NOT_AVAILABLE = "55P03"

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """SQLite ignores foreign keys unless asked; the tests rely on them."""

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _apply_postgresql_timeouts(engine: Engine, settings) -> None:  # type: ignore[no-untyped-def]
    """Bound every lock wait, and the transactions that cause them (concern 57).

    Two settings, because the hang had two halves. ``lock_timeout`` bounds the
    *waiter*: a statement blocked on a row lock fails with SQLSTATE 55P03
    instead of waiting for a transaction that may never end.
    ``idle_in_transaction_session_timeout`` bounds the *holder*: a connection
    left inside an open transaction -- which is what an abandoned HTTP request
    leaves behind -- is ended by the server, and its locks go with it.

    Applied per connection at connect time rather than per statement: a lock
    wait can happen on any write in the run, and enumerating them would mean
    every new write is unbounded until someone remembers.

    Neither weakens mutual exclusion. A lock that is available is still taken
    and still held for the whole transaction; what changed is that waiting for
    one is no longer unbounded.
    """
    lock_ms = int(settings.db_lock_timeout_seconds * 1000)
    idle_ms = int(settings.db_idle_in_transaction_timeout_seconds * 1000)

    @event.listens_for(engine, "connect")
    def _set_timeouts(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET lock_timeout = {lock_ms}")
            cursor.execute(f"SET idle_in_transaction_session_timeout = {idle_ms}")
        finally:
            cursor.close()


def _translate_lock_timeout(engine: Engine) -> None:
    """Turn SQLSTATE 55P03 into one named error the whole application knows.

    Without this the bound exists but says ``OperationalError``, which is the
    same thing every other database fault says. The point of bounding the wait
    was to be able to tell this apart from a broken connection or a bad query.
    """

    @event.listens_for(engine, "handle_error")
    def _handle(context) -> None:  # type: ignore[no-untyped-def]
        original = context.original_exception
        sqlstate = getattr(getattr(original, "diag", None), "sqlstate", None)
        if sqlstate == _LOCK_NOT_AVAILABLE:
            raise LockWaitTimeout(
                "gave up waiting for a database lock after "
                f"{get_settings().db_lock_timeout_seconds:g}s; another "
                "transaction holds the rows this operation needs"
            ) from original


def create_db_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs.pop("pool_pre_ping")
    engine = create_engine(url, **kwargs)  # type: ignore[arg-type]
    if engine.dialect.name == "sqlite":
        _apply_sqlite_pragmas(engine)
    else:
        _apply_postgresql_timeouts(engine, settings)
        _translate_lock_timeout(engine)
    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def reset_engine() -> None:
    """Drop cached engine/session factory. Used by tests."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
