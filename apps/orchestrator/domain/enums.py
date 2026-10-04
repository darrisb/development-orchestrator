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
    #: Terminal, and the only one an in-flight workflow can still race with:
    #: an operator abandons a run from a different transaction than the one
    #: executing it (concern 64).
    ABANDONED = "ABANDONED"


#: Run statuses that mean a run is in flight: opened, or executing.
#:
#: One definition, used by every question that is actually about flight rather
#: than about a name. ``TaskRunRepository.list_incomplete`` filters on it, so
#: recovery never sees a terminal run; concern 65's operator retry refuses
#: while a run of the task is in one of these states, which is deliberately the
#: same set -- a retry must refuse exactly when recovery would consider the
#: task's work unfinished, or the two answers would disagree about whether the
#: task has a run going.
IN_FLIGHT_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PENDING, RunStatus.RUNNING}
)

#: Run statuses an operator may abandon (concern 64).
#:
#: ``ABANDONED`` is deliberately absent, and that is the whole point of this
#: constant. A run that is already abandoned is not an *active abandonable*
#: run; it is an abandoned one, and re-abandoning it is a question about
#: idempotency rather than about eligibility. Carrying ``ABANDONED`` here --
#: as the first cut of ``services.abandon`` did -- made one predicate answer
#: two different questions, and callers that asked the eligibility question
#: got "yes" for a run that was already closed. ``abandon_run`` recognizes an
#: already-abandoned run on its own, before this set is consulted, and returns
#: it without a second event.
#:
#: Every member is in flight. ``SUCCEEDED`` and ``FAILED`` are already
#: terminal, so abandoning them would rewrite a completed run's outcome rather
#: than stop work.
#:
#: An alias rather than a second literal. Abandonability and flight are asked
#: separately -- "may this run be abandoned" is not "is this run in flight",
#: and ``abandon_run`` handles the idempotent case of an already-abandoned run
#: without consulting either -- but for now the only in-flight runs are the
#: only abandonable ones, and two literals would be free to drift apart.
ABANDONABLE_RUN_STATUSES: frozenset[RunStatus] = IN_FLIGHT_RUN_STATUSES


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
    DEPENDENCY_BOOTSTRAP = "DEPENDENCY_BOOTSTRAP"
    BUILD = "BUILD"
    LINT = "LINT"
    TESTS = "TESTS"
    SECURITY = "SECURITY"
    #: The optional runtime/browser contract a project declared (concern 81).
    #: It runs after every command category, because starting an application
    #: that does not build proves nothing, and before the diff checks, because
    #: a running server writes caches into the worktree.
    RUNTIME = "RUNTIME"
    DIFF_POLICY = "DIFF_POLICY"
    #: The cumulative gate over the merged tree (concern 51). The same commands
    #: as the three above, run against a different tree, so they are recorded
    #: under their own types rather than being filed as another candidate check:
    #: "did this task's candidate pass" and "did the baseline still pass after
    #: merging it" are different questions with different answers, and one
    #: ``TESTS`` row cannot say which one it answered.
    INTEGRATION_BUILD = "INTEGRATION_BUILD"
    INTEGRATION_LINT = "INTEGRATION_LINT"
    INTEGRATION_TESTS = "INTEGRATION_TESTS"
    INTEGRATION_SECURITY = "INTEGRATION_SECURITY"


class VerificationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


class VerificationClassification(StrEnum):
    """What a verification outcome means *relative to the known baseline*.

    ``VerificationStatus`` answers "what did this command return". This answers
    the question concern 78 stage 2 exists for: a non-zero test command is not
    by itself evidence that the candidate broke something, because the tree the
    candidate started from may already have been failing. The orchestrator can
    only say so deterministically, from recorded baseline evidence, and when it
    cannot it must say *that* rather than guess.

    So four states, and the asymmetry between the last two is the whole point:
    ``KNOWN_BASELINE_ONLY`` is a positive finding backed by evidence, while
    ``UNCLASSIFIED_FAILURE`` is the absence of one. No failure is ever called
    known because it looked familiar.
    """

    #: Nothing that ran failed. No comparison was needed.
    PASSED = "PASSED"
    #: Verification failed, and every observed failure identity is present in a
    #: valid baseline for the exact tree the candidate started from.
    KNOWN_BASELINE_ONLY = "KNOWN_BASELINE_ONLY"
    #: At least one failure identity is not in that baseline.
    NEW_REGRESSION = "NEW_REGRESSION"
    #: Verification failed and the orchestrator could not establish, from
    #: evidence, that every failure was already known. The fail-closed state:
    #: no baseline, stale provenance, an unparseable runner, a timeout, or a
    #: failing category that has no stable failure identities at all.
    UNCLASSIFIED_FAILURE = "UNCLASSIFIED_FAILURE"


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
    #: An accepted candidate that will not go into the cumulative baseline --
    #: a merge conflict against it, or the merged tree failing the project's own
    #: verification (concern 51). Not a failure of the task, which is complete
    #: with a reviewed candidate on its own branch: a failure of *composition*,
    #: and the only one that has to be resolved before anything that depends on
    #: the task may run.
    INTEGRATION_BLOCKED = "INTEGRATION_BLOCKED"
    REVIEW_CHANGES_REQUESTED = "REVIEW_CHANGES_REQUESTED"
    REVIEWER_UNAVAILABLE = "REVIEWER_UNAVAILABLE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    RUNTIME_EXHAUSTED = "RUNTIME_EXHAUSTED"
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
    #: The cumulative accepted baseline moved to include this run (concern 51).
    INTEGRATION_ADVANCED = "INTEGRATION_ADVANCED"
    #: An accepted candidate could not be integrated -- a merge conflict, or
    #: cumulative verification failing on the merged tree. The baseline did not
    #: move, so the next task still starts from the last good state and an
    #: operator has to resolve this one.
    INTEGRATION_BLOCKED = "INTEGRATION_BLOCKED"
    PUSH_COMPLETED = "PUSH_COMPLETED"
    TASK_COMPLETED = "TASK_COMPLETED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    #: An operator intentionally terminated an active durable run (concern 64).
    #: The run is terminal and cannot be resumed.
    RUN_ABANDONED = "RUN_ABANDONED"
    #: An operator authorized a new run for a failed task (concern 65).
    #:
    #: The first event type that is deliberately about a *task* rather than
    #: about a run: the authorization happens before any run exists, so the row
    #: carries a task id and a null ``task_run_id``. That is what the nullable
    #: column is for (see ``services.lessons._lesson_event``), and attaching
    #: this to the abandoned run instead would put a decision on the record of
    #: an execution that had nothing to do with it.
    TASK_RETRY_AUTHORIZED = "TASK_RETRY_AUTHORIZED"
    #: An operator recovered a stranded in-flight run (concern 67).
    #:
    #: About a *run*, unlike ``TASK_RETRY_AUTHORIZED``: the run already exists
    #: and keeps its identity, its history and its number, and what the event
    #: records is that execution ownership moved to a new generation. Exactly
    #: one of these is appended per successful recovery, and it carries the
    #: generation that was fenced and the one that was taken, so a later reader
    #: can tell which durable work belongs to which executor.
    RUN_RECOVERY_AUTHORIZED = "RUN_RECOVERY_AUTHORIZED"
    #: The request that started an in-flight run was cancelled, so the run was
    #: settled deterministically instead of being left to unwind (concern 71).
    #:
    #: About a *run*, and automatic where ``RUN_ABANDONED`` is a person's
    #: decision: the client is gone, but the run is still a durable fact and
    #: the reason it stopped is worth one append-only line next to the attempt
    #: that was interrupted. Exactly one of these is written per settlement.
    RUN_CANCELLED = "RUN_CANCELLED"
    #: An operator's project-specification correction was merged into the
    #: cumulative accepted baseline.
    #:
    #: About a *project*, which no other event type is: it carries a project id
    #: and a null ``task_run_id`` *and* a null ``task_id``, because the
    #: correction belongs to no task and no execution. That is the point --
    #: attaching it to a task would record a specification fix as if some task
    #: had produced it, which is exactly the false completion provenance this
    #: operation exists to avoid.
    BASELINE_CORRECTION_APPLIED = "BASELINE_CORRECTION_APPLIED"
    #: Concern 79. The run was granted its one bounded verification repair:
    #: a reviewer-driven correction produced a candidate that deterministic
    #: verification rejected, and it happened on the last attempt the task's
    #: budget allowed.
    #:
    #: This is the allowance's book of record, not a log line. The grant is
    #: appended before the repair turn is made and inside the same durable
    #: turn boundary, so a reader -- and a resumed run -- can tell a run that
    #: has spent its allowance from one that still has it. There is at most one
    #: per run; see ``domain.limits.VERIFICATION_REPAIR_ALLOWANCE``.
    VERIFICATION_REPAIR_GRANTED = "VERIFICATION_REPAIR_GRANTED"


class CorrectionSource(StrEnum):
    """Where the instruction a coding turn was given came from (concern 79).

    The fix loop has always handed one ``feedback`` string to the next turn
    without recording which of the three things that write one produced it: a
    deterministic verification report, a reviewer's blocking findings, or a
    person answering an escalation. For routing a correction that did not
    matter -- the text is the text. For concern 79's allowance it is the whole
    question, because the allowance exists for exactly one provenance and must
    not be inferred from an attempt number or from the shape of the text.

    ``CODING`` is the turn whose predecessor never reached a verifier at all --
    a refused edit set, an unusable response. It is kept distinct from
    ``INITIAL`` so that "nothing has been tried yet" and "the last try
    produced nothing to verify" are not the same answer.
    """

    #: The first coding turn of a run: nobody has asked for anything.
    INITIAL = "INITIAL"
    #: A person's answer to an escalation, carried in ``initial_feedback``.
    HUMAN = "HUMAN"
    #: The previous turn failed before verification could run.
    CODING = "CODING"
    #: Deterministic verification evidence: the real command and its output.
    VERIFICATION = "VERIFICATION"
    #: A reviewer's actionable blocking findings.
    REVIEW = "REVIEW"


class WorkerProfile(StrEnum):
    NODE = "node"
    JAVA = "java"
    PYTHON = "python"
