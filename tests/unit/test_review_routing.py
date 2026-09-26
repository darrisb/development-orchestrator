"""Where a review sends a task (build.md sections 22, 23 and 37).

Three rules are under test here and they interact, which is the whole reason
routing is a separate function from parsing:

* only blocking issues force a retry (section 22);
* the review budget is finite, and running out of it escalates rather than
  loops (section 23);
* a change in a high-risk area is never accepted automatically, and
  confidence does not buy a way past that (sections 21 and 37).
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.enums import (
    FailureReason,
    IssueCategory,
    IssueSeverity,
    ReviewDecision,
    TaskStatus,
)
from apps.orchestrator.domain.models import ReviewIssue
from apps.orchestrator.domain.review import (
    HumanApprovalPolicy,
    ReviewResult,
    route_review,
)
from apps.orchestrator.domain.scope import SensitiveCategory


def issue(severity: IssueSeverity = IssueSeverity.HIGH) -> ReviewIssue:
    return ReviewIssue(
        severity=severity,
        category=IssueCategory.REQUIREMENT,
        problem="a requirement is not met",
        required_fix="meet it",
    )


def result(
    decision: ReviewDecision = ReviewDecision.APPROVED,
    *,
    issues: tuple[ReviewIssue, ...] = (),
    confidence: float | None = 0.9,
) -> ReviewResult:
    return ReviewResult(
        decision=decision,
        summary="a summary",
        issues=issues,
        confidence=confidence,
        provider_id="test-reviewer",
        model_name="reviewer-test",
    )


def route(review: ReviewResult, **overrides):
    arguments = {"cycle": 1, "max_review_cycles": 3}
    arguments.update(overrides)
    return route_review(review, **arguments)


# --- the three decisions -----------------------------------------------------


def test_an_approval_completes_the_task():
    routing = route(result())

    assert routing.approved
    assert routing.task_status is TaskStatus.APPROVED
    assert routing.failure_reason is None
    assert routing.feedback is None


def test_a_change_request_goes_back_to_the_coder_with_the_findings():
    routing = route(result(ReviewDecision.CHANGES_REQUESTED, issues=(issue(),)))

    assert routing.needs_fix
    assert routing.task_status is TaskStatus.CHANGES_REQUESTED
    assert routing.failure_reason is FailureReason.REVIEW_CHANGES_REQUESTED
    assert "a requirement is not met" in routing.feedback
    assert not routing.retry_exhausted


def test_a_human_review_request_stops_and_says_why():
    routing = route(result(ReviewDecision.HUMAN_REVIEW_REQUIRED))

    assert routing.needs_human
    assert routing.task_status is TaskStatus.HUMAN_REVIEW
    assert routing.failure_reason is FailureReason.HUMAN_DECISION_REQUIRED
    assert routing.human_review_reasons


# --- section 23: the review budget -------------------------------------------


def test_the_last_cycle_escalates_instead_of_looping():
    routing = route(
        result(ReviewDecision.CHANGES_REQUESTED, issues=(issue(),)),
        cycle=3,
        max_review_cycles=3,
    )

    assert routing.needs_human
    assert routing.retry_exhausted
    assert routing.failure_reason is FailureReason.RETRY_EXHAUSTED
    # The findings are still carried, so a human sees what the coder would have.
    assert "a requirement is not met" in routing.feedback


def test_a_cycle_within_budget_still_goes_to_the_coder():
    routing = route(
        result(ReviewDecision.CHANGES_REQUESTED, issues=(issue(),)),
        cycle=2,
        max_review_cycles=3,
    )
    assert routing.needs_fix


# --- section 37: the human-approval gate -------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        SensitiveCategory.SECURITY,
        SensitiveCategory.PAYMENT,
        SensitiveCategory.MIGRATION,
        SensitiveCategory.CI_DEPLOYMENT,
        SensitiveCategory.DEPENDENCY_MANIFEST,
    ],
)
def test_an_approval_in_a_high_risk_area_needs_a_human(category: SensitiveCategory):
    routing = route(result(), sensitive=[category])

    assert routing.needs_human
    assert routing.escalated_by_policy
    assert routing.reviewer_decision is ReviewDecision.APPROVED
    wanted = category.value.replace("_", " ")
    assert any(wanted in reason for reason in routing.human_review_reasons)


def test_confidence_cannot_buy_a_way_past_the_gate():
    """Section 21: confidence must not override mandatory human-review policy."""
    routing = route(result(confidence=1.0), sensitive=[SensitiveCategory.SECURITY])
    assert routing.needs_human


def test_the_gate_does_not_apply_to_a_change_request():
    """It governs acceptance. Nothing is being accepted, so the fix loop runs
    and a person is not asked to watch a model iterate."""
    routing = route(
        result(ReviewDecision.CHANGES_REQUESTED, issues=(issue(),)),
        sensitive=[SensitiveCategory.SECURITY],
    )

    assert routing.needs_fix
    # The reason is carried forward, so the eventual approval still trips it.
    assert routing.human_review_reasons


def test_a_low_confidence_approval_is_sent_to_a_human():
    routing = route(result(confidence=0.4))

    assert routing.needs_human
    assert any("confidence" in reason for reason in routing.human_review_reasons)


def test_a_missing_confidence_does_not_trip_the_gate():
    """An endpoint that does not report confidence is not a suspicious one."""
    assert route(result(confidence=None)).approved


def test_deleting_more_files_than_policy_allows_needs_a_human():
    routing = route(result(), deleted_paths=["a", "b", "c", "d"])

    assert routing.needs_human
    assert any("deletes 4 files" in reason for reason in routing.human_review_reasons)


def test_verification_flags_are_carried_into_the_gate():
    """The pipeline saw the whole diff; routing only sees what it is handed."""
    routing = route(result(), pending_review_reasons=["a secret-shaped string was added"])

    assert routing.needs_human
    assert "a secret-shaped string was added" in routing.human_review_reasons


def test_every_reason_is_reported_not_just_the_first():
    """An escalation that names one of three reasons invites a human to clear
    it and be surprised by the next."""
    routing = route(
        result(confidence=0.1),
        sensitive=[SensitiveCategory.SECURITY, SensitiveCategory.PAYMENT],
        deleted_paths=["a", "b", "c", "d"],
    )
    assert len(routing.human_review_reasons) == 4


def test_a_disabled_policy_accepts_what_it_would_otherwise_gate():
    routing = route(
        result(confidence=0.1),
        sensitive=[SensitiveCategory.SECURITY],
        policy=HumanApprovalPolicy(
            gated_categories=frozenset(), min_confidence=None, max_deleted_files=10**6
        ),
    )
    assert routing.approved


def test_an_ungated_sensitive_category_does_not_escalate():
    routing = route(
        result(),
        sensitive=[SensitiveCategory.LOCKFILE],
        policy=HumanApprovalPolicy(gated_categories=frozenset({SensitiveCategory.SECURITY})),
    )
    assert routing.approved


def test_the_routing_describes_a_disagreement_with_the_reviewer():
    routing = route(result(), sensitive=[SensitiveCategory.SECURITY])
    described = routing.describe()

    assert described["decision"] == ReviewDecision.HUMAN_REVIEW_REQUIRED.value
    assert described["reviewer_decision"] == ReviewDecision.APPROVED.value
    assert "reviewer said APPROVED" in routing.summary()
