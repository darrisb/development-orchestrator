from __future__ import annotations

from uuid import uuid4

from apps.orchestrator.domain.enums import IssueCategory, IssueSeverity, ReviewDecision
from apps.orchestrator.domain.models import Lesson, Review, ReviewIssue


def _issue(severity: IssueSeverity, resolved: bool = False) -> ReviewIssue:
    return ReviewIssue(
        severity=severity,
        category=IssueCategory.REQUIREMENT,
        problem="p",
        required_fix="f",
        resolved=resolved,
    )


def test_only_blocking_severities_force_a_retry():
    """Section 22: non-blocking observations must not restart the loop."""
    assert _issue(IssueSeverity.CRITICAL).is_blocking
    assert _issue(IssueSeverity.HIGH).is_blocking
    assert _issue(IssueSeverity.MEDIUM).is_blocking
    assert not _issue(IssueSeverity.LOW).is_blocking
    assert not _issue(IssueSeverity.INFO).is_blocking


def test_review_blocking_issues_exclude_resolved_and_advisory_ones():
    review = Review(
        task_run_id=uuid4(),
        reviewer_provider="mock",
        reviewer_model="mock-1",
        decision=ReviewDecision.CHANGES_REQUESTED,
        summary="s",
        issues=[
            _issue(IssueSeverity.HIGH),
            _issue(IssueSeverity.HIGH, resolved=True),
            _issue(IssueSeverity.LOW),
        ],
    )
    assert len(review.blocking_issues) == 1


def test_project_lessons_are_not_global():
    """Section 32 rule 4: project lessons never implicitly become global."""
    assert Lesson(category="c", title="t", lesson="l", project_id=uuid4()).is_global is False
    assert Lesson(category="c", title="t", lesson="l").is_global is True
