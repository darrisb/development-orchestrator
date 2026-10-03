from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import ProjectStatus, WorkerProfile
from ..domain.models import Project
from ..domain.verification import VerificationProfile
from ..services.baseline_correction import BaselineCorrection
from ..services.task_importer import ImportReport


class VerificationCommands(BaseModel):
    """A project's verification profile over the API (build.md section 18)."""

    model_config = ConfigDict(extra="forbid")

    build: list[str] = Field(default_factory=list)
    lint: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    security: list[str] = Field(default_factory=list)

    def to_domain(self) -> VerificationProfile:
        return VerificationProfile(
            build=tuple(self.build),
            lint=tuple(self.lint),
            tests=tuple(self.tests),
            security=tuple(self.security),
        )

    @classmethod
    def from_domain(cls, profile: VerificationProfile) -> VerificationCommands:
        return cls(
            build=list(profile.build),
            lint=list(profile.lint),
            tests=list(profile.tests),
            security=list(profile.security),
        )


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    repository_path: str = Field(min_length=1, max_length=1024)
    external_project_id: str | None = Field(default=None, max_length=100)
    default_branch: str = Field(default="main", min_length=1, max_length=200)
    worker_profile: WorkerProfile = WorkerProfile.NODE
    protected_paths: list[str] = Field(default_factory=list)
    sensitive_path_exceptions: list[str] = Field(default_factory=list)
    generated_path_exceptions: list[str] = Field(default_factory=list)
    dependency_paths: list[str] = Field(default_factory=list)
    approval_gated_categories: list[str] | None = None
    #: Usually left empty here and supplied by the manifest, which is where
    #: section 18 puts a project's commands.
    verification: VerificationCommands = Field(default_factory=lambda: VerificationCommands())
    milestone_interval: int | None = Field(default=None, ge=1)


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    external_project_id: str | None
    repository_path: str
    default_branch: str
    worker_profile: WorkerProfile
    status: ProjectStatus
    protected_paths: list[str]
    sensitive_path_exceptions: list[str]
    generated_path_exceptions: list[str]
    dependency_paths: list[str]
    approval_gated_categories: list[str] | None
    verification: VerificationCommands
    milestone_interval: int | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_domain(cls, project: Project) -> ProjectResponse:
        return cls(
            id=project.id,
            name=project.name,
            external_project_id=project.external_project_id,
            repository_path=project.repository_path,
            default_branch=project.default_branch,
            worker_profile=project.worker_profile,
            status=project.status,
            protected_paths=list(project.protected_paths),
            sensitive_path_exceptions=list(project.sensitive_path_exceptions),
            generated_path_exceptions=list(project.generated_path_exceptions),
            dependency_paths=list(project.dependency_paths),
            approval_gated_categories=project.approval_gated_categories,
            verification=VerificationCommands.from_domain(project.verification),
            milestone_interval=project.milestone_interval,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


class ImportTasksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_path: str | None = Field(
        default=None,
        description="Defaults to build.tasks.yaml at the project's repository path.",
    )


class ImportTasksResponse(BaseModel):
    """Exactly what the import changed, so a re-sync is auditable."""

    project_id: UUID
    external_project_id: str | None
    project_created: bool
    created: list[str]
    updated: list[str]
    unchanged: list[str]
    skipped_active: list[str]
    orphaned: list[str]
    ready: list[str]
    blocked: list[str]
    warnings: list[str]

    @classmethod
    def from_report(cls, report: ImportReport) -> ImportTasksResponse:
        return cls(
            project_id=report.project_id,
            external_project_id=report.external_project_id,
            project_created=report.project_created,
            created=list(report.created),
            updated=list(report.updated),
            unchanged=list(report.unchanged),
            skipped_active=list(report.skipped_active),
            orphaned=list(report.orphaned),
            ready=list(report.ready),
            blocked=list(report.blocked),
            warnings=list(report.warnings),
        )


class BaselineCorrectionRequest(BaseModel):
    """An operator's request to carry a specification fix into the baseline."""

    model_config = ConfigDict(extra="forbid")

    commit_sha: str = Field(
        min_length=1,
        description=(
            "The operator's correction commit. Any revision Git resolves; the "
            "full SHA is what is recorded."
        ),
    )
    reason: str = Field(
        min_length=1,
        description="Why this correction is being applied. Recorded in the provenance.",
    )
    requested_by: str = Field(
        min_length=1,
        description="Operator identity. Recorded in the provenance.",
    )


class BaselineCorrectionResponse(BaseModel):
    """Exactly what the correction moved, so the operation is auditable.

    ``applied`` and ``already_applied`` are separate fields rather than one
    tri-state: a replay has to be distinguishable from a fresh application
    without the caller parsing prose, and ``applied=false,
    already_applied=true`` is the truthful reading of "it was already there".
    """

    project_id: UUID
    applied: bool
    already_applied: bool
    previous_sha: str
    baseline_sha: str
    correction_sha: str
    merged_sha: str | None
    changed_paths: list[str]
    reason: str
    requested_by: str
    event_id: UUID | None
    #: What re-synchronising the corrected manifest changed. Absent on a replay.
    tasks: ImportTasksResponse | None

    @classmethod
    def from_correction(cls, correction: BaselineCorrection) -> BaselineCorrectionResponse:
        return cls(
            project_id=correction.project_id,
            applied=correction.applied,
            already_applied=correction.already_applied,
            previous_sha=correction.previous_sha,
            baseline_sha=correction.baseline_sha,
            correction_sha=correction.correction_sha,
            merged_sha=correction.merged_sha,
            changed_paths=list(correction.changed_paths),
            reason=correction.reason,
            requested_by=correction.requested_by,
            event_id=correction.event_id,
            tasks=(
                ImportTasksResponse.from_report(correction.import_report)
                if correction.import_report is not None
                else None
            ),
        )
