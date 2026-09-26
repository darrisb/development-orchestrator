"""The Lessons System: extraction, approval and retrieval (build.md sections 32-33).

The pure decisions live in ``domain.lessons``; this module is the part that has
a database. It is deliberately thin for that reason -- it knows how to ask the
repository what already exists and what to do with an answer, and nothing about
what a lesson should say.

Three entry points, matching the three things a node or a person can do:

* :func:`propose_lessons_for_run` -- after a review/fix cycle has been verified.
* :func:`list_approval_queue`, :func:`approve_lesson`, :func:`reject_lesson` --
  the human gate.
* :func:`retrieve_ranked_lessons` and :func:`lesson_prompt_block` -- what the
  context builder puts in front of a coder.

The one thing worth stating up front is the ordering in
:func:`propose_lessons_for_run`: a finding that has been raised before
increments the existing lesson's ``occurrences`` *instead of* proposing a
duplicate, and the search crosses every status including ``REJECTED``. A lesson
this project already declined should be re-raised as a decline, not silently
re-proposed in a form that looks new.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import LessonStatus, RunEventType
from ..domain.lessons import (
    DEFAULT_RETRIEVAL_LIMIT,
    LessonCandidate,
    RetrievedLesson,
    check_approval,
    extract_candidates,
    rank_lessons,
    retrieval_prompt_lines,
)
from ..domain.models import Lesson, ReviewIssue, RunEvent, Task
from ..repositories import (
    LessonRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from .errors import EntityConflict, EntityNotFound

logger = get_logger(__name__)

#: How many approved lessons a broad query pulls before ranking. Ranking scores
#: every candidate it is given, so the pool has to be wider than the answer --
#: applying the limit before the comparison that decides it would mean the limit
#: chose the lessons rather than the ranking.
_CANDIDATE_POOL = 50


# ------------------------------------------------------------------ extraction


def propose_lessons_for_run(session: Session, task_run_id: UUID) -> list[Lesson]:
    """Turn a run's resolved review findings into lessons, or into occurrences.

    Only findings a later reviewer did *not* re-raise are considered. That is
    the specification's "verified cycle", and it is the whole difference between
    a lesson and a guess: a finding the coder has not been told is fixed is not
    evidence of anything.

    Returns the lessons created. A run that passed first time has no fix cycle
    and returns nothing, which is the common case and is correct.

    Raises:
        EntityNotFound: no such run or task.
    """
    run = TaskRunRepository(session).get(task_run_id)
    if run is None:
        raise EntityNotFound("Run", task_run_id)
    task = TaskRepository(session).get(run.task_id)
    if task is None:
        raise EntityNotFound("Task", run.task_id)

    resolved = _resolved_issues(session, run.id)
    if not resolved:
        logger.info(
            "lesson_extraction_skipped",
            run_id=str(run.id),
            task=task.external_task_id,
            reason="no resolved review issues; a run with no fix cycle teaches nothing",
        )
        return []

    lessons = LessonRepository(session)
    created: list[Lesson] = []
    for candidate in extract_candidates(
        resolved,
        source_task_id=str(task.id),
        source_run_id=str(run.id),
    ):
        primary = _primary_issue(resolved, candidate)
        existing = lessons.find_same_finding(
            project_id=task.project_id,
            category=candidate.category,
            file=primary.file if primary else None,
            requirement_id=primary.requirement_id if primary else None,
        )
        if existing is not None:
            # Rule 2, taken literally: the evidence from a repeat finding is
            # accumulated on the lesson that already says it, so the queue does
            # not fill with the same sentence once per cycle.
            updated = lessons.record_occurrence(existing.id, source_run_id=run.id)
            _record_event(
                session,
                RunEventType.LESSON_PROPOSED,
                {
                    "lesson_id": str(updated.id),
                    "outcome": "occurrence",
                    "occurrences": updated.occurrences,
                    "confidence": updated.confidence.value,
                },
                task_run_id=run.id,
                project_id=task.project_id,
                task_id=task.id,
            )
            continue
        lesson = _persist(session, task, candidate, primary)
        created.append(lesson)
        _record_event(
            session,
            RunEventType.LESSON_PROPOSED,
            {
                "lesson_id": str(lesson.id),
                "outcome": "proposed",
                "category": lesson.category,
                "title": lesson.title,
                "confidence": lesson.confidence.value,
                "source_issue_ids": list(candidate.source_issue_ids),
            },
            task_run_id=run.id,
            project_id=task.project_id,
            task_id=task.id,
        )
    logger.info(
        "lessons_proposed",
        run_id=str(run.id),
        task=task.external_task_id,
        resolved_issues=len(resolved),
        candidates=len(created),
    )
    return created


def _resolved_issues(session: Session, task_run_id: UUID) -> list[ReviewIssue]:
    """Findings the fix loop marked addressed, oldest cycle first.

    ``resolved`` is the flag the fix loop sets when a cycle's findings have
    been addressed, so it is the system's own record that the cycle completed
    rather than this module inferring success from a run status.
    """
    return [
        issue
        for review in ReviewRepository(session).list_for_run(task_run_id)
        for issue in review.issues
        if issue.resolved
    ]


def _primary_issue(
    issues: list[ReviewIssue], candidate: LessonCandidate
) -> ReviewIssue | None:
    """The finding a candidate is mostly about.

    ``extract_candidates`` groups by category and file, so a group is already
    homogeneous about both. The requirement id is what distinguishes members of
    a group, and the first is as good a representative as any.
    """
    by_id = {str(issue.id): issue for issue in issues}
    for issue_id in candidate.source_issue_ids:
        if issue_id in by_id:
            return by_id[issue_id]
    return None


def _persist(
    session: Session,
    task: Task,
    candidate: LessonCandidate,
    primary: ReviewIssue | None,
) -> Lesson:
    """Store a candidate as ``PROPOSED``, in its project's scope.

    The project comes from the task and is never reassigned afterwards. That is
    section 32 rule 4 enforced structurally: no code path in the codebase sets
    a lesson's ``project_id`` to ``None``, so a project lesson cannot become a
    global one by being approved, promoted, or re-extracted.
    """
    return LessonRepository(session).add(
        Lesson(
            project_id=task.project_id,
            language=candidate.language,
            framework=candidate.framework,
            category=candidate.category,
            title=candidate.title,
            lesson=candidate.lesson,
            tags=list(candidate.tags),
            source_review_issue_id=(
                UUID(candidate.source_issue_ids[0]) if candidate.source_issue_ids else None
            ),
            source_task_id=(
                UUID(candidate.source_task_id) if candidate.source_task_id else None
            ),
            requirement_id=primary.requirement_id if primary else None,
            source_file=primary.file if primary else None,
            source_run_id=UUID(candidate.source_run_id) if candidate.source_run_id else None,
            confidence=candidate.confidence,
            occurrences=candidate.occurrences,
            status=LessonStatus.PROPOSED,
        )
    )


# -------------------------------------------------------------------- approval


def list_approval_queue(
    session: Session,
    project_id: UUID | None = None,
    *,
    status: LessonStatus = LessonStatus.PROPOSED,
    limit: int = 50,
) -> list[Lesson]:
    """What is waiting on a person, newest first.

    ``None`` for ``project_id`` lists the queue across every project, which is
    what an operator wants. It does not make those lessons global: scope is a
    property of a lesson, not of a query.
    """
    return LessonRepository(session).list_by_status(
        status, project_id=project_id, limit=limit
    )


def approve_lesson(
    session: Session, lesson_id: UUID, *, approved_by: str | None = None
) -> Lesson:
    """Promote a proposed lesson so retrieval may return it.

    Refuses a candidate that fails ``check_approval``: an untraceable or empty
    candidate is not made retrievable by somebody clicking approve. The check
    is run against the stored row re-read as a candidate, so what is approved is
    what was reviewed rather than what was proposed and then edited.

    Raises:
        EntityNotFound: no such lesson.
        EntityConflict: the candidate is not approvable as it stands.
    """
    lessons = LessonRepository(session)
    lesson = lessons.get(lesson_id)
    if lesson is None:
        raise EntityNotFound("Lesson", lesson_id)
    check = check_approval(_as_candidate(lesson))
    if not check.approved:
        raise EntityConflict(
            f"Lesson {lesson_id} is not approvable: " + "; ".join(check.reasons)
        )
    approved = lessons.approve(lesson_id, approved_by=approved_by)
    _lesson_event(
        session,
        approved,
        RunEventType.LESSON_APPROVED,
        {
            "lesson_id": str(approved.id),
            "approved_by": approved_by,
            "occurrences": approved.occurrences,
        },
    )
    logger.info(
        "lesson_approved",
        lesson_id=str(approved.id),
        project_id=str(approved.project_id) if approved.project_id else None,
        approved_by=approved_by,
    )
    return approved


def reject_lesson(
    session: Session, lesson_id: UUID, *, reason: str | None = None
) -> Lesson:
    """Decline a lesson. It is kept, not deleted.

    The row is the record that this project considered the finding and decided
    it was not a rule worth teaching, and it is what a later run raising the
    same finding finds -- so the decision reappears instead of the same
    sentence being proposed again. Deleting it would leave nothing behind but
    the repetition.

    Raises:
        EntityNotFound: no such lesson.
    """
    lessons = LessonRepository(session)
    if lessons.get(lesson_id) is None:
        raise EntityNotFound("Lesson", lesson_id)
    rejected = lessons.reject(lesson_id, reason=reason)
    _lesson_event(
        session,
        rejected,
        RunEventType.LESSON_REJECTED,
        {
            "lesson_id": str(rejected.id),
            "reason": reason,
            "occurrences": rejected.occurrences,
        },
    )
    logger.info(
        "lesson_rejected",
        lesson_id=str(rejected.id),
        project_id=str(rejected.project_id) if rejected.project_id else None,
        reason=reason,
    )
    return rejected


def retire_lesson(
    session: Session, lesson_id: UUID, *, reason: str | None = None
) -> Lesson:
    """Withdraw an approved lesson that has turned out to be wrong.

    Retired rather than rejected: it *was* approved and in use, so its retrieval
    and application counts are evidence about guidance that was being followed,
    and calling it rejected would misdescribe that history.

    Raises:
        EntityNotFound: no such lesson.
    """
    lessons = LessonRepository(session)
    if lessons.get(lesson_id) is None:
        raise EntityNotFound("Lesson", lesson_id)
    return lessons.retire(lesson_id, reason=reason)


def _as_candidate(lesson: Lesson) -> LessonCandidate:
    """A stored lesson rendered back as the candidate it was.

    Approval is checked on a candidate, so the candidate is re-derived from the
    row rather than the check being re-implemented against the row. The source
    issue list is rebuilt from the single issue the lesson cites, which is the
    list extraction produced for a single-finding candidate.
    """
    issue_ids: tuple[str, ...] = ()
    if lesson.source_review_issue_id is not None:
        issue_ids = (str(lesson.source_review_issue_id),)
    return LessonCandidate(
        title=lesson.title,
        lesson=lesson.lesson,
        category=lesson.category,
        language=lesson.language,
        framework=lesson.framework,
        tags=tuple(lesson.tags),
        source_issue_ids=issue_ids,
        source_task_id=str(lesson.source_task_id) if lesson.source_task_id else None,
        source_run_id=str(lesson.source_run_id) if lesson.source_run_id else None,
        occurrences=lesson.occurrences,
        confidence=lesson.confidence,
    )


# ------------------------------------------------------------------- retrieval


def retrieve_ranked_lessons(
    session: Session,
    project_id: UUID,
    *,
    keywords: tuple[str, ...] | list[str] = (),
    language: str | None = None,
    framework: str | None = None,
    category: str | None = None,
    limit: int = DEFAULT_RETRIEVAL_LIMIT,
) -> tuple[RetrievedLesson, ...]:
    """The few approved lessons worth showing a coder, most relevant first.

    Only ``APPROVED``, and the filter is in the query rather than in the ranking
    so a proposed lesson cannot be reached by raising its score. The
    language/framework/category arguments are ranking inputs and not SQL
    predicates: a lesson with no language recorded applies to every language,
    and filtering on the column would drop exactly the general lessons that are
    most worth showing.

    Project and global lessons are ranked together, and the retrieval counters
    are incremented here. Section 32 rule 6 asks for retrieval to be tracked; a
    counter only a test writes to is not tracked.

    Raises:
        EntityNotFound: no such project.
    """
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    lessons = LessonRepository(session)
    ranked = rank_lessons(
        lessons.search(project_id=project_id, limit=_CANDIDATE_POOL),
        keywords=keywords,
        language=language,
        framework=framework,
        category=category,
        limit=limit,
    )
    if not ranked:
        return ()
    lessons.record_retrieval([entry.lesson.id for entry in ranked])
    for entry in ranked:
        logger.debug(
            "lesson_retrieved",
            lesson_id=str(entry.lesson.id),
            project_id=str(project_id),
            score=entry.score,
            reasons=list(entry.reasons),
        )
    return ranked


def retrieve_lessons(
    session: Session,
    project_id: UUID,
    *,
    keywords: tuple[str, ...] | list[str] = (),
    language: str | None = None,
    framework: str | None = None,
    category: str | None = None,
    limit: int = DEFAULT_RETRIEVAL_LIMIT,
) -> list[Lesson]:
    """The same retrieval, without the reasons. For callers that only render text."""
    return [
        entry.lesson
        for entry in retrieve_ranked_lessons(
            session,
            project_id,
            keywords=keywords,
            language=language,
            framework=framework,
            category=category,
            limit=limit,
        )
    ]


def lesson_prompt_block(
    session: Session,
    project_id: UUID,
    *,
    keywords: tuple[str, ...] | list[str] = (),
    language: str | None = None,
    framework: str | None = None,
    limit: int = DEFAULT_RETRIEVAL_LIMIT,
) -> list[str]:
    """Retrieval rendered as the prompt lines the context builder appends.

    Empty when nothing matched, which is the normal case for a project with no
    approved lessons yet. An empty block is better than a placeholder: the
    coder is told the project has nothing approved to say, rather than being
    handed advice nobody vetted.
    """
    ranked = retrieve_ranked_lessons(
        session,
        project_id,
        keywords=keywords,
        language=language,
        framework=framework,
        limit=limit,
    )
    return retrieval_prompt_lines(ranked)


def record_lessons_applied(session: Session, lesson_ids: list[UUID]) -> None:
    """Mark lessons whose application was explicitly validated.

    Acceptance after retrieval is deliberately not enough: callers must have
    compared the lesson with the resulting change. Delivery never calls this
    function automatically, preventing the metric from incrementing itself.
    """
    if lesson_ids:
        LessonRepository(session).record_applied(lesson_ids)


# ---------------------------------------------------------------------- events


def _record_event(
    session: Session,
    event_type: RunEventType,
    payload: dict[str, object],
    *,
    task_run_id: UUID | None,
    project_id: UUID | None,
    task_id: UUID | None,
) -> None:
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=task_run_id,
            project_id=project_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        )
    )


def _lesson_event(
    session: Session, lesson: Lesson, event_type: RunEventType, payload: dict[str, object]
) -> None:
    """Log a decision somebody made at a desk rather than in a workflow.

    It is written against the run that raised the finding, because that is the
    run a person debugging this project will be reading. A lesson whose source
    run has since been deleted falls back to a project-level event, which is
    what the nullable ``task_run_id`` on ``run_events`` is for.
    """
    if lesson.source_run_id is not None:
        _record_event(
            session,
            event_type,
            payload,
            task_run_id=lesson.source_run_id,
            project_id=lesson.project_id,
            task_id=lesson.source_task_id,
        )
        return
    if lesson.project_id is None:
        # A global lesson with no surviving run has nowhere to be filed; the
        # decision is still recorded on the row, which is the durable record.
        logger.info(
            "lesson_event_unfilable",
            lesson_id=str(lesson.id),
            event_type=str(event_type),
        )
        return
    _record_event(
        session,
        event_type,
        payload,
        task_run_id=None,
        project_id=lesson.project_id,
        task_id=None,
    )
