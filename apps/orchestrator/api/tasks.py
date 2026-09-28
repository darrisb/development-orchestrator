from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..schemas.runs import TaskRunResponse
from ..schemas.tasks import (
    OperatorReason,
    PauseRequestResponse,
    PauseTaskRequest,
    RetryTaskRequest,
    TaskResponse,
)
from ..services import abandon as abandon_service
from ..services import pauses as pause_service
from ..services import retry as retry_service
from ..services import runs as run_service
from ..services import tasks as task_service

router = APIRouter(tags=["tasks"])


class AbandonRunRequest(BaseModel):
    """An operator's request to abandon one durable run (concern 64)."""

    reason: OperatorReason = Field(
        ...,
        min_length=1,
        description=(
            "Why the run is being abandoned. Required: an unexplained terminal "
            "state is indistinguishable from a bug, and the reason is the only "
            "thing a later reader has to go on."
        ),
    )
    requested_by: str | None = Field(
        None, description="Operator identifier, recorded in the event payload."
    )


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


@router.post("/runs/{run_id}/abandon", response_model=TaskRunResponse)
def abandon_run(
    run_id: UUID,
    payload: AbandonRunRequest,
    session: Session = Depends(get_db),
) -> TaskRunResponse:
    """Abandon an in-flight durable run (concern 64).

    An operator action, deliberately absent from the model/coding-agent tool
    surface: the only thing that may stop a run on purpose is a person.

    Responses:

    * ``200`` -- the run is ABANDONED. Also returned, with the same body and
      without a second event, when the run was already ABANDONED: abandoning
      is idempotent, so a retried request is a no-op rather than a second
      piece of evidence. ``previous_status`` in the event is therefore the
      status the *first* request found.
    * ``404`` -- no such run.
    * ``409`` -- the run already reached a terminal status of its own
      (``SUCCEEDED`` or ``FAILED``), or it was abandoned between the request
      being read and it being written.
    * ``422`` -- ``reason`` is missing, empty, or only whitespace.

    The run and its task are written in one transaction: a task that cannot be
    moved fails the whole request, and the run is not left ABANDONED with its
    task still in CODING.
    """
    run = abandon_service.abandon_run(
        session,
        run_id,
        reason=payload.reason,
        requested_by=payload.requested_by,
    )
    return TaskRunResponse.from_domain(run)


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
def retry_task(
    task_id: UUID,
    payload: RetryTaskRequest,
    session: Session = Depends(get_db),
) -> TaskResponse:
    """Authorize a new run for a failed task (concern 65).

    An operator action, like abandonment: the only thing that may start a run
    for a task the orchestrator itself gave up on is a person. It is not in the
    model tool surface.

    The authorization is not the execution. The task becomes ``READY`` -- the
    state the scheduler selects from and the only one a new run may be created
    from -- and the next project execution creates the run through the same
    machinery as any other, from the current accepted integration baseline. The
    response is the task, because the task is what changed.

    Responses:

    * ``200`` -- the task is now ``READY`` and eligible for a new run.
    * ``404`` -- no such task.
    * ``409`` -- the task is not ``FAILED`` (including a task that is
      ``COMPLETE``, and including a task that stopped being ``FAILED`` while
      the request was in flight); a run of the task is in flight; a dependency
      is not complete in the integration baseline; the project is not runnable;
      or a pause is in force. Every one of those is a statement about the world
      as it is now, not a policy about the request.
    * ``422`` -- ``reason`` is missing, empty, or only whitespace.

    Repeating the request is not idempotent in the sense of quietly doing the
    thing again: the second call finds a task that is no longer ``FAILED`` and
    is refused with 409, having created no run and recorded no second
    authorization.
    """
    return TaskResponse.from_domain(
        retry_service.retry_failed_task(
            session,
            task_id,
            reason=payload.reason,
            requested_by=payload.requested_by,
        )
    )


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
