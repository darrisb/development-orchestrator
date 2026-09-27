"""The Coding Agent (build.md section 14, phase G).

One attempt, start to finish: build the context, ask for a plan when the task
is complex enough to need one, validate that plan, ask for code, apply the
edits the policy permits, capture the diff, measure it against the task's
scope, and write a completion report. Then stop. Nothing here verifies,
reviews or commits -- phases H, I and C own those, and an agent that did them
would be deciding whether its own work succeeded.

The agent owns no policy of its own. What may be written comes from
``domain.scope``, what a plan may propose from ``domain.plan``, what an edit
may look like from ``domain.edits``, and what the coder is told from
``agents.prompts``. This module is the order those are applied in, and the
record it leaves behind.

**What raises and what is returned.** Everything the coder is responsible for
-- a refused plan, unparseable edits, an out-of-scope write, a change set that
changed nothing -- comes back as a ``CodingAttempt`` carrying a
``FailureReason`` and the feedback to send with the next attempt. Everything
else -- an unreachable endpoint, a timeout, a prompt that does not fit --
raises, because those are not the model's output, their policy is retry, and
the workflow owns the retry ceiling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.completion import (
    COMPLETION_REPORT_ARTIFACT,
    CompletionReport,
    build_completion_report,
)
from ..domain.edits import (
    EDIT_SCHEMA,
    EDIT_SCHEMA_VERSION,
    CodeChangeSet,
    MalformedChangeSet,
    max_edit_bytes_for_context,
)
from ..domain.enums import (
    FailureReason,
    ModelPurpose,
    RunEventType,
    RunStatus,
    ScopePolicyDecision,
    TaskStatus,
)
from ..domain.models import Project, RunEvent, Task, TaskRun
from ..domain.plan import (
    PLAN_SCHEMA,
    CodingPlan,
    PlanAssessment,
    requires_plan,
    validate_plan,
)
from ..domain.scope import ScopeAssessment, ScopePolicy, evaluate_scope
from ..domain.state_machine import can_transition
from ..providers import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StructuredSchema,
    TokenUsage,
)
from ..repositories import RunEventRepository, TaskRepository, TaskRunRepository
from ..services import artifact_store
from ..services.code_edits import EditApplication, apply_change_set
from ..services.context_builder import ContextBuildResult, build_task_context
from ..services.model_runs import (
    coding_purpose,
    ensure_model,
    record_model_call,
    record_response,
)
from ..services.workspace import DiffCapture, TaskWorkspace, capture_diff, load_run_context
from .prompts import (
    CODER_PROMPT_VERSION,
    CODER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    render_coding_instructions,
    render_plan_instructions,
)

logger = get_logger(__name__)

#: Artifact names, following section 9's suggested layout.
PLAN_PROMPT_ARTIFACT = "plan-prompt.txt"
PLAN_RESPONSE_ARTIFACT = "plan-response.txt"
PLAN_ARTIFACT = "plan.json"
CODER_PROMPT_ARTIFACT = "prompt.txt"
CODER_RESPONSE_ARTIFACT = "coder-response.txt"
CANDIDATE_PATCH_ARTIFACT = "candidate.patch"

#: What the run records as the prompt contract it was served under. The task
#: block's own version is in ``context-manifest.json`` and is folded into the
#: context hash, so this names the two parts the manifest does not: the coder's
#: instructions and the edit schema its answer had to satisfy (section 34).
CODING_PROMPT_CONTRACT = f"{CODER_PROMPT_VERSION}+{EDIT_SCHEMA_VERSION}"

#: Ceiling on the diff text kept as an artifact and handed on to a reviewer.
#: The structured summary is never truncated, so the guards always measure the
#: whole change even when its text is clipped.
MAX_DIFF_ARTIFACT_BYTES = 512_000


@dataclass(frozen=True, slots=True)
class CodingAttempt:
    """Everything one coding attempt produced.

    ``failure_reason`` is ``None`` only when the attempt produced a candidate
    the scope guard did not block. That is not "the code works": whether it
    works is phase H's answer.
    """

    task_run_id: UUID
    external_task_id: str
    attempt: int
    context_hash: str
    provider_id: str
    model_name: str
    plan: CodingPlan | None = None
    plan_assessment: PlanAssessment | None = None
    change_set: CodeChangeSet | None = None
    application: EditApplication | None = None
    diff: DiffCapture | None = None
    scope: ScopeAssessment | None = None
    report: CompletionReport | None = None
    failure_reason: FailureReason | None = None
    #: Text to send back with the next attempt, when there is a next attempt.
    feedback: str | None = None
    duration_ms: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    artifacts: Mapping[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.failure_reason is None

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return self.application.applied_paths if self.application else ()


async def run_coding_attempt(
    session: Session,
    workspace: TaskWorkspace,
    *,
    provider: ModelProvider,
    settings: Settings | None = None,
    review_feedback: str | None = None,
    context: ContextBuildResult | None = None,
    plan_required: bool | None = None,
) -> CodingAttempt:
    """Run one coding attempt inside ``workspace``.

    Args:
        workspace: the run's isolated worktree. Every write lands here; the
            managed repository is never touched.
        provider: the coder. Selected by the caller, because which model serves
            a role is routing policy (section 31) and not the agent's choice.
        review_feedback: findings from the previous cycle, for a fix attempt
            (section 23). Passed through to the prompt untouched.
        context: a pre-built context package, for a retry that should use the
            same context as the attempt before it. Built here when absent.
        plan_required: overrides ``domain.plan.requires_plan``. The fix loop
            passes ``False`` for a correction attempt: the approach was already
            planned and validated, the reviewer's findings are what the attempt
            is following now, and asking a complex task to re-plan from scratch
            spends a model call on a plan the coder is not being asked for and
            risks refusing an attempt over an approach that is not the subject.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        ModelProviderError: the endpoint failed, timed out, or answered in a
            shape that is not usable at all.
        ContextBudgetTooSmall: the configured budget cannot hold the task.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    policy = ScopePolicy.for_task(task, project)
    sink = _ArtifactSink(
        session=session,
        task_run_id=run.id,
        settings=config,
        prefix=artifact_store.attempt_prefix(run),
    )

    built = context or build_task_context(
        session, run.id, workspace=workspace, settings=config
    )
    # Which model coded this run, recorded before the first call rather than
    # after the last: a run that fails mid-attempt must still say who was
    # asked (concern 3, sections 34 and 35).
    coder = ensure_model(session, provider.config)
    TaskRunRepository(session).update_fields(
        run.id,
        status=RunStatus.RUNNING,
        prompt_version=CODING_PROMPT_CONTRACT,
        coder_model_id=coder.id,
    )

    # Section 15 clips any single context item to the per-item budget, and the
    # edit contract asks for the *complete new contents* of every file the
    # coder changes. For a writable file the coder has only seen the start of,
    # those two demands cannot both be met: it will invent or drop the part it
    # could not read, and the result is a large deletion in the diff rather
    # than an error. Refused here, before a model is asked, because no feedback
    # the coder could act on would change the outcome (concern 1).
    clipped = _clipped_writable_paths(built, policy)
    if clipped:
        return _failed(
            session, run, task, project, built, provider,
            FailureReason.HUMAN_DECISION_REQUIRED,
            feedback=(
                "This attempt was refused before it started. The task may write "
                + ", ".join(clipped)
                + ", and the context budget could only show the coder part of "
                + ("each of those files" if len(clipped) > 1 else "that file")
                + ". Asking for the complete new contents of a file it has not "
                "fully read would destroy the part it could not see. Raise "
                "CONTEXT_MAX_ITEM_TOKENS, or split the task so each attempt "
                "writes a smaller file."
            ),
            plan=None, assessment=None, duration_ms=0, usage=TokenUsage(), sink=sink,
        )

    plan: CodingPlan | None = None
    assessment: PlanAssessment | None = None
    if requires_plan(task) if plan_required is None else plan_required:
        _transition(session, task, TaskStatus.PLANNING)
        plan, assessment, plan_usage = await _request_plan(
            session, run, task, project, built, provider, policy, sink
        )
        if not assessment.approved or assessment.needs_human:
            return _refused_plan_attempt(
                session, run, task, project, built, provider, plan, assessment,
                plan_usage, sink,
            )

    _transition(session, task, TaskStatus.CODING)
    _emit(
        session,
        run,
        task,
        project,
        RunEventType.CODING_STARTED,
        {
            "context_hash": built.context_hash,
            "planned": plan is not None,
            "provider_id": provider.config.provider_id,
            "model_name": provider.config.model_name,
            "prompt_version": CODING_PROMPT_CONTRACT,
            "is_fix_attempt": review_feedback is not None,
        },
    )

    request = ModelRequest(
        system_instructions=CODER_SYSTEM_PROMPT,
        task_instructions=render_coding_instructions(task, plan),
        context=built.text,
        review_feedback=review_feedback,
        schema=StructuredSchema(name="code_edits", schema=EDIT_SCHEMA),
        metadata={"task": task.external_task_id, "purpose": "code"},
    )
    response = await _generate_recorded(
        session,
        provider,
        request,
        task_run_id=run.id,
        purpose=coding_purpose(review_feedback is not None),
        prompt_artifact=CODER_PROMPT_ARTIFACT,
        response_artifact=CODER_RESPONSE_ARTIFACT,
        sink=sink,
    )

    try:
        change_set = CodeChangeSet.from_payload(
            response.data or {},
            # Concern 11: the same number that bounded what the context builder
            # could show of a file bounds what may come back for it.
            max_edit_bytes=max_edit_bytes_for_context(config.context_max_item_tokens),
        )
    except MalformedChangeSet as error:
        # The endpoint answered and the JSON parsed; what came back was not a
        # usable set of edits. That is evidence for the coder, not a retry.
        return _failed(
            session,
            run,
            task,
            project,
            built,
            provider,
            FailureReason.INVALID_MODEL_RESPONSE,
            feedback=(
                f"Your previous answer could not be applied: {error}. Return one JSON "
                f"object with an 'edits' array, and the complete new contents of every "
                f"file you change."
            ),
            plan=plan,
            assessment=assessment,
            duration_ms=response.duration_ms,
            usage=response.usage,
            sink=sink,
        )

    application = apply_change_set(change_set, root=workspace.path, policy=policy)
    diff = capture_diff(workspace, max_bytes=MAX_DIFF_ARTIFACT_BYTES)
    scope = evaluate_scope(diff.summary, policy)
    report = build_completion_report(
        external_task_id=task.external_task_id,
        attempt=run.attempt_number,
        change_set=change_set,
        applied_paths=application.written,
        deleted_paths=application.deleted,
        rejected_edits=application.rejected,
        scope=scope,
        planned=plan is not None,
        planned_paths=plan.write_paths if plan else (),
        warnings=application.warnings,
    )
    sink.text(CANDIDATE_PATCH_ARTIFACT, diff.text)
    sink.json(COMPLETION_REPORT_ARTIFACT, report.describe())

    failure_reason, feedback = _judge(application, scope, report)
    _emit(
        session,
        run,
        task,
        project,
        RunEventType.CODING_COMPLETED,
        {
            "written": list(application.written),
            "deleted": list(application.deleted),
            "rejected": [edit.describe() for edit in application.rejected],
            "files_changed": scope.files_changed,
            "diff_lines": scope.diff_lines,
            "scope_decision": scope.decision.value,
            "scope_findings": [finding.describe() for finding in scope.findings],
            "discrepancies": list(report.discrepancies),
            "failure_reason": failure_reason.value if failure_reason else None,
            "model_name": response.model_name,
        },
    )
    logger.info(
        "coding_attempt_finished",
        run_id=str(run.id),
        task=task.external_task_id,
        attempt=run.attempt_number,
        written=len(application.written),
        rejected=len(application.rejected),
        files_changed=scope.files_changed,
        diff_lines=scope.diff_lines,
        scope_decision=scope.decision.value,
        failure_reason=failure_reason.value if failure_reason else None,
    )
    return CodingAttempt(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        attempt=run.attempt_number,
        context_hash=built.context_hash,
        provider_id=response.provider_id,
        model_name=response.model_name,
        plan=plan,
        plan_assessment=assessment,
        change_set=change_set,
        application=application,
        diff=diff,
        scope=scope,
        report=report,
        failure_reason=failure_reason,
        feedback=feedback,
        duration_ms=response.duration_ms,
        usage=response.usage,
        artifacts=dict(sink.paths),
    )


# ---------------------------------------------------------------------- plan


async def _request_plan(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    built: ContextBuildResult,
    provider: ModelProvider,
    policy: ScopePolicy,
    sink: _ArtifactSink,
) -> tuple[CodingPlan, PlanAssessment, TokenUsage]:
    """Ask for a plan and validate it (section 14, phase G items 2 and 3)."""
    request = ModelRequest(
        system_instructions=PLANNER_SYSTEM_PROMPT,
        task_instructions=render_plan_instructions(task),
        context=built.text,
        schema=StructuredSchema(name="coding_plan", schema=PLAN_SCHEMA),
        metadata={"task": task.external_task_id, "purpose": "plan"},
    )
    response = await _generate_recorded(
        session,
        provider,
        request,
        task_run_id=run.id,
        purpose=ModelPurpose.PLAN,
        prompt_artifact=PLAN_PROMPT_ARTIFACT,
        response_artifact=PLAN_RESPONSE_ARTIFACT,
        sink=sink,
    )

    plan = CodingPlan.from_payload(response.data or {})
    assessment = validate_plan(plan, task, policy)
    sink.json(PLAN_ARTIFACT, {"plan": plan.describe(), "validation": assessment.describe()})
    _emit(
        session,
        run,
        task,
        project,
        RunEventType.PLAN_CREATED,
        {
            "decision": assessment.decision.value,
            "files_to_modify": list(plan.files_to_modify),
            "files_to_create": list(plan.files_to_create),
            "steps": len(plan.approach),
            "findings": [finding.describe() for finding in assessment.findings],
            "model_name": response.model_name,
        },
    )
    logger.info(
        "plan_validated",
        run_id=str(run.id),
        task=task.external_task_id,
        decision=assessment.decision.value,
        write_paths=len(plan.write_paths),
    )
    return plan, assessment, response.usage


def _refused_plan_attempt(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    built: ContextBuildResult,
    provider: ModelProvider,
    plan: CodingPlan,
    assessment: PlanAssessment,
    usage: TokenUsage,
    sink: _ArtifactSink,
) -> CodingAttempt:
    """A plan that may not be executed. Nothing was written, so nothing to undo.

    Section 14 offers two answers and they are different: a plan that would
    write where it may not is *rejected* and the coder can try again with the
    reasons; a plan that is merely suspiciously wide is *escalated*, because
    nobody but a human can say whether a task was under-specified or misread.
    """
    reason = (
        FailureReason.HUMAN_DECISION_REQUIRED
        if assessment.needs_human
        else FailureReason.SCOPE_VIOLATION
    )
    return _failed(
        session,
        run,
        task,
        project,
        built,
        provider,
        reason,
        feedback=assessment.feedback(),
        plan=plan,
        assessment=assessment,
        duration_ms=0,
        usage=usage,
        sink=sink,
    )


# -------------------------------------------------------------------- refusal


def _clipped_writable_paths(
    built: ContextBuildResult, policy: ScopePolicy
) -> tuple[str, ...]:
    """Files the coder may rewrite but was shown only part of (concern 1).

    Only files inside a *declared* allowance. A task that declared none is not
    checked, and that is a known gap rather than an oversight: with no
    allowance every file in the package is nominally writable, so refusing on
    any clipped item would refuse most tasks on a repository with large files.
    The size and protected-path guards still apply to what such a task writes,
    and the manifest is where the real fix lives -- a task that says what it
    changes gets this protection.
    """
    if not policy.has_allowance:
        return ()
    return tuple(
        path
        for path in built.package.truncated_paths
        if policy.is_allowed(path) and not policy.is_inspect_only(path)
    )


# ------------------------------------------------------------------- judging


def _judge(
    application: EditApplication, scope: ScopeAssessment, report: CompletionReport
) -> tuple[FailureReason | None, str | None]:
    """Turn the measured outcome into one reason and one piece of feedback.

    Order matters, and it is the order of what the coder can do about it. A
    coder that went out of bounds is told that first, because "you wrote
    nothing" is misleading when the writes were refused for leaving the
    allowance. A coder whose edits simply did not match the tree wrote nothing
    either, but that is evidence it can act on, so it must not be read as a
    scope violation just because an empty diff also fails the guard.
    """
    if application.scope_refusals:
        refusals = "; ".join(
            f"{edit.path}: {edit.reason}" for edit in application.scope_refusals
        )
        return FailureReason.SCOPE_VIOLATION, (
            f"These edits were refused and never written: {refusals}. Change only the "
            f"files the task allows."
        )
    if not application.changed_anything:
        rejected = "; ".join(
            f"{edit.path}: {edit.reason}" for edit in application.rejected
        )
        return FailureReason.INVALID_MODEL_RESPONSE, (
            "None of your edits could be applied"
            + (f": {rejected}." if rejected else ".")
            + " Name files that exist for 'update' and use 'create' for new ones."
        )
    if scope.decision is ScopePolicyDecision.BLOCK:
        return FailureReason.SCOPE_VIOLATION, (
            "The change was blocked by the scope guard: " + scope.summary()
        )
    if report.discrepancies:
        # Not a failure: the candidate stands or falls on the verification
        # commands. The discrepancies are recorded so the reviewer sees them.
        logger.info(
            "completion_report_discrepancies",
            task=report.external_task_id,
            discrepancies=list(report.discrepancies),
        )
    return None, None


def _failed(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    built: ContextBuildResult,
    provider: ModelProvider,
    reason: FailureReason,
    *,
    feedback: str,
    plan: CodingPlan | None,
    assessment: PlanAssessment | None,
    duration_ms: int,
    usage: TokenUsage,
    sink: _ArtifactSink,
) -> CodingAttempt:
    """Record a failed attempt and return it. The run is not closed here.

    Whether a failure ends the run, retries it or escalates is
    ``domain.failure_policy``'s answer and the workflow's decision; an agent
    that closed the run would be making it.
    """
    _emit(
        session,
        run,
        task,
        project,
        RunEventType.CODING_COMPLETED,
        {"failure_reason": reason.value, "feedback": feedback},
    )
    logger.warning(
        "coding_attempt_failed",
        run_id=str(run.id),
        task=task.external_task_id,
        attempt=run.attempt_number,
        failure_reason=reason.value,
    )
    return CodingAttempt(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        attempt=run.attempt_number,
        context_hash=built.context_hash,
        provider_id=provider.config.provider_id,
        model_name=provider.config.model_name,
        plan=plan,
        plan_assessment=assessment,
        failure_reason=reason,
        feedback=feedback,
        duration_ms=duration_ms,
        usage=usage,
        artifacts=dict(sink.paths),
    )


# ------------------------------------------------------------------- helpers


async def _generate_recorded(
    session: Session,
    provider: ModelProvider,
    request: ModelRequest,
    *,
    task_run_id: UUID,
    purpose: ModelPurpose,
    prompt_artifact: str,
    response_artifact: str,
    sink: _ArtifactSink,
) -> ModelResponse:
    """Send one request, keep both artifacts, and record the call.

    The recording is here rather than at each call site so that a call cannot
    be added without being counted: ``model_runs`` is what sections 34 and 35
    are arithmetic over, and an uncounted call makes both of them quietly
    wrong rather than visibly incomplete.

    A failure is recorded before it is re-raised. How often an endpoint times
    out is exactly the kind of thing section 35 wants to be able to ask, and
    a table holding only the calls that worked cannot answer it.
    """
    sink.text(prompt_artifact, _render_prompt(request))
    try:
        response = await provider.generate(request)
    except Exception:
        record_model_call(
            session,
            task_run_id=task_run_id,
            config=provider.config,
            purpose=purpose,
            status=RunStatus.FAILED,
            prompt_artifact=sink.paths.get(prompt_artifact),
        )
        raise
    sink.text(response_artifact, response.raw_text)
    record_response(
        session,
        response,
        task_run_id=task_run_id,
        config=provider.config,
        purpose=purpose,
        prompt_artifact=sink.paths.get(prompt_artifact),
        response_artifact=sink.paths.get(response_artifact),
    )
    return response


def _render_prompt(request: ModelRequest) -> str:
    """The prompt exactly as it was sent, as one readable artifact."""
    return "\n\n".join(
        f"=== {message.role.value} ===\n{message.content}" for message in request.messages()
    )


@dataclass(slots=True)
class _ArtifactSink:
    """Writes this attempt's artifacts and remembers where each one landed."""

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

    A fix attempt re-enters ``CODING`` from a task that is already ``CODING``,
    and a run started by a caller that set the status itself is not an error
    either. An illegal move is logged rather than raised: the attempt's outcome
    is the interesting record, and losing it to a bookkeeping error would be
    the worse failure.
    """
    if task.status is status:
        return
    if not can_transition(task.status, status):
        logger.warning(
            "task_transition_skipped",
            task=task.external_task_id,
            current=task.status.value,
            requested=status.value,
        )
        return
    TaskRepository(session).transition(task.id, status)
    task.status = status


__all__ = [
    "CANDIDATE_PATCH_ARTIFACT",
    "CODING_PROMPT_CONTRACT",
    "CODER_PROMPT_ARTIFACT",
    "CODER_RESPONSE_ARTIFACT",
    "MAX_DIFF_ARTIFACT_BYTES",
    "PLAN_ARTIFACT",
    "CodingAttempt",
    "run_coding_attempt",
]
