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
    source_revision: str = Field(
        description="Git commit SHA baked into the image at build time"
    )
    source_dirty: bool | None = Field(
        description=(
            "Whether the source had uncommitted changes at build time; null "
            "when the build could not determine it"
        )
    )
    source_state: str = Field(
        description=(
            "Single-line build identity: 'clean@<sha>', 'dirty@<sha>', or "
            "'unknown/dev'. The value to compare against the intended source."
        )
    )
    build_time: str = Field(
        description="ISO-8601 timestamp of when the image was built"
    )
    components: list[ComponentHealth]
    worktrees: WorktreeHealth | None = None
