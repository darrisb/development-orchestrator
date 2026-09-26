"""FastAPI application entrypoint.

The API exposes resources, never LangGraph internals (build.md section 39).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import api_router, register_exception_handlers
from .config import get_settings
from .config.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    for directory in (settings.runs_dir, settings.training_dir, settings.artifacts_dir):
        directory.mkdir(parents=True, exist_ok=True)
    get_logger(__name__).info(
        "orchestrator_started",
        worker_backend=settings.worker_backend.value,
        git_push_enabled=settings.git_push_enabled,
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Local AI Development Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
