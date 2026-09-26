"""Project registration and lifecycle (build.md sections 39 and 5)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import ProjectStatus, WorkerProfile
from ..domain.models import PauseRequest, Project
from ..domain.verification import VerificationProfile
from ..repositories import PauseRequestRepository, ProjectRepository
from .errors import EntityConflict, EntityNotFound

logger = get_logger(__name__)

#: Statuses a paused project may be resumed into.
_RESUMABLE_FROM: frozenset[ProjectStatus] = frozenset({ProjectStatus.PAUSED})


def create_project(
    session: Session,
    *,
    name: str,
    repository_path: str,
    external_project_id: str | None = None,
    default_branch: str = "main",
    worker_profile: WorkerProfile = WorkerProfile.NODE,
    protected_paths: list[str] | None = None,
    sensitive_path_exceptions: list[str] | None = None,
    generated_path_exceptions: list[str] | None = None,
    dependency_paths: list[str] | None = None,
    approval_gated_categories: list[str] | None = None,
    verification: VerificationProfile | None = None,
    milestone_interval: int | None = None,
) -> Project:
    """Register a repository as a managed project.

    Raises:
        EntityConflict: ``external_project_id`` is already registered.
    """
    projects = ProjectRepository(session)
    if external_project_id and projects.get_by_external_id(external_project_id) is not None:
        raise EntityConflict(f"Project {external_project_id} is already registered")

    project = projects.add(
        Project(
            name=name,
            repository_path=repository_path,
            external_project_id=external_project_id,
            default_branch=default_branch,
            worker_profile=worker_profile,
            protected_paths=list(protected_paths or []),
            sensitive_path_exceptions=list(sensitive_path_exceptions or []),
            generated_path_exceptions=list(generated_path_exceptions or []),
            dependency_paths=list(dependency_paths or []),
            approval_gated_categories=approval_gated_categories,
            verification=verification or VerificationProfile(),
            milestone_interval=milestone_interval,
        )
    )
    logger.info(
        "project_registered",
        project_id=str(project.id),
        external_project_id=project.external_project_id,
        repository_path=project.repository_path,
    )
    return project


def get_project(session: Session, project_id: UUID) -> Project:
    """Raises:
    EntityNotFound: no such project.
    """
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise EntityNotFound("Project", project_id)
    return project


def list_projects(session: Session) -> list[Project]:
    return ProjectRepository(session).list()


def pause_project(session: Session, project_id: UUID) -> Project:
    """Stop scheduling new work. Runs already in flight are not interrupted here.

    Raises:
        EntityNotFound: no such project.
        EntityConflict: the project has already finished or failed.
    """
    project = get_project(session, project_id)
    pauses = PauseRequestRepository(session)
    if project.status is ProjectStatus.PAUSED:
        return project
    if project.status in (ProjectStatus.COMPLETE, ProjectStatus.FAILED):
        raise EntityConflict(f"Cannot pause a {project.status} project")
    if not pauses.list_in_force(project_id=project_id):
        pauses.add(PauseRequest(project_id=project_id))
    return ProjectRepository(session).set_status(project_id, ProjectStatus.PAUSED)


def resume_project(session: Session, project_id: UUID) -> Project:
    """Raises:
    EntityNotFound: no such project.
    EntityConflict: the project is not paused.
    """
    project = get_project(session, project_id)
    if project.status not in _RESUMABLE_FROM:
        raise EntityConflict(f"Cannot resume a {project.status} project")
    PauseRequestRepository(session).release(project_id=project_id)
    return ProjectRepository(session).set_status(project_id, ProjectStatus.RUNNING)
