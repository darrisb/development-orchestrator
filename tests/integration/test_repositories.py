"""Persistence round-trips (Phase A exit condition)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import (
    IssueCategory,
    IssueSeverity,
    ModelRole,
    ProjectStatus,
    ReviewDecision,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.errors import InvalidStateTransition
from apps.orchestrator.domain.models import (
    Lesson,
    Model,
    Project,
    Review,
    ReviewIssue,
    RunEvent,
    Task,
    TaskLimits,
    TaskRun,
)
from apps.orchestrator.repositories import (
    LessonRepository,
    ModelRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            external_project_id="tracestack",
            repository_path="/workspace/tracestack",
            protected_paths=[".git/**", ".env"],
        )
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    return TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="TS-001",
            title="Scaffold extension",
            section=1,
            depends_on=[],
            verify_commands=["npm run compile", "npm test"],
            limits=TaskLimits(max_attempts=2, max_diff_lines=500),
        )
    )


def test_project_round_trips_with_json_columns(session: Session, project: Project):
    fetched = ProjectRepository(session).get(project.id)
    assert fetched is not None
    assert fetched.protected_paths == [".git/**", ".env"]
    assert fetched.status is ProjectStatus.REGISTERED
    assert fetched.created_at is not None


def test_project_lookup_by_external_id(session: Session, project: Project):
    assert ProjectRepository(session).get_by_external_id("tracestack").id == project.id


def test_task_round_trips_with_limits(session: Session, task: Task):
    fetched = TaskRepository(session).get(task.id)
    assert fetched is not None
    assert fetched.limits.max_attempts == 2
    assert fetched.limits.max_diff_lines == 500
    # Unspecified limits fall back to the documented defaults.
    assert fetched.limits.max_review_cycles == 3
    assert fetched.verify_commands == ["npm run compile", "npm test"]


def test_repository_enforces_the_state_machine(session: Session, task: Task):
    repo = TaskRepository(session)
    assert repo.transition(task.id, TaskStatus.READY).status is TaskStatus.READY
    with pytest.raises(InvalidStateTransition):
        repo.transition(task.id, TaskStatus.COMPLETE)
    # The rejected move left the row untouched.
    assert repo.get(task.id).status is TaskStatus.READY


def test_run_numbers_increment_per_task(session: Session, task: Task):
    repo = TaskRunRepository(session)
    assert repo.next_run_number(task.id) == 1
    repo.add(TaskRun(task_id=task.id, run_number=1, starting_commit="abc123"))
    assert repo.next_run_number(task.id) == 2


def test_incomplete_runs_are_discoverable_after_restart(session: Session, task: Task):
    """Section 28: PostgreSQL identifies runs that were in flight."""
    repo = TaskRunRepository(session)
    running = repo.add(TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING))
    repo.add(TaskRun(task_id=task.id, run_number=2, status=RunStatus.SUCCEEDED))
    incomplete = repo.list_incomplete()
    assert [run.id for run in incomplete] == [running.id]


def test_finishing_a_run_records_the_failure_reason(session: Session, task: Task):
    repo = TaskRunRepository(session)
    run = repo.add(TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING))
    finished = repo.finish(run.id, RunStatus.FAILED, failure_reason="TEST_FAILED")
    assert finished.status is RunStatus.FAILED
    assert finished.failure_reason == "TEST_FAILED"
    assert finished.completed_at is not None


def test_update_fields_rejects_unknown_columns(session: Session, task: Task):
    repo = TaskRunRepository(session)
    run = repo.add(TaskRun(task_id=task.id, run_number=1))
    repo.update_fields(run.id, candidate_commit="def456")
    assert repo.get(run.id).candidate_commit == "def456"
    with pytest.raises(AttributeError):
        repo.update_fields(run.id, not_a_column="x")


def test_review_persists_with_its_issues(session: Session, task: Task):
    run = TaskRunRepository(session).add(TaskRun(task_id=task.id, run_number=1))
    review = ReviewRepository(session).add(
        Review(
            task_run_id=run.id,
            reviewer_provider="mock",
            reviewer_model="mock-1",
            decision=ReviewDecision.CHANGES_REQUESTED,
            confidence=0.93,
            summary="Misses selection restoration.",
            issues=[
                ReviewIssue(
                    severity=IssueSeverity.HIGH,
                    category=IssueCategory.REQUIREMENT,
                    file="src/providers/navigation-tree-provider.ts",
                    line=84,
                    requirement_id="TS-004-R7",
                    problem="Saved selection is not restored.",
                    required_fix="Restore the stored selection.",
                ),
                ReviewIssue(
                    severity=IssueSeverity.LOW,
                    category=IssueCategory.STYLE,
                    problem="Naming nit.",
                    required_fix="Rename.",
                ),
            ],
        )
    )
    fetched = ReviewRepository(session).get(review.id)
    assert len(fetched.issues) == 2
    assert len(fetched.blocking_issues) == 1
    assert fetched.confidence == pytest.approx(0.93)


def test_resolving_an_issue_clears_it_from_blocking(session: Session, task: Task):
    run = TaskRunRepository(session).add(TaskRun(task_id=task.id, run_number=1))
    repo = ReviewRepository(session)
    review = repo.add(
        Review(
            task_run_id=run.id,
            reviewer_provider="mock",
            reviewer_model="mock-1",
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="s",
            issues=[
                ReviewIssue(
                    severity=IssueSeverity.HIGH,
                    category=IssueCategory.CORRECTNESS,
                    problem="p",
                    required_fix="f",
                )
            ],
        )
    )
    repo.mark_issue_resolved(review.issues[0].id)
    assert repo.get(review.id).blocking_issues == []


def test_run_events_come_back_in_order(session: Session, project: Project, task: Task):
    run = TaskRunRepository(session).add(TaskRun(task_id=task.id, run_number=1))
    repo = RunEventRepository(session)
    for event_type in (
        RunEventType.TASK_SELECTED,
        RunEventType.WORKSPACE_CREATED,
        RunEventType.CONTEXT_BUILT,
    ):
        repo.append(
            RunEvent(
                task_run_id=run.id,
                project_id=project.id,
                task_id=task.id,
                event_type=event_type,
                attempt=1,
                payload={"detail": event_type.value},
            )
        )
    events = repo.list_for_run(run.id)
    assert [event.event_type for event in events] == [
        RunEventType.TASK_SELECTED,
        RunEventType.WORKSPACE_CREATED,
        RunEventType.CONTEXT_BUILT,
    ]
    assert events[0].payload == {"detail": "TASK_SELECTED"}
    # Sequence is assigned by the repository, independent of timestamp resolution.
    assert [event.sequence for event in events] == [1, 2, 3]


def test_model_metadata_round_trips(session: Session):
    repo = ModelRepository(session)
    repo.add(
        Model(
            provider="llama.cpp",
            model_name="qwen-coder-14b",
            role=ModelRole.CODER,
            endpoint="http://192.168.0.126/v1",
            context_window=32768,
            metadata={"quant": "Q5_K_M"},
        )
    )
    repo.add(
        Model(
            provider="llama.cpp",
            model_name="retired-model",
            role=ModelRole.CODER,
            endpoint="http://192.168.0.126/v1",
            enabled=False,
        )
    )
    coders = repo.list(role=ModelRole.CODER, enabled_only=True)
    assert [model.model_name for model in coders] == ["qwen-coder-14b"]
    assert coders[0].metadata == {"quant": "Q5_K_M"}


def test_lesson_search_never_leaks_across_projects(session: Session, project: Project):
    repo = LessonRepository(session)
    other_project = ProjectRepository(session).add(
        Project(name="Other", repository_path="/workspace/other")
    )
    own = repo.add(
        Lesson(
            category="lifecycle",
            title="Own lesson",
            lesson="...",
            project_id=project.id,
            language="typescript",
        )
    )
    foreign = repo.add(
        Lesson(
            category="lifecycle",
            title="Foreign lesson",
            lesson="...",
            project_id=other_project.id,
            language="typescript",
        )
    )
    glob = repo.add(
        Lesson(category="lifecycle", title="Global lesson", lesson="...", language="typescript")
    )
    # Search only ever returns approved lessons, so approval is part of setting
    # the fixture up rather than something the query is being asked about.
    for lesson in (own, foreign, glob):
        repo.approve(lesson.id)

    titles = {lesson.title for lesson in repo.search(project_id=project.id, language="typescript")}
    assert titles == {"Own lesson", "Global lesson"}


def test_lesson_retrieval_counter_increments(session: Session):
    repo = LessonRepository(session)
    lesson = repo.add(Lesson(category="c", title="t", lesson="l"))
    repo.record_retrieval([lesson.id])
    repo.record_retrieval([lesson.id])
    assert repo.get(lesson.id).times_retrieved == 2
