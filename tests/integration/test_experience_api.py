"""The experience endpoints (build.md sections 32--35, 39).

Section 32's approval gate is only real if a person can reach the queue and
answer it over HTTP, so the lesson routes are tested as a workflow: propose,
read, approve, retrieve. The metrics and history routes are tested for the two
things a caller gets wrong when they are not enforced -- scope, and the
difference between 404 and an empty list.

The app is wired to the same ``session`` fixture the tests write through, so the
rows here are the rows the endpoints see.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.session import get_db
from apps.orchestrator.domain.enums import (
    IssueCategory,
    IssueSeverity,
    ReviewDecision,
    RiskLevel,
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
from apps.orchestrator.main import create_app
from apps.orchestrator.repositories import (
    LessonRepository,
    ProjectRepository,
    ReviewRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services import training as training_service

pytestmark = pytest.mark.integration


@pytest.fixture
def client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def project(session: Session, tmp_path: Path) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            external_project_id="tracestack",
            repository_path=str(tmp_path / "repo"),
        )
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    return TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="TS-004",
            title="Restore the saved selection",
            limits=TaskLimits(max_attempts=3),
        )
    )


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return TaskRunRepository(session).finish(
        TaskRunRepository(session).add(
            TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
        ).id,
        RunStatus.SUCCEEDED,
    )


def _other_project(session: Session, tmp_path: Path) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Other",
            external_project_id="other",
            repository_path=str(tmp_path / "other-repo"),
        )
    )


def _reviewed(session: Session, run: TaskRun) -> Review:
    """A review whose single finding was then resolved: the cycle section 32
    calls 'verified', and the only kind that should produce a lesson."""
    return ReviewRepository(session).add(
        Review(
            task_run_id=run.id,
            reviewer_provider="env:reviewer",
            reviewer_model="reviewer-test",
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary="Needs work.",
            confidence=0.8,
            risk=RiskLevel.MEDIUM,
            cycle=1,
            issues=[
                ReviewIssue(
                    severity=IssueSeverity.HIGH,
                    category=IssueCategory.TESTING,
                    problem="The navigation test asserts on component internals.",
                    required_fix="Assert on the rendered output of the navigation component.",
                    file="src/components/navigation.tsx",
                    line=42,
                    requirement_id="REQ-3",
                    resolved=True,
                )
            ],
        )
    )


def _lesson(session: Session, project_id, **overrides) -> Lesson:
    """A lesson row for tests about listing and counting.

    Deliberately untraceable, because that is the state a lesson is in before
    anybody has extracted it from a review.
    """
    fields = {
        "project_id": project_id,
        "category": "testing",
        "title": "Assert on behaviour",
        "lesson": "Assert on rendered output rather than on internals.",
    }
    fields.update(overrides)
    return LessonRepository(session).add(Lesson(**fields))


def _proposed_lesson(client: TestClient, session: Session, run: TaskRun) -> Lesson:
    """A lesson as the system produces one: extracted from a reviewed run, so it
    carries the source that rule 3 makes approval conditional on."""
    _reviewed(session, run)
    return LessonRepository(session).get(
        UUID(client.post(f"/runs/{run.id}/lessons/propose").json()[0]["id"])
    )


# --- the queue --------------------------------------------------------------


def test_the_queue_defaults_to_what_is_waiting_on_a_person(
    client: TestClient, session: Session, project: Project
):
    proposed = _lesson(session, project.id)
    _lesson(session, project.id, status="approved")

    listed = client.get("/lessons")

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [item["id"] for item in body] == [str(proposed.id)]
    assert body[0]["status"] == "proposed"


def test_the_queue_can_be_narrowed_to_one_project(
    client: TestClient, session: Session, project: Project, tmp_path: Path
):
    mine = _lesson(session, project.id)
    _lesson(session, _other_project(session, tmp_path).id)

    listed = client.get(f"/lessons?project_id={project.id}")

    assert [item["id"] for item in listed.json()] == [str(mine.id)]


def test_the_queue_can_be_read_across_projects(
    client: TestClient, session: Session, project: Project, tmp_path: Path
):
    """A cross-project view is a convenience, not a scope change."""
    mine = _lesson(session, project.id)
    theirs = _lesson(session, _other_project(session, tmp_path).id)

    assert {item["id"] for item in client.get("/lessons").json()} == {
        str(mine.id),
        str(theirs.id),
    }


def test_the_queue_accepts_another_status(
    client: TestClient, session: Session, project: Project
):
    _lesson(session, project.id)
    approved_id = _lesson(session, project.id, status="approved").id

    assert [item["id"] for item in client.get("/lessons?status=approved").json()] == [
        str(approved_id)
    ]


def test_an_empty_queue_is_an_empty_list_not_a_404(client: TestClient):
    response = client.get("/lessons")

    assert response.status_code == 200
    assert response.json() == []


def test_the_limit_is_bounded_by_the_query(client: TestClient):
    assert client.get("/lessons?limit=0").status_code == 422
    assert client.get("/lessons?limit=100000").status_code == 422


def test_a_status_that_does_not_exist_is_a_422(client: TestClient):
    assert client.get("/lessons?status=vindicated").status_code == 422


# --- one lesson -------------------------------------------------------------


def test_a_lesson_is_readable_with_the_source_needed_to_judge_it(
    client: TestClient, session: Session, run: TaskRun
):
    _reviewed(session, run)
    lesson_id = client.post(f"/runs/{run.id}/lessons/propose").json()[0]["id"]

    response = client.get(f"/lessons/{lesson_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    # The fields rule 3 is about: a person cannot judge "add a test" without
    # knowing which requirement it came from and where it was raised.
    assert body["requirement_id"] == "REQ-3"
    assert body["source_file"] == "src/components/navigation.tsx"
    assert body["source_review_issue_id"] is not None
    assert body["is_retrievable"] is False


def test_a_missing_lesson_is_a_404(client: TestClient):
    assert client.get(f"/lessons/{uuid4()}").status_code == 404


def test_a_malformed_lesson_id_is_a_422(client: TestClient):
    assert client.get("/lessons/not-a-uuid").status_code == 422


# --- approval ---------------------------------------------------------------


def test_a_proposed_lesson_can_be_approved_over_http(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    lesson_id = _proposed_lesson(client, session, run).id

    response = client.post(
        f"/lessons/{lesson_id}/approve", json={"approved_by": "darri"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == "darri"
    assert body["approved_at"] is not None
    assert body["is_retrievable"] is True


def test_approving_without_naming_anyone_is_allowed(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    lesson_id = _proposed_lesson(client, session, run).id

    assert client.post(f"/lessons/{lesson_id}/approve", json={}).status_code == 200


def test_a_proposal_can_be_rejected_with_its_reason(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    lesson_id = _proposed_lesson(client, session, run).id

    response = client.post(
        f"/lessons/{lesson_id}/reject", json={"reason": "too specific to this file"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert response.json()["rejection_reason"] == "too specific to this file"


def test_a_lesson_can_be_retired_after_use(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    lesson = _proposed_lesson(client, session, run)
    LessonRepository(session).approve(lesson.id)

    response = client.post(
        f"/lessons/{lesson.id}/retire", json={"reason": "no longer true here"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "retired"


def test_answering_for_a_lesson_that_does_not_exist_is_a_404(client: TestClient):
    missing = uuid4()

    assert client.post(f"/lessons/{missing}/approve", json={}).status_code == 404
    assert client.post(f"/lessons/{missing}/reject", json={"reason": "x"}).status_code == 404
    assert client.post(f"/lessons/{missing}/retire", json={"reason": "x"}).status_code == 404


# --- usefulness -------------------------------------------------------------


def test_usefulness_of_a_lesson_nobody_has_been_shown(
    client: TestClient, session: Session, project: Project
):
    lesson = _lesson(session, project.id)

    body = client.get(f"/lessons/{lesson.id}/usefulness").json()

    assert body["times_retrieved"] == 0
    assert body["applied_per_retrieval"] is None
    assert body["ever_retrieved"] is False


def test_usefulness_after_retrieval_and_application(
    client: TestClient, session: Session, project: Project
):
    lesson = _lesson(session, project.id)
    LessonRepository(session).record_retrieval([lesson.id])
    LessonRepository(session).record_applied([lesson.id])

    body = client.get(f"/lessons/{lesson.id}/usefulness").json()

    assert body["times_retrieved"] == 1
    assert body["times_applied"] == 1
    assert body["applied_per_retrieval"] == 1.0


def test_usefulness_of_a_missing_lesson_is_a_404(client: TestClient):
    assert client.get(f"/lessons/{uuid4()}/usefulness").status_code == 404


# --- proposing from a run ---------------------------------------------------


def test_a_runs_verified_findings_can_be_proposed_over_http(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    _reviewed(session, run)

    response = client.post(f"/runs/{run.id}/lessons/propose")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["status"] == "proposed"
    assert body[0]["project_id"] == str(project.id)


def test_the_proposal_then_leaves_the_queue(client: TestClient, session: Session,
                                            run: TaskRun):
    """Propose, read, answer: the workflow section 32 describes, over HTTP."""
    _reviewed(session, run)
    lesson_id = client.post(f"/runs/{run.id}/lessons/propose").json()[0]["id"]

    assert [item["id"] for item in client.get("/lessons").json()] == [lesson_id]

    client.post(f"/lessons/{lesson_id}/approve", json={"approved_by": "darri"})

    assert client.get("/lessons").json() == []
    assert [item["id"] for item in client.get("/lessons?status=approved").json()] == [
        lesson_id
    ]


def test_proposing_from_a_run_with_nothing_teachable_is_an_empty_list(
    client: TestClient, run: TaskRun
):
    assert client.post(f"/runs/{run.id}/lessons/propose").json() == []


def test_proposing_from_a_missing_run_is_a_404(client: TestClient):
    assert client.post(f"/runs/{uuid4()}/lessons/propose").status_code == 404


# --- review history ---------------------------------------------------------


def test_a_tasks_review_history_spans_its_runs(client: TestClient, session: Session,
                                                task: Task, run: TaskRun):
    _reviewed(session, run)

    history = client.get(f"/tasks/{task.id}/review-history")

    assert history.status_code == 200, history.text
    assert len(history.json()) == 1


def test_a_tasks_history_with_no_reviews_is_an_empty_list(
    client: TestClient, task: Task
):
    assert client.get(f"/tasks/{task.id}/review-history").json() == []


def test_a_projects_history_spans_its_tasks(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    _reviewed(session, run)

    history = client.get(f"/projects/{project.id}/review-history")

    assert history.status_code == 200, history.text
    assert len(history.json()) == 1


def test_a_projects_history_does_not_include_another_project(
    client: TestClient, session: Session, project: Project, tmp_path: Path
):
    other = _other_project(session, tmp_path)
    other_task = TaskRepository(session).add(
        Task(project_id=other.id, external_task_id="O-1", title="Theirs")
    )
    other_run = TaskRunRepository(session).add(
        TaskRun(task_id=other_task.id, run_number=1, status=RunStatus.SUCCEEDED)
    )
    _reviewed(session, other_run)

    assert client.get(f"/projects/{project.id}/review-history").json() == []
    assert len(client.get(f"/projects/{other.id}/review-history").json()) == 1


def test_history_for_a_missing_project_or_task_is_a_404(client: TestClient):
    assert client.get(f"/projects/{uuid4()}/review-history").status_code == 404
    assert client.get(f"/tasks/{uuid4()}/review-history").status_code == 404


def test_recurring_findings_of_a_missing_project_is_a_404(client: TestClient):
    assert client.get(f"/projects/{uuid4()}/recurring-findings").status_code == 404


# --- recurring findings -----------------------------------------------------


def test_a_projects_recurring_findings_are_listed(
    client: TestClient, session: Session, project: Project, task: Task
):
    runs = [
        TaskRunRepository(session).add(
            TaskRun(task_id=task.id, run_number=number, status=RunStatus.FAILED)
        )
        for number in (1, 2)
    ]
    for run in runs:
        _reviewed(session, run)

    findings = client.get(f"/projects/{project.id}/recurring-findings")

    assert findings.status_code == 200, findings.text
    body = findings.json()
    assert len(body) == 1
    assert body[0]["count"] == 2
    assert body[0]["category"] == "testing"


def test_a_projects_findings_with_nothing_recurring_is_an_empty_list(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    """A single review of a single finding is not a pattern."""
    _reviewed(session, run)

    assert client.get(f"/projects/{project.id}/recurring-findings").json() == []


# --- training ---------------------------------------------------------------


def test_a_runs_accepted_work_can_be_captured_over_http(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    response = client.post(f"/runs/{run.id}/training/capture")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "captured"
    assert body["project_id"] == str(project.id)
    assert body["outcome"] == "accepted"


def test_a_projects_training_examples_are_listed(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    training_service.capture_accepted_run(session, run.id)

    response = client.get(f"/projects/{project.id}/training")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["outcome"] == "accepted"
    assert body[0]["external_task_id"] == "TS-004"


def test_the_training_list_can_be_filtered_by_status(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    training_service.capture_accepted_run(session, run.id)

    assert len(client.get(f"/projects/{project.id}/training?status=captured").json()) == 1
    assert client.get(f"/projects/{project.id}/training?status=selected").json() == []


def test_a_training_status_filter_stays_inside_the_project(
    client: TestClient, session: Session, project: Project, tmp_path: Path, run: TaskRun
):
    """The regression this pins: filtering by status used to drop the project
    filter entirely, so one project's curation queue listed another project's
    examples."""
    training_service.capture_accepted_run(session, run.id)
    other = _other_project(session, tmp_path)
    other_task = TaskRepository(session).add(
        Task(project_id=other.id, external_task_id="O-1", title="Theirs")
    )
    other_run = TaskRunRepository(session).add(
        TaskRun(task_id=other_task.id, run_number=1, status=RunStatus.SUCCEEDED)
    )
    training_service.capture_accepted_run(session, other_run.id)

    assert len(client.get(f"/projects/{project.id}/training?status=captured").json()) == 1
    assert len(client.get(f"/projects/{other.id}/training?status=captured").json()) == 1


def test_a_runs_training_example_is_readable(client: TestClient, session: Session,
                                             run: TaskRun):
    training_service.capture_accepted_run(session, run.id)

    response = client.get(f"/runs/{run.id}/training")

    assert response.status_code == 200, response.text
    assert response.json()["task_run_id"] == str(run.id)


def test_a_run_that_was_not_captured_reports_null_not_a_404(
    client: TestClient, run: TaskRun
):
    """"This run was rejected" is an answer; a 404 would say the run is unknown."""
    response = client.get(f"/runs/{run.id}/training")

    assert response.status_code == 200, response.text
    assert response.json() is None


def test_the_training_example_of_a_run_that_does_not_exist_is_a_404(client: TestClient):
    assert client.get(f"/runs/{uuid4()}/training").status_code == 404


def test_capturing_a_run_twice_refiles_rather_than_duplicating(
    client: TestClient, project: Project, run: TaskRun
):
    first = client.post(f"/runs/{run.id}/training/capture").json()
    second = client.post(f"/runs/{run.id}/training/capture").json()

    assert first["id"] == second["id"]
    assert len(client.get(f"/projects/{project.id}/training").json()) == 1


def test_capturing_a_rejected_run_is_refused(
    client: TestClient, session: Session, task: Task
):
    """Section 34 is about accepted work; a failure filed as training data would
    teach the failure."""
    rejected = TaskRunRepository(session).finish(
        TaskRunRepository(session).add(
            TaskRun(task_id=task.id, run_number=2, status=RunStatus.RUNNING)
        ).id,
        RunStatus.FAILED,
        failure_reason="gave up",
    )

    assert client.post(f"/runs/{rejected.id}/training/capture").status_code == 409


def test_capturing_a_missing_run_is_a_404(client: TestClient):
    assert client.post(f"/runs/{uuid4()}/training/capture").status_code == 404


# --- metrics ----------------------------------------------------------------


def test_a_projects_metrics_report_every_part(
    client: TestClient, session: Session, project: Project, run: TaskRun
):
    _reviewed(session, run)
    _lesson(session, project.id)

    body = client.get(f"/projects/{project.id}/metrics").json()

    assert body["project_id"] == str(project.id)
    assert body["runs"]["total_runs"] == 1
    assert body["reviews"]["total"] == 1
    assert body["lessons"]["by_status"]["proposed"] == 1


def test_metrics_for_a_project_with_nothing_in_them(
    client: TestClient, project: Project
):
    body = client.get(f"/projects/{project.id}/metrics").json()

    assert body["runs"]["total_runs"] == 0
    assert body["reviews"]["total"] == 0


def test_metrics_for_a_missing_project_is_a_404(client: TestClient):
    assert client.get(f"/projects/{uuid4()}/metrics").status_code == 404


def test_a_projects_training_examples_for_a_missing_project_is_a_404(client: TestClient):
    """An unknown project and a project that has captured nothing are different
    answers, and only one of them is an empty list."""
    assert client.get(f"/projects/{uuid4()}/training").status_code == 404


def test_a_tasks_metrics(client: TestClient, session: Session, project: Project,
                         task: Task):
    TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.SUCCEEDED)
    )

    body = client.get(f"/tasks/{task.id}/metrics").json()

    assert body["total_runs"] == 1
    assert body["successes"] == 1


def test_task_metrics_for_a_missing_task_is_a_404(client: TestClient):
    assert client.get(f"/tasks/{uuid4()}/metrics").status_code == 404


def test_model_metrics_can_be_read_across_projects(client: TestClient):
    body = client.get("/models/metrics").json()

    assert body["project_id"] is None
    assert "models" in body
    assert "totals" in body


def test_model_metrics_for_one_project(client: TestClient, project: Project):
    assert client.get(f"/models/metrics?project_id={project.id}").json()[
        "project_id"
    ] == str(project.id)


def test_the_lesson_and_training_metrics_are_separate_endpoints(
    client: TestClient, session: Session, project: Project
):
    _lesson(session, project.id)

    lessons = client.get(f"/projects/{project.id}/lesson-metrics")
    training = client.get(f"/projects/{project.id}/training-metrics")

    assert lessons.status_code == 200, lessons.text
    assert lessons.json()["by_status"]["proposed"] == 1
    assert training.status_code == 200, training.text
    assert training.json()["total"] == 0


def test_the_lesson_and_training_metrics_of_a_missing_project_are_404s(
    client: TestClient,
):
    missing = uuid4()

    assert client.get(f"/projects/{missing}/lesson-metrics").status_code == 404
    assert client.get(f"/projects/{missing}/training-metrics").status_code == 404
