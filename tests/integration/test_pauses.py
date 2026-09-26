from __future__ import annotations

from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services.pauses import pause_task, resume_task


def test_a_ready_task_pauses_immediately_and_resumes_to_ready(session: Session):
    project = ProjectRepository(session).add(
        Project(name="pause fixture", repository_path="/tmp/pause-fixture")
    )
    tasks = TaskRepository(session)
    task = tasks.add(Task(project_id=project.id, external_task_id="T-1", title="one"))
    task = tasks.transition(task.id, TaskStatus.READY)

    request = pause_task(session, task.id, reason="maintenance", requested_by="operator")

    assert request.honoured_at is not None
    assert tasks.get(task.id).status is TaskStatus.PAUSED
    resumed = resume_task(session, task.id)
    assert resumed.status is TaskStatus.READY


def test_resuming_a_dependency_blocked_task_does_not_promote_it(session: Session):
    project = ProjectRepository(session).add(
        Project(name="pause fixture", repository_path="/tmp/pause-fixture-2")
    )
    tasks = TaskRepository(session)
    dependency = tasks.add(
        Task(project_id=project.id, external_task_id="T-1", title="one")
    )
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="T-2",
            title="two",
            depends_on=[dependency.external_task_id],
        )
    )

    pause_task(session, task.id)
    resumed = resume_task(session, task.id)

    assert resumed.status is TaskStatus.PENDING
