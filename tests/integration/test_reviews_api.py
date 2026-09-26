"""The review and escalation endpoints (build.md section 39).

Two of section 39's V1 endpoints exist so a human review is answerable
rather than merely recorded: ``GET /runs/{id}/reviews`` shows what a reviewer
said, and ``POST /escalations/{id}/resolve`` records what a person decided.

Resolving an escalation deliberately does not move the task. What an answer
means for the run is a workflow decision (phase K), and an endpoint that
guessed it would be the system deciding what the human decided.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.orchestrator.config import get_settings
from apps.orchestrator.db.session import get_db
from apps.orchestrator.domain.enums import (
    EscalationStatus,
    IssueCategory,
    IssueSeverity,
    ReviewDecision,
    RiskLevel,
    TaskStatus,
)
from apps.orchestrator.domain.models import (
    HumanEscalation,
    Project,
    Review,
    ReviewIssue,
    Task,
    TaskRun,
)
from apps.orchestrator.main import create_app
from apps.orchestrator.repositories import (
    EscalationRepository,
    ProjectRepository,
    ReviewRepository,
    TaskRepository,
)
from apps.orchestrator.services.runs import create_run

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
def task(session: Session, tmp_path: Path) -> Task:
    project = ProjectRepository(session).add(
        Project(name="TraceStack", repository_path=str(tmp_path / "repo"))
    )
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="TS-004",
            title="Restore the saved selection",
        )
    )
    return tasks.transition(task.id, TaskStatus.READY)


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return create_run(session, task.id)


def test_a_runs_reviews_are_listed_oldest_cycle_first(
    client: TestClient, session: Session, run: TaskRun
):
    reviews = ReviewRepository(session)
    for cycle, decision in (
        (1, ReviewDecision.CHANGES_REQUESTED),
        (2, ReviewDecision.APPROVED),
    ):
        reviews.add(
            Review(
                task_run_id=run.id,
                reviewer_provider="env:reviewer",
                reviewer_model="reviewer-test",
                decision=decision,
                summary=f"cycle {cycle}",
                confidence=0.9,
                risk=RiskLevel.LOW,
                cycle=cycle,
                issues=(
                    [
                        ReviewIssue(
                            severity=IssueSeverity.HIGH,
                            category=IssueCategory.REQUIREMENT,
                            problem="Saved selection is not restored.",
                            required_fix="Restore it.",
                            file="src/nav.ts",
                            line=84,
                        )
                    ]
                    if cycle == 1
                    else []
                ),
            )
        )

    listed = client.get(f"/runs/{run.id}/reviews")
    assert listed.status_code == 200, listed.text
    body = listed.json()

    assert [review["cycle"] for review in body] == [1, 2]
    assert body[1]["decision"] == ReviewDecision.APPROVED.value
    (issue,) = body[0]["issues"]
    assert issue["problem"] == "Saved selection is not restored."
    # The blocking distinction the workflow acted on is visible, not re-derived.
    assert issue["blocking"] is True


def test_a_run_with_no_reviews_is_an_empty_list_and_a_missing_run_is_404(
    client: TestClient, run: TaskRun
):
    assert client.get(f"/runs/{run.id}/reviews").json() == []
    assert client.get(f"/runs/{uuid4()}/reviews").status_code == 404


def test_an_open_escalation_is_listed_and_can_be_answered(
    client: TestClient, session: Session, task: Task, run: TaskRun
):
    escalation = EscalationRepository(session).add(
        HumanEscalation(
            task_id=task.id,
            task_run_id=run.id,
            reason="RETRY_EXHAUSTED",
            summary="TASK TS-004 — HUMAN REVIEW REQUIRED",
            options=["A. Accept the candidate.", "B. Reword the task."],
        )
    )

    listed = client.get("/escalations").json()
    assert [entry["id"] for entry in listed] == [str(escalation.id)]
    assert listed[0]["options"][0].startswith("A.")

    answered = client.post(
        f"/escalations/{escalation.id}/resolve",
        json={"resolution": "A: accept the candidate as it stands."},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == EscalationStatus.RESOLVED.value
    assert answered.json()["resolved_at"]

    # It leaves the queue, and the task is untouched: phase K decides what the
    # answer means for the run.
    assert client.get("/escalations").json() == []
    assert TaskRepository(session).get(task.id).status is TaskStatus.READY


def test_answering_the_same_escalation_twice_is_a_conflict(
    client: TestClient, session: Session, task: Task
):
    escalation = EscalationRepository(session).add(
        HumanEscalation(task_id=task.id, reason="HUMAN_DECISION_REQUIRED", summary="s")
    )
    body = {"resolution": "decided"}

    assert client.post(f"/escalations/{escalation.id}/resolve", json=body).status_code == 200
    second = client.post(f"/escalations/{escalation.id}/resolve", json=body)
    assert second.status_code == 409
    assert "already resolved" in second.json()["detail"]


def test_an_escalation_can_be_dismissed_rather_than_resolved(
    client: TestClient, session: Session, task: Task
):
    escalation = EscalationRepository(session).add(
        HumanEscalation(task_id=task.id, reason="HUMAN_DECISION_REQUIRED", summary="s")
    )

    response = client.post(
        f"/escalations/{escalation.id}/resolve",
        json={"resolution": "raised in error", "status": "DISMISSED"},
    )
    assert response.json()["status"] == EscalationStatus.DISMISSED.value


def test_an_empty_resolution_is_refused(client: TestClient, session: Session, task: Task):
    """An escalation closed with no answer is worse than one left open."""
    escalation = EscalationRepository(session).add(
        HumanEscalation(task_id=task.id, reason="HUMAN_DECISION_REQUIRED", summary="s")
    )
    response = client.post(
        f"/escalations/{escalation.id}/resolve", json={"resolution": "  "}
    )
    assert response.status_code in (400, 422)


def test_resolving_an_unknown_escalation_is_404(client: TestClient):
    response = client.post(
        f"/escalations/{uuid4()}/resolve", json={"resolution": "whatever"}
    )
    assert response.status_code == 404
