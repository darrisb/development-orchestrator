"""The structured review contract (build.md section 21).

The first test is section 21's own example response, parsed unchanged: if the
specification's sample does not survive this module, the schema is wrong and
not the sample. The rest are the ways a real local reviewer answers badly --
a severity it invented, a confidence expressed as a percentage, an approval
that contradicts its own findings -- and what each of those becomes.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.enums import (
    IssueCategory,
    IssueSeverity,
    ReviewDecision,
    RiskLevel,
)
from apps.orchestrator.domain.review import (
    UNKNOWN_CATEGORY,
    UNKNOWN_SEVERITY,
    MalformedReview,
    parse_review,
    reconcile,
    render_review_feedback,
)

#: build.md section 21, verbatim.
SPECIFICATION_EXAMPLE = {
    "taskId": "TS-004",
    "decision": "CHANGES_REQUESTED",
    "confidence": 0.93,
    "risk": "MEDIUM",
    "summary": "Implementation is mostly correct but misses selection restoration.",
    "issues": [
        {
            "severity": "HIGH",
            "category": "requirement",
            "file": "src/providers/navigation-tree-provider.ts",
            "line": 84,
            "requirementId": "TS-004-R7",
            "problem": "Saved selection is not restored.",
            "requiredFix": "Restore the stored selection after opening the document.",
        }
    ],
}


def test_the_specifications_own_example_parses():
    result = parse_review(SPECIFICATION_EXAMPLE, external_task_id="TS-004")

    assert result.decision is ReviewDecision.CHANGES_REQUESTED
    assert result.confidence == pytest.approx(0.93)
    assert result.risk is RiskLevel.MEDIUM
    assert result.reported_task_id == "TS-004"
    assert not result.warnings

    (issue,) = result.issues
    assert issue.severity is IssueSeverity.HIGH
    assert issue.category is IssueCategory.REQUIREMENT
    assert issue.file == "src/providers/navigation-tree-provider.ts"
    assert issue.line == 84
    assert issue.requirement_id == "TS-004-R7"
    assert issue.is_blocking
    assert result.blocking_issues == result.issues


def test_an_answer_with_no_decision_is_not_a_review():
    with pytest.raises(MalformedReview):
        parse_review({"summary": "looks fine", "issues": []})


def test_an_unrecognised_decision_is_refused_rather_than_guessed():
    with pytest.raises(MalformedReview, match="LGTM"):
        parse_review({"decision": "LGTM", "summary": "fine", "issues": []})


def test_a_decision_is_read_through_spacing_and_case():
    result = parse_review({"decision": "changes requested", "summary": "x", "issues": [
        {"severity": "HIGH", "category": "correctness", "problem": "p", "requiredFix": "f"}
    ]})
    assert result.decision is ReviewDecision.CHANGES_REQUESTED


def test_an_invented_severity_is_read_as_blocking():
    """An unknown word is still a complaint. Reading it as INFO would let a
    defect through on a spelling mistake."""
    result = parse_review(
        {
            "decision": "CHANGES_REQUESTED",
            "summary": "x",
            "issues": [
                {
                    "severity": "BLOCKER",
                    "category": "correctness",
                    "problem": "p",
                    "requiredFix": "f",
                }
            ],
        }
    )
    (issue,) = result.issues
    assert issue.severity is UNKNOWN_SEVERITY
    assert issue.is_blocking
    assert any("BLOCKER" in warning for warning in result.warnings)


def test_an_invented_category_falls_back_and_is_recorded():
    result = parse_review(
        {
            "decision": "CHANGES_REQUESTED",
            "summary": "x",
            "issues": [
                {
                    "severity": "HIGH",
                    "category": "performance",
                    "problem": "p",
                    "requiredFix": "f",
                }
            ],
        }
    )
    assert result.issues[0].category is UNKNOWN_CATEGORY
    assert any("performance" in warning for warning in result.warnings)


def test_a_percentage_confidence_is_read_as_the_reviewer_meant_it():
    result = parse_review({"decision": "APPROVED", "summary": "x", "issues": [], "confidence": 93})
    assert result.confidence == pytest.approx(0.93)
    assert any("93" in warning for warning in result.warnings)


def test_a_nonsense_confidence_is_dropped_rather_than_clamped():
    result = parse_review(
        {"decision": "APPROVED", "summary": "x", "issues": [], "confidence": 4200}
    )
    assert result.confidence is None
    assert any("out of range" in warning for warning in result.warnings)


def test_an_issue_without_a_problem_is_dropped_and_one_without_a_fix_is_kept():
    """A defect described without a remedy is still a defect; a remedy with
    nothing wrong is noise."""
    result = parse_review(
        {
            "decision": "CHANGES_REQUESTED",
            "summary": "x",
            "issues": [
                {"severity": "HIGH", "category": "correctness", "requiredFix": "do it"},
                {"severity": "HIGH", "category": "correctness", "problem": "broken"},
            ],
        }
    )
    (issue,) = result.issues
    assert issue.problem == "broken"
    assert issue.required_fix
    assert len(result.warnings) == 2


def test_issues_that_are_not_a_list_are_refused():
    with pytest.raises(MalformedReview):
        parse_review({"decision": "APPROVED", "summary": "x", "issues": "none"})


def test_a_task_id_mismatch_is_a_warning_and_not_a_rejection():
    """The diff is what was reviewed; the identifier is the model's
    transcription of it."""
    result = parse_review({**SPECIFICATION_EXAMPLE, "taskId": "TS-009"}, external_task_id="TS-004")
    assert result.decision is ReviewDecision.CHANGES_REQUESTED
    assert any("TS-009" in warning for warning in result.warnings)


# --- reconciliation ----------------------------------------------------------


def test_an_approval_that_lists_a_blocking_issue_is_not_an_approval():
    result = parse_review(
        {
            "decision": "APPROVED",
            "summary": "looks good",
            "issues": [
                {
                    "severity": "CRITICAL",
                    "category": "security",
                    "problem": "the token is logged",
                    "requiredFix": "stop logging it",
                }
            ],
        }
    )
    assert result.decision is ReviewDecision.CHANGES_REQUESTED
    assert any("approved while reporting" in warning for warning in result.warnings)


def test_an_approval_with_only_observations_stands():
    result = parse_review(
        {
            "decision": "APPROVED",
            "summary": "fine",
            "issues": [
                {
                    "severity": "INFO",
                    "category": "observation",
                    "problem": "could be tidier later",
                    "requiredFix": "none",
                }
            ],
        }
    )
    assert result.decision is ReviewDecision.APPROVED
    assert not result.blocking_issues
    assert result.observations


def test_changes_requested_with_no_issues_becomes_a_human_decision():
    """There is nothing to put in a correction prompt, so the coder would burn
    a cycle guessing."""
    result = parse_review({"decision": "CHANGES_REQUESTED", "summary": "not happy", "issues": []})
    assert result.decision is ReviewDecision.HUMAN_REVIEW_REQUIRED


def test_changes_requested_with_only_minor_issues_is_left_alone():
    """The severities may be under-graded, and the findings are still real."""
    decision, warnings = reconcile(
        ReviewDecision.CHANGES_REQUESTED,
        parse_review(
            {
                "decision": "CHANGES_REQUESTED",
                "summary": "x",
                "issues": [
                    {
                        "severity": "LOW",
                        "category": "style",
                        "problem": "p",
                        "requiredFix": "f",
                    }
                ],
            }
        ).issues,
    )
    assert decision is ReviewDecision.CHANGES_REQUESTED
    assert not warnings


# --- feedback ----------------------------------------------------------------


def test_feedback_quotes_the_blocking_issues_only():
    result = parse_review(
        {
            "decision": "CHANGES_REQUESTED",
            "summary": "misses a requirement",
            "issues": [
                {
                    "severity": "HIGH",
                    "category": "requirement",
                    "file": "src/nav.ts",
                    "line": 84,
                    "requirementId": "TS-004-R7",
                    "problem": "Saved selection is not restored.",
                    "requiredFix": "Restore it after opening the document.",
                },
                {
                    "severity": "INFO",
                    "category": "observation",
                    "problem": "a nicer name exists",
                    "requiredFix": "rename it one day",
                },
            ],
        }
    )
    feedback = render_review_feedback(result)

    assert "Saved selection is not restored." in feedback
    assert "src/nav.ts:84" in feedback
    assert "TS-004-R7" in feedback
    assert "a nicer name exists" not in feedback


def test_feedback_falls_back_to_minor_issues_rather_than_being_empty():
    result = parse_review(
        {
            "decision": "CHANGES_REQUESTED",
            "summary": "small things",
            "issues": [
                {
                    "severity": "LOW",
                    "category": "style",
                    "problem": "inconsistent quoting",
                    "requiredFix": "use double quotes",
                }
            ],
        }
    )
    assert "inconsistent quoting" in render_review_feedback(result)
