"""Lesson persistence (build.md sections 32 and 33).

The translation layer, plus the two queries section 32's rules need from the
database rather than from memory: finding an existing lesson for a finding that
has now recurred, and listing what a person is being asked to approve.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select

from ..db.models import LessonRow
from ..domain.enums import LessonStatus
from ..domain.lessons import lesson_confidence
from ..domain.models import Lesson
from .base import Repository


class LessonRepository(Repository[LessonRow, Lesson]):
    row_type = LessonRow

    def _to_domain(self, row: LessonRow) -> Lesson:
        return Lesson(
            id=row.id,
            project_id=row.project_id,
            language=row.language,
            framework=row.framework,
            category=row.category,
            title=row.title,
            lesson=row.lesson,
            tags=list(row.tags or []),
            source_review_issue_id=row.source_review_issue_id,
            source_task_id=row.source_task_id,
            requirement_id=row.requirement_id,
            source_file=row.source_file,
            source_run_id=row.source_run_id,
            confidence=row.confidence,
            occurrences=row.occurrences,
            status=row.status,
            approved_by=row.approved_by,
            approved_at=row.approved_at,
            rejection_reason=row.rejection_reason,
            times_retrieved=row.times_retrieved,
            times_applied=row.times_applied,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def add(self, lesson: Lesson) -> Lesson:
        row = LessonRow(
            id=lesson.id,
            project_id=lesson.project_id,
            language=lesson.language,
            framework=lesson.framework,
            category=lesson.category,
            title=lesson.title,
            lesson=lesson.lesson,
            tags=list(lesson.tags),
            source_review_issue_id=lesson.source_review_issue_id,
            source_task_id=lesson.source_task_id,
            requirement_id=lesson.requirement_id,
            source_file=lesson.source_file,
            source_run_id=lesson.source_run_id,
            last_seen_run_id=lesson.source_run_id,
            confidence=lesson.confidence,
            occurrences=lesson.occurrences,
            status=lesson.status,
            approved_by=lesson.approved_by,
            approved_at=lesson.approved_at,
            rejection_reason=lesson.rejection_reason,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, lesson_id: UUID) -> Lesson | None:
        row = self._get_row(lesson_id)
        return self._to_domain(row) if row else None

    def search(
        self,
        project_id: UUID | None = None,
        language: str | None = None,
        framework: str | None = None,
        category: str | None = None,
        limit: int = 10,
        status: LessonStatus = LessonStatus.APPROVED,
        include_global: bool = True,
    ) -> list[Lesson]:
        """Metadata retrieval (build.md section 33).

        Project-scoped lessons are returned alongside global ones, but a
        project's lessons never leak into another project -- which is the
        database half of section 32 rule 4.

        ``status`` defaults to ``APPROVED`` and that default is the enforcement:
        a proposed or rejected lesson is invisible to every caller that does not
        name a status explicitly, so adding a retrieval site cannot accidentally
        put an unapproved lesson in a prompt.

        ``include_global=False`` is what a per-project view wants. The default
        keeps the two together because a global lesson is meant to apply
        everywhere, and dropping it because someone asked for one project's
        lessons would make the global set useless.
        """
        stmt = select(LessonRow).where(LessonRow.status == status)
        if project_id is not None:
            if include_global:
                stmt = stmt.where(
                    or_(LessonRow.project_id == project_id, LessonRow.project_id.is_(None))
                )
            else:
                stmt = stmt.where(LessonRow.project_id == project_id)
        else:
            stmt = stmt.where(LessonRow.project_id.is_(None))
        if language is not None:
            stmt = stmt.where(LessonRow.language == language)
        if framework is not None:
            stmt = stmt.where(LessonRow.framework == framework)
        if category is not None:
            stmt = stmt.where(LessonRow.category == category)
        rows = self.session.scalars(
            stmt.order_by(
                LessonRow.occurrences.desc(),
                LessonRow.times_applied.desc(),
                LessonRow.id,
            ).limit(limit)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_by_status(
        self,
        status: LessonStatus,
        *,
        project_id: UUID | None = None,
        limit: int = 50,
    ) -> list[Lesson]:
        """The approval queue: what is waiting on a person, newest first."""
        stmt = select(LessonRow).where(LessonRow.status == status)
        if project_id is not None:
            stmt = stmt.where(LessonRow.project_id == project_id)
        rows = self.session.scalars(
            stmt.order_by(LessonRow.created_at.desc(), LessonRow.id).limit(limit)
        ).all()
        return [self._to_domain(row) for row in rows]

    def find_same_finding(
        self,
        *,
        project_id: UUID | None,
        category: str,
        file: str | None,
        requirement_id: str | None,
    ) -> Lesson | None:
        """The lesson that already covers this finding, if there is one.

        Identity is the same triple ``domain.review.issue_fingerprint`` uses to
        decide a finding was re-raised: requirement, file, category. Reusing it
        is the point -- if the two disagreed, a finding that recurred would
        produce a second lesson instead of a second occurrence, and rule 2
        ("prefer recurring") would have nothing to count.

        Matched across every status, deliberately: a *rejected* lesson matching
        a recurring finding is how "this was considered and declined" reaches
        the person being asked, instead of the same proposal reappearing every
        cycle.
        """
        clauses = [LessonRow.category == category]
        if requirement_id:
            clauses.append(LessonRow.requirement_id == requirement_id)
        if file:
            clauses.append(LessonRow.source_file == file)
        if project_id is None:
            clauses.append(LessonRow.project_id.is_(None))
        else:
            clauses.append(
                or_(LessonRow.project_id == project_id, LessonRow.project_id.is_(None))
            )
        row = self.session.scalar(
            select(LessonRow).where(*clauses).order_by(LessonRow.occurrences.desc())
        )
        return self._to_domain(row) if row else None

    def record_occurrence(
        self, lesson_id: UUID, *, source_run_id: UUID | None = None
    ) -> Lesson:
        """Record that another run raised the same finding.

        A run that has already been counted does not count again: one review
        cycle filing the same requirement twice, a fix loop that revisits a
        finding, or the same run being proposed from twice, would otherwise
        inflate the count -- and with it the confidence, which is what makes a
        lesson rank above its neighbours.

        Confidence is re-derived from the new count rather than left as it was,
        because recurrence is the only evidence rule 2 has: a count that rose
        without the confidence following it would make the field decorative.
        """
        row = self._get_row(lesson_id)
        if row is None:
            raise LookupError(f"Lesson {lesson_id} not found")
        if source_run_id is not None and row.last_seen_run_id == source_run_id:
            return self._to_domain(row)
        row.occurrences += 1
        if source_run_id is not None:
            row.last_seen_run_id = source_run_id
        row.confidence = lesson_confidence(row.occurrences)
        row.updated_at = datetime.now(UTC)
        self.session.flush()
        return self._to_domain(row)

    def approve(
        self, lesson_id: UUID, *, approved_by: str | None = None
    ) -> Lesson:
        """Promote a candidate so a coder may be shown it (section 32).

        Approval does not change the project scope. Section 32 rule 4 is
        structural: a lesson is stored with a project or with none, and nothing
        here -- or anywhere else in the codebase -- moves one between them.
        """
        return self._set_status(
            lesson_id,
            LessonStatus.APPROVED,
            approved_by=approved_by,
            rejection_reason=None,
        )

    def reject(self, lesson_id: UUID, *, reason: str | None = None) -> Lesson:
        return self._set_status(
            lesson_id, LessonStatus.REJECTED, approved_by=None, rejection_reason=reason
        )

    def retire(self, lesson_id: UUID, *, reason: str | None = None) -> Lesson:
        return self._set_status(
            lesson_id, LessonStatus.RETIRED, approved_by=None, rejection_reason=reason
        )

    def _set_status(
        self,
        lesson_id: UUID,
        status: LessonStatus,
        *,
        approved_by: str | None,
        rejection_reason: str | None,
    ) -> Lesson:
        row = self._get_row(lesson_id)
        if row is None:
            raise LookupError(f"Lesson {lesson_id} not found")
        row.status = status
        if status is LessonStatus.APPROVED:
            row.approved_by = approved_by
            row.approved_at = datetime.now(UTC)
            row.rejection_reason = None
        elif status in {LessonStatus.REJECTED, LessonStatus.RETIRED}:
            row.approved_by = None
            row.approved_at = None
            row.rejection_reason = rejection_reason
        else:
            # Back to proposed: the approval a person gave is withdrawn with
            # the status, rather than left behind describing a lesson nobody
            # has approved.
            row.approved_by = None
            row.approved_at = None
            row.rejection_reason = None
        self.session.flush()
        return self._to_domain(row)

    def record_retrieval(self, lesson_ids: list[UUID]) -> None:
        for lesson_id in lesson_ids:
            row = self._get_row(lesson_id)
            if row is not None:
                row.times_retrieved += 1
        self.session.flush()

    def record_applied(self, lesson_ids: list[UUID]) -> None:
        """Count a lesson as applied by a run that was accepted after it.

        This is the half of section 32 rule 6 that retrieval cannot supply.
        ``times_retrieved`` rising while ``times_applied`` stays at zero is the
        signal that guidance is being sent and ignored, and the only honest way
        to know is to compare it against what the run went on to do.
        """
        for lesson_id in lesson_ids:
            row = self._get_row(lesson_id)
            if row is not None:
                row.times_applied += 1
        self.session.flush()


__all__ = ["LessonRepository"]
