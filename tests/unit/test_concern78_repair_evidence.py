"""Bounded repair evidence, and what it refuses to say (concern 78, stage 3).

Stage 2 decided *whether* a candidate caused a failure. These tests pin the
packaging that follows: an attributed regression becomes a structured, bounded,
provider-independent object, and everything else becomes nothing at all.

The negative tests are the load-bearing ones. ``build`` returning ``None`` for
``UNCLASSIFIED_FAILURE``, for a baseline-only failure and for a lint or build
failure is the fail-closed rule as a constructor: if stage 2 would not attribute
a failure to the candidate, stage 3 must not hand a coder an object whose first
sentence says it did.

Pure: a report and a comparison in, evidence out. The routing through the fix
loop is in ``tests/integration/test_concern78_repair_routing.py``.
"""

from __future__ import annotations

from uuid import uuid4

from apps.orchestrator.domain.enums import (
    VerificationClassification,
    VerificationStatus,
    VerificationType,
)
from apps.orchestrator.domain.failure_identity import compare, unavailable
from apps.orchestrator.domain.repair_evidence import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_COMMANDS,
    MAX_EVIDENCE_EXCERPT_CHARS,
    MAX_EVIDENCE_EXCERPT_LINES,
    MAX_EVIDENCE_IDENTITIES,
    RepairEvidence,
)
from apps.orchestrator.domain.repair_evidence import build as build_evidence
from apps.orchestrator.domain.verification import VerificationReport, VerificationStep

from .test_failure_identity import pytest_output


def step(
    category: VerificationType = VerificationType.TESTS,
    *,
    command: str = "pytest",
    output: str = "",
    log: str | None = "attempt-1/tests/01-pytest.log",
    status: VerificationStatus = VerificationStatus.FAILED,
    exit_code: int | None = 1,
) -> VerificationStep:
    return VerificationStep(
        verification_type=category,
        status=status,
        command=command,
        exit_code=exit_code,
        executed=True,
        output=output,
        log_artifact=log,
    )


def report(**kwargs) -> VerificationReport:
    return VerificationReport(task_run_id=uuid4(), external_task_id="TS-078", **kwargs)


def regressing_report(
    *steps: VerificationStep, baseline: set[str], candidate: set[str]
) -> VerificationReport:
    return report(
        steps=steps,
        comparison=compare(
            baseline=baseline, candidate=candidate, baseline_sha="abc1234"
        ),
    )


# --- 1 and 2: a new regression becomes bounded evidence with its identities --


def test_a_new_regression_produces_evidence_carrying_the_stable_identities():
    regression = "tests/test_nav.py::test_navigate"
    known = "tests/test_old.py::test_legacy"
    failing = regressing_report(
        step(output=pytest_output([known, regression])),
        baseline={known},
        candidate={known, regression},
    )

    evidence = failing.repair_evidence
    assert isinstance(evidence, RepairEvidence)
    assert evidence.classification is VerificationClassification.NEW_REGRESSION
    assert evidence.failure_identities == (regression,)
    assert evidence.baseline_sha == "abc1234"
    # The pre-existing failure is not in the evidence at all: not as an
    # identity, and not through the excerpt.
    assert known not in evidence.render()


def test_the_evidence_keeps_the_command_and_category_each_failure_came_from():
    """Provenance, which the aggregated comparison cannot express."""
    unit = "tests/test_nav.py::test_navigate"
    contract = "tests/test_api.py::test_contract"
    failing = regressing_report(
        step(command="pytest tests/unit", output=pytest_output([unit])),
        step(command="pytest tests/contract", output=pytest_output([contract])),
        baseline=set(),
        candidate={unit, contract},
    )

    evidence = failing.repair_evidence
    assert evidence is not None
    attribution = {
        command.command: command.failure_identities for command in evidence.commands
    }
    assert attribution == {
        "pytest tests/unit": (unit,),
        "pytest tests/contract": (contract,),
    }
    for command in evidence.commands:
        assert command.verification_type == VerificationType.TESTS.value


def test_the_evidence_is_deterministic_and_serialisable():
    first = "tests/test_a.py::test_one"
    second = "tests/test_b.py::test_two"
    failing = regressing_report(
        step(output=pytest_output([second, first])),
        baseline=set(),
        candidate={first, second},
    )

    one = failing.repair_evidence
    two = failing.repair_evidence
    assert one is not None and two is not None
    assert one.describe() == two.describe()
    # Sorted, so a set's iteration order never reaches a prompt.
    assert one.failure_identities == (first, second)
    assert one.render() == two.render()
    # The serialised form is what the report and the loop record.
    assert failing.describe()["repair_evidence"] == one.describe()


# --- 3 and 4: the bounds, and what must never travel ------------------------


def test_the_identity_list_is_bounded_and_says_what_it_dropped():
    regressions = {f"tests/test_x.py::test_{index}" for index in range(60)}
    failing = regressing_report(
        step(output=pytest_output(sorted(regressions))),
        baseline=set(),
        candidate=regressions,
    )

    evidence = failing.repair_evidence
    assert evidence is not None
    assert len(evidence.failure_identities) == MAX_EVIDENCE_IDENTITIES
    assert evidence.omitted_identities == 60 - MAX_EVIDENCE_IDENTITIES
    assert "40 more" in evidence.render()


def test_the_excerpt_is_bounded_by_lines_and_by_characters():
    regression = "tests/test_nav.py::test_navigate"
    noisy = "\n".join(
        [f"{regression} chatter {index}" for index in range(400)]
        + [f"FAILED {regression} - boom", "=== 1 failed, 9 passed in 1.0s ==="]
    )
    failing = regressing_report(
        step(output=noisy), baseline=set(), candidate={regression}
    )

    evidence = failing.repair_evidence
    assert evidence is not None
    (command,) = evidence.commands
    assert len(command.excerpt.splitlines()) <= MAX_EVIDENCE_EXCERPT_LINES + 1
    assert len(command.excerpt) <= MAX_EVIDENCE_EXCERPT_CHARS + 64
    assert "more line(s)" in command.excerpt

    wide = "\n".join(
        [f"{regression} {'x' * 2_000}" for _ in range(5)]
        + [f"FAILED {regression} - boom", "=== 1 failed, 9 passed in 1.0s ==="]
    )
    widest = regressing_report(
        step(output=wide), baseline=set(), candidate={regression}
    ).repair_evidence
    assert widest is not None
    assert len(widest.commands[0].excerpt) <= MAX_EVIDENCE_EXCERPT_CHARS + 64


def test_only_a_bounded_number_of_failing_commands_is_carried():
    regressions = [f"tests/test_{index}.py::test_one" for index in range(5)]
    failing = regressing_report(
        *(
            step(command=f"pytest shard-{index}", output=pytest_output([identity]))
            for index, identity in enumerate(regressions)
        ),
        baseline=set(),
        candidate=set(regressions),
    )

    evidence = failing.repair_evidence
    assert evidence is not None
    assert len(evidence.commands) == MAX_EVIDENCE_COMMANDS
    assert evidence.omitted_commands == 5 - MAX_EVIDENCE_COMMANDS
    assert "further failing command(s) not shown" in evidence.render()
    # The identities whose command was dropped are still named, and are not
    # silently attributed to a command that did not produce them.
    assert set(evidence.unattributed_identities) == set(regressions[MAX_EVIDENCE_COMMANDS:])


def test_the_full_verification_output_never_reaches_the_rendered_evidence():
    """The property requirement 4 is about, asserted on real-shaped output."""
    regression = "tests/test_nav.py::test_navigate"
    noise = [f"tests/test_old_{index}.py::test_legacy" for index in range(42)]
    output = "\n".join(
        [
            "============ test session starts ============",
            *[f"src/thing_{index}.py ...F..." for index in range(200)],
            pytest_output([*noise, regression]),
        ]
    )
    failing = regressing_report(
        step(output=output), baseline=set(noise), candidate={*noise, regression}
    )

    rendered = failing.feedback
    assert rendered is not None
    assert rendered == failing.repair_evidence.render()
    assert len(rendered) < len(output)
    assert len(rendered) <= MAX_EVIDENCE_CHARS
    # Not one pre-existing failure, and none of the session chatter.
    assert not any(identity in rendered for identity in noise)
    assert "test session starts" not in rendered
    # The log is referenced by path instead.
    assert "attempt-1/tests/01-pytest.log" in rendered


# --- 7 to 10: what does *not* become a regression repair --------------------


def test_a_passing_report_has_no_repair_evidence():
    passing = report(
        steps=(
            step(status=VerificationStatus.PASSED, exit_code=0, output="all good"),
        )
    )
    assert passing.classification is VerificationClassification.PASSED
    assert passing.repair_evidence is None
    assert passing.feedback is None


def test_known_baseline_only_produces_no_repair_evidence():
    known = "tests/test_old.py::test_legacy"
    failing = regressing_report(
        step(output=pytest_output([known])), baseline={known}, candidate={known}
    )

    assert failing.classification is VerificationClassification.KNOWN_BASELINE_ONLY
    assert failing.no_new_regressions
    assert failing.repair_evidence is None
    # Stage 2's text is unchanged: these failures are pre-existing.
    feedback = failing.feedback
    assert feedback is not None
    assert "do not investigate" in feedback
    assert "No new regression was attributed" in feedback
    assert "Repair only the regression(s)" not in feedback


def test_an_unclassified_failure_is_never_packaged_as_a_regression():
    """Fail-closed. Stage 2 could not attribute it; stage 3 does not either."""
    failing = report(
        steps=(step(output="Timeout: the suite never finished"),),
        comparison=unavailable("the output was clipped", baseline_sha="abc1234"),
    )

    assert failing.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert not failing.no_new_regressions
    assert failing.repair_evidence is None
    feedback = failing.feedback
    assert feedback is not None
    # The pre-stage-3 deterministic text, which claims nothing about cause.
    assert feedback.startswith("Verification failed.")
    assert "new regression" not in feedback


def test_build_refuses_a_regression_classification_without_a_comparison():
    """The constructor cannot be talked into it by its argument either."""
    assert (
        build_evidence(
            (step(output=pytest_output(["tests/test_a.py::test_one"])),),
            None,
            classification=VerificationClassification.NEW_REGRESSION,
        )
        is None
    )
    assert (
        build_evidence(
            (step(output=pytest_output(["tests/test_a.py::test_one"])),),
            unavailable("no baseline"),
            classification=VerificationClassification.NEW_REGRESSION,
        )
        is None
    )


def test_an_infrastructure_or_lint_failure_is_not_a_code_regression():
    """Requirement 8: a nonzero command is not evidence of a regression.

    A category with no stable failure identities makes the comparison
    unavailable upstream, so the report is unclassified and no evidence is
    built -- and the feedback keeps the existing build/lint semantics.
    """
    for category, command in (
        (VerificationType.LINT, "ruff check ."),
        (VerificationType.BUILD, "npm run build"),
    ):
        failing = report(
            steps=(step(category, command=command, output="error: boom"),),
            comparison=unavailable(
                f"{category.value.casefold()} failures have no stable failure "
                "identities to compare",
                baseline_sha="abc1234",
            ),
        )
        assert failing.classification is VerificationClassification.UNCLASSIFIED_FAILURE
        assert failing.repair_evidence is None
        feedback = failing.feedback
        assert feedback is not None
        assert command in feedback
        assert "new regression" not in feedback


def test_a_resolved_pre_existing_failure_is_a_count_not_an_instruction():
    regression = "tests/test_nav.py::test_navigate"
    failing = regressing_report(
        step(output=pytest_output([regression])),
        baseline={"tests/test_old.py::test_a", "tests/test_old.py::test_b"},
        candidate={regression},
    )

    evidence = failing.repair_evidence
    assert evidence is not None
    assert evidence.resolved_count == 2
    rendered = evidence.render()
    assert "resolved 2 pre-existing failure(s)" in rendered
    assert "Leave them fixed." in rendered


# --- the prompt contract the evidence carries -------------------------------


def test_the_rendered_evidence_states_the_stage_1_division_of_labour():
    regression = "tests/test_nav.py::test_navigate"
    rendered = regressing_report(
        step(output=pytest_output([regression])),
        baseline=set(),
        candidate={regression},
    ).feedback
    assert rendered is not None
    flat = " ".join(rendered.split()).lower()

    assert "authoritative verification found 1 new regression(s)" in flat
    assert "repair only the regression(s) above" in flat
    assert "you may run the specific tests directly affected" in flat
    assert "full verification suite will be run by the orchestrator" in flat
    assert "do not establish a baseline and do not compare one" in flat
