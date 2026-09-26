from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import EscalationStatus, RunStatus, TaskStatus
from apps.orchestrator.domain.escalation import EscalationIntent, EscalationOption
from apps.orchestrator.domain.models import HumanEscalation, Project, Task
from apps.orchestrator.repositories import (
    EscalationRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import prepare_workspace
from apps.orchestrator.workflow.resolution import apply_escalation_answer


def test_accepting_an_escalated_candidate_lands_it_and_releases_the_tree(
    session: Session, fixture_repo: Path, git_settings
):
    project = ProjectRepository(session).add(
        Project(name="fixture", repository_path=str(fixture_repo))
    )
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="TS-9", title="change answer")
    )
    task = TaskRepository(session).transition(task.id, TaskStatus.READY)
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=git_settings)
    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )
    TaskRepository(session).transition(task.id, TaskStatus.HUMAN_REVIEW)
    TaskRunRepository(session).finish(run.id, RunStatus.FAILED, "HUMAN_DECISION_REQUIRED")
    escalation = EscalationRepository(session).add(
        HumanEscalation(
            task_id=task.id,
            task_run_id=run.id,
            reason="HUMAN_DECISION_REQUIRED",
            summary="Candidate awaits a person.",
            options=[
                EscalationOption(
                    "A",
                    EscalationIntent.ACCEPT_CANDIDATE,
                    "Accept the candidate.",
                )
            ],
        )
    )

    answered = apply_escalation_answer(
        session,
        escalation.id,
        resolution="Accepted after inspection.",
        intent=EscalationIntent.ACCEPT_CANDIDATE,
        status=EscalationStatus.RESOLVED,
        settings=git_settings,
    )

    stored_task = TaskRepository(session).get(task.id)
    stored_run = TaskRunRepository(session).get(run.id)
    assert answered.resolution_intent is EscalationIntent.ACCEPT_CANDIDATE
    assert stored_task is not None and stored_task.status is TaskStatus.COMPLETE
    assert stored_run is not None and stored_run.status is RunStatus.SUCCEEDED
    assert stored_run.candidate_commit
    assert not workspace.path.exists()
