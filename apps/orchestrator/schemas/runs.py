from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from ..domain.enums import RunStatus
from ..domain.models import TaskRun


class TaskRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    run_number: int
    attempt_number: int
    review_cycle: int
    status: RunStatus
    branch_name: str | None
    starting_commit: str | None
    candidate_commit: str | None
    coder_model_id: UUID | None
    worker_image: str | None
    context_hash: str | None
    prompt_version: str | None
    failure_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    active_runtime_ms: int
    active_started_at: datetime | None

    @classmethod
    def from_domain(cls, run: TaskRun) -> TaskRunResponse:
        return cls(
            id=run.id,
            task_id=run.task_id,
            run_number=run.run_number,
            attempt_number=run.attempt_number,
            review_cycle=run.review_cycle,
            status=run.status,
            branch_name=run.branch_name,
            starting_commit=run.starting_commit,
            candidate_commit=run.candidate_commit,
            coder_model_id=run.coder_model_id,
            worker_image=run.worker_image,
            context_hash=run.context_hash,
            prompt_version=run.prompt_version,
            failure_reason=run.failure_reason,
            started_at=run.started_at,
            completed_at=run.completed_at,
            active_runtime_ms=run.active_runtime_ms,
            active_started_at=run.active_started_at,
        )


class ProjectRunResponse(BaseModel):
    run_id: UUID | None = None
    outcome: str | None = None
    state: dict[str, object] | None = None
    no_task_reason: str | None = None
