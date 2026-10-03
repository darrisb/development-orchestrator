from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db.session import get_db, get_session_factory
from ..schemas.runs import (
    RecoverabilityResponse,
    RunRecoveryResponse,
    RunUsageResponse,
    TaskRunResponse,
)
from ..schemas.tasks import (
    OperatorReason,
    PauseRequestResponse,
    PauseTaskRequest,
    RetryTaskRequest,
    TaskResponse,
)
from ..services import abandon as abandon_service
from ..services import model_usage as usage_service
from ..services import pauses as pause_service
from ..services import retry as retry_service
from ..services import run_recovery as recovery_service
from ..services import runs as run_service
from ..services import tasks as task_service
from ..workflow import WorkflowRunner

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


class RecoverRunRequest(BaseModel):
    """An operator's request to recover one stranded in-flight run (concern 67)."""

    reason: OperatorReason = Field(
        ...,
        min_length=1,
        description=(
            "Why this run is being recovered. Required, for the same reason "
            "abandonment requires one: taking execution of a live run away "
            "from whoever may hold it is a decision, and an unexplained "
            "decision is indistinguishable from a bug."
        ),
    )
    requested_by: str | None = Field(
        None, description="Operator identifier, recorded in the event payload."
    )
    override_active_owner: bool = Field(
        False,
        description=(
            "Recover even though a dispatch is recorded as holding this run. "
            "Needed only when an executor was killed without unwinding, which "
            "leaves an owner stamp nothing will ever clear. The older executor "
            "is fenced by the execution generation whether it is alive or not, "
            "so this widens what may be recovered and never what may be "
            "persisted."
        ),
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


@router.get("/runs/{run_id}/usage", response_model=RunUsageResponse)
def get_run_usage(run_id: UUID, session: Session = Depends(get_db)) -> RunUsageResponse:
    """What this run spent on models: tokens, duration and persisted cost.

    Concern 78. Read-only and derived entirely from ``model_runs``, so asking
    costs nothing and changes nothing. The run is fetched first so that an
    unknown ``run_id`` is a 404 rather than an empty usage report, which would
    otherwise be indistinguishable from a real run that made no model calls.

    Costs are the ones persisted when each call was made. They are not
    recalculated from the model's current pricing, so this answer about a past
    run does not change when an operator edits a price.

    Raises:
        EntityNotFound: no such run. Mapped to 404.
    """
    run = run_service.get_run(session, run_id)
    return RunUsageResponse.from_summary(
        usage_service.usage_for_run(session, run.id), run_id=run.id
    )


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


@router.get("/runs/{run_id}/recoverability", response_model=RecoverabilityResponse)
def run_recoverability(
    run_id: UUID, session: Session = Depends(get_db)
) -> RecoverabilityResponse:
    """Whether this run could be recovered, and why or why not (concern 67).

    Read-only, and that is a contract rather than an implementation detail: it
    takes no lock, acquires no execution ownership, increments no generation
    and writes no event, so it is safe to ask about a run nobody intends to
    touch -- including a live campaign's run during an investigation.

    It is a snapshot and says so by reporting ``execution_generation``. The
    mutating operation re-derives the whole assessment inside its own
    transaction and guards its acquisition on that number, so a "yes" here is
    never the authority for a later write.

    ``recovery_mode`` (concern 68) says what a recovery would *do*:
    ``continue`` when a coder attempt remains inside the task's budget, or
    ``settlement_only`` when none does but the run is non-terminal and the fix
    loop still owes it a deterministic ``ESCALATED / RETRY_EXHAUSTED`` ending.
    A settlement-only recovery acquires ownership exactly as a continuation
    does, and executes no attempt, no provider call and no verification.

    Responses:

    * ``200`` -- the assessment, whatever it says. A run that cannot be
      recovered is not an error to ask about; ``recoverable`` is false and
      every failing check is named.
    * ``404`` -- no such run.
    """
    return RecoverabilityResponse.from_report(
        recovery_service.assess_recoverability(session, run_id)
    )


@router.post("/runs/{run_id}/recover", response_model=RunRecoveryResponse)
async def recover_run(
    run_id: UUID,
    payload: RecoverRunRequest,
    session: Session = Depends(get_db),
) -> RunRecoveryResponse:
    """Recover a stranded in-flight run (concern 67).

    An operator action, like abandonment and retry, and deliberately absent
    from the model tool surface: taking execution of a run is not a decision
    the orchestrator makes about itself.

    **``{run_id}`` is the durable ``task_runs.id`` UUID, never the external
    ``RUN-YYYYMMDD-NNNNNN`` identity.** It is the same path parameter as
    ``GET /runs/{run_id}`` and ``POST /runs/{run_id}/abandon``, and it means
    the same thing in all three; accepting both spellings on the most
    consequential of the three would make "which run did I just take over"
    a question about parsing. The external identity is now returned by
    ``GET /runs/{run_id}`` and ``GET /tasks/{task_id}/runs`` so an operator can
    map one to the other without reading artifacts off disk.

    **What it does not do.** No new ``TaskRun`` is created, ``run_number`` is
    not incremented, the external run identity is unchanged, and every
    historical attempt, event, model call and review stays exactly as it is.
    Git history and campaign state are never silently repaired: a missing
    worktree, a starting commit that is gone, or an integration baseline that
    diverged are refusals, not things to fix on the operator's behalf.

    **What it does.** It re-derives the recoverability assessment, atomically
    takes the next ``execution_generation`` (which fences any older executor
    out of persisting anything, alive or not), appends exactly one
    ``RUN_RECOVERY_AUTHORIZED`` event, and then continues the *existing* run
    through the ordinary workflow and fix loop -- reconstructing attempt and
    review accounting from the durable record, so an interrupted attempt is
    neither repeated nor double-counted.

    **Two kinds of recovery, one operation (concern 68).** When the coder budget
    is already spent, the run is still re-entered -- but only so the workflow can
    write the ending it owes. The response's ``recovery_mode`` is then
    ``settlement_only``, and the fix loop's own exhausted-budget path settles the
    run as ``ESCALATED`` / ``RETRY_EXHAUSTED``: no attempt is started, no
    provider is called, no verification command is run, no reviewer is asked, no
    candidate is built and ``attempt_number`` does not move. There is no separate
    code path and no request flag for this: it is the same call, and the
    distinction is the fix loop's arithmetic rather than a branch the operator
    chooses. What would previously have happened instead is a ``409``, leaving a
    ``RUNNING`` run nothing would ever finish.

    Responses:

    * ``200`` -- ownership was acquired and the run was executed. The body
      carries the run, the generation that was fenced, the generation now held,
      and the workflow's outcome.
    * ``404`` -- no such run.
    * ``409`` -- the run is not recoverable. Every case is one of these, and
      the detail names which: a terminal ``SUCCEEDED`` or ``FAILED`` run; an
      ``ABANDONED`` run; a run superseded by a competing in-flight run of the
      same task; a ``PAUSED`` task (which has ``/tasks/{id}/resume``); a pause
      in force; a missing workflow checkpoint; a missing or unreadable starting
      commit; a diverged integration baseline; a missing worktree; attempt
      accounting that could not be reconstructed or contradicts itself; a
      dispatch that currently holds the run; or a competing recovery that
      acquired the run while this request was in flight. A *spent* attempt
      budget is no longer among them -- see ``settlement_only`` above.
    * ``422`` -- ``reason`` is missing, empty, or only whitespace.

    Two simultaneous requests cannot both acquire ownership: the acquisition is
    one guarded ``UPDATE`` whose predicate includes the generation each request
    assessed, so the loser quotes a number the winner already replaced and is
    told so. Repeating a request after a successful one is therefore not a
    second executor; it is a second recovery of a run that has since moved on,
    and it is judged on its own merits at that moment.
    """
    authorization = recovery_service.recover_run(
        session,
        run_id,
        reason=payload.reason,
        requested_by=payload.requested_by,
        override_active_owner=payload.override_active_owner,
    )
    # The acquisition and its event have to be durable before anything executes
    # against them: the generation in the row is what the executor below will
    # quote back at every checkpoint, and an uncommitted one would fence
    # nothing.
    session.commit()
    runner = WorkflowRunner.configured(get_session_factory())
    try:
        state = await runner.run(
            run_id, acquired=(authorization.owner, authorization.generation)
        )
    finally:
        await runner.aclose()
    return RunRecoveryResponse.from_authorization(
        authorization, run_service.get_run(session, run_id), dict(state)
    )


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
