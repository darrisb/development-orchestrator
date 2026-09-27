"""The Review Agent (build.md sections 21-24, phase I).

One review, start to finish: assemble the bounded package, send it to the
configured ``ReviewProvider``, persist the structured answer, apply the
human-approval policy, move the task, and -- when a human must decide --
leave an escalation written the way section 24 asks for.

What this module does *not* do is as important as what it does.

* **It does not decide whether code is acceptable.** The reviewer does that,
  and ``domain.review`` turns the answer into a route. The agent is the order
  those happen in and the record they leave.
* **It does not run the fix loop.** A ``CHANGES_REQUESTED`` routing carries
  the correction text, and phase J sends it. Doing it here would put the
  retry ceiling inside the thing being counted.
* **It does not invent a verdict when the reviewer fails.** An unreachable
  reviewer and an unparseable answer both raise, because a verified candidate
  with no review is a state the workflow must handle, not a state to paper
  over with a default decision (principle 3).

A review is persisted before it is routed. If the process dies between the
two, the next run sees a stored review and a task still in ``REVIEWING``,
which is recoverable; the other order would lose the reviewer's answer and
charge for it again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from time import monotonic
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.completion import CompletionReport
from ..domain.enums import (
    EscalationStatus,
    ModelPurpose,
    ReviewDecision,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from ..domain.models import HumanEscalation, Project, Review, RunEvent, Task, TaskRun
from ..domain.redaction import Redactor
from ..domain.review import (
    REVIEW_SCHEMA_VERSION,
    HumanApprovalPolicy,
    ReviewResult,
    ReviewRouting,
    escalation_options,
    render_escalation,
    route_review,
)
from ..domain.review_package import (
    REVIEW_PACKAGE_ARTIFACT,
    ReviewPackage,
    redact_package,
)
from ..domain.scope import ScopePolicy, SensitiveCategory, evaluate_scope
from ..domain.state_machine import can_transition
from ..domain.verification import VerificationReport
from ..providers.review import ReviewCall, ReviewProvider, ReviewRequest
from ..repositories import (
    EscalationRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from ..services import artifact_store
from ..services.model_runs import describe_error, ensure_model, record_model_call
from ..services.review_context import MAX_REVIEW_DIFF_BYTES, build_review_package
from ..services.workspace import (
    DiffCapture,
    TaskWorkspace,
    capture_diff,
    load_run_context,
)
from .review_prompts import REVIEWER_PROMPT_VERSION
from .timing import elapsed_ms, started_at

logger = get_logger(__name__)

#: Artifact names, following section 9's layout.
REVIEW_PACKAGE_MANIFEST_ARTIFACT = "review-package.json"
REVIEW_PROMPT_ARTIFACT = "review-prompt.txt"
REVIEW_RESPONSE_ARTIFACT = "review-response.txt"
REVIEW_ARTIFACT = "review.json"
ESCALATION_ARTIFACT = "escalation.txt"

#: What the run records as the review contract it was served under: the
#: reviewer's instructions plus the schema its answer had to satisfy.
REVIEW_PROMPT_CONTRACT = f"{REVIEWER_PROMPT_VERSION}+{REVIEW_SCHEMA_VERSION}"


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """Everything one review cycle produced."""

    task_run_id: UUID
    external_task_id: str
    cycle: int
    package: ReviewPackage
    result: ReviewResult
    review: Review
    routing: ReviewRouting
    escalation: HumanEscalation | None = None
    artifacts: Mapping[str, str] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.routing.approved

    @property
    def feedback(self) -> str | None:
        """The correction text for the next coding attempt, when there is one."""
        return self.routing.feedback


def policy_from_settings(settings: Settings | None = None) -> HumanApprovalPolicy:
    """The human-approval policy this installation runs under (section 37)."""
    config = settings or get_settings()
    if not config.review_human_approval_enabled:
        return HumanApprovalPolicy(
            gated_categories=frozenset(),
            min_confidence=None,
            max_deleted_files=10**6,
        )
    return HumanApprovalPolicy(
        min_confidence=config.review_min_confidence or None,
        max_deleted_files=config.review_max_deleted_files,
    )


async def run_review(
    session: Session,
    workspace: TaskWorkspace,
    *,
    provider: ReviewProvider,
    verification: VerificationReport | None = None,
    completion_report: CompletionReport | None = None,
    diff: DiffCapture | None = None,
    package: ReviewPackage | None = None,
    settings: Settings | None = None,
    policy: HumanApprovalPolicy | None = None,
    checkpoint_call: Callable[[], None] | None = None,
) -> ReviewOutcome:
    """Review the candidate in ``workspace`` and route the result.

    Args:
        workspace: the run's worktree, holding the verified candidate.
        provider: the reviewer. Selected by the caller, because which model
            reviews is routing policy (section 31) and not the agent's choice.
        verification: the pipeline's report. Passed through to the package and
            to the routing, whose ``REQUIRE_REVIEW`` findings it carries.
        package: a pre-built package, for a re-review that must be judged
            against exactly what a previous cycle saw. Built here when absent.
        checkpoint_call: committed after the model's call is recorded, including
            before a failure is re-raised, so an unreachable reviewer is a row
            rather than a gap.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        ReviewerUnavailable: the reviewer could not produce a review.
        InvalidModelResponse: it answered with something that is not one.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    cycle = run.review_cycle + 1
    prefix = artifact_store.attempt_prefix(run, cycle=cycle)
    sink = _ArtifactSink(
        session=session, task_run_id=run.id, settings=config, prefix=prefix
    )

    _transition(session, task, TaskStatus.REVIEWING)
    built = package or build_review_package(
        session,
        workspace,
        verification=verification,
        completion_report=completion_report,
        diff=diff,
        cycle=cycle,
        settings=config,
    )
    if config.redact_review_package():
        # Section 36 applies to everything that leaves the orchestrator, and the
        # package is the largest thing that does: the raw diff and the raw
        # contents of supporting files, to whatever ``REVIEW_BASE_URL`` points
        # at (concern 28). Redacted *before* the artifact is written, so what is
        # stored, what is hashed and what is sent are the same bytes.
        built = redact_package(
            built, Redactor.for_values([config.review_api_key or None])
        )
    sink.text(REVIEW_PACKAGE_ARTIFACT, built.render())
    sink.json(REVIEW_PACKAGE_MANIFEST_ARTIFACT, built.describe())

    reviewer = ensure_model(session, provider.config)
    _emit(
        session,
        run,
        task,
        project,
        RunEventType.REVIEW_STARTED,
        {
            "cycle": cycle,
            "provider_id": provider.config.provider_id,
            "model_name": provider.config.model_name,
            "reviewer_model_id": str(reviewer.id),
            "prompt_version": REVIEW_PROMPT_CONTRACT,
            "package_hash": built.content_hash,
            "package_complete": built.complete,
            "package_redacted": built.redacted,
            "files_changed": built.files_changed,
        },
    )

    call = await _review_recorded(
        session, provider, built, cycle=cycle, run=run, sink=sink,
        checkpoint_call=checkpoint_call,
    )
    result = call.result
    # The cycle is spent only once a reviewer has actually returned a verdict.
    # Counting it before the call would let three unreachable-reviewer retries
    # exhaust a task's review budget without a reviewer ever having read the
    # change, which is not what section 23 is counting.
    TaskRunRepository(session).update_fields(run.id, review_cycle=cycle)
    run.review_cycle = cycle

    review = ReviewRepository(session).add(result.to_review(task_run_id=run.id, cycle=cycle))
    routing = _route(
        session, workspace, run, task, project, result, diff=diff, config=config, policy=policy,
        verification=verification, completion_report=completion_report, cycle=cycle,
    )
    sink.json(
        REVIEW_ARTIFACT,
        {
            "review_id": str(review.id),
            "cycle": cycle,
            "package_hash": built.content_hash,
            "prompt_version": REVIEW_PROMPT_CONTRACT,
            **result.describe(),
            "routing": routing.describe(),
        },
    )

    escalation = None
    if routing.needs_human:
        escalation = _escalate(
            session, run, task, project, result, routing, sink=sink
        )
    _apply(session, run, task, project, result, routing, cycle=cycle)

    logger.info(
        "review_finished",
        run_id=str(run.id),
        task=task.external_task_id,
        cycle=cycle,
        reviewer_decision=result.decision.value,
        decision=routing.decision.value,
        blocking_issues=len(result.blocking_issues),
        confidence=result.confidence,
        warnings=len(result.warnings),
        escalated=escalation is not None,
    )
    return ReviewOutcome(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        cycle=cycle,
        package=built,
        result=result,
        review=review,
        routing=routing,
        escalation=escalation,
        artifacts=dict(sink.paths),
    )


# ------------------------------------------------------------------- routing


def _route(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    task: Task,
    project: Project,
    result: ReviewResult,
    *,
    diff: DiffCapture | None,
    config: Settings,
    policy: HumanApprovalPolicy | None,
    verification: VerificationReport | None,
    completion_report: CompletionReport | None,
    cycle: int,
) -> ReviewRouting:
    """Apply sections 22, 23 and 37 to the reviewer's answer.

    The sensitive categories are re-measured from the diff rather than read
    off the verification report: section 37 is a gate on *accepting* this
    change, and it must not depend on whether a report was supplied.
    """
    captured = diff or capture_diff(workspace, max_bytes=MAX_REVIEW_DIFF_BYTES)
    scope = evaluate_scope(captured.summary, ScopePolicy.for_task(task, project))
    approval_policy = policy or policy_from_settings(config)
    if project.approval_gated_categories is not None:
        approval_policy = replace(
            approval_policy,
            gated_categories=frozenset(
                SensitiveCategory(value)
                for value in project.approval_gated_categories
            ),
        )
    return route_review(
        result,
        cycle=cycle,
        max_review_cycles=task.limits.max_review_cycles,
        policy=approval_policy,
        sensitive=scope.sensitive,
        deleted_paths=captured.summary.deleted_paths,
        pending_review_reasons=tuple(
            [
                *(verification.human_review_reasons if verification else ()),
                *(
                    (
                        "the candidate changed files outside its approved plan: "
                        + ", ".join(completion_report.unplanned_paths),
                    )
                    if completion_report and completion_report.unplanned_paths
                    else ()
                ),
            ]
        ),
    )


def _apply(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    result: ReviewResult,
    routing: ReviewRouting,
    *,
    cycle: int,
) -> None:
    """Move the task and emit the event this routing calls for (section 40)."""
    event_type = {
        ReviewDecision.APPROVED: RunEventType.APPROVED,
        ReviewDecision.CHANGES_REQUESTED: RunEventType.CHANGES_REQUESTED,
        ReviewDecision.HUMAN_REVIEW_REQUIRED: RunEventType.HUMAN_REVIEW_REQUIRED,
    }[routing.decision]
    _transition(session, task, routing.task_status)
    _emit(
        session,
        run,
        task,
        project,
        event_type,
        {
            "cycle": cycle,
            "summary": result.summary,
            "confidence": result.confidence,
            "risk": result.risk.value if result.risk else None,
            "issues": len(result.issues),
            "review_warnings": list(result.warnings),
            **routing.describe(),
        },
    )


def _escalate(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    result: ReviewResult,
    routing: ReviewRouting,
    *,
    sink: _ArtifactSink,
) -> HumanEscalation:
    """Write the escalation a human will read (section 24).

    The summary is generated rather than stored as a template so that the
    reasons, the attempts and the reviewer's own words are the ones from this
    run. Section 24's requirement is that nobody has to reconstruct the
    history, and a record that says only "review failed" would fail it.
    """
    options = escalation_options(routing)
    summary = render_escalation(
        external_task_id=task.external_task_id,
        reason=(
            "The task's review budget is spent."
            if routing.retry_exhausted
            else "A human decision is required before this change can be accepted."
        ),
        requirement=task.instructions or task.title,
        routing=routing,
        result=result,
        attempts=(
            f"coding attempt {run.attempt_number}",
            f"review cycle {run.review_cycle}",
        ),
        options=options,
        restored_commit=run.starting_commit,
    )
    sink.text(ESCALATION_ARTIFACT, summary)
    escalation = EscalationRepository(session).add(
        HumanEscalation(
            task_id=task.id,
            task_run_id=run.id,
            reason=(
                routing.failure_reason.value
                if routing.failure_reason
                else ReviewDecision.HUMAN_REVIEW_REQUIRED.value
            ),
            summary=summary,
            options=list(options),
            status=EscalationStatus.OPEN,
        )
    )
    logger.warning(
        "human_review_required",
        run_id=str(run.id),
        task=task.external_task_id,
        escalation_id=str(escalation.id),
        reasons=list(routing.human_review_reasons),
    )
    return escalation


# ------------------------------------------------------------------- helpers


async def _review_recorded(
    session: Session,
    provider: ReviewProvider,
    package: ReviewPackage,
    *,
    cycle: int,
    run: TaskRun,
    sink: _ArtifactSink,
    checkpoint_call: Callable[[], None] | None = None,
) -> ReviewCall:
    """Send one review, keep both artifacts, and record the call.

    A failure is recorded before it is re-raised, for the same reason the
    coding agent does it, and committed for the same reason: how often a
    reviewer is unreachable or answers unusably is exactly what section 35
    wants to be able to ask, and a row inside a transaction that is about to
    roll back is not a record of anything.

    ``duration_ms`` is measured rather than defaulted, so a reviewer that hung
    until its timeout is distinguishable from one that was refused instantly.
    """
    request = ReviewRequest(package=package, cycle=cycle)
    started = monotonic()
    try:
        call = await provider.review(request)
    except Exception as error:
        record_model_call(
            session,
            task_run_id=run.id,
            config=provider.config,
            purpose=ModelPurpose.REVIEW,
            status=RunStatus.FAILED,
            duration_ms=elapsed_ms(started),
            started_at=started_at(started),
            error_detail=describe_error(error),
            attempt=run.attempt_number,
            review_cycle=cycle,
        )
        if checkpoint_call is not None:
            checkpoint_call()
        raise
    sink.text(REVIEW_PROMPT_ARTIFACT, call.prompt_text)
    sink.text(REVIEW_RESPONSE_ARTIFACT, call.raw_response)
    record_model_call(
        session,
        task_run_id=run.id,
        config=provider.config,
        purpose=ModelPurpose.REVIEW,
        status=RunStatus.SUCCEEDED,
        duration_ms=call.result.duration_ms,
        usage=call.usage,
        prompt_artifact=sink.paths.get(REVIEW_PROMPT_ARTIFACT),
        response_artifact=sink.paths.get(REVIEW_RESPONSE_ARTIFACT),
        started_at=started_at(started),
        attempt=run.attempt_number,
        review_cycle=cycle,
    )
    if checkpoint_call is not None:
        checkpoint_call()
    return call


@dataclass(slots=True)
class _ArtifactSink:
    """Writes this cycle's artifacts and remembers where each one landed."""

    session: Session
    task_run_id: UUID
    settings: Settings
    prefix: str = ""
    paths: dict[str, str] = field(default_factory=dict)

    def text(self, name: str, text: str) -> None:
        stored = artifact_store.write_text(
            self.session,
            self.task_run_id,
            self.prefix + name,
            text,
            kind=name,
            settings=self.settings,
        )
        self.paths[name] = stored.relative_path

    def json(self, name: str, payload: object) -> None:
        stored = artifact_store.write_json(
            self.session,
            self.task_run_id,
            self.prefix + name,
            payload,
            kind=name,
            settings=self.settings,
        )
        self.paths[name] = stored.relative_path


def _emit(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    event_type: RunEventType,
    payload: dict[str, object],
) -> None:
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=project.id,
            task_id=task.id,
            event_type=event_type,
            attempt=run.attempt_number,
            payload=payload,
        )
    )


def _transition(session: Session, task: Task, status: TaskStatus) -> None:
    """Move the task to ``status`` when the move is legal.

    Logged rather than raised when it is not, for the same reason as in the
    coding agent: the review's outcome is the interesting record, and losing
    it to a bookkeeping error would be the worse failure.
    """
    if task.status is status:
        return
    if not can_transition(task.status, status):
        logger.warning(
            "review_transition_skipped",
            task=task.external_task_id,
            current=task.status.value,
            requested=status.value,
        )
        return
    TaskRepository(session).transition(task.id, status)
    task.status = status


__all__ = [
    "ESCALATION_ARTIFACT",
    "REVIEW_ARTIFACT",
    "REVIEW_PACKAGE_MANIFEST_ARTIFACT",
    "REVIEW_PROMPT_ARTIFACT",
    "REVIEW_PROMPT_CONTRACT",
    "REVIEW_RESPONSE_ARTIFACT",
    "ReviewOutcome",
    "policy_from_settings",
    "run_review",
]
