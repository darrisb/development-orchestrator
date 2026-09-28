"""Readiness checks (build.md section 47)."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings
from ..config.settings import WorkerBackend
from ..db.base import Base
from ..db.session import get_engine
from ..schemas.health import ComponentHealth, HealthResponse, WorktreeHealth
from .deployment import current_source
from .worker_errors import WorkerBackendUnavailable
from .worker_service import assert_backend_available
from .worktrees import census

VERSION = "0.1.0"


def check_database() -> ComponentHealth:
    try:
        engine = get_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            present = set(inspect(connection).get_table_names())
            missing = set(Base.metadata.tables) - present
            if missing:
                detail = ", ".join(sorted(missing)[:3])
                if len(missing) > 3:
                    detail += f", and {len(missing) - 3} more"
                return ComponentHealth(
                    name="database",
                    healthy=False,
                    detail=f"schema missing tables: {detail}",
                )
    except SQLAlchemyError as exc:
        return ComponentHealth(name="database", healthy=False, detail=type(exc).__name__)
    return ComponentHealth(name="database", healthy=True)


def check_artifact_root(settings: Settings) -> ComponentHealth:
    root = settings.artifact_root
    if not root.exists():
        return ComponentHealth(name="artifact_root", healthy=False, detail="missing")
    probe = root / ".write-probe"
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        return ComponentHealth(name="artifact_root", healthy=False, detail=type(exc).__name__)
    return ComponentHealth(name="artifact_root", healthy=True)


def check_worker_backend(settings: Settings) -> ComponentHealth:
    """Whether a verification command could actually be run (section 47).

    Worth a readiness check rather than a surprise mid-run: a host with no
    container runtime cannot verify anything, and finding that out on the first
    task would spend an attempt on an operator problem. The subprocess backend
    reports healthy and says what it is, because an installation running with
    weaker isolation should have to see that on /health.
    """
    if settings.worker_backend is not WorkerBackend.DOCKER:
        return ComponentHealth(
            name="worker_backend",
            healthy=True,
            detail=f"{settings.worker_backend.value}: weaker isolation than a container",
        )
    try:
        assert_backend_available(settings)
    except WorkerBackendUnavailable as exc:
        return ComponentHealth(name="worker_backend", healthy=False, detail=str(exc))
    return ComponentHealth(name="worker_backend", healthy=True, detail="docker")


def check_worktrees(settings: Settings) -> WorktreeHealth | None:
    """Expose accumulation without making an old tree fail readiness."""
    try:
        with Session(get_engine()) as session:
            report = census(session, settings=settings)
    except SQLAlchemyError:
        return None
    return WorktreeHealth(
        total=report.total,
        releasable=report.releasable,
        unclaimed=len(report.unclaimed),
    )


def build_health_report(settings: Settings) -> HealthResponse:
    components = [
        check_database(),
        check_artifact_root(settings),
        check_worker_backend(settings),
    ]
    status = "ok" if all(component.healthy for component in components) else "degraded"
    source = current_source()
    return HealthResponse(
        status=status,
        version=VERSION,
        source_revision=source.revision,
        source_dirty=source.dirty,
        source_state=source.state,
        build_time=source.built_at,
        components=components,
        worktrees=check_worktrees(settings),
    )
