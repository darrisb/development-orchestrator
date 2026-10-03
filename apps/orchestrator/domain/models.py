"""Core domain models (build.md section 7).

Plain dataclasses, framework-independent by design: no FastAPI, SQLAlchemy,
LangGraph, Docker or LLM SDK may appear in this module. Persistence mapping
lives in ``apps.orchestrator.db``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from .enums import (
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
from .escalation import EscalationIntent, EscalationOption
from .model_policy import ModelPolicy
from .verification import VerificationProfile


def _new_id() -> UUID:
    return uuid4()


@dataclass(slots=True)
class TaskLimits:
    """Per-task bounds. Defaults follow build.md section 23."""

    max_attempts: int = 3
    max_review_cycles: int = 3
    max_runtime_minutes: int = 30
    max_files_changed: int = 12
    max_diff_lines: int = 1200


@dataclass(slots=True)
class Project:
    name: str
    repository_path: str
    default_branch: str = "main"
    worker_profile: WorkerProfile = WorkerProfile.NODE
    external_project_id: str | None = None
    status: ProjectStatus = ProjectStatus.REGISTERED
    protected_paths: list[str] = field(default_factory=list)
    sensitive_path_exceptions: list[str] = field(default_factory=list)
    generated_path_exceptions: list[str] = field(default_factory=list)
    dependency_paths: list[str] = field(default_factory=list)
    dependency_bootstrap_commands: list[str] = field(default_factory=list)
    #: ``None`` keeps the conservative built-in approval categories; an
    #: explicit list (including an empty one) is a project-owned override.
    approval_gated_categories: list[str] | None = None
    #: The commands the verification pipeline runs, per category (section 18).
    #: Project-controlled and declared in the manifest: the orchestrator runs
    #: exactly these, and a model never adds one.
    verification: VerificationProfile = field(default_factory=VerificationProfile)
    #: Which registered model each role runs on (section 31). Declared in the
    #: manifest and persisted here; an empty policy means no preference, and
    #: the role's first enabled provider is used as it always was.
    model_policy: ModelPolicy = field(default_factory=ModelPolicy)
    milestone_interval: int | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Task:
    project_id: UUID
    external_task_id: str
    title: str
    section: int | None = None
    instructions: str | None = None
    complexity: Complexity = Complexity.MEDIUM
    risk_level: RiskLevel = RiskLevel.LOW
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    verify_commands: list[str] = field(default_factory=list)
    #: The task specification contract (section 6): what the coder should read
    #: and what it is allowed to touch. Repository-relative paths or globs.
    files_to_inspect: list[str] = field(default_factory=list)
    files_to_modify: list[str] = field(default_factory=list)
    files_to_create: list[str] = field(default_factory=list)
    limits: TaskLimits = field(default_factory=TaskLimits)
    #: An accepted candidate commit of this task that is *not* in the project's
    #: cumulative integration baseline (concern 51). ``None`` -- the normal case
    #: -- means nothing of this task is outstanding: either it integrated, or it
    #: produced nothing to integrate. While it is set, the task is COMPLETE and
    #: its work is real, but the tree the next task starts from does not contain
    #: it, so nothing that depends on this task may run.
    unintegrated_commit: str | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_integrated(self) -> bool:
        """Whether this task's accepted output is in the baseline.

        True for a task that never produced anything to integrate, which is the
        honest answer: the invariant is about a tree containing a dependency's
        work, and a dependency with no work of its own cannot be missing from it.
        """
        return self.unintegrated_commit is None

    @property
    def allowed_paths(self) -> list[str]:
        """Paths this task may write. The scope guard's allowance (section 20).

        An empty list means the task declared no boundary, which is a weaker
        specification rather than permission to change everything: the guard
        falls back to the diff-size limits.
        """
        return [*self.files_to_modify, *self.files_to_create]

    @property
    def declared_paths(self) -> list[str]:
        """Everything the task named, read-only entries included."""
        return [*self.files_to_inspect, *self.files_to_modify, *self.files_to_create]


@dataclass(slots=True)
class TaskRun:
    task_id: UUID
    run_number: int
    attempt_number: int = 1
    review_cycle: int = 0
    status: RunStatus = RunStatus.PENDING
    external_run_id: str | None = None
    coder_model_id: UUID | None = None
    worker_image: str | None = None
    starting_commit: str | None = None
    candidate_commit: str | None = None
    branch_name: str | None = None
    context_hash: str | None = None
    prompt_version: str | None = None
    failure_reason: str | None = None
    artifact_path: str | None = None
    id: UUID = field(default_factory=_new_id)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    active_runtime_ms: int = 0
    active_started_at: datetime | None = None
    #: Concern 67. The fencing token an executor must still own to persist.
    execution_generation: int = 0
    #: Concern 67. The dispatch holding this run, or ``None`` when none does.
    execution_owner: str | None = None
    execution_started_at: datetime | None = None


@dataclass(slots=True)
class VerificationRun:
    task_run_id: UUID
    verification_type: VerificationType
    command: str
    status: VerificationStatus
    exit_code: int | None = None
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    duration_ms: int | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None


@dataclass(slots=True)
class ReviewIssue:
    severity: IssueSeverity
    category: IssueCategory
    problem: str
    required_fix: str
    review_id: UUID | None = None
    file: str | None = None
    line: int | None = None
    requirement_id: str | None = None
    resolved: bool = False
    id: UUID = field(default_factory=_new_id)

    @property
    def is_blocking(self) -> bool:
        return self.severity.is_blocking


@dataclass(slots=True)
class Review:
    task_run_id: UUID
    reviewer_provider: str
    reviewer_model: str
    decision: ReviewDecision
    summary: str
    confidence: float | None = None
    risk: RiskLevel | None = None
    cycle: int = 1
    issues: list[ReviewIssue] = field(default_factory=list)
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None

    @property
    def blocking_issues(self) -> list[ReviewIssue]:
        """Only blocking issues force a retry (build.md section 22)."""
        return [issue for issue in self.issues if issue.is_blocking and not issue.resolved]


@dataclass(slots=True)
class Lesson:
    category: str
    title: str
    lesson: str
    project_id: UUID | None = None
    language: str | None = None
    framework: str | None = None
    tags: list[str] = field(default_factory=list)
    source_review_issue_id: UUID | None = None
    source_task_id: UUID | None = None
    #: The finding's identity, as ``domain.review.issue_fingerprint`` states it:
    #: the requirement it cites, the file it points at, and ``category``. Stored
    #: so a recurring finding finds this lesson rather than proposing a
    #: duplicate, which is what makes ``occurrences`` mean anything.
    requirement_id: str | None = None
    source_file: str | None = None
    #: The run that first raised it. ``source_review_issue_id`` says which
    #: finding, this says which run the finding came from -- and it is what
    #: makes a second occurrence findable, so the same lesson does not get
    #: proposed twice under two rows.
    source_run_id: UUID | None = None
    #: Defaults to the same value ``domain.lessons.lesson_confidence(1)``
    #: derives, so a lesson built by hand and one extracted from a single
    #: finding agree about how much recurrence is behind it. A default of
    #: ``MEDIUM`` would have a lesson with no recurrence at all claiming some.
    confidence: LessonConfidence = LessonConfidence.LOW
    #: How many distinct runs have raised this same finding (section 32 rule
    #: 2). The single most useful number about a lesson: it is the difference
    #: between a pattern and a one-off, and nothing else here can tell them
    #: apart.
    occurrences: int = 1
    #: Proposed lessons are not retrievable. See ``is_retrievable``.
    status: LessonStatus = LessonStatus.PROPOSED
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    times_retrieved: int = 0
    times_applied: int = 0
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_global(self) -> bool:
        """Project-specific lessons never become global implicitly (section 32)."""
        return self.project_id is None

    @property
    def is_retrievable(self) -> bool:
        """Only an approved lesson is ever put in front of a coder.

        Section 32's six rules are only enforceable if a candidate has to be
        promoted first, so retrieval reads one status and not the row. A
        proposed lesson is a question for a person, and a retired one is a
        known mistake.
        """
        return self.status is LessonStatus.APPROVED


@dataclass(slots=True)
class TrainingExample:
    """One accepted run, preserved for a future dataset-curation process.

    Section 34 asks for eleven things to be kept for an accepted task. The
    bytes are under ``TRAINING_ROOT``; this row is the index the specification
    asks for and the curation state, because *do not automatically train on
    every accepted example* means capture and selection are different acts and
    only the first one happens here.
    """

    task_run_id: UUID
    project_id: UUID
    task_id: UUID
    external_project_id: str
    external_task_id: str
    external_run_id: str
    artifact_path: str
    outcome: str
    manifest_sha256: str
    coder_model_id: UUID | None = None
    reviewer_model: str | None = None
    prompt_version: str | None = None
    attempts: int = 1
    review_cycles: int = 0
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    status: TrainingStatus = TrainingStatus.CAPTURED
    #: Why an example was excluded, when it was. ``None`` while captured or
    #: selected: the interesting case is the exclusion nobody can explain.
    exclusion_reason: str | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None

    @property
    def selectable(self) -> bool:
        """Whether a curation process may choose this example (section 34)."""
        return self.status is not TrainingStatus.EXCLUDED


@dataclass(slots=True)
class Model:
    provider: str
    model_name: str
    role: ModelRole
    endpoint: str
    external_model_id: str | None = None
    enabled: bool = True
    timeout_seconds: int = 600
    context_window: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ModelRun:
    task_run_id: UUID
    model_id: UUID
    purpose: ModelPurpose
    status: RunStatus = RunStatus.PENDING
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    prompt_artifact: str | None = None
    response_artifact: str | None = None
    #: The failure, sanitized and bounded. ``None`` on a call that returned.
    #: Present on every call that raised, including one whose turn was rolled
    #: back: the call happened, and whether the workflow kept the rest of the
    #: turn says nothing about that.
    error_detail: str | None = None
    #: Which coding attempt made this call. The turn is charged when the call is
    #: made, so this is what a rollback must not be allowed to erase.
    attempt: int | None = None
    #: Which review cycle the call answered.
    review_cycle: int | None = None
    id: UUID = field(default_factory=_new_id)
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class HumanEscalation:
    task_id: UUID
    reason: str
    summary: str
    task_run_id: UUID | None = None
    #: The decisions offered, each carrying the intent the workflow acts on
    #: (section 24, concern 32). An escalation written before intents existed
    #: restores with an empty list and can only be dismissed.
    options: list[EscalationOption] = field(default_factory=list)
    status: EscalationStatus = EscalationStatus.OPEN
    resolution: str | None = None
    #: Which option the person chose. ``None`` on an open escalation, and on a
    #: dismissal, which is an answer that asks for nothing to happen.
    resolution_intent: EscalationIntent | None = None
    #: The Git commit SHA the operator supplied when resolving with
    #: ``COMPLETED_BY_HAND`` (concern 73). ``None`` means no code change was
    #: needed -- the explicit no-code completion path.
    #:
    #: The *historical human source commit*, always: the commit a person wrote.
    #: It is preserved unchanged even when it could not be merged cleanly, in
    #: which case ``integration_resolution_commit`` records the separate commit
    #: that carries its work onto the baseline.
    human_commit: str | None = None
    #: The distinct commit that carries ``human_commit``'s work onto the
    #: integration baseline after an operator resolved a merge conflict.
    #: ``None`` when the canonical merge succeeded, when the resolution was the
    #: explicit no-code path, or on an escalation that has not been reconciled.
    integration_resolution_commit: str | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(slots=True)
class Milestone:
    project_id: UUID
    name: str
    after_task_count: int
    status: MilestoneStatus = MilestoneStatus.PENDING
    starting_commit: str | None = None
    ending_commit: str | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class PauseRequest:
    """An operator's request that work stop at the next safe boundary.

    Kept apart from ``TaskStatus.PAUSED`` on purpose (section 28). The status
    is where a run *is*, and during a run only the workflow may write it;
    setting it from an API call mid-attempt is precisely the corruption
    section 28 warns about. This is the request, which anyone may write at any
    time and which the workflow honours where it is safe to.

    A request scoped to a project pauses every task in it; one scoped to a
    task pauses that task alone.
    """

    project_id: UUID
    task_id: UUID | None = None
    reason: str | None = None
    requested_by: str | None = None
    #: Set when the request is lifted. An unreleased request is in force.
    released_at: datetime | None = None
    #: Set when a run actually stopped for it, so an operator can tell a
    #: request that has taken effect from one still waiting for a boundary.
    honoured_at: datetime | None = None
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None

    @property
    def in_force(self) -> bool:
        return self.released_at is None


@dataclass(slots=True)
class RunEvent:
    """Append-only workflow audit record (build.md section 40)."""

    task_run_id: UUID | None
    event_type: str
    sequence: int = 0
    project_id: UUID | None = None
    task_id: UUID | None = None
    attempt: int | None = None
    worker_id: str | None = None
    model_id: UUID | None = None
    payload: dict[str, object] = field(default_factory=dict)
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None


@dataclass(slots=True)
class Artifact:
    """Metadata for a large object stored on disk under ARTIFACT_ROOT."""

    task_run_id: UUID
    kind: str
    path: str
    sha256: str
    size_bytes: int
    id: UUID = field(default_factory=_new_id)
    created_at: datetime | None = None
