from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from ..domain.enums import RunStatus
from ..domain.models import TaskRun
from ..services.run_recovery import (
    RecoverabilityReport,
    RecoveryAuthorization,
    RecoveryMode,
)


class TaskRunResponse(BaseModel):
    id: UUID
    task_id: UUID
    run_number: int
    #: Concern 67. The external ``RUN-YYYYMMDD-NNNNNN`` identity, which is what
    #: every artifact directory, log line and piece of operator correspondence
    #: names a run by -- and which this schema did not carry, so recovering the
    #: right run meant reading a run artifact off disk to map ``RUN-...`` to a
    #: durable UUID by hand. That is a safe thing to get wrong exactly once.
    #: It is exposed here rather than made addressable in the route, because
    #: the route must be unambiguous about what ``{run_id}`` means; see
    #: ``api.tasks.recover_run``.
    external_run_id: str | None
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
    #: Concern 67: the fencing token, and whether a dispatch is inside this run.
    execution_generation: int
    execution_owner: str | None
    execution_started_at: datetime | None

    @classmethod
    def from_domain(cls, run: TaskRun) -> TaskRunResponse:
        return cls(
            id=run.id,
            task_id=run.task_id,
            run_number=run.run_number,
            external_run_id=run.external_run_id,
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
            execution_generation=run.execution_generation,
            execution_owner=run.execution_owner,
            execution_started_at=run.execution_started_at,
        )


class RecoverabilityCheckResponse(BaseModel):
    """One durable question asked of a run, and its answer."""

    name: str
    passed: bool
    detail: str


class RecoverabilityResponse(BaseModel):
    """The read-only eligibility answer for ``POST /runs/{run_id}/recover``.

    Produced without writing anything and without taking ownership, so it is
    safe to ask about a run nobody intends to touch. It is a snapshot: the
    mutating operation re-derives the same assessment inside its own
    transaction and guards the acquisition on the generation reported here.
    """

    run_id: UUID
    external_run_id: str | None
    recoverable: bool
    #: Concern 68 and concern 76. ``continue`` when a coder attempt remains inside
    #: the task's budget; ``delivery_only`` when the review already approved a
    #: committed candidate and only delivery is owed, so recovery re-enters the
    #: ordinary delivery node without any model work; ``settlement_only`` when no
    #: attempt remains but the run is non-terminal and the fix loop's deterministic
    #: exhausted-budget settlement is still owed to it; ``None`` when the attempt
    #: accounting could not be trusted, which always accompanies a refusal. Both a
    #: settlement-only and a delivery-only recovery take ownership exactly as a
    #: continuation does and execute no coder attempt.
    recovery_mode: RecoveryMode | None
    execution_generation: int
    execution_owner: str | None
    next_attempt: int | None
    attempts_started: int | None
    reviews_completed: int | None
    review_cycle: int | None
    max_attempts: int | None
    candidate_commit: str | None
    integration_advanced_since_start: bool | None
    checks: list[RecoverabilityCheckResponse]

    @classmethod
    def from_report(cls, report: RecoverabilityReport) -> RecoverabilityResponse:
        return cls(
            run_id=report.run_id,
            external_run_id=report.external_run_id,
            recoverable=report.recoverable,
            recovery_mode=report.recovery_mode,
            execution_generation=report.execution_generation,
            execution_owner=report.execution_owner,
            next_attempt=report.next_attempt,
            attempts_started=report.attempts_started,
            reviews_completed=report.reviews_completed,
            review_cycle=report.review_cycle,
            max_attempts=report.max_attempts,
            candidate_commit=report.candidate_commit,
            integration_advanced_since_start=report.integration_advanced_since_start,
            checks=[
                RecoverabilityCheckResponse(**check.describe())  # type: ignore[arg-type]
                for check in report.checks
            ],
        )


class RunRecoveryResponse(BaseModel):
    """What a completed recovery did, and what the run looks like afterwards."""

    run: TaskRunResponse
    previous_generation: int
    generation: int
    #: Concern 68: what this recovery was authorized to do. A
    #: ``settlement_only`` recovery executed no coder attempt.
    recovery_mode: RecoveryMode | None = None
    next_attempt: int | None = None
    outcome: str | None = None
    state: dict[str, object] | None = None

    @classmethod
    def from_authorization(
        cls,
        authorization: RecoveryAuthorization,
        run: TaskRun,
        state: dict[str, object] | None,
    ) -> RunRecoveryResponse:
        return cls(
            run=TaskRunResponse.from_domain(run),
            previous_generation=authorization.previous_generation,
            generation=authorization.generation,
            recovery_mode=authorization.recovery_mode,
            next_attempt=authorization.next_attempt,
            outcome=str(state.get("outcome")) if state and state.get("outcome") else None,
            state=dict(state) if state else None,
        )


class ProjectRunResponse(BaseModel):
    run_id: UUID | None = None
    outcome: str | None = None
    state: dict[str, object] | None = None
    no_task_reason: str | None = None
