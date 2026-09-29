from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from ..domain.enums import (
    EscalationStatus,
    IssueCategory,
    IssueSeverity,
    ReviewDecision,
    RiskLevel,
)
from ..domain.escalation import EscalationIntent, option_labels
from ..domain.models import HumanEscalation, Review, ReviewIssue


class ReviewIssueResponse(BaseModel):
    id: UUID
    severity: IssueSeverity
    category: IssueCategory
    file: str | None
    line: int | None
    requirement_id: str | None
    problem: str
    required_fix: str
    resolved: bool
    #: Derived, not stored: whether this issue forces a retry (section 22).
    #: Exposed so a reader of the API sees the same distinction the workflow
    #: acted on rather than having to re-derive it from the severity.
    blocking: bool

    @classmethod
    def from_domain(cls, issue: ReviewIssue) -> ReviewIssueResponse:
        return cls(
            id=issue.id,
            severity=issue.severity,
            category=issue.category,
            file=issue.file,
            line=issue.line,
            requirement_id=issue.requirement_id,
            problem=issue.problem,
            required_fix=issue.required_fix,
            resolved=issue.resolved,
            blocking=issue.is_blocking,
        )


class ReviewResponse(BaseModel):
    id: UUID
    task_run_id: UUID
    reviewer_provider: str
    reviewer_model: str
    decision: ReviewDecision
    confidence: float | None
    risk: RiskLevel | None
    summary: str
    cycle: int
    issues: list[ReviewIssueResponse]
    created_at: datetime | None

    @classmethod
    def from_domain(cls, review: Review) -> ReviewResponse:
        return cls(
            id=review.id,
            task_run_id=review.task_run_id,
            reviewer_provider=review.reviewer_provider,
            reviewer_model=review.reviewer_model,
            decision=review.decision,
            confidence=review.confidence,
            risk=review.risk,
            summary=review.summary,
            cycle=review.cycle,
            issues=[ReviewIssueResponse.from_domain(issue) for issue in review.issues],
            created_at=review.created_at,
        )


class EscalationResponse(BaseModel):
    id: UUID
    task_id: UUID
    task_run_id: UUID | None
    reason: str
    summary: str
    options: list[str]
    option_intents: dict[str, EscalationIntent]
    status: EscalationStatus
    resolution: str | None
    #: The Git commit SHA the operator supplied for a ``COMPLETED_BY_HAND``
    #: resolution (concern 73). ``None`` for all other intents and for the
    #: explicit no-code completion path.
    human_commit: str | None
    created_at: datetime | None
    resolved_at: datetime | None

    @classmethod
    def from_domain(cls, escalation: HumanEscalation) -> EscalationResponse:
        return cls(
            id=escalation.id,
            task_id=escalation.task_id,
            task_run_id=escalation.task_run_id,
            reason=escalation.reason,
            summary=escalation.summary,
            options=list(option_labels(escalation.options)),
            option_intents={
                option.key: option.intent
                for option in escalation.options
                if hasattr(option, "intent")
            },
            status=escalation.status,
            resolution=escalation.resolution,
            human_commit=escalation.human_commit,
            created_at=escalation.created_at,
            resolved_at=escalation.resolved_at,
        )


class ResolveEscalationRequest(BaseModel):
    """A human's answer to an escalation (section 24).

    ``resolution`` is required and free text: the options the orchestrator
    offered are a prompt, not an enumeration, and a person who chose none of
    them still has the answer. What the answer *does* to the task is phase K's
    business; recording it is this endpoint's.
    """

    resolution: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1)
    ]
    #: The displayed option key. Required for a resolved escalation so the
    #: workflow acts on an explicit intent rather than parsing free text.
    option_key: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=8)
    ] = None
    #: ``DISMISSED`` for an escalation that turned out not to need an answer.
    status: EscalationStatus = EscalationStatus.RESOLVED
    #: The Git commit SHA the operator produced by hand, for a
    #: ``COMPLETED_BY_HAND`` resolution (concern 73). When supplied, the
    #: orchestrator validates the commit and integrates it through the
    #: canonical mechanism before marking the task COMPLETE. When absent, the
    #: resolution is treated as an explicit no-code completion.
    human_commit: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=7, max_length=64)
    ] = None


class ReconcileHumanCommitRequest(BaseModel):
    """Reconcile a historical COMPLETED_BY_HAND escalation with a human commit.

    For escalations resolved before concern 73, where the human commit was not
    recorded or integrated. This endpoint integrates the commit through the
    canonical mechanism and updates the escalation record.
    """

    human_commit: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=7, max_length=64)
    ]
