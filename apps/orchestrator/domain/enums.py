"""Domain enumerations.

These are part of the domain contract and must not depend on FastAPI,
SQLAlchemy, LangGraph, Docker, or any LLM SDK.
"""

from __future__ import annotations

from enum import StrEnum


class ProjectStatus(StrEnum):
    REGISTERED = "REGISTERED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class TaskStatus(StrEnum):
    """Task lifecycle states (build.md section 26)."""

    PENDING = "PENDING"
    READY = "READY"
    PLANNING = "PLANNING"
    CODING = "CODING"
    VERIFYING = "VERIFYING"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    PAUSED = "PAUSED"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class Complexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VerificationType(StrEnum):
    SCOPE = "SCOPE"
    BUILD = "BUILD"
    LINT = "LINT"
    TESTS = "TESTS"
    SECURITY = "SECURITY"
    DIFF_POLICY = "DIFF_POLICY"


class VerificationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


class ReviewDecision(StrEnum):
    """Allowed reviewer decisions (build.md section 21)."""

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class IssueSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def is_blocking(self) -> bool:
        """Only blocking issues force a retry (build.md section 22)."""
        return self in _BLOCKING_SEVERITIES


_BLOCKING_SEVERITIES = frozenset(
    {IssueSeverity.CRITICAL, IssueSeverity.HIGH, IssueSeverity.MEDIUM}
)


class IssueCategory(StrEnum):
    REQUIREMENT = "requirement"
    ARCHITECTURE = "architecture"
    CORRECTNESS = "correctness"
    SECURITY = "security"
    TESTING = "testing"
    STYLE = "style"
    OBSERVATION = "observation"


class ScopePolicyDecision(StrEnum):
    """Scope guard outcomes (build.md section 20)."""

    ALLOW = "ALLOW"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"
    BLOCK = "BLOCK"


class ModelRole(StrEnum):
    CODER = "coder"
    REVIEWER = "reviewer"
    PLANNER = "planner"


class ModelPurpose(StrEnum):
    PLAN = "PLAN"
    CODE = "CODE"
    FIX = "FIX"
    REVIEW = "REVIEW"
    LESSON_EXTRACTION = "LESSON_EXTRACTION"


class EscalationStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class MilestoneStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class LessonConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LessonStatus(StrEnum):
    """Where a lesson sits between a finding and guidance (build.md section 32).

    A candidate is not a lesson yet. Section 32 says a lesson is a *reusable
    engineering instruction* extracted from a review/fix cycle, and the six
    rules below it are all about restraint: do not create one from every
    comment, prefer the recurring ones, keep each traceable to its source, and
    never let a project-specific lesson become a global one.

    That restraint is only enforceable if a candidate has to be *promoted*
    before anything is sent to a coder. So extraction writes ``PROPOSED``,
    approval writes ``APPROVED``, and retrieval reads ``APPROVED`` and nothing
    else. A person approving is not ceremony here: it is the only step at which
    "is this general enough to be reusable?" is actually answered.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    #: A person looked at it and declined. Kept rather than deleted: it is the
    #: record that the finding was considered, and re-proposing the same
    #: finding every cycle is exactly what a rejected lesson exists to stop.
    REJECTED = "rejected"
    #: Withdrawn after approval, when a lesson turns out to have been wrong.
    RETIRED = "retired"


class TrainingStatus(StrEnum):
    """Curation state of a captured training example (build.md section 34).

    Section 34 is explicit: *do not automatically train on every accepted
    example.* Capture is not selection, so capture writes ``CAPTURED`` and a
    future curation process is what moves an example to ``SELECTED``. A
    captured example that nobody curates is a file on disk, which is the safe
    direction: an uncurated set can be thrown away, a set nobody reviewed
    cannot.
    """

    CAPTURED = "captured"
    SELECTED = "selected"
    EXCLUDED = "excluded"


class FailureReason(StrEnum):
    """Failure classification (build.md section 49).

    Each class carries a deterministic policy; see ``failure_policy``.
    """

    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    INVALID_MODEL_RESPONSE = "INVALID_MODEL_RESPONSE"
    BUILD_FAILED = "BUILD_FAILED"
    LINT_FAILED = "LINT_FAILED"
    TEST_FAILED = "TEST_FAILED"
    SECURITY_FAILED = "SECURITY_FAILED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    GIT_CONFLICT = "GIT_CONFLICT"
    REVIEW_CHANGES_REQUESTED = "REVIEW_CHANGES_REQUESTED"
    REVIEWER_UNAVAILABLE = "REVIEWER_UNAVAILABLE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    WORKER_FAILURE = "WORKER_FAILURE"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"


class FailureAction(StrEnum):
    RETRY = "RETRY"
    SEND_TO_CODER = "SEND_TO_CODER"
    ROLLBACK = "ROLLBACK"
    PAUSE = "PAUSE"
    ESCALATE = "ESCALATE"


class RunEventType(StrEnum):
    """Meaningful workflow events (build.md section 40)."""

    TASK_SELECTED = "TASK_SELECTED"
    WORKSPACE_CREATED = "WORKSPACE_CREATED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    PLAN_CREATED = "PLAN_CREATED"
    CODING_STARTED = "CODING_STARTED"
    CODING_COMPLETED = "CODING_COMPLETED"
    BUILD_STARTED = "BUILD_STARTED"
    BUILD_FAILED = "BUILD_FAILED"
    TESTS_PASSED = "TESTS_PASSED"
    REVIEW_STARTED = "REVIEW_STARTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    FIX_STARTED = "FIX_STARTED"
    APPROVED = "APPROVED"
    LESSON_CAPTURED = "LESSON_CAPTURED"
    #: A lesson candidate extracted from a review/fix cycle, waiting for a
    #: person (section 32).
    LESSON_PROPOSED = "LESSON_PROPOSED"
    LESSON_APPROVED = "LESSON_APPROVED"
    LESSON_REJECTED = "LESSON_REJECTED"
    #: The run's own summary, written so the run directory answers section 9's
    #: questions without reading the database.
    OUTCOME_RECORDED = "OUTCOME_RECORDED"
    #: A training example preserved for an accepted run (section 34).
    TRAINING_CAPTURED = "TRAINING_CAPTURED"
    COMMIT_CREATED = "COMMIT_CREATED"
    PUSH_COMPLETED = "PUSH_COMPLETED"
    TASK_COMPLETED = "TASK_COMPLETED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class WorkerProfile(StrEnum):
    NODE = "node"
    JAVA = "java"
    PYTHON = "python"
