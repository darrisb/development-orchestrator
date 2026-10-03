from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from ..db.session import get_db, get_session_factory
from ..domain.enums import TaskStatus
from ..schemas.projects import (
    BaselineCorrectionRequest,
    BaselineCorrectionResponse,
    ImportTasksRequest,
    ImportTasksResponse,
    ProjectCreateRequest,
    ProjectResponse,
)
from ..schemas.runs import CampaignAdvanceResponse, ProjectRunResponse, RunUsageResponse
from ..schemas.tasks import NextTaskResponse, TaskResponse
from ..services import model_usage as usage_service
from ..services import projects as project_service
from ..services import tasks as task_service
from ..services.baseline_correction import apply_baseline_correction
from ..services.campaign import CampaignRunner
from ..services.manifest_loader import load_manifest, manifest_path_for
from ..services.scheduler import select_next_task
from ..services.task_importer import import_manifest
from ..workflow import WorkflowRunner

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreateRequest, session: Session = Depends(get_db)
) -> ProjectResponse:
    project = project_service.create_project(
        session,
        name=payload.name,
        repository_path=payload.repository_path,
        external_project_id=payload.external_project_id,
        default_branch=payload.default_branch,
        worker_profile=payload.worker_profile,
        protected_paths=payload.protected_paths,
        sensitive_path_exceptions=payload.sensitive_path_exceptions,
        generated_path_exceptions=payload.generated_path_exceptions,
        dependency_paths=payload.dependency_paths,
        approval_gated_categories=payload.approval_gated_categories,
        verification=payload.verification.to_domain(),
        milestone_interval=payload.milestone_interval,
    )
    return ProjectResponse.from_domain(project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(session: Session = Depends(get_db)) -> list[ProjectResponse]:
    return [
        ProjectResponse.from_domain(project)
        for project in project_service.list_projects(session)
    ]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: UUID, session: Session = Depends(get_db)) -> ProjectResponse:
    return ProjectResponse.from_domain(project_service.get_project(session, project_id))


@router.post("/{project_id}/import-tasks", response_model=ImportTasksResponse)
def import_tasks(
    project_id: UUID,
    payload: ImportTasksRequest | None = None,
    session: Session = Depends(get_db),
) -> ImportTasksResponse:
    """Import or re-synchronise the project's manifest.

    Runtime task status is preserved; see ``services.task_importer``.
    """
    project = project_service.get_project(session, project_id)
    requested = (payload.manifest_path if payload else None) or manifest_path_for(
        project.repository_path
    )
    manifest = load_manifest(Path(requested))
    report = import_manifest(session, manifest, project_id=project.id)
    return ImportTasksResponse.from_report(report)


@router.post(
    "/{project_id}/baseline-corrections",
    response_model=BaselineCorrectionResponse,
    status_code=status.HTTP_200_OK,
)
def create_baseline_correction(
    project_id: UUID,
    payload: BaselineCorrectionRequest,
    session: Session = Depends(get_db),
) -> BaselineCorrectionResponse:
    """Carry an operator's specification correction into the accepted baseline.

    Not an integration: no task is completed, no run is created and no
    task-completion provenance is written. See
    ``services.baseline_correction``.

    200 rather than 201 for both outcomes, including the replay: the resource
    being reported on is the project's baseline, which already existed, and a
    replay creates nothing at all.
    """
    correction = apply_baseline_correction(
        session,
        project_id,
        commit_sha=payload.commit_sha,
        reason=payload.reason,
        requested_by=payload.requested_by,
    )
    return BaselineCorrectionResponse.from_correction(correction)


@router.get("/{project_id}/tasks", response_model=list[TaskResponse])
def list_project_tasks(
    project_id: UUID,
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    session: Session = Depends(get_db),
) -> list[TaskResponse]:
    return [
        TaskResponse.from_domain(task)
        for task in task_service.list_tasks(session, project_id, task_status)
    ]


@router.get("/{project_id}/usage", response_model=RunUsageResponse)
def project_usage(project_id: UUID, session: Session = Depends(get_db)) -> RunUsageResponse:
    """What every model call on this project's tasks spent (concern 78).

    The project-wide counterpart of ``GET /runs/{run_id}/usage``, and the wider
    of the two figures: it includes the runs that were retried or abandoned,
    because those calls were made and charged whatever the run went on to do.

    Raises:
        EntityNotFound: no such project. Mapped to 404.
    """
    project = project_service.get_project(session, project_id)
    return RunUsageResponse.from_summary(
        usage_service.usage_for_project(session, project.id), project_id=project.id
    )


@router.get("/{project_id}/next-task", response_model=NextTaskResponse)
def next_task(project_id: UUID, session: Session = Depends(get_db)) -> NextTaskResponse:
    """The task the orchestrator would work on next, or why there is none."""
    project_service.get_project(session, project_id)
    return NextTaskResponse.from_selection(select_next_task(session, project_id))


@router.post("/{project_id}/run", response_model=ProjectRunResponse)
async def run_project(project_id: UUID) -> ProjectRunResponse:
    """Run the next task through the durable workflow, not graph internals."""
    session_factory = get_session_factory()
    with session_factory.begin() as session:
        project_service.get_project(session, project_id)

    runner = WorkflowRunner.configured(session_factory)
    try:
        selection, state = await runner.run_next(project_id)
    finally:
        await runner.aclose()
    if state is None:
        return ProjectRunResponse(
            no_task_reason=selection.reason.value if selection.reason else "PAUSED"
        )
    return ProjectRunResponse(
        run_id=UUID(state["run_id"]),
        outcome=state.get("outcome"),
        state=dict(state),
    )


@router.post("/{project_id}/campaign/advance", response_model=CampaignAdvanceResponse)
async def advance_campaign(
    project_id: UUID,
    transition_limit: int = Query(default=10, ge=1, le=100),
) -> CampaignAdvanceResponse:
    """Advance a project campaign through bounded existing task workflows."""
    session_factory = get_session_factory()
    with session_factory.begin() as session:
        project_service.get_project(session, project_id)

    workflow = WorkflowRunner.configured(session_factory)

    async def execute_existing_task_workflow(run_id: UUID) -> dict[str, object]:
        return dict(await workflow.run(run_id))

    campaign = CampaignRunner(
        session_factory,
        task_executor=execute_existing_task_workflow,
        transition_limit=transition_limit,
    )
    try:
        report = await campaign.advance(project_id)
    finally:
        await workflow.aclose()
    return CampaignAdvanceResponse.from_report(report)


@router.post("/{project_id}/pause", response_model=ProjectResponse)
def pause_project(project_id: UUID, session: Session = Depends(get_db)) -> ProjectResponse:
    return ProjectResponse.from_domain(project_service.pause_project(session, project_id))


@router.post("/{project_id}/resume", response_model=ProjectResponse)
def resume_project(project_id: UUID, session: Session = Depends(get_db)) -> ProjectResponse:
    return ProjectResponse.from_domain(project_service.resume_project(session, project_id))
