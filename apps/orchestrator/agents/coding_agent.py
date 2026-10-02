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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
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
    EditOperation,
    MalformedChangeSet,
    max_edit_bytes_for_context,
    per_path_edit_allowance,
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
    describe_error,
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
from .timing import elapsed_ms, started_at

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
    review_cycle: int | None = None,
    checkpoint_call: Callable[[], None] | None = None,
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
        review_cycle: the cycle this attempt is being made for, recorded on
            every model call it makes.
        checkpoint_call: committed after each model call's record is written,
            including before a failure is re-raised. See ``_generate_recorded``.

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

    # The edit contract asks for the *complete new contents* of every file the
    # coder changes, so a writable file it has not seen whole cannot be asked
    # for: it will invent or drop the part it could not read, and the result is
    # a large deletion in the diff rather than an error. The package records,
    # per writable file, whether its complete contents reached the coder --
    # clipping is only one of the ways they might not have. Refused here,
    # before a model is asked, because no feedback the coder could act on would
    # change the outcome (concerns 1 and 58).
    incomplete = _incomplete_writable_sources(built, policy)
    if incomplete:
        return _failed(
            session, run, task, project, built, provider,
            FailureReason.HUMAN_DECISION_REQUIRED,
            feedback=(
                "This attempt was refused before it started. The task may write "
                + ("these files" if len(incomplete) > 1 else "this file")
                + ", and the coder was not given the complete current contents "
                "of "
                + ("each of them" if len(incomplete) > 1 else "it")
                + ":\n"
                + "\n".join(f"- {path}: {reason}" for path, reason in incomplete)
                + "\nAsking for the complete new contents of a file it has not "
                "fully read would destroy the part it could not see. Raise the "
                "context budget (CONTEXT_MAX_TOKENS, or CONTEXT_MAX_FILE_BYTES "
                "for a file that was skipped outright), or split the task so "
                "each attempt writes a smaller file."
            ),
            plan=None, assessment=None, duration_ms=0, usage=TokenUsage(), sink=sink,
        )

    plan: CodingPlan | None = None
    assessment: PlanAssessment | None = None
    if requires_plan(task) if plan_required is None else plan_required:
        _transition(session, task, TaskStatus.PLANNING)
        plan, assessment, plan_usage = await _request_plan(
            session, run, task, project, built, provider, policy, sink,
            review_cycle=review_cycle, checkpoint_call=checkpoint_call,
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

    # Concern 62: compute the per-path output allowances before the model call
    # so they can be communicated in the prompt. The same allowances are used
    # for enforcement after the model responds.
    path_output_limits = _complete_writable_allowances(
        built,
        outer_ceiling=config.context_max_file_bytes,
        absolute_growth_allowance=config.context_absolute_growth_allowance_bytes,
    )

    request = ModelRequest(
        system_instructions=CODER_SYSTEM_PROMPT,
        task_instructions=render_coding_instructions(
            task, plan, path_output_limits=path_output_limits
        ),
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
        attempt=run.attempt_number,
        review_cycle=review_cycle,
        checkpoint_call=checkpoint_call,
    )

    try:
        change_set = CodeChangeSet.from_payload(
            response.data or {},
            # Concern 11: the same number that bounded what the context builder
            # could show of a file bounds what may come back for it.
            max_edit_bytes=max_edit_bytes_for_context(config.context_max_item_tokens),
            # Concern 61: a complete writable file gets an output allowance
            # derived from the source the coder actually saw, not from the
            # per-item input ceiling. Concern 62: the allowance includes an
            # absolute growth term so medium files have room for legitimate
            # additions beyond proportional growth. The same allowances were
            # communicated in the prompt before the model call.
            path_max_bytes=path_output_limits,
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
                f"object with an 'edits' array. For an existing writable file whose "
                f"complete original contents were supplied and whose complete-file "
                f"update fits the stated limit, prefer operation 'update' with the "
                f"COMPLETE resulting file contents. Use 'create' for new files. Use "
                f"'replace' only when a complete-file update is not appropriate or "
                f"permitted, with 'oldText' copied exactly from the file and the "
                f"'newText' that replaces it. An omitted test is a deleted test: keep "
                f"the content the task did not ask you to remove."
            ),
            plan=plan,
            assessment=assessment,
            duration_ms=response.duration_ms,
            usage=response.usage,
            sink=sink,
        )

    # Concern 61: a model-requested edit that was rejected during parsing must
    # not disappear while the remaining edits proceed. The attempt fails closed
    # with INVALID_MODEL_RESPONSE so the coder sees the rejected path and reason.
    if change_set.has_parse_rejections:
        detail = "; ".join(
            f"{r.path or '(unknown)'}: {r.reason}" for r in change_set.rejected_parse_edits
        )
        return _failed(
            session,
            run,
            task,
            project,
            built,
            provider,
            FailureReason.INVALID_MODEL_RESPONSE,
            feedback=(
                "Your previous answer contained edits that could not be applied: "
                f"{detail}. For an existing writable file whose complete original "
                "contents were supplied and whose complete-file update fits the "
                "stated limit, prefer operation 'update' with the COMPLETE resulting "
                "file contents. Use 'create' for new files. Use 'replace' only when "
                "a complete-file update is not appropriate or permitted, with "
                "'oldText' copied exactly from the file's current contents and "
                "'newText' that replaces it, and leave 'content' as an empty string. "
                "Keep all the content the task did not ask you to change: an omitted "
                "test is a deleted test."
            ),
            plan=plan,
            assessment=assessment,
            duration_ms=response.duration_ms,
            usage=response.usage,
            sink=sink,
        )

    # Concern 70: the resulting size of a targeted edit is bounded by the
    # largest file the context builder will read into a prompt at all. It is
    # deliberately not the per-path output allowance above, which measures what
    # the model *sent*; a faithful targeted edit sends only its region and
    # still has to be allowed to leave a legitimately larger file behind. The
    # resulting diff is bounded separately, by the scope guard.
    application = apply_change_set(
        change_set,
        root=workspace.path,
        policy=policy,
        result_max_bytes=config.context_max_file_bytes,
    )
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
    *,
    review_cycle: int | None = None,
    checkpoint_call: Callable[[], None] | None = None,
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
        attempt=run.attempt_number,
        review_cycle=review_cycle,
        checkpoint_call=checkpoint_call,
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


def _complete_writable_allowances(
    built: ContextBuildResult,
    *,
    outer_ceiling: int,
    absolute_growth_allowance: int,
) -> dict[str, int]:
    """Per-path output ceilings for complete writable files (concerns 61, 62).

    For each existing writable file that was supplied to the coder complete,
    the output allowance is derived from the source file's actual size with
    three components: a floor (``MAX_EDIT_BYTES``), an absolute growth term,
    and a proportional headroom term. The largest of these, capped at
    ``outer_ceiling``. New files, files not supplied whole, and paths without
    a trustworthy complete source record fall back to the default
    ``max_edit_bytes``.

    The invariant: a bounded task may rewrite a complete writable file with
    reasonable growth proportional to the file it was shown, but model output
    remains bounded.
    """
    allowances: dict[str, int] = {}
    for item in built.package.items:
        if (
            item.path is not None
            and item.requires_complete
            and not item.truncated
            and item.source_bytes > 0
        ):
            allowances[item.path] = per_path_edit_allowance(
                item.source_bytes,
                outer_ceiling=outer_ceiling,
                absolute_growth_allowance=absolute_growth_allowance,
            )
    return allowances


def _incomplete_writable_sources(
    built: ContextBuildResult, policy: ScopePolicy
) -> tuple[tuple[str, str], ...]:
    """Files the coder may rewrite but was not shown whole (concerns 1 and 58).

    Two sources, and the first is the load-bearing one. The package states, per
    existing writable file, whether its complete original contents are in it --
    a fact recorded while the package was built. The second is the original
    check over ``truncated_paths``, kept as defence in depth: if the record is
    ever wrong or missing, a visibly clipped writable file still refuses.

    Reasoning from truncation alone was not enough. A file larger than
    ``CONTEXT_MAX_FILE_BYTES``, a binary one, or one excluded from selection
    never becomes an item at all, so there is no truncated path to notice and
    the coder would be asked to replace a file it had never seen.

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

    def writable(path: str) -> bool:
        return policy.is_allowed(path) and not policy.is_inspect_only(path)

    reasons: dict[str, str] = {}
    for source in built.package.incomplete_required:
        if writable(source.path):
            reasons[source.path] = source.reason or "was not supplied whole"
    for path in built.package.truncated_paths:
        if writable(path):
            reasons.setdefault(path, "only part of it fitted in the context budget")
    return tuple(sorted(reasons.items()))


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
        # Read from the refusals rather than from ``application.targeted``, which
        # only lists what was actually written: the feedback that matters here is
        # the one for a targeted edit that did not apply, and that edit is in
        # ``rejected`` precisely because nothing was written.
        asked_for_targeted = any(
            edit.operation == EditOperation.REPLACE.value for edit in application.rejected
        )
        lead = (
            "None of your edits could be applied, so nothing was written"
            if application.refused_whole
            else "None of your edits could be applied"
        )
        return FailureReason.INVALID_MODEL_RESPONSE, (
            f"{lead}"
            + (f": {rejected}." if rejected else ".")
            + " Name files that exist for 'update' and use 'create' for new ones."
            + (
                " For a 'replace' fallback, copy 'oldText' exactly from the file's "
                "current contents -- it must occur exactly once -- provide the "
                "'newText' that replaces it, and leave 'content' as an empty string. "
                "That way the rest of the file, including its existing tests, is "
                "preserved exactly."
                if asked_for_targeted
                else ""
            )
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
    attempt: int | None = None,
    review_cycle: int | None = None,
    checkpoint_call: Callable[[], None] | None = None,
) -> ModelResponse:
    """Send one request, keep both artifacts, and record the call.

    The recording is here rather than at each call site so that a call cannot
    be added without being counted: ``model_runs`` is what sections 34 and 35
    are arithmetic over, and an uncounted call makes both of them quietly
    wrong rather than visibly incomplete.

    A failure is recorded before it is re-raised, and ``checkpoint_call`` is
    invoked before the re-raise. How often an endpoint times out is exactly the
    kind of thing section 35 wants to be able to ask, and a table holding only
    the calls that worked cannot answer it -- but a row written into the
    transaction that is about to be rolled back cannot answer it either. The
    checkpoint is what separates "this call failed" from "everything after the
    last turn boundary was lost": the call happened either way, and the loop's
    reconstruction reads this row to know an attempt was really made.
    """
    sink.text(prompt_artifact, _render_prompt(request))
    # Concern 66: no database transaction may remain open across a model-provider
    # call. Commit the pre-call work now so the transaction is closed while we
    # wait for the external operation.
    if checkpoint_call is not None:
        checkpoint_call()
    else:
        session.commit()
    started = monotonic()
    try:
        response = await provider.generate(request)
    except Exception as error:
        # Re-open a fresh transaction for post-failure persistence. The run may
        # have been abandoned or finished while we were waiting, so fence first.
        TaskRunRepository(session).require_in_flight(task_run_id)
        record_model_call(
            session,
            task_run_id=task_run_id,
            config=provider.config,
            purpose=purpose,
            status=RunStatus.FAILED,
            duration_ms=elapsed_ms(started),
            started_at=started_at(started),
            prompt_artifact=sink.paths.get(prompt_artifact),
            error_detail=describe_error(error),
            attempt=attempt,
            review_cycle=review_cycle,
        )
        if checkpoint_call is not None:
            checkpoint_call()
        else:
            session.commit()
        raise
    # Concern 66: after a possibly slow external call, revalidate ownership
    # before committing model results. A late answer must not resurrect an
    # abandoned or terminal run.
    TaskRunRepository(session).require_in_flight(task_run_id)
    sink.text(response_artifact, response.raw_text)
    record_response(
        session,
        response,
        task_run_id=task_run_id,
        config=provider.config,
        purpose=purpose,
        prompt_artifact=sink.paths.get(prompt_artifact),
        response_artifact=sink.paths.get(response_artifact),
        started_at=started_at(started),
        attempt=attempt,
        review_cycle=review_cycle,
    )
    if checkpoint_call is not None:
        checkpoint_call()
    else:
        session.commit()
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
