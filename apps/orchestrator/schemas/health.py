from __future__ import annotations

from pydantic import BaseModel, Field


class ComponentHealth(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class WorktreeHealth(BaseModel):
    total: int
    releasable: int
    unclaimed: int


class HealthResponse(BaseModel):
    status: str = Field(description="ok when every required component is healthy")
    version: str
    components: list[ComponentHealth]
    worktrees: WorktreeHealth | None = None
