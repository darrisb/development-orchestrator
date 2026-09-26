from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from ..domain.dependencies import ReadinessReport
from ..domain.enums import Complexity, RiskLevel, TaskStatus
from ..domain.models import PauseRequest, Task, TaskLimits
from ..services.scheduler import NoTaskReason, Selection


class TaskLimitsResponse(BaseModel):
    max_attempts: int
    max_review_cycles: int
    max_runtime_minutes: int
    max_files_changed: int
    max_diff_lines: int

    @classmethod
    def from_domain(cls, limits: TaskLimits) -> TaskLimitsResponse:
        return cls(
            max_attempts=limits.max_attempts,
            max_review_cycles=limits.max_review_cycles,
            max_runtime_minutes=limits.max_runtime_minutes,
            max_files_changed=limits.max_files_changed,
            max_diff_lines=limits.max_diff_lines,
        )


class TaskResponse(BaseModel):
    id: UUID
    project_id: UUID
    external_task_id: str
    title: str
    section: int | None
    instructions: str | None
    complexity: Complexity
    risk_level: RiskLevel
    status: TaskStatus
    depends_on: list[str]
    verify_commands: list[str]
    files_to_inspect: list[str]
    files_to_modify: list[str]
    files_to_create: list[str]
    limits: TaskLimitsResponse
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_domain(cls, task: Task) -> TaskResponse:
        return cls(
            id=task.id,
            project_id=task.project_id,
            external_task_id=task.external_task_id,
            title=task.title,
            section=task.section,
            instructions=task.instructions,
            complexity=task.complexity,
            risk_level=task.risk_level,
            status=task.status,
            depends_on=list(task.depends_on),
            verify_commands=list(task.verify_commands),
            files_to_inspect=list(task.files_to_inspect),
            files_to_modify=list(task.files_to_modify),
            files_to_create=list(task.files_to_create),
            limits=TaskLimitsResponse.from_domain(task.limits),
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class ReadinessResponse(BaseModel):
    """Why each unfinished task can or cannot start (build.md section 26)."""

    ready: list[str]
    blocked: dict[str, list[str]]
    waiting: dict[str, list[str]]

    @classmethod
    def from_domain(cls, report: ReadinessReport) -> ReadinessResponse:
        return cls(
            ready=list(report.ready),
            blocked={key: list(value) for key, value in report.blocked.items()},
            waiting={key: list(value) for key, value in report.waiting.items()},
        )


class NextTaskResponse(BaseModel):
    task: TaskResponse | None
    reason: NoTaskReason | None
    readiness: ReadinessResponse

    @classmethod
    def from_selection(cls, selection: Selection) -> NextTaskResponse:
        return cls(
            task=TaskResponse.from_domain(selection.task) if selection.task else None,
            reason=selection.reason,
            readiness=ReadinessResponse.from_domain(selection.readiness),
        )


class PauseTaskRequest(BaseModel):
    reason: str | None = None
    requested_by: str | None = None


class PauseRequestResponse(BaseModel):
    id: UUID
    project_id: UUID
    task_id: UUID | None
    reason: str | None
    requested_by: str | None
    created_at: datetime | None
    honoured_at: datetime | None
    released_at: datetime | None

    @classmethod
    def from_domain(cls, request: PauseRequest) -> PauseRequestResponse:
        return cls(
            id=request.id,
            project_id=request.project_id,
            task_id=request.task_id,
            reason=request.reason,
            requested_by=request.requested_by,
            created_at=request.created_at,
            honoured_at=request.honoured_at,
            released_at=request.released_at,
        )
