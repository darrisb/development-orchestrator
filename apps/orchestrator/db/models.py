"""SQLAlchemy mappings for the domain models (build.md sections 7 and 8).

Large payloads -- source snapshots, full logs, patches -- are never stored in
these columns. They live under ARTIFACT_ROOT and are referenced by path plus
hash (section 8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import (
    Complexity,
    EscalationStatus,
    IssueCategory,
    IssueSeverity,
    LessonConfidence,
    LessonStatus,
    MilestoneStatus,
    ModelPurpose,
    ModelRole,
    ProjectStatus,
    ReviewDecision,
    RiskLevel,
    RunStatus,
    TaskStatus,
    TrainingStatus,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from .base import Base, Timestamps, UUIDPrimaryKey
from .types import StrEnumType


class ProjectRow(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_project_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    repository_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(200), nullable=False, default="main")
    worker_profile: Mapped[WorkerProfile] = mapped_column(
        StrEnumType(WorkerProfile, 32), nullable=False, default=WorkerProfile.NODE
    )
    status: Mapped[ProjectStatus] = mapped_column(
        StrEnumType(ProjectStatus, 32), nullable=False, default=ProjectStatus.REGISTERED
    )
    protected_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sensitive_path_exceptions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    generated_path_exceptions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    dependency_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: ``server_default`` matches the migration that added this column, so a
    #: schema built by ``create_all`` accepts the same column-omitting inserts
    #: a migrated schema does.
    dependency_bootstrap_commands: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    approval_gated_categories: Mapped[list[str] | None] = mapped_column(JSON)
    #: The verification profile (section 18): {"build": [...], "lint": [...],
    #: "tests": [...], "security": [...]}. Stored as declared so a re-import
    #: can tell a changed profile from an unchanged one.
    verification_profile: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    milestone_interval: Mapped[int | None] = mapped_column(Integer)

    tasks: Mapped[list[TaskRow]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    milestones: Mapped[list[MilestoneRow]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class TaskRow(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "external_task_id"),
        Index("ix_tasks_project_status", "project_id", "status"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    external_task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    section: Mapped[int | None] = mapped_column(Integer)
    instructions: Mapped[str | None] = mapped_column(Text)
    complexity: Mapped[Complexity] = mapped_column(
        StrEnumType(Complexity, 16), nullable=False, default=Complexity.MEDIUM
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        StrEnumType(RiskLevel, 16), nullable=False, default=RiskLevel.LOW
    )
    status: Mapped[TaskStatus] = mapped_column(
        StrEnumType(TaskStatus, 32), nullable=False, default=TaskStatus.PENDING
    )
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    verify_commands: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    files_to_inspect: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    files_to_modify: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    files_to_create: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_review_cycles: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_runtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_files_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    max_diff_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=1200)

    #: An accepted candidate commit that is not in the cumulative integration
    #: baseline (concern 51). Runtime state rather than a declarative field, and
    #: on the task rather than on the run because it is the *task* a dependent
    #: one is waiting for: readiness reads it without a join, which is what lets
    #: the invariant be checked on every scheduling pass.
    unintegrated_commit: Mapped[str | None] = mapped_column(String(64))

    project: Mapped[ProjectRow] = relationship(back_populates="tasks")
    runs: Mapped[list[TaskRunRow]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskRunRow(UUIDPrimaryKey, Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "run_number"),
        Index("ix_task_runs_status", "status"),
    )

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[RunStatus] = mapped_column(
        StrEnumType(RunStatus, 32), nullable=False, default=RunStatus.PENDING
    )
    external_run_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    coder_model_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL")
    )
    worker_image: Mapped[str | None] = mapped_column(String(300))
    starting_commit: Mapped[str | None] = mapped_column(String(64))
    candidate_commit: Mapped[str | None] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(String(300))
    context_hash: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    failure_reason: Mapped[str | None] = mapped_column(String(50))
    artifact_path: Mapped[str | None] = mapped_column(String(1024))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_runtime_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The fencing token for execution ownership (concern 67). Every dispatch
    #: of this run increments it, and every durable checkpoint validates the
    #: executor's copy against it, so an executor whose run was recovered
    #: underneath it can no longer commit progress.
    execution_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    #: The dispatch currently executing this run, or NULL when no executor
    #: holds it. Set when a dispatch acquires ownership and cleared when that
    #: dispatch returns, so it is a *held/not held* marker and never a liveness
    #: claim: a process killed mid-dispatch leaves it set, and recovery then
    #: refuses until an operator says otherwise.
    execution_owner: Mapped[str | None] = mapped_column(String(64))
    execution_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    task: Mapped[TaskRow] = relationship(back_populates="runs")
    verifications: Mapped[list[VerificationRunRow]] = relationship(
        back_populates="task_run", cascade="all, delete-orphan"
    )
    reviews: Mapped[list[ReviewRow]] = relationship(
        back_populates="task_run", cascade="all, delete-orphan"
    )


class VerificationRunRow(UUIDPrimaryKey, Base):
    __tablename__ = "verification_runs"

    task_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    verification_type: Mapped[VerificationType] = mapped_column(
        StrEnumType(VerificationType, 32), nullable=False
    )
    command: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[VerificationStatus] = mapped_column(
        StrEnumType(VerificationStatus, 32), nullable=False
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_artifact: Mapped[str | None] = mapped_column(String(1024))
    stderr_artifact: Mapped[str | None] = mapped_column(String(1024))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task_run: Mapped[TaskRunRow] = relationship(back_populates="verifications")


class ReviewRow(UUIDPrimaryKey, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
    )

    task_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_provider: Mapped[str] = mapped_column(String(100), nullable=False)
    reviewer_model: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[ReviewDecision] = mapped_column(
        StrEnumType(ReviewDecision, 32), nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    risk: Mapped[RiskLevel | None] = mapped_column(StrEnumType(RiskLevel, 16))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task_run: Mapped[TaskRunRow] = relationship(back_populates="reviews")
    issues: Mapped[list[ReviewIssueRow]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


class ReviewIssueRow(UUIDPrimaryKey, Base):
    __tablename__ = "review_issues"

    review_id: Mapped[UUID] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[IssueSeverity] = mapped_column(StrEnumType(IssueSeverity, 16), nullable=False)
    category: Mapped[IssueCategory] = mapped_column(StrEnumType(IssueCategory, 32), nullable=False)
    file: Mapped[str | None] = mapped_column(String(1024))
    line: Mapped[int | None] = mapped_column(Integer)
    requirement_id: Mapped[str | None] = mapped_column(String(100))
    problem: Mapped[str] = mapped_column(Text, nullable=False)
    required_fix: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    review: Mapped[ReviewRow] = relationship(back_populates="issues")


class LessonRow(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "lessons"
    __table_args__ = (
        Index("ix_lessons_retrieval", "language", "framework", "category"),
        # Retrieval filters on status first and orders by evidence, so the
        # status is the leading column of the index that serves it.
        Index("ix_lessons_status_evidence", "status", "occurrences", "times_applied"),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'retired')",
            name="lesson_status_valid",
        ),
        CheckConstraint("occurrences >= 1", name="lesson_occurrences_positive"),
    )

    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    language: Mapped[str | None] = mapped_column(String(50))
    framework: Mapped[str | None] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_review_issue_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("review_issues.id", ondelete="SET NULL")
    )
    source_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    #: The finding this lesson generalises, stored the way
    #: ``domain.review.issue_fingerprint`` identifies one: the requirement it
    #: cites, the file it points at, and the category. Holding the triple is
    #: what lets a *second* occurrence find this lesson and increment
    #: ``occurrences`` instead of proposing a duplicate, which is how rule 2
    #: ("prefer recurring") has anything to count.
    requirement_id: Mapped[str | None] = mapped_column(String(100))
    source_file: Mapped[str | None] = mapped_column(String(1024))
    #: The run that first raised the finding this lesson summarises. A second
    #: occurrence increments ``occurrences`` on the existing row rather than
    #: proposing a duplicate, so this is what makes a repeat findable.
    source_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL")
    )
    #: The most recent run whose finding was counted in ``occurrences``. Holding
    #: the run that last counted is what makes the count mean *distinct* runs:
    #: comparing against ``source_run_id`` alone would let one run contribute
    #: twice, and a count that inflates also inflates confidence.
    last_seen_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL")
    )
    confidence: Mapped[LessonConfidence] = mapped_column(
        StrEnumType(LessonConfidence, 16), nullable=False, default=LessonConfidence.LOW
    )
    #: Distinct runs that have raised this same finding (section 32 rule 2).
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: ``proposed`` rows are visible to a person and invisible to a coder: only
    #: ``approved`` is retrieved, which is the enforcement of section 32's
    #: rules rather than a convention about them.
    status: Mapped[LessonStatus] = mapped_column(
        StrEnumType(LessonStatus, 16), nullable=False, default=LessonStatus.PROPOSED
    )
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    times_retrieved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    times_applied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TrainingExampleRow(UUIDPrimaryKey, Base):
    """A preserved accepted run (build.md section 34).

    Capture and selection are different acts. Section 34 says not to train on
    every accepted example, so this row is written ``captured`` and a future
    curation process is what moves it to ``selected``; nothing in V1 does.
    """

    __tablename__ = "training_examples"
    __table_args__ = (
        UniqueConstraint("task_run_id"),
        Index("ix_training_examples_status", "status"),
        Index("ix_training_examples_project", "project_id", "created_at"),
        CheckConstraint(
            "status IN ('captured', 'selected', 'excluded')",
            name="training_status_valid",
        ),
    )

    task_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    external_project_id: Mapped[str] = mapped_column(String(100), nullable=False)
    external_task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    external_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Relative to ``ARTIFACT_ROOT``, as every artifact path is, so the
    #: example moves with the root and is not tied to one mount point.
    artifact_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    coder_model_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL")
    )
    reviewer_model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_cycles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[TrainingStatus] = mapped_column(
        StrEnumType(TrainingStatus, 16), nullable=False, default=TrainingStatus.CAPTURED
    )
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelRow(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("provider", "model_name", "role"),)

    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_model_id: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[ModelRole] = mapped_column(StrEnumType(ModelRole, 32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    context_window: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)


class ModelRunRow(UUIDPrimaryKey, Base):
    __tablename__ = "model_runs"

    task_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[UUID] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[ModelPurpose] = mapped_column(StrEnumType(ModelPurpose, 32), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        StrEnumType(RunStatus, 32), nullable=False, default=RunStatus.PENDING
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_artifact: Mapped[str | None] = mapped_column(String(1024))
    response_artifact: Mapped[str | None] = mapped_column(String(1024))
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int | None] = mapped_column(Integer)
    review_cycle: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HumanEscalationRow(UUIDPrimaryKey, Base):
    __tablename__ = "human_escalations"

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_runs.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[EscalationStatus] = mapped_column(
        StrEnumType(EscalationStatus, 32), nullable=False, default=EscalationStatus.OPEN
    )
    resolution: Mapped[str | None] = mapped_column(Text)
    #: Which offered option the person chose, as an ``EscalationIntent``
    #: (concern 32). Stored beside the free text rather than instead of it:
    #: the text is why, and this is what.
    resolution_intent: Mapped[str | None] = mapped_column(String(32))
    #: The Git commit SHA an operator supplied when resolving with
    #: ``COMPLETED_BY_HAND`` (concern 73). NULL means no code change was
    #: needed -- the explicit no-code completion path.
    #:
    #: Always the *historical human source commit*: the commit a person wrote,
    #: exactly as they wrote it. It is never rebased, re-signed or replaced,
    #: even when the orchestrator could not merge it cleanly (see
    #: ``integration_resolution_commit``).
    human_commit: Mapped[str | None] = mapped_column(String(64))
    #: The distinct commit that actually carries ``human_commit``'s work onto
    #: the integration baseline, created by
    #: ``services.human_resolution.authorize_human_resolution`` when the
    #: canonical merge conflicts. NULL on every escalation resolved by a clean
    #: merge, because then the merge commit's own second parent *is* the human
    #: commit and no separate value is needed.
    #:
    #: The pair is the provenance record: which commit a person authored, and
    #: which commit the baseline now contains. Conflating them would either
    #: lose the human commit or pretend the orchestrator's resolution is the
    #: human's own work.
    integration_resolution_commit: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PauseRequestRow(UUIDPrimaryKey, Base):
    """An operator's pause request (build.md section 28).

    A row rather than a status, because a request and a state are different
    things: the status says where a run is and only the workflow may write it
    mid-run, while this may be written at any moment by anyone.
    """

    __tablename__ = "pause_requests"
    __table_args__ = (Index("ix_pause_requests_project_released", "project_id", "released_at"),)

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL pauses the whole project.
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    honoured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MilestoneRow(UUIDPrimaryKey, Base):
    __tablename__ = "milestones"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    after_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[MilestoneStatus] = mapped_column(
        StrEnumType(MilestoneStatus, 32), nullable=False, default=MilestoneStatus.PENDING
    )
    starting_commit: Mapped[str | None] = mapped_column(String(64))
    ending_commit: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[ProjectRow] = relationship(back_populates="milestones")


class RunEventRow(UUIDPrimaryKey, Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_sequence", "task_run_id", "sequence"),)

    #: Monotonic per-run counter. Timestamps alone are not a reliable sort key:
    #: several events can share a timestamp, and the audit trail must stay
    #: strictly chronological.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    model_id: Mapped[UUID | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ArtifactRow(UUIDPrimaryKey, Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_run_kind", "task_run_id", "kind"),)

    task_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --- LangGraph persistence (build.md section 27, phase K item 4) -------------
#
# These two tables are written by ``workflow.checkpoints`` on LangGraph's
# behalf and are the only tables in the schema that hold opaque bytes. They are
# not a second source of truth: every fact a person or an operator needs is in
# the tables above, and a checkpoint that is lost costs a run its place in the
# graph, not its history. They are mapped here rather than in the workflow
# package so that one migration describes the whole schema.


class WorkflowCheckpointRow(Base):
    """One LangGraph checkpoint: a thread's state after a step."""

    __tablename__ = "workflow_checkpoints"
    __table_args__ = (
        Index("ix_workflow_checkpoints_thread", "thread_id", "checkpoint_ns", "checkpoint_id"),
    )

    #: The run this thread belongs to, as a string: LangGraph owns the key
    #: space and requires a string, so this is not a foreign key.
    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(200), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(64))
    #: The serializer's own type tag, kept so a checkpoint written by one
    #: serializer is never handed to another as if it were its own.
    checkpoint_type: Mapped[str] = mapped_column(String(50), nullable=False)
    checkpoint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_type: Mapped[str] = mapped_column(String(50), nullable=False)
    checkpoint_metadata: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowWriteRow(Base):
    """A pending write recorded against a checkpoint.

    LangGraph writes these when a step produces values before the next
    checkpoint exists; on resume they are replayed so a step is not repeated.
    """

    __tablename__ = "workflow_writes"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(200), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(200), nullable=False)
    value_type: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    task_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
