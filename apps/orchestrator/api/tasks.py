from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..schemas.runs import TaskRunResponse
from ..schemas.tasks import PauseRequestResponse, PauseTaskRequest, TaskResponse
from ..services import pauses as pause_service
from ..services import runs as run_service
from ..services import tasks as task_service

router = APIRouter(tags=["tasks"])


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: UUID, session: Session = Depends(get_db)) -> TaskResponse:
    return TaskResponse.from_domain(task_service.get_task(session, task_id))


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRunResponse])
def list_task_runs(task_id: UUID, session: Session = Depends(get_db)) -> list[TaskRunResponse]:
    return [
        TaskRunResponse.from_domain(run)
        for run in run_service.list_runs_for_task(session, task_id)
    ]


@router.get("/runs/{run_id}", response_model=TaskRunResponse)
def get_run(run_id: UUID, session: Session = Depends(get_db)) -> TaskRunResponse:
    return TaskRunResponse.from_domain(run_service.get_run(session, run_id))


@router.post("/tasks/{task_id}/pause", response_model=PauseRequestResponse)
def pause_task(
    task_id: UUID,
    payload: PauseTaskRequest | None = None,
    session: Session = Depends(get_db),
) -> PauseRequestResponse:
    request = pause_service.pause_task(
        session,
        task_id,
        reason=payload.reason if payload else None,
        requested_by=payload.requested_by if payload else None,
    )
    return PauseRequestResponse.from_domain(request)


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
def resume_task(task_id: UUID, session: Session = Depends(get_db)) -> TaskResponse:
    return TaskResponse.from_domain(pause_service.resume_task(session, task_id))
