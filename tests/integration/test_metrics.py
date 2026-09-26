"""Section 35: what the system can say about itself.

The metrics are the phase's deliverable for a reason -- they are the only place
the lessons and training data are read as a population rather than a row. So the
tests here are about the *edges*: a run that is still in flight must not be
counted as a failure, a lesson that has never been shown must not report an
application rate, and a project's numbers must not include another project's.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import (
    IssueCategory,
    IssueSeverity,
    LessonStatus,
    ModelPurpose,
    ModelRole,
    ReviewDecision,
    RiskLevel,
    RunStatus,
    TrainingStatus,
)
from apps.orchestrator.domain.models import (
    Lesson,
    Project,
    Review,
    ReviewIssue,
    Task,
    TaskRun,
    TrainingExample,
)
from apps.orchestrator.providers import ProviderConfig, TokenUsage
from apps.orchestrator.repositories import (
    LessonRepository,
    ProjectRepository,
    ReviewRepository,
    TaskRepository,
    TaskRunRepository,
    TrainingExampleRepository,
)
from apps.orchestrator.services import metrics
from apps.orchestrator.services.errors import EntityNotFound
from apps.orchestrator.services.metrics import recurring_findings
from apps.orchestrator.services.model_runs import record_model_call

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
def other_project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Other",
            external_project_id="other",
            repository_path="/workspace/other",
        )
    )


def _task(session: Session, project: Project, external_id: str = "TS-001") -> Task:
    return TaskRepository(session).add(
        Task(project_id=project.id, external_task_id=external_id, title="Do the thing")
    )


def _run(session: Session, task: Task, status: RunStatus, run_number: int = 1) -> TaskRun:
    """A settled run, with the attempt number the real path would leave behind."""
    run = TaskRunRepository(session).add(
        TaskRun(
            task_id=task.id,
            run_number=run_number,
            attempt_number=run_number,
            status=RunStatus.RUNNING,
        )
    )
    return TaskRunRepository(session).finish(run.id, status)


def _issue(
    *,
    category: IssueCategory = IssueCategory.TESTING,
    file: str = "src/nav.tsx",
    requirement_id: str = "REQ-1",
) -> ReviewIssue:
    return ReviewIssue(
        severity=IssueSeverity.HIGH,
        category=category,
        problem="A problem.",
        required_fix="A required fix.",
        file=file,
        line=10,
        requirement_id=requirement_id,
        resolved=True,
    )


def _review(
    session: Session, run: TaskRun, issues: list[ReviewIssue], cycle: int = 1
) -> Review:
    return ReviewRepository(session).add(
        Review(
            task_run_id=run.id,
            reviewer_provider="mock",
            reviewer_model="mock-reviewer",
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="Needs work.",
            risk=RiskLevel.MEDIUM,
            cycle=cycle,
            issues=issues,
        )
    )


def _spend(session: Session, run: TaskRun, *, input_tokens: int = 1000) -> None:
    record_model_call(
        session,
        task_run_id=run.id,
        config=ProviderConfig(
            provider_id="env:local-coder",
            base_url="http://192.168.0.126:8080/v1",
            model_name="qwen-coder-14b",
            role=ModelRole.CODER,
        ),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        duration_ms=1000,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=input_tokens // 4),
    )


def _lesson(session: Session, project: Project, **overrides) -> Lesson:
    fields = {
        "project_id": project.id,
        "category": "testing",
        "title": "Assert on behaviour",
        "lesson": "Assert on rendered output rather than on internals.",
    }
    fields.update(overrides)
    return LessonRepository(session).add(Lesson(**fields))


def _example(
    session: Session, project: Project, run: TaskRun, task: Task, **overrides
) -> TrainingExample:
    fields = {
        "task_run_id": run.id,
        "project_id": project.id,
        "task_id": task.id,
        "external_project_id": project.external_project_id,
        "external_task_id": task.external_task_id,
        "external_run_id": f"RUN-{run.run_number}",
        "artifact_path": f"training/RUN-{run.run_number}",
        "manifest_sha256": "a" * 64,
        "outcome": "accepted",
        "status": TrainingStatus.CAPTURED,
    }
    fields.update(overrides)
    return TrainingExampleRepository(session).add(TrainingExample(**fields))


# --- run metrics (section 35) -----------------------------------------------


def test_a_project_with_no_runs_reports_zero_rather_than_failing(
    session: Session, project: Project
):
    runs = metrics.project_metrics(session, project.id)["runs"]

    assert runs["total_runs"] == 0
    assert runs["success_rate"] == 0.0
    assert runs["first_pass_success_rate"] == 0.0


def test_a_run_in_flight_is_not_a_failure(session: Session, project: Project):
    """An unfinished run has not failed yet; counting it as one would make the
    success rate a function of how long the queue happens to be."""
    task = _task(session, project)
    TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
    )

    runs = metrics.project_metrics(session, project.id)["runs"]

    assert runs["total_runs"] == 0
    assert runs["statuses"] == {"RUNNING": 1}


def test_the_success_rate_is_accepted_runs_over_settled_runs(
    session: Session, project: Project
):
    task = _task(session, project)
    _run(session, task, RunStatus.SUCCEEDED, run_number=1)
    _run(session, task, RunStatus.FAILED, run_number=2)
    _run(session, task, RunStatus.SUCCEEDED, run_number=3)

    runs = metrics.project_metrics(session, project.id)["runs"]

    assert runs["total_runs"] == 3
    assert runs["successes"] == 2
    assert runs["failures"] == 1
    assert runs["success_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_an_abandoned_run_is_in_the_denominator(session: Session, project: Project):
    """A run nobody finished is a run the orchestrator did not deliver; leaving
    it out turns the rate into a measure of how often *finished* work passed."""
    task = _task(session, project)
    _run(session, task, RunStatus.SUCCEEDED, run_number=1)
    _run(session, task, RunStatus.ABANDONED, run_number=2)

    runs = metrics.project_metrics(session, project.id)["runs"]

    assert runs["abandoned"] == 1
    assert runs["success_rate"] == pytest.approx(0.5)


def test_first_pass_and_success_are_reported_separately(session: Session, project: Project):
    """A project where most tasks pass on attempt 3 and one where most pass on
    attempt 1 have the same success rate; only the first is producing work that
    was right the first time."""
    task = _task(session, project)
    _run(session, task, RunStatus.SUCCEEDED, run_number=1)
    _run(session, task, RunStatus.FAILED, run_number=2)
    _run(session, task, RunStatus.SUCCEEDED, run_number=3)

    runs = metrics.project_metrics(session, project.id)["runs"]

    assert runs["first_pass_successes"] == 1
    assert runs["first_pass_success_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert runs["retry_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_average_attempts_reflects_the_retries(session: Session, project: Project):
    task = _task(session, project)
    _run(session, task, RunStatus.SUCCEEDED, run_number=1)
    _run(session, task, RunStatus.SUCCEEDED, run_number=2)

    runs = metrics.project_metrics(session, project.id)["runs"]

    assert runs["average_attempts"] == pytest.approx(1.5)


def test_task_metrics_answer_the_same_questions_for_one_task(
    session: Session, project: Project
):
    task = _task(session, project)
    _run(session, task, RunStatus.FAILED, run_number=1)
    _run(session, task, RunStatus.SUCCEEDED, run_number=2)

    reported = metrics.task_metrics(session, TaskRunRepository(session).list_for_task(task.id))

    assert reported.total_runs == 2
    assert reported.successes == 1
    assert reported.success_rate == pytest.approx(0.5)


def test_task_metrics_do_not_include_another_tasks_runs(session: Session, project: Project):
    mine = _task(session, project, "TS-001")
    theirs = _task(session, project, "TS-002")
    _run(session, mine, RunStatus.SUCCEEDED)
    _run(session, theirs, RunStatus.FAILED)

    reported = metrics.task_metrics(session, TaskRunRepository(session).list_for_task(mine.id))

    assert reported.total_runs == 1
    assert reported.successes == 1


# --- review metrics ---------------------------------------------------------


def test_issue_counts_are_grouped_by_severity_and_category(
    session: Session, project: Project
):
    task = _task(session, project)
    run = _run(session, task, RunStatus.FAILED)
    _review(
        session,
        run,
        [
            _issue(requirement_id="REQ-1"),
            _issue(requirement_id="REQ-2"),
            _issue(category=IssueCategory.SECURITY, requirement_id="REQ-3"),
        ],
    )

    reported = metrics.review_metrics(ReviewRepository(session).list_for_project(project.id))

    assert reported["issue_categories"] == {"testing": 2, "security": 1}
    assert reported["issue_severities"] == {"HIGH": 3}
    assert reported["average_issues_per_review"] == 3.0


def test_reviews_of_several_runs_are_aggregated(session: Session, project: Project):
    task = _task(session, project)
    first = _run(session, task, RunStatus.FAILED, run_number=1)
    second = _run(session, task, RunStatus.FAILED, run_number=2)
    _review(session, first, [_issue()], cycle=1)
    _review(session, second, [_issue(requirement_id="REQ-9")], cycle=1)

    reported = metrics.review_metrics(ReviewRepository(session).list_for_project(project.id))

    assert reported["total"] == 2
    assert reported["decisions"] == {"CHANGES_REQUESTED": 2}


def test_review_metrics_with_no_reviews_report_zero(session: Session, project: Project):
    reported = metrics.review_metrics([])

    assert reported["total"] == 0
    assert reported["issue_categories"] == {}
    assert reported["average_issues_per_review"] == 0.0


# --- recurring findings -----------------------------------------------------


def test_the_same_finding_in_two_runs_is_reported_as_recurring(
    session: Session, project: Project
):
    task = _task(session, project)
    first = _run(session, task, RunStatus.FAILED, run_number=1)
    second = _run(session, task, RunStatus.FAILED, run_number=2)
    _review(session, first, [_issue()], cycle=1)
    _review(session, second, [_issue()], cycle=1)

    findings = recurring_findings(ReviewRepository(session).list_for_project(project.id))

    assert len(findings) == 1
    assert findings[0]["count"] == 2
    assert findings[0]["category"] == "testing"
    assert findings[0]["requirement_id"] == "REQ-1"


def test_a_one_off_finding_is_not_called_recurring(session: Session, project: Project):
    """A reviewer raising something once is an opinion; the list is for deciding
    what to fix structurally."""
    task = _task(session, project)
    run = _run(session, task, RunStatus.FAILED)
    _review(session, run, [_issue()])

    assert recurring_findings(ReviewRepository(session).list_for_project(project.id)) == []


def test_a_finding_a_coder_never_fixed_is_reported_as_still_open(
    session: Session, project: Project
):
    """The reason this list exists is to find what keeps coming back; a finding
    that is still open is the more urgent half of that."""
    task = _task(session, project)
    first = _run(session, task, RunStatus.FAILED, run_number=1)
    second = _run(session, task, RunStatus.FAILED, run_number=2)
    _review(session, first, [_issue()], cycle=1)
    _review(session, second, [replace(_issue(), resolved=False)], cycle=1)

    findings = recurring_findings(ReviewRepository(session).list_for_project(project.id))

    assert findings[0]["still_open"] == 1
    assert findings[0]["resolved"] == 1


def test_the_worst_recurring_findings_come_first(session: Session, project: Project):
    task = _task(session, project)
    runs = [_run(session, task, RunStatus.FAILED, run_number=n) for n in (1, 2, 3)]
    for run in runs:
        _review(session, run, [_issue(requirement_id="REQ-1")], cycle=1)
    # Only seen twice, in two of the three runs.
    _review(session, runs[0], [_issue(requirement_id="REQ-9")], cycle=2)
    _review(session, runs[1], [_issue(requirement_id="REQ-9")], cycle=2)

    findings = recurring_findings(ReviewRepository(session).list_for_project(project.id))

    assert [finding["count"] for finding in findings] == [3, 2]


def test_recurring_findings_are_bounded(session: Session, project: Project):
    task = _task(session, project)
    runs = [_run(session, task, RunStatus.FAILED, run_number=n) for n in (1, 2)]
    for index in range(5):
        for run in runs:
            _review(session, run, [_issue(requirement_id=f"REQ-{index}")], cycle=index)

    findings = recurring_findings(
        ReviewRepository(session).list_for_project(project.id), limit=2
    )

    assert len(findings) == 2


def test_a_finding_in_two_files_is_two_findings(session: Session, project: Project):
    """The lesson system groups by file for the same reason: a rule about one
    file is not a rule about another."""
    task = _task(session, project)
    runs = [_run(session, task, RunStatus.FAILED, run_number=n) for n in (1, 2)]
    for file_name in ("src/a.tsx", "src/b.tsx"):
        for run in runs:
            _review(session, run, [_issue(file=file_name, requirement_id="REQ-1")], cycle=1)

    findings = recurring_findings(ReviewRepository(session).list_for_project(project.id))

    assert len(findings) == 2
    assert all(finding["count"] == 2 for finding in findings)
    assert {finding["file"] for finding in findings} == {"src/a.tsx", "src/b.tsx"}


def test_the_ordering_is_stable_for_equal_counts(session: Session, project: Project):
    """Metrics that reshuffle between two identical calls are not reportable."""
    task = _task(session, project)
    runs = [_run(session, task, RunStatus.FAILED, run_number=n) for n in (1, 2)]
    for run in runs:
        _review(
            session,
            run,
            [_issue(requirement_id="REQ-1"), _issue(requirement_id="REQ-2")],
            cycle=1,
        )
    reviews = ReviewRepository(session).list_for_project(project.id)

    assert recurring_findings(reviews) == recurring_findings(list(reversed(reviews)))


# --- lesson metrics ---------------------------------------------------------


def test_lesson_counts_cover_the_lifecycle(session: Session, project: Project):
    _lesson(session, project, status=LessonStatus.PROPOSED)
    _lesson(session, project, status=LessonStatus.REJECTED)
    _lesson(session, project, status=LessonStatus.RETIRED)
    _lesson(session, project, status=LessonStatus.APPROVED)

    reported = metrics.lesson_metrics(session, project.id)

    assert reported["by_status"] == {
        "proposed": 1,
        "approved": 1,
        "rejected": 1,
        "retired": 1,
    }


def test_a_project_with_no_lessons_reports_nothing_pending(session: Session, project: Project):
    reported = metrics.lesson_metrics(session, project.id)

    assert sum(reported["by_status"].values()) == 0
    assert reported["unused_approved"] == 0


def test_an_approved_lesson_nobody_has_been_shown_is_reported(
    session: Session, project: Project
):
    """Section 32's approval gate only earns its cost if somebody can see which
    approved lessons are going unused."""
    _lesson(session, project, status=LessonStatus.APPROVED)
    _lesson(session, project, status=LessonStatus.PROPOSED)

    reported = metrics.lesson_metrics(session, project.id)

    assert reported["unused_approved"] == 1
    assert len(reported["unused_approved_ids"]) == 1


def test_lesson_counts_are_scoped_to_the_project(
    session: Session, project: Project, other_project: Project
):
    _lesson(session, project, status=LessonStatus.PROPOSED)
    _lesson(session, other_project, status=LessonStatus.PROPOSED)
    _lesson(session, other_project, status=LessonStatus.PROPOSED)

    assert metrics.lesson_metrics(session, project.id)["by_status"]["proposed"] == 1


# --- lesson usefulness (rule 6) ---------------------------------------------


def test_usefulness_needs_a_lesson_that_was_actually_shown(
    session: Session, project: Project
):
    """A ratio over lessons no coder has seen is a division of nothing."""
    lesson = _lesson(session, project)

    reported = metrics.lesson_usefulness(session, lesson.id)

    assert reported["times_retrieved"] == 0
    assert reported["times_applied"] == 0
    assert reported["applied_per_retrieval"] is None
    assert reported["ever_retrieved"] is False


def test_usefulness_is_applied_over_retrieved(session: Session, project: Project):
    lesson = _lesson(session, project)
    repo = LessonRepository(session)
    for _ in range(3):
        repo.record_retrieval([lesson.id])
    repo.record_applied([lesson.id])

    reported = metrics.lesson_usefulness(session, lesson.id)

    assert reported["times_retrieved"] == 3
    assert reported["times_applied"] == 1
    assert reported["applied_per_retrieval"] == pytest.approx(1 / 3, abs=1e-3)
    assert reported["ever_applied"] is True


def test_usefulness_reports_the_recurrence_behind_the_lesson(
    session: Session, project: Project
):
    """A lesson's usefulness is only interpretable next to how often it was found."""
    lesson = _lesson(session, project)

    reported = metrics.lesson_usefulness(session, lesson.id)

    assert reported["occurrences"] == 1
    assert reported["confidence"] == "low"


def test_usefulness_of_an_unknown_lesson_is_a_404(session: Session):
    with pytest.raises(EntityNotFound):
        metrics.lesson_usefulness(session, uuid4())


# --- model metrics ----------------------------------------------------------


def test_model_cost_is_totalled_per_model(session: Session, project: Project):
    task = _task(session, project)
    run = _run(session, task, RunStatus.SUCCEEDED)
    _spend(session, run, input_tokens=1000)
    _spend(session, run, input_tokens=1000)

    reported = metrics.model_metrics(session, project.id)

    assert reported["totals"]["calls"] == 2
    assert reported["totals"]["input_tokens"] == 2000
    assert reported["totals"]["output_tokens"] == 500
    assert reported["totals"]["total_tokens"] == 2500
    assert len(reported["models"]) == 1
    assert reported["models"][0]["calls"] == 2
    # The split that matters for a coder-only deployment: what share of the
    # spend was the thing the coder is for.
    assert reported["models"][0]["coding_tokens"] == 2500


def test_the_models_report_says_which_purposes_were_counted(
    session: Session, project: Project
):
    """A cost figure with an unstated denominator is not interpretable."""
    reported = metrics.model_metrics(session, project.id)

    assert ModelPurpose.CODE.value in reported["coding_purposes"]


def test_model_metrics_can_span_projects(
    session: Session, project: Project, other_project: Project
):
    """An operator asking what the deployment costs must not have to enumerate
    projects first."""
    mine = _run(session, _task(session, project), RunStatus.SUCCEEDED)
    theirs = _run(session, _task(session, other_project, "O-1"), RunStatus.SUCCEEDED)
    _spend(session, mine, input_tokens=100)
    _spend(session, theirs, input_tokens=100)

    assert metrics.model_metrics(session)["totals"]["calls"] == 2
    assert metrics.model_metrics(session, project.id)["totals"]["calls"] == 1
    assert metrics.model_metrics(session, project.id)["project_id"] == str(project.id)


def test_model_metrics_with_no_calls_report_zero(session: Session, project: Project):
    reported = metrics.model_metrics(session, project.id)

    assert reported["totals"]["calls"] == 0
    assert reported["models"] == []


# --- training metrics -------------------------------------------------------


def test_training_counts_report_what_was_preserved(
    session: Session, project: Project
):
    task = _task(session, project)
    run = _run(session, task, RunStatus.SUCCEEDED)
    _example(session, project, run, task, input_tokens=100, output_tokens=50)

    reported = metrics.training_metrics(session, project.id)

    assert reported["total"] == 1
    assert reported["by_status"] == {"captured": 1}
    assert reported["total_input_tokens"] == 100
    assert reported["total_output_tokens"] == 50


def test_captured_but_unselected_examples_are_visible_as_such(
    session: Session, project: Project
):
    """Section 34 says not to train on every accepted example, so the backlog of
    examples nobody has ruled on is itself a number worth seeing."""
    task = _task(session, project)
    first = _run(session, task, RunStatus.SUCCEEDED, run_number=1)
    second = _run(session, task, RunStatus.SUCCEEDED, run_number=2)
    _example(session, project, first, task)
    _example(session, project, second, task, status=TrainingStatus.SELECTED)

    reported = metrics.training_metrics(session, project.id)

    assert reported["total"] == 2
    assert reported["captured_but_not_selected"] == 1


def test_training_metrics_are_scoped_to_the_project(
    session: Session, project: Project, other_project: Project
):
    my_task = _task(session, project)
    their_task = _task(session, other_project, "O-1")
    my_run = _run(session, my_task, RunStatus.SUCCEEDED)
    _example(session, project, my_run, my_task)
    for number in (1, 2):
        their_run = _run(session, their_task, RunStatus.SUCCEEDED, run_number=number)
        _example(session, other_project, their_run, their_task)

    assert metrics.training_metrics(session, project.id)["total"] == 1
    assert metrics.training_metrics(session, other_project.id)["total"] == 2


def test_training_metrics_with_nothing_preserved_report_zero(
    session: Session, project: Project
):
    assert metrics.training_metrics(session, project.id)["total"] == 0


# --- the whole view ---------------------------------------------------------


def test_the_project_view_assembles_every_part(session: Session, project: Project):
    task = _task(session, project)
    run = _run(session, task, RunStatus.SUCCEEDED)
    _review(session, run, [_issue()], cycle=1)
    _lesson(session, project)
    _spend(session, run, input_tokens=100)

    reported = metrics.project_metrics(session, project.id)

    assert set(reported) == {"project_id", "runs", "reviews", "lessons", "training", "models"}
    assert reported["project_id"] == str(project.id)


def test_the_project_view_does_not_include_another_project(
    session: Session, project: Project, other_project: Project
):
    my_task = _task(session, project)
    their_task = _task(session, other_project, "O-1")
    _run(session, my_task, RunStatus.SUCCEEDED)
    _run(session, their_task, RunStatus.FAILED)
    _lesson(session, other_project)
    _spend(session, _run(session, their_task, RunStatus.FAILED, run_number=2))

    reported = metrics.project_metrics(session, project.id)

    assert reported["runs"]["total_runs"] == 1
    assert sum(reported["lessons"]["by_status"].values()) == 0
    assert reported["models"] == []


def test_metrics_for_an_unknown_project_is_a_404(session: Session):
    with pytest.raises(EntityNotFound):
        metrics.project_metrics(session, uuid4())
