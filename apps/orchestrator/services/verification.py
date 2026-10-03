"""The verification pipeline (build.md section 17, phase H).

Section 17's order, executed:

```text
scope validation -> build -> lint -> targeted tests -> security -> diff policy
```

and then, only if all of it passed, the candidate is worth a reviewer's time.

This module is the step between phase G's coding attempt and phase I's
review, and it exists for one sentence in section 17: *never accept a model's
statement that a command passed -- the orchestrator must execute it.* So
nothing here reads the coder's completion report. It re-measures the diff, it
runs the project's own commands in a disposable worker, and what it returns
is what happened.

Four properties are worth knowing before reading the code.

* **It stops at the first failing category.** A candidate that does not
  compile has nothing useful to say about its lint or its tests, and the
  reviewer is never reached: the deterministic failure goes back to the coder
  (section 17), carrying the command and its real output.
* **The scope guard runs before a worker is started, and again afterwards.**
  Before, because a candidate that already broke its allowance should not be
  given a container. Afterwards because a command can write into the worktree
  -- a `dist/` that appeared during `npm run compile`, a refreshed lockfile --
  and the candidate that goes to a reviewer must be the one that was measured.
  What makes that true is the restore in ``_run_command_categories``: the
  worktree is reset and the measured candidate patch reapplied before
  ``DIFF_POLICY`` looks, so the commands' side effects are discarded rather
  than classified. `DIFF_POLICY` is therefore also the check that would notice
  if the restore had not worked. This is concern 20: a project whose build
  writes output that is not in `.gitignore` would otherwise have every
  otherwise-passing candidate turned into a `SCOPE_VIOLATION` whose real fix
  is a `.gitignore` entry, routed to `ROLLBACK` without the coder being told.
* **A skipped category is recorded, not assumed.** A project with no lint
  command has not passed lint. The row says ``SKIPPED`` and the report counts
  it as neither a pass nor a failure.
* **Human review is a third outcome.** ``REQUIRE_REVIEW`` from the scope
  guard or the security scan is not a defect the coder can fix by trying
  again (section 20); it is carried on the report for the workflow to route,
  and it does not make ``passed`` false.

What raises, and what comes back as a report: a failing command, a timeout, a
blocked diff and a security finding are all *results*, returned. A worker
that cannot be started, a rejected command, a missing run -- those raise,
because their policy is retry or an operator's attention (section 49) and the
workflow owns both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from time import monotonic
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import (
    RunEventType,
    ScopePolicyDecision,
    TaskStatus,
    VerificationStatus,
    VerificationType,
)
from ..domain.failure_identity import FailureComparison
from ..domain.models import Project, RunEvent, Task, TaskRun, VerificationRun
from ..domain.scope import ScopeAssessment, ScopePolicy, evaluate_scope
from ..domain.security import SecurityAssessment, scan_candidate
from ..domain.state_machine import can_transition
from ..domain.verification import (
    COMMAND_CATEGORIES,
    VerificationProfile,
    VerificationReport,
    VerificationStep,
    classify_command,
    status_for_decision,
)
from ..repositories import (
    RunEventRepository,
    TaskRepository,
    VerificationRunRepository,
)
from . import artifact_store
from .command_execution import CommandExecution, execute_commands
from .dependency_bootstrap import (
    DEPENDENCY_FAILURES,
    NETWORKLESS_VERIFICATION_NETWORK,
    bootstrap_dependencies,
)
from .verification_baseline import classify as classify_against_baseline
from .worker_service import worker_session
from .workspace import DiffCapture, TaskWorkspace, capture_diff, load_run_context

logger = get_logger(__name__)

#: The pipeline's own record, beside the coder's ``completion-report.json``.
#: Reading the two together is how a later reader sees claimed against
#: measured (section 34).
VERIFICATION_ARTIFACT = "verification.json"

#: Ceiling on the diff text re-captured for the security scan. The structured
#: summary is never clipped, so the scope and size checks always see the whole
#: change; a clipped *text* is reported by the scan as incomplete.
MAX_DIFF_SCAN_BYTES = 512_000

#: Output kept on a step for the coder's feedback. The full log is on disk;
#: this is the part that travels back into a prompt.
MAX_STEP_OUTPUT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class _Recorder:
    """Writes one step to ``verification_runs`` and returns it."""

    session: Session
    task_run_id: UUID

    def record(self, step: VerificationStep) -> VerificationStep:
        VerificationRunRepository(self.session).add(
            VerificationRun(
                task_run_id=self.task_run_id,
                verification_type=step.verification_type,
                command=step.command,
                status=step.status,
                exit_code=step.exit_code,
                stdout_artifact=step.log_artifact,
                duration_ms=step.duration_ms,
            )
        )
        return step


def resolve_profile(project: Project, task: Task) -> VerificationProfile:
    """The commands this task will be verified with (sections 6 and 18).

    The project's profile plus whatever the task declared in its own
    ``verify`` list. Additive, never substitutive: a task can ask for more
    verification than the project requires and never for less. See
    ``VerificationProfile.with_task_commands`` for why that direction is the
    only safe one.
    """
    return project.verification.with_task_commands(task.verify_commands)


def verify_candidate(
    session: Session,
    workspace: TaskWorkspace,
    *,
    settings: Settings | None = None,
    secrets: Mapping[str, str] | None = None,
) -> VerificationReport:
    """Run the whole pipeline against the candidate in ``workspace``.

    Args:
        workspace: the run's worktree, holding the coder's uncommitted work.
        secrets: values the commands need, injected into the worker
            individually and redacted out of every log (section 36).

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        CommandRejected: a configured command is not permitted. Import
            validates the manifest, so reaching this means the policy or the
            profile changed underneath a project.
        WorkspaceMountRejected: the worktree is not one a worker may be given.
        WorkerBackendUnavailable: the container runtime is not usable.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    policy = ScopePolicy.for_task(task, project)
    profile = resolve_profile(project, task)
    prefix = artifact_store.attempt_prefix(run)
    recorder = _Recorder(session=session, task_run_id=run.id)

    _transition(session, task, TaskStatus.VERIFYING)
    _emit(session, run, task, project, RunEventType.BUILD_STARTED, {
        "profile": profile.describe(),
        "attempt": run.attempt_number,
    })

    steps: list[VerificationStep] = []
    review_reasons: list[str] = []

    # --- scope validation, before anything is executed ---------------------
    diff = capture_diff(workspace, max_bytes=MAX_DIFF_SCAN_BYTES)
    scope = evaluate_scope(diff.summary, policy)
    steps.append(recorder.record(_scope_step(VerificationType.SCOPE, scope)))
    review_reasons.extend(_review_reasons(scope))

    if steps[-1].failed:
        return _finish(session, run, task, project, steps, review_reasons, config, prefix)

    try:
        bootstrap = bootstrap_dependencies(
            session,
            project,
            run,
            worktree_path=workspace.path,
            worktree_git=workspace.git,
            integration_sha=workspace.starting_commit,
            settings=config,
            prefix=prefix,
            secrets=secrets,
        )
    except DEPENDENCY_FAILURES as error:
        # A filesystem failure or a malformed dependency path is a failed
        # verification step with a readable reason, not an exception escaping
        # into the fix loop.
        steps.append(recorder.record(_bootstrap_error_step(str(error))))
        return _finish(session, run, task, project, steps, review_reasons, config, prefix)
    for execution in bootstrap.executions:
        steps.append(recorder.record(_bootstrap_command_step(execution)))
    if any(step.failed for step in steps):
        return _finish(session, run, task, project, steps, review_reasons, config, prefix)

    # --- the project's commands, in one worker -----------------------------
    if profile.is_empty:
        logger.warning(
            "no_verification_profile",
            run_id=str(run.id),
            task=task.external_task_id,
            detail=(
                "the project declares no verification commands; the report will "
                "pass without being verified (VerificationReport.verified is False)"
            ),
        )
    executed: list[tuple[VerificationType, CommandExecution]] = []
    steps.extend(
        _run_command_categories(
            session, run, task, project, workspace, profile, recorder,
            prefix=prefix, settings=config, secrets=secrets, executed=executed,
        )
    )

    if any(step.failed for step in steps):
        # Concern 78, stage 2. The commands have run and something failed; the
        # remaining deterministic question is whether this candidate caused it.
        # Asked here rather than inside the loop above because it needs the
        # whole category's executions, passing ones included, and no model and
        # no further command run is involved in answering it.
        comparison = classify_against_baseline(
            session,
            project_id=project.id,
            baseline_sha=workspace.starting_commit,
            worker_profile=project.worker_profile,
            executions=executed,
        )
        return _finish(
            session, run, task, project, steps, review_reasons, config, prefix,
            comparison=comparison,
        )

    # --- security and diff policy, over the post-command worktree ------
    rescan = capture_diff(workspace, max_bytes=MAX_DIFF_SCAN_BYTES)
    security = scan_candidate(
        rescan.text,
        rescan.summary,
        truncated=rescan.truncated,
        generated_path_exceptions=tuple(project.generated_path_exceptions),
    )
    steps.append(recorder.record(_security_step(security)))
    review_reasons.extend(
        finding.detail
        for finding in security.findings
        if finding.decision is ScopePolicyDecision.REQUIRE_REVIEW
    )
    if not steps[-1].failed:
        final_scope = evaluate_scope(rescan.summary, policy)
        steps.append(
            recorder.record(_scope_step(VerificationType.DIFF_POLICY, final_scope, rescan))
        )
        review_reasons.extend(_review_reasons(final_scope))

    return _finish(session, run, task, project, steps, review_reasons, config, prefix)


# --------------------------------------------------------------------- steps


def _run_command_categories(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    workspace: TaskWorkspace,
    profile: VerificationProfile,
    recorder: _Recorder,
    *,
    prefix: str,
    settings: Settings,
    secrets: Mapping[str, str] | None,
    executed: list[tuple[VerificationType, CommandExecution]],
) -> list[VerificationStep]:
    """Build, lint, tests and security, in order, in one worker.

    One worker for all four: they run against the same worktree in the same
    state, and starting four containers would make the build's output
    invisible to the tests. The worker is destroyed before this returns,
    whatever happens (section 11).
    """
    planned = {
        category: profile.commands_for(category) for category in COMMAND_CATEGORIES
    }
    # A category with no command is recorded as skipped rather than left out:
    # a project with no lint step has not passed lint. ``SECURITY`` is the
    # exception -- the orchestrator's own scan of the diff runs whether or not
    # a project configured an audit command, so the category is never empty.
    steps: list[VerificationStep] = [
        recorder.record(_skipped_step(category))
        for category, commands in planned.items()
        if not commands and category is not VerificationType.SECURITY
    ]
    to_run = {category: commands for category, commands in planned.items() if commands}
    if not to_run:
        return steps

    candidate_patch = workspace.git.get_diff(workspace.starting_commit)
    try:
        with worker_session(
            workspace.path,
            profile=project.worker_profile,
            settings=settings,
            secrets=secrets,
            worker_network=NETWORKLESS_VERIFICATION_NETWORK,
        ) as worker:
            worker_deadline = monotonic() + settings.worker_timeout_seconds
            for category, commands in to_run.items():
                executions = execute_commands(
                    session,
                    worker,
                    run.id,
                    commands,
                    category=category.value.casefold(),
                    prefix=prefix,
                    settings=settings,
                    stop_on_failure=True,
                    deadline_monotonic=worker_deadline,
                )
                # Kept beside the steps, not derived from them: baseline
                # classification needs the command's *source* text and its
                # untruncated capture flags, and a ``VerificationStep`` carries
                # neither (it holds the argv rendering and a tail of output).
                executed.extend((category, execution) for execution in executions)
                category_steps = [
                    recorder.record(_command_step(category, execution))
                    for execution in executions
                ]
                steps.extend(category_steps)
                if any(step.failed for step in category_steps):
                    # A candidate that does not compile has nothing to say about
                    # its own tests, and a worker killed by a timeout cannot run
                    # another command anyway.
                    _emit(
                        session, run, task, project, RunEventType.BUILD_FAILED,
                        {
                            "category": category.value,
                            "command": category_steps[-1].command,
                            "status": category_steps[-1].status.value,
                            "exit_code": category_steps[-1].exit_code,
                        },
                    )
                    break
    finally:
        workspace.git.restore_patch(workspace.starting_commit, candidate_patch)
    return _in_pipeline_order(steps)


def _command_step(
    category: VerificationType, execution: CommandExecution
) -> VerificationStep:
    result = execution.result
    status = classify_command(
        category, exit_code=result.exit_code, timed_out=result.timed_out
    )
    detail = ""
    if result.timed_out:
        detail = "the command was killed at its timeout and returned no verdict"
    elif result.truncated:
        detail = f"output was clipped; {result.output_bytes} bytes were produced"
    return VerificationStep(
        verification_type=category,
        status=status,
        command=result.command.display,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        log_artifact=execution.log_artifact,
        executed=True,
        detail=detail,
        output=result.combined_output[-MAX_STEP_OUTPUT_CHARS:],
    )


def _bootstrap_command_step(execution: CommandExecution) -> VerificationStep:
    result = execution.result
    status = classify_command(
        VerificationType.DEPENDENCY_BOOTSTRAP,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
    )
    detail = ""
    if result.timed_out:
        detail = "dependency bootstrap timed out"
    elif result.truncated:
        detail = f"output was clipped; {result.output_bytes} bytes were produced"
    elif not result.succeeded:
        detail = "dependency bootstrap command failed"
    return VerificationStep(
        verification_type=VerificationType.DEPENDENCY_BOOTSTRAP,
        status=status,
        command=result.command.display,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        log_artifact=execution.log_artifact,
        executed=True,
        detail=detail,
        output=result.combined_output[-MAX_STEP_OUTPUT_CHARS:],
    )


def _bootstrap_error_step(detail: str) -> VerificationStep:
    return VerificationStep(
        verification_type=VerificationType.DEPENDENCY_BOOTSTRAP,
        status=VerificationStatus.ERROR,
        command="dependency bootstrap",
        detail=detail,
    )


def _skipped_step(category: VerificationType) -> VerificationStep:
    return VerificationStep(
        verification_type=category,
        status=VerificationStatus.SKIPPED,
        command="",
        detail=f"the project declares no {category.value.casefold()} command",
    )


def _scope_step(
    category: VerificationType,
    scope: ScopeAssessment,
    diff: DiffCapture | None = None,
) -> VerificationStep:
    """The scope guard's verdict as a pipeline step (sections 17 and 20)."""
    status = status_for_decision(scope.decision)
    detail = scope.summary()
    if diff is not None and diff.truncated:
        detail = f"{detail}; the captured diff text was clipped"
    return VerificationStep(
        verification_type=category,
        status=status,
        command=(
            "scope guard"
            if category is VerificationType.SCOPE
            else "diff policy (post-command)"
        ),
        detail=detail,
        output="\n".join(finding.detail for finding in scope.findings),
    )


def _security_step(security: SecurityAssessment) -> VerificationStep:
    return VerificationStep(
        verification_type=VerificationType.SECURITY,
        status=(
            VerificationStatus.FAILED
            if security.blocked
            else VerificationStatus.PASSED
        ),
        command="security scan (orchestrator)",
        detail=security.summary(),
        output="\n".join(finding.detail for finding in security.findings),
    )


def _review_reasons(scope: ScopeAssessment) -> list[str]:
    return [
        finding.detail
        for finding in scope.findings
        if finding.decision is ScopePolicyDecision.REQUIRE_REVIEW
    ]


def _in_pipeline_order(steps: Sequence[VerificationStep]) -> list[VerificationStep]:
    """Order steps by category, keeping each category's commands in sequence.

    Skipped categories are recorded up front so a category can never be left
    out of the record; ordering here means the report still reads as the
    pipeline, not as the order the rows happened to be written in.
    """
    order = {category: index for index, category in enumerate(COMMAND_CATEGORIES)}
    return sorted(steps, key=lambda step: order.get(step.verification_type, 99))


# -------------------------------------------------------------------- report


def _finish(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    steps: list[VerificationStep],
    review_reasons: list[str],
    settings: Settings,
    prefix: str,
    comparison: FailureComparison | None = None,
) -> VerificationReport:
    report = VerificationReport(
        task_run_id=run.id,
        external_task_id=task.external_task_id,
        attempt=run.attempt_number,
        steps=tuple(steps),
        human_review_reasons=tuple(dict.fromkeys(review_reasons)),
        comparison=comparison,
    )
    stored = artifact_store.write_json(
        session,
        run.id,
        prefix + VERIFICATION_ARTIFACT,
        report.describe(),
        kind=VERIFICATION_ARTIFACT,
        settings=settings,
    )
    report = replace(report, artifacts={VERIFICATION_ARTIFACT: stored.relative_path})

    if report.no_new_regressions:
        # The pipeline's own claim, and the only one it makes: every declared
        # check was executed and returned a pass -- or, under concern 78 stage
        # 2, every failure it returned was already failing in the recorded
        # baseline for the exact tree this candidate started from. Either way
        # the candidate broke nothing that was working, which is what earns a
        # reviewer's time. Whether it is *good* is phase I's question, and the
        # reviewer still runs: nothing here approves anything.
        _transition(session, task, TaskStatus.REVIEW_PENDING)
        _emit(session, run, task, project, RunEventType.TESTS_PASSED, {
            "classification": report.classification.value,
            "comparison": comparison.describe() if comparison else None,
            "verified": report.verified,
            "commands_run": len(report.commands_run),
            "checks_performed": len(report.performed),
            "requires_human_review": report.requires_human_review,
            "human_review_reasons": list(report.human_review_reasons),
        })
    else:
        _emit(session, run, task, project, RunEventType.BUILD_FAILED, {
            "failure_reason": report.failure_reason.value if report.failure_reason else None,
            "classification": report.classification.value,
            "comparison": comparison.describe() if comparison else None,
            "summary": report.summary(),
            "steps": [step.describe() for step in report.failures],
        })

    logger.info(
        "verification_finished",
        run_id=str(run.id),
        task=task.external_task_id,
        attempt=run.attempt_number,
        passed=report.passed,
        verified=report.verified,
        classification=report.classification.value,
        no_new_regressions=report.no_new_regressions,
        commands_run=len(report.commands_run),
        failure_reason=report.failure_reason.value if report.failure_reason else None,
        requires_human_review=report.requires_human_review,
    )
    return report


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
    """Move the task when the move is legal, log when it is not.

    The pipeline moves a task into ``VERIFYING`` and, on a pass, into
    ``REVIEW_PENDING``. It never moves a failing task: whether a deterministic
    failure goes back to the coder, escalates or ends the run is
    ``domain.failure_policy``'s answer and the workflow's decision (phase K).
    """
    if task.status is status:
        return
    if not can_transition(task.status, status):
        logger.warning(
            "verification_transition_skipped",
            task=task.external_task_id,
            current=task.status.value,
            requested=status.value,
        )
        return
    TaskRepository(session).transition(task.id, status)
    task.status = status


__all__ = [
    "MAX_DIFF_SCAN_BYTES",
    "MAX_STEP_OUTPUT_CHARS",
    "VERIFICATION_ARTIFACT",
    "resolve_profile",
    "verify_candidate",
]
