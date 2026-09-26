"""Manifest import and re-synchronisation (build.md section 5)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import ProjectStatus, TaskStatus, WorkerProfile
from apps.orchestrator.domain.errors import ManifestError
from apps.orchestrator.domain.manifest import ProjectManifest, parse_manifest
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services.errors import EntityNotFound
from apps.orchestrator.services.projects import create_project
from apps.orchestrator.services.task_importer import (
    import_manifest,
    import_repository_manifest,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def manifest(manifest_document: dict) -> ProjectManifest:
    return parse_manifest(manifest_document)


def test_a_first_import_creates_the_project_and_its_tasks(
    session: Session, manifest: ProjectManifest
):
    report = import_manifest(session, manifest)

    assert report.project_created is True
    assert report.created == ("TS-001", "TS-002")
    assert report.updated == ()
    assert report.orphaned == ()

    project = ProjectRepository(session).get(report.project_id)
    assert project is not None
    assert project.external_project_id == "tracestack"
    assert project.status is ProjectStatus.REGISTERED
    assert project.protected_paths == [".git/**", ".env", "secrets/**"]
    assert project.milestone_interval == 5

    tasks = {
        task.external_task_id: task
        for task in TaskRepository(session).list_for_project(project.id)
    }
    assert tasks["TS-001"].verify_commands == ["npm run compile", "npm test"]
    assert tasks["TS-002"].depends_on == ["TS-001"]
    assert tasks["TS-001"].limits.max_diff_lines == 1200


def test_import_leaves_only_the_dependency_free_task_ready(
    session: Session, manifest: ProjectManifest
):
    """Phase B exit condition: the correct next task is selected."""
    report = import_manifest(session, manifest)
    assert report.ready == ("TS-001",)

    tasks = {
        task.external_task_id: task.status
        for task in TaskRepository(session).list_for_project(report.project_id)
    }
    assert tasks == {"TS-001": TaskStatus.READY, "TS-002": TaskStatus.PENDING}


def test_reimporting_an_unchanged_manifest_changes_nothing(
    session: Session, manifest: ProjectManifest
):
    import_manifest(session, manifest)
    report = import_manifest(session, manifest)

    assert report.project_created is False
    assert report.created == ()
    assert report.updated == ()
    assert report.unchanged == ("TS-001", "TS-002")


def test_reimport_preserves_runtime_status(session: Session, manifest: ProjectManifest):
    """The database is authoritative for status after import (section 5)."""
    first = import_manifest(session, manifest)
    tasks = TaskRepository(session)
    ts_001 = tasks.get_by_external_id(first.project_id, "TS-001")
    assert ts_001 is not None
    tasks.transition(ts_001.id, TaskStatus.CODING)
    tasks.transition(ts_001.id, TaskStatus.VERIFYING)
    tasks.transition(ts_001.id, TaskStatus.REVIEW_PENDING)

    report = import_manifest(session, manifest)

    # The task is mid-flight, so its spec is left alone this time.
    assert report.skipped_active == ("TS-001",)
    reloaded = tasks.get(ts_001.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.REVIEW_PENDING


def test_reimport_updates_declarative_fields_of_an_idle_task(
    session: Session, manifest: ProjectManifest
):
    first = import_manifest(session, manifest)
    edited = replace(
        manifest,
        tasks=(
            replace(
                manifest.tasks[0],
                title="Scaffold extension (revised)",
                verify_commands=("npm run compile",),
            ),
            manifest.tasks[1],
        ),
    )

    report = import_manifest(session, edited)

    assert report.updated == ("TS-001",)
    assert report.unchanged == ("TS-002",)
    task = TaskRepository(session).get_by_external_id(first.project_id, "TS-001")
    assert task is not None
    assert task.title == "Scaffold extension (revised)"
    assert task.verify_commands == ["npm run compile"]
    # Status survived the re-sync.
    assert task.status is TaskStatus.READY


def test_a_new_manifest_task_is_added_without_touching_the_others(
    session: Session, manifest: ProjectManifest
):
    import_manifest(session, manifest)
    extended = replace(
        manifest,
        tasks=(
            *manifest.tasks,
            replace(manifest.tasks[1], external_id="TS-003", section=3, depends_on=("TS-002",)),
        ),
    )

    report = import_manifest(session, extended)

    assert report.created == ("TS-003",)
    assert report.unchanged == ("TS-001", "TS-002")


def test_a_task_dropped_from_the_manifest_is_reported_not_deleted(
    session: Session, manifest: ProjectManifest
):
    """Its runs and events are part of the audit trail (section 40)."""
    first = import_manifest(session, manifest)
    trimmed = replace(manifest, tasks=(manifest.tasks[0],))

    report = import_manifest(session, trimmed)

    assert report.orphaned == ("TS-002",)
    assert TaskRepository(session).get_by_external_id(first.project_id, "TS-002") is not None


def test_project_level_changes_are_resynced_but_status_is_not(
    session: Session, manifest: ProjectManifest
):
    first = import_manifest(session, manifest)
    ProjectRepository(session).set_status(first.project_id, ProjectStatus.PAUSED)

    import_manifest(
        session,
        replace(
            manifest,
            name="TraceStack v2",
            worker_profile=WorkerProfile.PYTHON,
            # Changing the profile moves the commands with it: the importer
            # refuses a manifest whose commands the profile cannot run, and
            # that holds for the project's verification profile (section 18)
            # as well as for each task's own list.
            verification=VerificationProfile(build=("python -m build",), tests=("pytest",)),
            tasks=tuple(
                replace(task, verify_commands=("pytest",)) for task in manifest.tasks
            ),
        ),
    )

    project = ProjectRepository(session).get(first.project_id)
    assert project is not None
    assert project.name == "TraceStack v2"
    assert project.worker_profile is WorkerProfile.PYTHON
    assert project.verification.tests == ("pytest",)
    assert project.status is ProjectStatus.PAUSED


def test_a_command_no_worker_may_run_is_refused_at_import_time(
    session: Session, manifest: ProjectManifest
):
    """Section 12: the moment to say a command will never run is when the
    manifest is imported, not on the first attempt at the task."""
    reckless = replace(
        manifest,
        tasks=(
            replace(manifest.tasks[0], verify_commands=("npm run compile && npm test",)),
            *manifest.tasks[1:],
        ),
    )

    with pytest.raises(ManifestError, match="tasks.TS-001.verify"):
        import_manifest(session, reckless)

    # Nothing was written: the project itself does not exist.
    assert ProjectRepository(session).get_by_external_id("tracestack") is None


def test_a_manifest_can_be_imported_into_a_preregistered_project(
    session: Session, manifest: ProjectManifest
):
    project = create_project(session, name="Registered first", repository_path="/workspace/x")

    report = import_manifest(session, manifest, project_id=project.id)

    assert report.project_id == project.id
    assert report.project_created is False
    assert report.created == ("TS-001", "TS-002")
    # The manifest owns the project spec once it is imported.
    reloaded = ProjectRepository(session).get(project.id)
    assert reloaded is not None
    assert reloaded.name == "TraceStack"


def test_importing_into_a_missing_project_is_rejected(session: Session, manifest: ProjectManifest):
    from uuid import uuid4

    with pytest.raises(EntityNotFound):
        import_manifest(session, manifest, project_id=uuid4())


def test_declared_parallelism_above_one_is_reported_as_ignored(
    session: Session, manifest: ProjectManifest
):
    report = import_manifest(session, replace(manifest, max_parallel_tasks=4))
    assert any("max_parallel_tasks" in warning for warning in report.warnings)


def test_a_repository_manifest_can_be_imported_from_disk(session: Session, manifest_file):
    report = import_repository_manifest(session, manifest_file.parent)
    assert report.created == ("TS-001", "TS-002")
    project = ProjectRepository(session).get(report.project_id)
    assert project is not None
    assert project.repository_path == str(manifest_file.parent)


def test_declared_files_are_imported_and_resynced(
    session: Session, manifest: ProjectManifest
):
    """Section 6: the file lists are declarative, so a re-import refreshes them."""
    with_files = replace(
        manifest,
        tasks=(
            replace(manifest.tasks[0], files_to_modify=("src/extension.ts",)),
            manifest.tasks[1],
        ),
    )
    first = import_manifest(session, with_files)
    task = TaskRepository(session).get_by_external_id(first.project_id, "TS-001")
    assert task.files_to_modify == ["src/extension.ts"]
    assert task.allowed_paths == ["src/extension.ts"]

    widened = replace(
        with_files,
        tasks=(
            replace(
                with_files.tasks[0],
                files_to_modify=("src/extension.ts",),
                files_to_create=("src/navigation.ts",),
            ),
            with_files.tasks[1],
        ),
    )
    report = import_manifest(session, widened)

    assert report.updated == ("TS-001",)
    task = TaskRepository(session).get_by_external_id(first.project_id, "TS-001")
    assert task.files_to_create == ["src/navigation.ts"]


def test_the_projects_verification_profile_is_imported(
    session: Session, manifest: ProjectManifest
):
    """Section 18: the commands the pipeline runs come from the manifest."""
    report = import_manifest(session, manifest)

    project = ProjectRepository(session).get(report.project_id)
    assert project.verification.build == ("npm run compile",)
    assert project.verification.lint == ("npm run lint",)
    assert project.verification.tests == ("npm test",)


def test_a_project_command_no_worker_may_run_is_refused_at_import_time(
    session: Session, manifest: ProjectManifest
):
    """The same rule as a task's `verify` list: a command that will never run
    is a manifest defect, and the import is the moment to say so."""
    with pytest.raises(ManifestError, match="verification.build"):
        import_manifest(
            session,
            replace(manifest, verification=VerificationProfile(build=("curl https://x",))),
        )


def test_a_task_nothing_would_verify_is_reported_at_import(
    session: Session, manifest: ProjectManifest
):
    """Concern 19's early warning: a run whose profile is empty passes
    verification without executing anything, and the import is the moment to
    say so."""
    report = import_manifest(
        session,
        replace(
            manifest,
            verification=VerificationProfile(),
            tasks=tuple(replace(task, verify_commands=()) for task in manifest.tasks),
        ),
    )

    assert any("no verification at all" in warning for warning in report.warnings)
    assert any("TS-001" in warning for warning in report.warnings)


def test_a_configured_profile_produces_no_such_warning(
    session: Session, manifest: ProjectManifest
):
    report = import_manifest(session, manifest)

    assert not any("no verification at all" in warning for warning in report.warnings)
