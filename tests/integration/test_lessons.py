"""The Lessons System end to end (build.md sections 32 and 33).

Exercises the service against a real database, because the rules that matter
here are enforced by *structure* -- which query runs, which status is read, what
a row can be changed into -- and a mocked repository demonstrates none of them.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import (
    IssueCategory,
    IssueSeverity,
    LessonStatus,
    ReviewDecision,
    RiskLevel,
    RunEventType,
    RunStatus,
)
from apps.orchestrator.domain.models import (
    Lesson,
    Project,
    Review,
    ReviewIssue,
    Task,
    TaskLimits,
    TaskRun,
)
from apps.orchestrator.repositories import (
    LessonRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services import lessons as lesson_service
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound

pytestmark = pytest.mark.integration


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            external_project_id="tracestack",
            repository_path="/workspace/tracestack",
        )
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    return TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="TS-004",
            title="Implement the navigation component",
            instructions="Add src/components/navigation.tsx and its tests.",
            files_to_create=["src/components/navigation.tsx"],
            limits=TaskLimits(max_attempts=3),
        )
    )


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.SUCCEEDED)
    )


@pytest.fixture
def other_project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Other",
            external_project_id="other",
            repository_path="/workspace/other",
        )
    )


def _issue(
    *,
    category: IssueCategory = IssueCategory.TESTING,
    severity: IssueSeverity = IssueSeverity.HIGH,
    file: str = "src/components/navigation.tsx",
    requirement_id: str = "REQ-3",
    resolved: bool = True,
) -> ReviewIssue:
    return ReviewIssue(
        severity=severity,
        category=category,
        problem="The navigation test asserts on component internals.",
        required_fix="Assert on the rendered output of the navigation component.",
        file=file,
        line=42,
        requirement_id=requirement_id,
        resolved=resolved,
    )


def _reviewed(session: Session, run: TaskRun, issues: list[ReviewIssue], cycle: int = 2) -> Review:
    """A review carrying ``issues``, recorded through the repository."""
    return ReviewRepository(session).add(
        Review(
            task_run_id=run.id,
            reviewer_provider="mock",
            reviewer_model="mock-reviewer",
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="The navigation test needs work.",
            risk=RiskLevel.MEDIUM,
            cycle=cycle,
            issues=issues,
        )
    )


def _second_run(session: Session, task: Task) -> TaskRun:
    return TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=2, status=RunStatus.SUCCEEDED)
    )


def _approved_lesson(session: Session, project_id, **overrides) -> Lesson:
    """An approved lesson, with the parts a test does not care about filled in."""
    fields = {
        "project_id": project_id,
        "language": "typescript",
        "category": "testing",
        "title": "Assert on behaviour",
        "lesson": "Assert on rendered output rather than on internals.",
    }
    fields.update(overrides)
    lesson = LessonRepository(session).add(Lesson(**fields))
    return LessonRepository(session).approve(lesson.id)


# --- extraction -------------------------------------------------------------


def test_a_verified_finding_becomes_a_proposed_lesson(
    session: Session, project: Project, run: TaskRun
):
    issue = _issue()
    _reviewed(session, run, [issue])

    proposed = lesson_service.propose_lessons_for_run(session, run.id)

    assert len(proposed) == 1
    assert proposed[0].status is LessonStatus.PROPOSED
    assert proposed[0].project_id == project.id
    assert proposed[0].source_review_issue_id == issue.id
    assert proposed[0].source_run_id == run.id
    assert proposed[0].requirement_id == "REQ-3"
    assert proposed[0].source_file == "src/components/navigation.tsx"


def test_a_proposed_lesson_is_not_retrievable(
    session: Session, project: Project, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    proposed = lesson_service.propose_lessons_for_run(session, run.id)

    assert not proposed[0].is_retrievable
    assert lesson_service.retrieve_lessons(session, project.id) == []


def test_an_unresolved_finding_teaches_nothing(session: Session, run: TaskRun):
    """A finding the coder was never told is fixed is not evidence of anything."""
    _reviewed(session, run, [_issue(resolved=False)])

    assert lesson_service.propose_lessons_for_run(session, run.id) == []


def test_a_run_with_no_reviews_proposes_nothing(session: Session, run: TaskRun):
    assert lesson_service.propose_lessons_for_run(session, run.id) == []


def test_a_style_finding_never_reaches_the_queue(session: Session, run: TaskRun):
    _reviewed(session, run, [_issue(category=IssueCategory.STYLE)])

    assert lesson_service.propose_lessons_for_run(session, run.id) == []


def test_an_info_finding_never_reaches_the_queue(session: Session, run: TaskRun):
    _reviewed(session, run, [_issue(severity=IssueSeverity.INFO)])

    assert lesson_service.propose_lessons_for_run(session, run.id) == []


def test_proposing_writes_an_event(session: Session, run: TaskRun):
    _reviewed(session, run, [_issue()])

    lesson_service.propose_lessons_for_run(session, run.id)

    events = RunEventRepository(session).list_for_run(run.id)
    proposals = [
        event for event in events if event.event_type == RunEventType.LESSON_PROPOSED
    ]
    assert len(proposals) == 1
    assert proposals[0].payload["outcome"] == "proposed"


def test_proposing_from_an_unknown_run_is_a_404(session: Session):
    with pytest.raises(EntityNotFound):
        lesson_service.propose_lessons_for_run(session, uuid4())


# --- recurrence (rule 2) ----------------------------------------------------


def test_the_same_finding_twice_is_an_occurrence_not_a_duplicate(
    session: Session, project: Project, task: Task, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    first = lesson_service.propose_lessons_for_run(session, run.id)

    second_run = _second_run(session, task)
    _reviewed(session, second_run, [_issue()])
    second = lesson_service.propose_lessons_for_run(session, second_run.id)

    assert len(first) == 1
    assert second == []
    # `search` defaults to approved, so a repeat finding has not quietly become
    # a second retrievable lesson.
    assert LessonRepository(session).search(project_id=project.id, limit=10) == []
    assert len(
        LessonRepository(session).search(
            project_id=project.id, limit=10, status=LessonStatus.PROPOSED
        )
    ) == 1


def test_recurrence_raises_the_occurrence_count_and_the_confidence(
    session: Session, task: Task, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_service.propose_lessons_for_run(session, run.id)

    second_run = _second_run(session, task)
    _reviewed(session, second_run, [_issue()])
    lesson_service.propose_lessons_for_run(session, second_run.id)

    stored = LessonRepository(session).get(lesson_service.list_approval_queue(session)[0].id)
    assert stored.occurrences == 2
    assert stored.confidence.value == "medium"


def test_a_repeat_occurrence_is_recorded_as_an_event(
    session: Session, task: Task, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_service.propose_lessons_for_run(session, run.id)

    second_run = _second_run(session, task)
    _reviewed(session, second_run, [_issue()])
    lesson_service.propose_lessons_for_run(session, second_run.id)

    events = RunEventRepository(session).list_for_run(second_run.id)
    proposals = [
        event for event in events if event.event_type == RunEventType.LESSON_PROPOSED
    ]
    assert proposals[0].payload["outcome"] == "occurrence"
    assert proposals[0].payload["occurrences"] == 2


def test_the_same_finding_twice_in_one_run_is_one_occurrence(
    session: Session, run: TaskRun
):
    """One cycle filing the same requirement twice is not a second sighting.

    The count drives confidence and confidence drives ranking, so a
    double-counted finding would promote a lesson on the strength of one review.
    """
    _reviewed(session, run, [_issue(), _issue()])

    lesson_service.propose_lessons_for_run(session, run.id)

    stored = LessonRepository(session).get(lesson_service.list_approval_queue(session)[0].id)
    assert stored.occurrences == 1


def test_two_different_findings_are_two_lessons(
    session: Session, project: Project, run: TaskRun
):
    _reviewed(
        session,
        run,
        [_issue(), _issue(category=IssueCategory.SECURITY, requirement_id="REQ-4")],
    )

    proposed = lesson_service.propose_lessons_for_run(session, run.id)

    assert len(proposed) == 2


def test_a_rejected_lesson_is_found_again_rather_than_reproposed(
    session: Session, task: Task, run: TaskRun
):
    """The decision to decline has to stick, or it is re-made every cycle."""
    _reviewed(session, run, [_issue()])
    proposed = lesson_service.propose_lessons_for_run(session, run.id)
    lesson_service.reject_lesson(session, proposed[0].id, reason="too specific")

    second_run = _second_run(session, task)
    _reviewed(session, second_run, [_issue()])

    assert lesson_service.propose_lessons_for_run(session, second_run.id) == []
    assert lesson_service.list_approval_queue(session) == []


# --- approval (rules 3 and 4) -----------------------------------------------


def test_approving_makes_a_lesson_retrievable(
    session: Session, project: Project, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_id = lesson_service.propose_lessons_for_run(session, run.id)[0].id

    approved = lesson_service.approve_lesson(session, lesson_id, approved_by="darri")

    assert approved.is_retrievable
    assert approved.approved_by == "darri"
    assert approved.approved_at is not None
    assert [lesson.id for lesson in lesson_service.retrieve_lessons(session, project.id)] == [
        lesson_id
    ]


def test_approving_writes_an_event_against_the_run_that_raised_it(
    session: Session, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_id = lesson_service.propose_lessons_for_run(session, run.id)[0].id

    lesson_service.approve_lesson(session, lesson_id, approved_by="darri")

    events = RunEventRepository(session).list_for_run(run.id)
    assert any(event.event_type == RunEventType.LESSON_APPROVED for event in events)


def test_an_untraceable_lesson_cannot_be_approved(session: Session):
    """Rule 3 as an enforcement rather than a convention."""
    lesson = LessonRepository(session).add(
        Lesson(
            project_id=None,
            category="testing",
            title="Something",
            lesson="Do the thing that someone said to do.",
        )
    )

    with pytest.raises(EntityConflict, match="cannot be traced"):
        lesson_service.approve_lesson(session, lesson.id)


def test_approving_a_missing_lesson_is_a_404(session: Session):
    with pytest.raises(EntityNotFound):
        lesson_service.approve_lesson(session, uuid4())


def test_approving_never_changes_a_lessons_project(
    session: Session, project: Project, run: TaskRun
):
    """Rule 4: a project lesson does not become global by being approved."""
    _reviewed(session, run, [_issue()])
    lesson_id = lesson_service.propose_lessons_for_run(session, run.id)[0].id

    approved = lesson_service.approve_lesson(session, lesson_id, approved_by="darri")

    assert approved.project_id == project.id
    assert not approved.is_global


def test_a_project_lesson_is_not_retrieved_for_another_project(
    session: Session, project: Project, other_project: Project, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_id = lesson_service.propose_lessons_for_run(session, run.id)[0].id
    lesson_service.approve_lesson(session, lesson_id, approved_by="darri")

    assert lesson_service.retrieve_lessons(session, other_project.id) == []


def test_a_global_lesson_is_retrieved_for_every_project(
    session: Session, project: Project, other_project: Project
):
    """A global lesson that applied only to its own project would apply nowhere."""
    global_lesson = _approved_lesson(session, None)

    assert global_lesson.id in {
        found.id for found in lesson_service.retrieve_lessons(session, project.id)
    }
    assert global_lesson.id in {
        found.id for found in lesson_service.retrieve_lessons(session, other_project.id)
    }


def test_rejecting_keeps_the_lesson_with_its_reason(session: Session):
    lesson = LessonRepository(session).add(
        Lesson(
            project_id=None,
            category="testing",
            title="A title",
            lesson="A long enough instruction to be a real lesson.",
        )
    )

    rejected = lesson_service.reject_lesson(session, lesson.id, reason="too specific")

    assert rejected.status is LessonStatus.REJECTED
    assert rejected.rejection_reason == "too specific"
    assert rejected.approved_at is None


def test_rejecting_writes_an_event(session: Session, run: TaskRun):
    _reviewed(session, run, [_issue()])
    lesson_id = lesson_service.propose_lessons_for_run(session, run.id)[0].id

    lesson_service.reject_lesson(session, lesson_id, reason="not general")

    events = RunEventRepository(session).list_for_run(run.id)
    assert any(event.event_type == RunEventType.LESSON_REJECTED for event in events)


def test_retiring_an_approved_lesson_takes_it_out_of_retrieval(
    session: Session, project: Project
):
    lesson = _approved_lesson(session, project.id)
    lesson_service.record_lessons_applied(session, [lesson.id])

    lesson_service.retire_lesson(session, lesson.id, reason="no longer true here")

    stored = LessonRepository(session).get(lesson.id)
    assert stored.status is LessonStatus.RETIRED
    # Retired rather than rejected: it *was* approved and in use, so its counts
    # are evidence about guidance that was being followed.
    assert stored.times_applied == 1
    assert lesson_service.retrieve_lessons(session, project.id) == []


# --- retrieval and usefulness (rules 5 and 6) -------------------------------


def test_retrieval_counts_each_time_a_lesson_is_shown(
    session: Session, project: Project
):
    lesson = _approved_lesson(session, project.id)

    lesson_service.retrieve_lessons(session, project.id)
    lesson_service.retrieve_lessons(session, project.id)

    assert LessonRepository(session).get(lesson.id).times_retrieved == 2


def test_retrieval_counts_nothing_when_nothing_was_retrieved(
    session: Session, project: Project, run: TaskRun
):
    """A counter only a test writes to is not a counter."""
    _reviewed(session, run, [_issue()])
    lesson_service.propose_lessons_for_run(session, run.id)

    lesson_service.retrieve_lessons(session, project.id)

    assert lesson_service.list_approval_queue(session)[0].times_retrieved == 0


def test_application_is_counted_separately_from_retrieval(
    session: Session, project: Project
):
    """Rule 6: sent and followed are different facts."""
    lesson = _approved_lesson(session, project.id)
    lesson_service.retrieve_lessons(session, project.id)

    lesson_service.record_lessons_applied(session, [lesson.id])

    stored = LessonRepository(session).get(lesson.id)
    assert stored.times_retrieved == 1
    assert stored.times_applied == 1


def test_the_prompt_block_says_why_each_lesson_was_chosen(
    session: Session, project: Project
):
    _approved_lesson(session, project.id)

    lines = lesson_service.lesson_prompt_block(
        session, project.id, language="typescript"
    )

    assert len(lines) == 1
    assert "Assert on behaviour" in lines[0]
    assert "selected because" in lines[0]
    assert "language typescript" in lines[0]


def test_a_project_with_no_lessons_gets_an_empty_prompt_block(
    session: Session, project: Project
):
    assert lesson_service.lesson_prompt_block(session, project.id) == []


def test_retrieval_is_bounded(session: Session, project: Project):
    for index in range(12):
        _approved_lesson(session, project.id, title=f"Lesson {index}")

    assert len(lesson_service.retrieve_lessons(session, project.id, limit=3)) == 3


def test_retrieval_for_a_project_that_does_not_exist_is_a_404(session: Session):
    with pytest.raises(EntityNotFound):
        lesson_service.retrieve_lessons(session, uuid4())


def test_keywords_from_the_task_change_the_order_retrieval_returns(
    session: Session, project: Project
):
    """A lesson about what the task is touching comes first.

    An approved lesson is never withheld outright -- the project vetted it -- but
    an approved lesson about the subject of today's task has to beat one about
    something else, or the ordering is decoration.
    """
    irrelevant = _approved_lesson(
        session,
        project.id,
        title="Database migrations must be reversible",
        lesson="Every migration must have a tested down path.",
    )
    relevant = _approved_lesson(
        session,
        project.id,
        title="Assert on the navigation output",
        lesson="Assert on the rendered output of the navigation component.",
    )

    ranked = lesson_service.retrieve_ranked_lessons(
        session, project.id, keywords=["navigation", "rendered", "component"]
    )

    assert [entry.lesson.id for entry in ranked][:2] == [relevant.id, irrelevant.id]
    assert "keyword match" in ranked[0].reasons[0]


# --- the queue --------------------------------------------------------------


def test_the_queue_defaults_to_what_is_waiting_on_a_person(
    session: Session, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_id = lesson_service.propose_lessons_for_run(session, run.id)[0].id

    assert [lesson.id for lesson in lesson_service.list_approval_queue(session)] == [
        lesson_id
    ]

    lesson_service.approve_lesson(session, lesson_id, approved_by="darri")
    assert lesson_service.list_approval_queue(session) == []
    assert len(
        lesson_service.list_approval_queue(session, status=LessonStatus.APPROVED)
    ) == 1


def test_the_queue_can_be_read_across_projects(
    session: Session, other_project: Project, run: TaskRun
):
    """A cross-project view is an operator's convenience, not a scope change."""
    _reviewed(session, run, [_issue()])
    lesson_service.propose_lessons_for_run(session, run.id)

    assert len(lesson_service.list_approval_queue(session)) == 1
    assert lesson_service.list_approval_queue(session, project_id=other_project.id) == []


def test_the_queue_can_be_filtered_to_one_project(
    session: Session, project: Project, run: TaskRun
):
    _reviewed(session, run, [_issue()])
    lesson_service.propose_lessons_for_run(session, run.id)

    assert len(lesson_service.list_approval_queue(session, project_id=project.id)) == 1
