"""Manifest import / re-synchronisation (build.md section 5).

The manifest declares the intended project and task graph. The database stays
authoritative for runtime status once a task exists, so an import never resets
progress: it creates missing tasks and refreshes declarative fields only.

Tasks that have vanished from the manifest are reported, never deleted -- their
runs and events are part of the audit trail (section 40).

Verification commands -- the project's profile (section 18) and each task's own
``verify`` list -- are checked against the worker's command policy here, at
import time (section 12). A manifest naming a command no worker will ever run is
a defect in the manifest, and the moment to say so is when it is imported --
not on the first attempt at the task, where it would look like a run failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.commands import CommandRejected
from ..domain.errors import ManifestError
from ..domain.manifest import ManifestTask, ProjectManifest
from ..domain.models import Project, Task
from ..domain.state_machine import is_active
from ..repositories import ProjectRepository, TaskRepository
from .errors import EntityNotFound
from .manifest_loader import load_repository_manifest
from .projects import create_project
from .scheduler import refresh_readiness
from .worker_service import policy_for_project

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What an import changed. Empty tuples mean "nothing in this category"."""

    project_id: UUID
    external_project_id: str | None
    project_created: bool = False
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    #: Present in the manifest but mid-run, so left untouched this time.
    skipped_active: tuple[str, ...] = ()
    #: Present in the database but absent from the manifest; kept for audit.
    orphaned: tuple[str, ...] = ()
    ready: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def task_count(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)


def import_manifest(
    session: Session,
    manifest: ProjectManifest,
    *,
    project_id: UUID | None = None,
    settings: Settings | None = None,
) -> ImportReport:
    """Create or re-synchronise a project and its tasks from ``manifest``.

    Args:
        project_id: import into this project instead of matching on the
            manifest's ``project.id``. Used when a project was registered
            through the API before its manifest existed.
        settings: supplies the worker command policy the manifest's
            verification commands are checked against.

    Raises:
        EntityNotFound: ``project_id`` was given but no such project exists.
        ManifestError: the project profile or a task declares a verification
            command no worker is permitted to run. Raised before anything is
            written.
    """
    _assert_commands_permitted(manifest, settings or get_settings())
    projects = ProjectRepository(session)
    project, created_project = _resolve_project(session, manifest, project_id)

    warnings: list[str] = []
    warnings.extend(_unverifiable_task_warnings(manifest))
    if manifest.max_parallel_tasks > 1:
        # Honouring this needs parallel scheduling, which V1 does not have.
        warnings.append(
            f"runtime.max_parallel_tasks={manifest.max_parallel_tasks} ignored: "
            "V1 executes one task at a time"
        )

    if not created_project:
        changes = _project_changes(project, manifest)
        if changes:
            project = projects.update_fields(project.id, **changes)
            logger.info(
                "project_resynced", project_id=str(project.id), fields=sorted(changes)
            )

    synced = _sync_tasks(session, project, manifest)
    readiness = refresh_readiness(session, project.id)
    report = replace(
        synced,
        project_created=created_project,
        ready=readiness.ready,
        blocked=tuple(sorted(readiness.blocked)),
        warnings=tuple(warnings),
    )
    logger.info(
        "manifest_imported",
        project_id=str(project.id),
        created=len(report.created),
        updated=len(report.updated),
        unchanged=len(report.unchanged),
        skipped_active=len(report.skipped_active),
        orphaned=len(report.orphaned),
        ready=list(report.ready),
    )
    return report


#: How many task ids a warning names before it stops listing them.
_MAX_LISTED_TASKS = 5


def _unverifiable_task_warnings(manifest: ProjectManifest) -> list[str]:
    """Warn about tasks no command would verify (section 17).

    A warning rather than a refusal: a project may legitimately be imported
    before its build is wired up, and refusing the import would make the
    orchestrator harder to adopt than it needs to be. But a task with no
    build, no test and no ``verify`` of its own produces a verification
    report that *passes without verifying anything* (concern 19), and the
    moment to say so is the import, not the review.
    """
    profile = manifest.verification
    if profile.build or profile.tests:
        return []
    unverifiable = [task.external_id for task in manifest.tasks if not task.verify_commands]
    if not unverifiable:
        return []
    listed = ", ".join(unverifiable[:_MAX_LISTED_TASKS])
    if len(unverifiable) > _MAX_LISTED_TASKS:
        listed += f", and {len(unverifiable) - _MAX_LISTED_TASKS} more"
    return [
        f"{len(unverifiable)} task(s) have no verification at all ({listed}): the "
        f"manifest declares no verification.build and no verification.tests, and "
        f"these tasks declare no verify commands. Their runs will pass "
        f"verification without any command having been executed."
    ]


def _assert_commands_permitted(manifest: ProjectManifest, settings: Settings) -> None:
    """Check every configured command against the worker policy.

    Reported as a ``ManifestError`` rather than a ``CommandRejected``: from the
    importer's point of view this is the same class of problem as a typo in a
    key or a dependency on a task that does not exist, and it deserves the same
    422 rather than a different error shape for the same kind of mistake.
    """
    policy = policy_for_project(manifest.worker_profile, settings)
    for category in ("build", "lint", "tests", "security"):
        try:
            policy.approve_all(getattr(manifest.verification, category))
        except CommandRejected as error:
            raise ManifestError(f"verification.{category}: {error}") from error
    try:
        policy.approve_all(manifest.dependency_bootstrap_commands)
    except CommandRejected as error:
        raise ManifestError(f"dependency_bootstrap_commands: {error}") from error
    for task in manifest.tasks:
        try:
            policy.approve_all(task.verify_commands)
        except CommandRejected as error:
            raise ManifestError(f"tasks.{task.external_id}.verify: {error}") from error


def import_repository_manifest(
    session: Session,
    repository_path: str | Path,
    *,
    project_id: UUID | None = None,
) -> ImportReport:
    """Load ``build.tasks.yaml`` from a repository and import it.

    Raises:
        ManifestError: the manifest is missing or invalid.
        EntityNotFound: ``project_id`` was given but no such project exists.
    """
    return import_manifest(
        session, load_repository_manifest(repository_path), project_id=project_id
    )


def _resolve_project(
    session: Session, manifest: ProjectManifest, project_id: UUID | None
) -> tuple[Project, bool]:
    projects = ProjectRepository(session)
    if project_id is not None:
        project = projects.get(project_id)
        if project is None:
            raise EntityNotFound("Project", project_id)
        return project, False

    existing = projects.get_by_external_id(manifest.external_id)
    if existing is not None:
        return existing, False

    created = create_project(
        session,
        name=manifest.name,
        repository_path=manifest.repository_path,
        external_project_id=manifest.external_id,
        default_branch=manifest.default_branch,
        worker_profile=manifest.worker_profile,
        protected_paths=list(manifest.protected_paths),
        sensitive_path_exceptions=list(manifest.sensitive_path_exceptions),
        generated_path_exceptions=list(manifest.generated_path_exceptions),
        dependency_paths=list(manifest.dependency_paths),
        dependency_bootstrap_commands=list(manifest.dependency_bootstrap_commands),
        approval_gated_categories=(
            list(manifest.approval_gated_categories)
            if manifest.approval_gated_categories is not None
            else None
        ),
        verification=manifest.verification,
        milestone_interval=manifest.milestone_interval,
    )
    return created, True


def _project_changes(project: Project, manifest: ProjectManifest) -> dict[str, object]:
    # Keyed by column, because the verification profile is stored as the
    # mapping the manifest declared and compared in that form.
    desired: dict[str, object] = {
        "name": manifest.name,
        "repository_path": manifest.repository_path,
        "default_branch": manifest.default_branch,
        "worker_profile": manifest.worker_profile,
        "protected_paths": list(manifest.protected_paths),
        "sensitive_path_exceptions": list(manifest.sensitive_path_exceptions),
        "generated_path_exceptions": list(manifest.generated_path_exceptions),
        "dependency_paths": list(manifest.dependency_paths),
        "dependency_bootstrap_commands": list(manifest.dependency_bootstrap_commands),
        "approval_gated_categories": (
            list(manifest.approval_gated_categories)
            if manifest.approval_gated_categories is not None
            else None
        ),
        "verification_profile": manifest.verification.describe(),
        "milestone_interval": manifest.milestone_interval,
    }
    current: dict[str, object] = {
        **{key: getattr(project, key) for key in desired if key != "verification_profile"},
        "verification_profile": project.verification.describe(),
    }
    return {key: value for key, value in desired.items() if current[key] != value}


def _desired_task_fields(task: ManifestTask) -> dict[str, object]:
    """Declarative fields the manifest owns. Status is deliberately absent."""
    return {
        "title": task.title,
        "section": task.section,
        "instructions": task.instructions,
        "complexity": task.complexity,
        "risk_level": task.risk_level,
        "depends_on": list(task.depends_on),
        "verify_commands": list(task.verify_commands),
        "files_to_inspect": list(task.files_to_inspect),
        "files_to_modify": list(task.files_to_modify),
        "files_to_create": list(task.files_to_create),
        "max_attempts": task.limits.max_attempts,
        "max_review_cycles": task.limits.max_review_cycles,
        "max_runtime_minutes": task.limits.max_runtime_minutes,
        "max_files_changed": task.limits.max_files_changed,
        "max_diff_lines": task.limits.max_diff_lines,
    }


def _current_task_fields(task: Task) -> dict[str, object]:
    return {
        "title": task.title,
        "section": task.section,
        "instructions": task.instructions,
        "complexity": task.complexity,
        "risk_level": task.risk_level,
        "depends_on": list(task.depends_on),
        "verify_commands": list(task.verify_commands),
        "files_to_inspect": list(task.files_to_inspect),
        "files_to_modify": list(task.files_to_modify),
        "files_to_create": list(task.files_to_create),
        "max_attempts": task.limits.max_attempts,
        "max_review_cycles": task.limits.max_review_cycles,
        "max_runtime_minutes": task.limits.max_runtime_minutes,
        "max_files_changed": task.limits.max_files_changed,
        "max_diff_lines": task.limits.max_diff_lines,
    }


def _sync_tasks(session: Session, project: Project, manifest: ProjectManifest) -> ImportReport:
    tasks = TaskRepository(session)
    existing = {
        task.external_task_id: task for task in tasks.list_for_project(project.id)
    }

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    skipped_active: list[str] = []

    for manifest_task in manifest.tasks:
        current = existing.get(manifest_task.external_id)
        if current is None:
            tasks.add(
                Task(
                    project_id=project.id,
                    external_task_id=manifest_task.external_id,
                    title=manifest_task.title,
                    section=manifest_task.section,
                    instructions=manifest_task.instructions,
                    complexity=manifest_task.complexity,
                    risk_level=manifest_task.risk_level,
                    status=manifest_task.status,
                    depends_on=list(manifest_task.depends_on),
                    verify_commands=list(manifest_task.verify_commands),
                    files_to_inspect=list(manifest_task.files_to_inspect),
                    files_to_modify=list(manifest_task.files_to_modify),
                    files_to_create=list(manifest_task.files_to_create),
                    limits=manifest_task.limits,
                )
            )
            created.append(manifest_task.external_id)
            continue

        if is_active(current.status):
            # Changing the spec under a running task would invalidate its
            # context hash and verification commands mid-flight.
            skipped_active.append(manifest_task.external_id)
            continue

        current_fields = _current_task_fields(current)
        changes = {
            key: value
            for key, value in _desired_task_fields(manifest_task).items()
            if current_fields[key] != value
        }
        if changes:
            tasks.update_fields(current.id, **changes)
            updated.append(manifest_task.external_id)
        else:
            unchanged.append(manifest_task.external_id)

    manifest_ids = {task.external_id for task in manifest.tasks}
    orphaned = sorted(task_id for task_id in existing if task_id not in manifest_ids)

    return ImportReport(
        project_id=project.id,
        external_project_id=project.external_project_id,
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        skipped_active=tuple(skipped_active),
        orphaned=tuple(orphaned),
    )
