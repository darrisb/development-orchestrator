from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..schemas.runs import TaskRunResponse
from ..schemas.tasks import PauseRequestResponse, PauseTaskRequest, TaskResponse
from ..services import abandon as abandon_service
from ..services import pauses as pause_service
from ..services import runs as run_service
from ..services import tasks as task_service

router = APIRouter(tags=["tasks"])


class AbandonRunRequest(BaseModel):
    """An operator's request to abandon one durable run (concern 64)."""

    reason: str = Field(
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

    @field_validator("reason")
    @classmethod
    def _reason_is_not_blank(cls, value: str) -> str:
        """Reject a reason that is only whitespace, as 422 rather than a 500.

        ``min_length=1`` above is about the request being well formed; it lets
        ``"   "`` through, and ``"   "`` records nothing a reader could learn
        from. The service refuses it too, but a ``ValueError`` raised inside a
        route is a 500 with a traceback, and the request was the caller's
        mistake, not the server's. A validator keeps it a 422 and keeps the
        text itself verbatim -- the operator's wording is evidence and is not
        silently trimmed.
        """
        if not value.strip():
            raise ValueError("reason is required")
        return value


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
