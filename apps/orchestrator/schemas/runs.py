from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from ..domain.enums import ModelPurpose, RunStatus
from ..domain.models import TaskRun
from ..services.campaign import CampaignReport, CampaignStatus
from ..services.model_usage import ModelCallUsage, UsageSummary
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


class CampaignTaskSummaryResponse(BaseModel):
    external_task_id: str
    task_id: UUID
    status: str
    integrated: bool
    run_ids: list[UUID]
    latest_run_status: str | None
    latest_run_failure_reason: str | None
    latest_candidate_commit: str | None
    unintegrated_commit: str | None


class CampaignAdvanceResponse(BaseModel):
    project_id: UUID
    status: CampaignStatus
    tasks_considered: list[str]
    tasks_completed_this_invocation: list[str]
    tasks_already_completed: list[str]
    tasks_escalated: list[str]
    tasks_blocked: list[str]
    current_task: str | None
    current_run_id: UUID | None
    last_task: str | None
    last_run_id: UUID | None
    run_ids: list[UUID]
    resumed_run_ids: list[UUID]
    integrated_commits: list[str]
    stop_reason: str
    autonomous_advance_possible: bool
    transition_limit: int
    transitions_used: int
    tasks: list[CampaignTaskSummaryResponse]

    @classmethod
    def from_report(cls, report: CampaignReport) -> CampaignAdvanceResponse:
        return cls(
            project_id=report.project_id,
            status=report.status,
            tasks_considered=list(report.tasks_considered),
            tasks_completed_this_invocation=list(report.tasks_completed_this_invocation),
            tasks_already_completed=list(report.tasks_already_completed),
            tasks_escalated=list(report.tasks_escalated),
            tasks_blocked=list(report.tasks_blocked),
            current_task=report.current_task,
            current_run_id=report.current_run_id,
            last_task=report.last_task,
            last_run_id=report.last_run_id,
            run_ids=list(report.run_ids),
            resumed_run_ids=list(report.resumed_run_ids),
            integrated_commits=list(report.integrated_commits),
            stop_reason=report.stop_reason,
            autonomous_advance_possible=report.autonomous_advance_possible,
            transition_limit=report.transition_limit,
            transitions_used=report.transitions_used,
            tasks=[
                CampaignTaskSummaryResponse(
                    external_task_id=task.external_task_id,
                    task_id=task.task_id,
                    status=task.status.value,
                    integrated=task.integrated,
                    run_ids=list(task.run_ids),
                    latest_run_status=task.latest_run_status.value
                    if task.latest_run_status is not None
                    else None,
                    latest_run_failure_reason=task.latest_run_failure_reason,
                    latest_candidate_commit=task.latest_candidate_commit,
                    unintegrated_commit=task.unintegrated_commit,
                )
                for task in report.tasks
            ],
        )


def _money(value: Decimal | None) -> str | None:
    """A monetary value as a plain decimal string, or ``None`` for unknown.

    ``format(..., "f")`` rather than ``str()`` because a quantized zero is
    ``Decimal("0E-12")``, whose ``str()`` is ``"0E-12"`` -- exponent notation
    that a client parsing with a plain decimal reader will reject, for a value
    that is simply zero. Every cost therefore goes out in the same fixed-point
    shape at the column's scale.
    """
    return format(value, "f") if value is not None else None


class ModelCallUsageResponse(BaseModel):
    """One model call's usage and its persisted cost (concern 78).

    ``cost`` is a decimal *string*, not a float. The value can be a small
    fraction of a cent and a client will sum many of them; serialising through
    a binary float would reintroduce exactly the error the persisted column
    exists to avoid. ``null`` means the cost is unknown -- no pricing was
    configured for the model, or the endpoint reported no usage -- and is not
    the same as ``"0"``, which is what a model priced at zero records.
    """

    model_run_id: UUID
    model_id: UUID
    model: str
    provider: str
    purpose: ModelPurpose
    status: RunStatus
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    cost: str | None
    currency: str | None
    attempt: int | None
    review_cycle: int | None

    @classmethod
    def from_domain(cls, call: ModelCallUsage) -> ModelCallUsageResponse:
        return cls(
            model_run_id=call.model_run_id,
            model_id=call.model_id,
            model=call.model_name,
            provider=call.provider,
            purpose=call.purpose,
            status=call.status,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            duration_ms=call.duration_ms,
            cost=_money(call.cost),
            currency=call.currency,
            attempt=call.attempt,
            review_cycle=call.review_cycle,
        )


class UsageSummaryResponse(BaseModel):
    """Totals over a set of model calls, with the unknowns declared.

    ``known_cost`` is the sum of the calls that *could* be priced, and
    ``has_unknown_cost`` says whether that is the whole story. They are
    reported as two fields rather than one total on purpose: a run where one
    call could not be priced has no total, and publishing the known part as
    though it were one would understate the spend in the direction nobody
    checks.

    ``known_cost`` and ``currency`` are ``null`` when the priced calls do not
    share a single currency; ``known_cost_by_currency`` then carries the
    separate totals. Nothing converts between currencies.
    """

    model_calls: int
    input_tokens: int
    output_tokens: int
    #: Calls that reported no tokens at all, so the token totals are a floor.
    calls_without_usage: int
    known_cost: str | None
    currency: str | None
    has_unknown_cost: bool
    mixed_currencies: bool
    known_cost_by_currency: dict[str, str]

    @classmethod
    def from_domain(cls, summary: UsageSummary) -> UsageSummaryResponse:
        return cls(
            model_calls=summary.model_calls,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
            calls_without_usage=summary.calls_without_usage,
            known_cost=_money(summary.known_cost),
            currency=summary.currency,
            has_unknown_cost=summary.has_unknown_cost,
            mixed_currencies=summary.mixed_currencies,
            known_cost_by_currency={
                currency: format(amount, "f")
                for currency, amount in sorted(summary.known_cost_by_currency.items())
            },
        )


class RunUsageResponse(BaseModel):
    """``GET /runs/{run_id}/usage`` and ``GET /projects/{id}/usage``.

    The per-call list is included rather than only the totals because the
    question this endpoint exists to answer is usually comparative -- what did
    the cloud coder cost next to the local reviewer -- and a single total
    cannot answer it. ``by_purpose`` and ``by_model`` are the same calls
    re-totalled, so each breakdown carries its own ``has_unknown_cost``.
    """

    run_id: UUID | None = None
    project_id: UUID | None = None
    totals: UsageSummaryResponse
    calls: list[ModelCallUsageResponse]
    by_purpose: dict[str, UsageSummaryResponse]
    by_model: dict[str, UsageSummaryResponse]

    @classmethod
    def from_summary(
        cls,
        summary: UsageSummary,
        *,
        run_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> RunUsageResponse:
        return cls(
            run_id=run_id,
            project_id=project_id,
            totals=UsageSummaryResponse.from_domain(summary),
            calls=[ModelCallUsageResponse.from_domain(call) for call in summary.calls],
            by_purpose={
                purpose.value: UsageSummaryResponse.from_domain(part)
                for purpose, part in summary.by_purpose().items()
            },
            by_model={
                name: UsageSummaryResponse.from_domain(part)
                for name, part in summary.by_model().items()
            },
        )
