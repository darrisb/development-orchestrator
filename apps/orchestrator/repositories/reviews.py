from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db.models import ReviewIssueRow, ReviewRow, TaskRow, TaskRunRow
from ..domain.models import Review, ReviewIssue
from .base import Repository


class ReviewRepository(Repository[ReviewRow, Review]):
    row_type = ReviewRow

    def _issue_to_domain(self, row: ReviewIssueRow) -> ReviewIssue:
        return ReviewIssue(
            id=row.id,
            review_id=row.review_id,
            severity=row.severity,
            category=row.category,
            file=row.file,
            line=row.line,
            requirement_id=row.requirement_id,
            problem=row.problem,
            required_fix=row.required_fix,
            resolved=row.resolved,
        )

    def _to_domain(self, row: ReviewRow) -> Review:
        return Review(
            id=row.id,
            task_run_id=row.task_run_id,
            reviewer_provider=row.reviewer_provider,
            reviewer_model=row.reviewer_model,
            decision=row.decision,
            confidence=row.confidence,
            risk=row.risk,
            summary=row.summary,
            cycle=row.cycle,
            issues=[self._issue_to_domain(issue) for issue in row.issues],
            created_at=row.created_at,
        )

    def add(self, review: Review) -> Review:
        row = ReviewRow(
            id=review.id,
            task_run_id=review.task_run_id,
            reviewer_provider=review.reviewer_provider,
            reviewer_model=review.reviewer_model,
            decision=review.decision,
            confidence=review.confidence,
            risk=review.risk,
            summary=review.summary,
            cycle=review.cycle,
        )
        row.issues = [
            ReviewIssueRow(
                id=issue.id,
                severity=issue.severity,
                category=issue.category,
                file=issue.file,
                line=issue.line,
                requirement_id=issue.requirement_id,
                problem=issue.problem,
                required_fix=issue.required_fix,
                resolved=issue.resolved,
            )
            for issue in review.issues
        ]
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, review_id: UUID) -> Review | None:
        row = self.session.scalar(
            select(ReviewRow)
            .options(selectinload(ReviewRow.issues))
            .where(ReviewRow.id == review_id)
        )
        return self._to_domain(row) if row else None

    def list_for_run(self, task_run_id: UUID) -> list[Review]:
        rows = self.session.scalars(
            select(ReviewRow)
            .options(selectinload(ReviewRow.issues))
            .where(ReviewRow.task_run_id == task_run_id)
            .order_by(ReviewRow.cycle)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_task(self, task_id: UUID) -> list[Review]:
        """Every review of a task, across all of its runs.

        Phase L's review history. A task is retried rather than abandoned, so
        the reviews that explain how it got to ``COMPLETE`` are spread over
        several runs, and looking only at the last one is how a reviewer
        becomes "the reviewer keeps asking for the same thing" with no way to
        see that it was fixed on the third run.
        """
        rows = self.session.scalars(
            select(ReviewRow)
            .join(TaskRunRow, ReviewRow.task_run_id == TaskRunRow.id)
            .options(selectinload(ReviewRow.issues))
            .where(TaskRunRow.task_id == task_id)
            .order_by(ReviewRow.created_at, ReviewRow.cycle)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_project(
        self, project_id: UUID, *, limit: int = 200
    ) -> list[Review]:
        rows = self.session.scalars(
            select(ReviewRow)
            .join(TaskRunRow, ReviewRow.task_run_id == TaskRunRow.id)
            .join(TaskRow, TaskRunRow.task_id == TaskRow.id)
            .options(selectinload(ReviewRow.issues))
            .where(TaskRow.project_id == project_id)
            .order_by(ReviewRow.created_at.desc(), ReviewRow.id)
            .limit(limit)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_runs(self, run_ids: Sequence[UUID]) -> list[Review]:
        """Reviews of several runs at once, oldest first.

        Phase L's project history is assembled from a set of runs, and a query
        per run is a round trip per task for an answer the database can give
        in one.
        """
        if not run_ids:
            return []
        rows = self.session.scalars(
            select(ReviewRow)
            .options(selectinload(ReviewRow.issues))
            .where(ReviewRow.task_run_id.in_(tuple(run_ids)))
            .order_by(ReviewRow.created_at, ReviewRow.cycle)
        ).all()
        return [self._to_domain(row) for row in rows]

    def mark_issue_resolved(self, issue_id: UUID, resolved: bool = True) -> None:
        row = self.session.get(ReviewIssueRow, issue_id)
        if row is None:
            raise LookupError(f"Review issue {issue_id} not found")
        row.resolved = resolved
        self.session.flush()
