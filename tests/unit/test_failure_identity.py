"""Failure identities and the set arithmetic over them (concern 78, stage 2).

The unit the classification rests on. Everything here is pure: output text in,
identities out; two sets in, three sets out. The pipeline's use of it is in
``tests/integration/test_concern78_baseline_classification.py``.

The tests that matter most are the ones about *not* answering. An extractor
that guesses is worse than no extractor, because a guessed-complete failure set
turns a real regression into a known failure and sends the candidate to a
reviewer with the orchestrator vouching for it.
"""

from __future__ import annotations

from uuid import uuid4

from apps.orchestrator.domain.enums import (
    VerificationClassification,
    VerificationStatus,
    VerificationType,
)
from apps.orchestrator.domain.failure_identity import (
    MAX_RENDERED_IDENTITIES,
    compare,
    extract_failure_identities,
    extract_pytest_failures,
    unavailable,
)
from apps.orchestrator.domain.verification import (
    MAX_REGRESSION_EXCERPT_LINES,
    VerificationReport,
    VerificationStep,
)


def pytest_output(identities: list[str], *, passed: int = 100) -> str:
    lines = [f"FAILED {identity} - AssertionError: nope" for identity in identities]
    failed = len(identities)
    banner = (
        f"=========== {failed} failed, {passed} passed in 1.23s ==========="
        if failed
        else f"=========== {passed} passed in 1.23s ==========="
    )
    return "\n".join(["=== short test summary info ===", *lines, banner])


# --- extraction --------------------------------------------------------------


def test_pytest_node_ids_are_extracted_with_the_banner_as_the_check():
    identities = extract_pytest_failures(
        pytest_output(["tests/test_a.py::test_one", "tests/test_b.py::test_two"])
    )
    assert identities == frozenset(
        {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}
    )


def test_a_collection_error_is_an_identity_too():
    text = "\n".join(
        [
            "ERROR tests/test_broken.py",
            "=== 1 error in 0.40s ===",
        ]
    )
    assert extract_pytest_failures(text) == frozenset({"tests/test_broken.py"})


def test_a_clipped_failure_list_is_unreadable_rather_than_short():
    """The one bug that would matter, pinned.

    The step's output is a tail, so losing the first ``FAILED`` lines is the
    normal way this goes wrong, and the result still looks perfectly
    well-formed. Reconciling against the banner's own count is what turns a
    silently-short set into an honest refusal.
    """
    full = pytest_output([f"tests/test_a.py::test_{index}" for index in range(5)])
    clipped = "\n".join(full.splitlines()[3:])

    assert "FAILED" in clipped and "failed," in clipped
    assert extract_pytest_failures(clipped) is None


def test_output_with_no_banner_is_not_claimed():
    assert extract_pytest_failures("FAILED tests/test_a.py::test_one") is None


def test_a_non_zero_run_that_reported_no_failures_has_nothing_to_compare():
    """A coverage threshold, an internal error, a collection abort.

    Zero failures and a non-zero exit code is a failure with no test identity,
    and an empty set here would read as "this command found nothing wrong" --
    which would make every candidate failure of the same command a regression,
    or, in the baseline direction, hide one.
    """
    assert extract_pytest_failures("=== 100 passed in 1.0s ===") is None


def test_unrecognised_output_is_unreadable_by_every_adapter():
    assert extract_failure_identities("ERROR: Build failed in 4s\n") is None
    assert extract_failure_identities("") is None


def test_the_adapter_that_read_the_output_is_named():
    read = extract_failure_identities(pytest_output(["tests/test_a.py::test_one"]))
    assert read is not None
    name, identities = read
    assert name == "pytest"
    assert identities == frozenset({"tests/test_a.py::test_one"})


def test_failed_appearing_inside_a_traceback_does_not_invent_an_identity():
    text = "\n".join(
        [
            "    assert FAILED tests/test_elsewhere.py::test_x",
            "FAILED tests/test_a.py::test_one - AssertionError",
            "=== 1 failed, 2 passed in 1.0s ===",
        ]
    )
    assert extract_pytest_failures(text) == frozenset({"tests/test_a.py::test_one"})


# --- comparison --------------------------------------------------------------


def test_the_three_sets_are_set_difference_and_nothing_else():
    result = compare(baseline={"A", "B", "C"}, candidate={"B", "C"})
    assert result.available
    assert result.known == frozenset({"B", "C"})
    assert result.new == frozenset()
    assert result.resolved == frozenset({"A"})


def test_equal_counts_with_different_identities_are_not_equivalent():
    """The whole reason counts are not an identity.

    One baseline failure fixed, one new one introduced: forty-two before and
    forty-two after, and a count-based comparison would call that clean.
    """
    baseline = {f"tests/test_a.py::test_{index}" for index in range(42)}
    candidate = (baseline - {"tests/test_a.py::test_0"}) | {
        "tests/test_new.py::test_regression"
    }
    assert len(baseline) == len(candidate)

    result = compare(baseline=baseline, candidate=candidate)
    assert result.new == frozenset({"tests/test_new.py::test_regression"})
    assert result.resolved == frozenset({"tests/test_a.py::test_0"})
    assert result.has_new_failures
    assert not result.clean_against_baseline


def test_a_mixture_of_known_and_new_separates_cleanly():
    result = compare(baseline={"A", "B"}, candidate={"A", "B", "C", "D"})
    assert result.known == frozenset({"A", "B"})
    assert result.new == frozenset({"C", "D"})
    assert result.resolved == frozenset()


def test_an_unavailable_comparison_is_empty_and_says_why():
    result = unavailable("no baseline", baseline_sha="abc")
    assert not result.available
    assert not result.has_new_failures
    assert not result.clean_against_baseline
    assert result.detail == "no baseline"


def test_described_identities_are_bounded():
    result = compare(baseline=set(), candidate={f"test_{index}" for index in range(80)})
    rendered = result.describe()["new"]
    assert isinstance(rendered, list)
    assert len(rendered) == MAX_RENDERED_IDENTITIES + 1
    assert rendered[-1].startswith("[... 60 more")
    assert result.describe()["counts"]["new"] == 80


# --- the report's classification --------------------------------------------


def failing_tests_step(output: str = "", log: str | None = "tests/01.log") -> VerificationStep:
    return VerificationStep(
        verification_type=VerificationType.TESTS,
        status=VerificationStatus.FAILED,
        command="pytest",
        exit_code=1,
        executed=True,
        output=output,
        log_artifact=log,
    )


def report(**kwargs) -> VerificationReport:
    return VerificationReport(
        task_run_id=uuid4(), external_task_id="TS-078", **kwargs
    )


def test_a_passing_report_classifies_as_passed():
    passing = report(
        steps=(
            VerificationStep(
                verification_type=VerificationType.TESTS,
                status=VerificationStatus.PASSED,
                command="pytest",
                exit_code=0,
                executed=True,
            ),
        )
    )
    assert passing.classification is VerificationClassification.PASSED
    assert passing.no_new_regressions
    assert passing.feedback is None


def test_a_failure_with_no_comparison_fails_closed():
    """The default, and the one that keeps pre-stage-2 behaviour intact."""
    failing = report(steps=(failing_tests_step(),))
    assert failing.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert not failing.no_new_regressions
    assert not failing.passed


def test_an_unavailable_comparison_fails_closed_just_the_same():
    failing = report(
        steps=(failing_tests_step(),),
        comparison=unavailable("no baseline evidence", baseline_sha="abc1234"),
    )
    assert failing.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert not failing.no_new_regressions


def test_failures_matching_the_baseline_classify_as_known_and_regress_nothing():
    failing = report(
        steps=(failing_tests_step(),),
        comparison=compare(
            baseline={"A", "B"}, candidate={"A", "B"}, baseline_sha="abc1234"
        ),
    )
    assert failing.classification is VerificationClassification.KNOWN_BASELINE_ONLY
    assert failing.no_new_regressions
    # Still a failing report. The claim is "broke nothing", never "passed".
    assert not failing.passed
    assert failing.failures


def test_one_extra_identity_is_a_regression():
    failing = report(
        steps=(failing_tests_step(),),
        comparison=compare(
            baseline={"A", "B"}, candidate={"A", "B", "C"}, baseline_sha="abc1234"
        ),
    )
    assert failing.classification is VerificationClassification.NEW_REGRESSION
    assert not failing.no_new_regressions


# --- repair feedback ---------------------------------------------------------


def test_repair_feedback_names_the_regression_and_leaves_the_suite_on_disk():
    """Stage 2's purpose, measured on the prompt it produces.

    Forty-two pre-existing failures are in the output. None of them belongs in
    the repair call: the coder cannot act on them, stage 1 told it not to
    investigate them, and the tokens are the budget it needs for the one
    failure it did cause.
    """
    noise = [f"tests/test_old.py::test_{index}" for index in range(42)]
    regression = "tests/test_nav.py::test_navigate"
    output = pytest_output([*noise, regression])

    failing = report(
        steps=(failing_tests_step(output=output, log="attempt-1/tests/01-pytest.log"),),
        comparison=compare(
            baseline=set(noise),
            candidate={*noise, regression},
            baseline_sha="abc1234",
        ),
    )
    feedback = failing.feedback
    assert feedback is not None

    assert "1 new regression" in feedback
    assert regression in feedback
    assert "abc1234" in feedback
    # The log is referenced, not pasted.
    assert "attempt-1/tests/01-pytest.log" in feedback
    # And not one of the forty-two pre-existing failures travels with it.
    assert not any(identity in feedback for identity in noise)
    assert len(feedback.splitlines()) < len(output.splitlines())
    # Stage 1's boundary is restated rather than relaxed.
    assert "full verification suite will be run by the orchestrator" in feedback


def test_a_resolved_baseline_failure_is_reported_and_not_requested_back():
    failing = report(
        steps=(failing_tests_step(output=pytest_output(["C"])),),
        comparison=compare(baseline={"A", "B"}, candidate={"C"}, baseline_sha="abc"),
    )
    feedback = failing.feedback
    assert feedback is not None
    assert "resolved 2 pre-existing failure(s)" in feedback
    assert "Leave them fixed." in feedback


def test_baseline_only_feedback_tells_the_coder_not_to_investigate():
    failing = report(
        steps=(failing_tests_step(output=pytest_output(["A"])),),
        comparison=compare(baseline={"A"}, candidate={"A"}, baseline_sha="abc1234"),
    )
    feedback = failing.feedback
    assert feedback is not None
    assert "pre-existing" in feedback
    assert "do not investigate" in feedback
    assert "abc1234" in feedback


def test_the_regression_excerpt_is_bounded():
    regression = "tests/test_nav.py::test_navigate"
    output = "\n".join(
        [f"{regression} line {index}" for index in range(500)]
        + [f"FAILED {regression} - boom", "=== 1 failed, 1 passed in 1.0s ==="]
    )
    failing = report(
        steps=(failing_tests_step(output=output),),
        comparison=compare(baseline=set(), candidate={regression}, baseline_sha="abc"),
    )
    feedback = failing.feedback
    assert feedback is not None
    assert len(feedback.splitlines()) < MAX_REGRESSION_EXCERPT_LINES + 20
    assert "more line(s)" in feedback


def test_without_a_comparison_the_feedback_is_the_old_deterministic_output():
    """Unchanged behaviour where stage 2 has nothing to say."""
    failing = report(steps=(failing_tests_step(output="src/nav.py:1: boom"),))
    feedback = failing.feedback
    assert feedback is not None
    assert feedback.startswith("Verification failed.")
    assert "src/nav.py:1: boom" in feedback


def test_the_report_description_carries_the_classification():
    failing = report(
        steps=(failing_tests_step(),),
        comparison=compare(baseline={"A"}, candidate={"A"}, baseline_sha="abc"),
    )
    described = failing.describe()
    assert described["classification"] == "KNOWN_BASELINE_ONLY"
    assert described["no_new_regressions"] is True
    assert described["comparison"]["baseline_sha"] == "abc"
    assert described["comparison"]["counts"] == {"known": 1, "new": 0, "resolved": 0}
