"""The verification contract (build.md sections 17, 18, 49, phase H).

What a verdict means, before any of it is executed: the profile a project
declares, the order the categories run in, what an exit code and a timeout
classify to, and what goes back to the coder when something fails.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.orchestrator.domain.enums import (
    FailureAction,
    FailureReason,
    ScopePolicyDecision,
    VerificationStatus,
    VerificationType,
)
from apps.orchestrator.domain.errors import ManifestError
from apps.orchestrator.domain.failure_policy import action_for
from apps.orchestrator.domain.manifest import parse_manifest
from apps.orchestrator.domain.verification import (
    COMMAND_CATEGORIES,
    FEEDBACK_OUTPUT_LINES,
    PIPELINE_ORDER,
    VerificationProfile,
    VerificationReport,
    VerificationStep,
    classify_command,
    render_feedback,
    status_for_decision,
    tail,
)


def step(
    category: VerificationType,
    status: VerificationStatus,
    *,
    command: str = "npm test",
    exit_code: int | None = None,
    output: str = "",
    detail: str = "",
    duration_ms: int = 0,
    executed: bool = False,
) -> VerificationStep:
    return VerificationStep(
        verification_type=category,
        status=status,
        command=command,
        exit_code=exit_code,
        output=output,
        detail=detail,
        duration_ms=duration_ms,
        executed=executed,
    )


def report(*steps: VerificationStep, reasons: tuple[str, ...] = ()) -> VerificationReport:
    return VerificationReport(
        task_run_id=uuid4(),
        external_task_id="TS-004",
        steps=steps,
        human_review_reasons=reasons,
    )


# --- the order (section 17) --------------------------------------------------


def test_the_pipeline_runs_in_section_17s_order():
    assert PIPELINE_ORDER == (
        VerificationType.SCOPE,
        VerificationType.BUILD,
        VerificationType.LINT,
        VerificationType.TESTS,
        VerificationType.SECURITY,
        # The optional runtime contract (concern 81) sits after everything that
        # is executed against the source and before the checks made over the
        # diff: an application that does not build cannot be started, and a
        # running application writes into the worktree.
        VerificationType.RUNTIME,
        VerificationType.DIFF_POLICY,
    )


def test_the_runtime_contract_is_not_a_command_category():
    """``COMMAND_CATEGORIES`` is the list of *project command lists*, and it is
    what ``verified`` and ``unverified_categories`` are computed from. A
    contract is not a command list, and adding it here would change both of
    those for every project that declares no contract."""
    assert VerificationType.RUNTIME not in COMMAND_CATEGORIES


def test_scope_and_diff_policy_are_not_commands():
    """They are decided from the diff, so no worker can be asked to run them."""
    assert VerificationType.SCOPE not in COMMAND_CATEGORIES
    assert VerificationType.DIFF_POLICY not in COMMAND_CATEGORIES


# --- the profile (section 18) ------------------------------------------------


def test_a_profile_holds_the_commands_the_project_declared():
    profile = VerificationProfile(
        build=("npm run compile",), lint=("npm run lint",), tests=("npm test",)
    )

    assert profile.commands_for(VerificationType.BUILD) == ("npm run compile",)
    assert profile.commands_for(VerificationType.LINT) == ("npm run lint",)
    assert profile.commands_for(VerificationType.TESTS) == ("npm test",)
    assert profile.commands_for(VerificationType.SECURITY) == ()


def test_a_task_adds_to_the_suite_and_never_replaces_it():
    """A task can ask for more verification than the project requires and
    never for less."""
    profile = VerificationProfile(
        build=("npm run compile",), lint=("npm run lint",), tests=("npm test",)
    )

    combined = profile.with_task_commands(["npm run test:e2e"])

    assert combined.tests == ("npm test", "npm run test:e2e")
    assert combined.build == ("npm run compile",)
    assert combined.lint == ("npm run lint",)


def test_a_task_that_repeats_the_profiles_commands_does_not_run_them_twice():
    """build.md section 5's own example task declares
    `verify: [npm run compile, npm test]`, both of which the project already
    runs. That must cost nothing, not two extra executions."""
    profile = VerificationProfile(build=("npm run compile",), tests=("npm test",))

    combined = profile.with_task_commands(["npm run compile", "npm test"])

    assert combined == profile


def test_a_task_that_declares_only_a_build_command_does_not_empty_the_suite():
    """The bug this direction exists to prevent: a task whose `verify` list
    holds only a compile step must not turn `TESTS: PASSED` into a statement
    about the compiler."""
    profile = VerificationProfile(build=("npm run compile",), tests=("npm test",))

    combined = profile.with_task_commands(["npm run compile"])

    assert combined.tests == ("npm test",)


def test_a_task_that_declared_no_commands_keeps_the_projects_suite():
    profile = VerificationProfile(tests=("npm test",))

    assert profile.with_task_commands([]).tests == ("npm test",)


def test_a_profile_survives_a_round_trip_through_storage():
    profile = VerificationProfile(build=("make",), security=("npm audit",))

    assert VerificationProfile.from_mapping(profile.describe()) == profile


def test_an_absent_profile_is_empty_rather_than_guessed():
    assert VerificationProfile.from_mapping(None).is_empty
    assert VerificationProfile.from_mapping({}).is_empty


# --- the manifest (section 18) -----------------------------------------------


def manifest_with(verification: dict) -> dict:
    return {
        "version": 1,
        "project": {"id": "ts", "name": "TraceStack", "repository": "/workspace/ts"},
        "verification": verification,
        "tasks": [{"id": "TS-001", "title": "Scaffold"}],
    }


def test_a_manifest_declares_the_verification_profile():
    manifest = parse_manifest(
        manifest_with(
            {
                "build": ["npm run compile"],
                "lint": ["npm run lint"],
                "tests": ["npm test"],
                "milestone_interval": 5,
            }
        )
    )

    assert manifest.verification.build == ("npm run compile",)
    assert manifest.verification.tests == ("npm test",)
    assert manifest.milestone_interval == 5


def test_a_manifest_without_a_verification_block_declares_no_commands():
    assert parse_manifest(manifest_with({})).verification.is_empty


def test_a_misspelled_verification_key_is_refused():
    """The same rule as everywhere else in the manifest: a typo must not
    silently mean "no build step"."""
    with pytest.raises(ManifestError, match="verification"):
        parse_manifest(manifest_with({"biuld": ["npm run compile"]}))


def test_a_bare_command_string_is_refused_rather_than_wrapped():
    with pytest.raises(ManifestError, match="must be a list"):
        parse_manifest(manifest_with({"build": "npm run compile"}))


# --- classification (section 49) ---------------------------------------------


def test_exit_zero_passes_and_anything_else_fails():
    assert (
        classify_command(VerificationType.BUILD, exit_code=0, timed_out=False)
        is VerificationStatus.PASSED
    )
    assert (
        classify_command(VerificationType.BUILD, exit_code=1, timed_out=False)
        is VerificationStatus.FAILED
    )


def test_a_timeout_is_not_recorded_as_a_failure_with_exit_code_zero():
    """A killed command produced no verdict. Recording one would let a caller
    that checks only the exit code read a killed suite as a pass."""
    status = classify_command(VerificationType.TESTS, exit_code=None, timed_out=True)

    assert status is VerificationStatus.TIMEOUT
    assert step(VerificationType.TESTS, status).failed


def test_every_category_classifies_to_its_own_failure_reason():
    reasons = {
        VerificationType.SCOPE: FailureReason.SCOPE_VIOLATION,
        VerificationType.BUILD: FailureReason.BUILD_FAILED,
        VerificationType.LINT: FailureReason.LINT_FAILED,
        VerificationType.TESTS: FailureReason.TEST_FAILED,
        VerificationType.SECURITY: FailureReason.SECURITY_FAILED,
        VerificationType.DIFF_POLICY: FailureReason.SCOPE_VIOLATION,
    }
    for category, reason in reasons.items():
        assert step(category, VerificationStatus.FAILED).failure_reason is reason


def test_a_passing_step_has_no_failure_reason():
    assert step(VerificationType.BUILD, VerificationStatus.PASSED).failure_reason is None


def test_require_review_is_not_a_verification_failure():
    """Section 20's middle answer is a request for a human, not a defect the
    coder can fix by trying again."""
    assert status_for_decision(ScopePolicyDecision.ALLOW) is VerificationStatus.PASSED
    assert (
        status_for_decision(ScopePolicyDecision.REQUIRE_REVIEW) is VerificationStatus.PASSED
    )
    assert status_for_decision(ScopePolicyDecision.BLOCK) is VerificationStatus.FAILED


# --- the report --------------------------------------------------------------


def test_a_skipped_category_is_neither_a_pass_nor_a_failure():
    """A project with no lint command has not passed lint."""
    result = report(
        step(VerificationType.BUILD, VerificationStatus.PASSED, exit_code=0, executed=True),
        step(VerificationType.LINT, VerificationStatus.SKIPPED, command=""),
    )

    assert result.passed
    assert result.verified
    assert len(result.performed) == 1
    assert result.step_for(VerificationType.LINT).status is VerificationStatus.SKIPPED
    assert VerificationType.LINT in result.unverified_categories


def test_the_reports_failure_reason_is_the_first_failure():
    result = report(
        step(VerificationType.BUILD, VerificationStatus.FAILED, exit_code=2),
        step(VerificationType.TESTS, VerificationStatus.FAILED, exit_code=1),
    )

    assert result.failure_reason is FailureReason.BUILD_FAILED


def test_a_report_that_requires_human_review_has_still_passed():
    result = report(
        step(VerificationType.BUILD, VerificationStatus.PASSED, exit_code=0, executed=True),
        reasons=("src/auth/session.ts is a security change the task did not declare",),
    )

    assert result.passed
    assert result.requires_human_review
    assert result.feedback is None


def test_the_report_describes_itself_for_the_run_record():
    described = report(
        step(VerificationType.BUILD, VerificationStatus.FAILED, exit_code=2)
    ).describe()

    assert described["passed"] is False
    assert described["failure_reason"] == FailureReason.BUILD_FAILED.value
    assert described["steps"][0]["exit_code"] == 2


# --- feedback to the coder (section 17) --------------------------------------


def test_the_coder_is_sent_the_command_and_what_it_actually_printed():
    feedback = render_feedback(
        [
            step(
                VerificationType.BUILD,
                VerificationStatus.FAILED,
                command="npm run compile",
                exit_code=2,
                output="src/nav.ts(14,3): error TS2551: Property 'labl' does not exist.",
            )
        ]
    )

    assert "npm run compile" in feedback
    assert "exit code 2" in feedback
    assert "TS2551" in feedback


def test_a_timeout_says_it_timed_out_rather_than_reporting_an_exit_code():
    feedback = render_feedback(
        [
            step(
                VerificationType.TESTS,
                VerificationStatus.TIMEOUT,
                command="npm test",
                duration_ms=900_000,
                detail="the command was killed at its timeout",
            )
        ]
    )

    assert "timed out after 900000ms" in feedback
    assert "exit code" not in feedback


def test_long_output_is_clipped_from_the_front_and_says_so():
    """The end of a build log is where the error is; the start is noise. A
    reader must never mistake a clipped log for a complete one."""
    long_output = "\n".join(f"line {index}" for index in range(500))

    body = tail(long_output, FEEDBACK_OUTPUT_LINES)

    assert body.startswith("[... 440 earlier line(s) omitted ...]")
    assert body.endswith("line 499")
    assert "line 0\n" not in body


def test_short_output_is_passed_through_untouched():
    assert tail("one\ntwo", FEEDBACK_OUTPUT_LINES) == "one\ntwo"


# --- the retry path (sections 17 and 49) -------------------------------------


def test_a_deterministic_failure_routes_back_to_the_coder():
    """Section 17: build/lint/test failures go to the coder before a reviewer
    is ever invoked. Section 49 is where that becomes a policy rather than an
    `if` somewhere in a workflow."""
    for category in (
        VerificationType.BUILD,
        VerificationType.LINT,
        VerificationType.TESTS,
    ):
        reason = step(category, VerificationStatus.FAILED).failure_reason
        assert action_for(reason) is FailureAction.SEND_TO_CODER


def test_an_unsafe_candidate_is_rolled_back_rather_than_retried():
    """A scope violation or a secret in the diff is not something to ask the
    coder to try again with: the candidate does not survive."""
    for category in (
        VerificationType.SCOPE,
        VerificationType.SECURITY,
        VerificationType.DIFF_POLICY,
    ):
        reason = step(category, VerificationStatus.FAILED).failure_reason
        assert action_for(reason) is FailureAction.ROLLBACK


def test_passing_is_not_verifying_when_no_command_ran():
    """The distinction concern 19 exists for: with an empty profile nothing
    fails, and a caller that checked only `passed` would send a completely
    unverified candidate to a reviewer."""
    result = report(
        step(VerificationType.BUILD, VerificationStatus.SKIPPED, command=""),
        step(VerificationType.LINT, VerificationStatus.SKIPPED, command=""),
        step(VerificationType.TESTS, VerificationStatus.SKIPPED, command=""),
    )

    assert result.passed
    assert not result.verified
    assert "nothing was verified" in result.summary()


def test_a_check_the_orchestrator_decided_is_not_a_command_that_ran():
    """The scope guard and the security scan reach verdicts without a worker.
    They are checks, not evidence that the project's own commands passed."""
    result = report(
        step(VerificationType.SCOPE, VerificationStatus.PASSED, command="scope guard"),
        step(VerificationType.SECURITY, VerificationStatus.PASSED, command="security scan"),
    )

    assert result.passed
    assert not result.verified
    assert len(result.performed) == 2
