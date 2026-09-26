"""The verification contract (build.md sections 17, 18 and 49).

Section 17 fixes the order -- scope, build, lint, targeted tests, security,
diff policy, and only then review -- and one rule that everything here exists
to serve:

> Never accept a model's statement that a command passed. The orchestrator
> must execute it.

So this module holds no claim a model made. It holds the commands the
*project* declared (section 18), the order they run in, and the arithmetic
that turns an exit code into a verdict a workflow can act on. Running them is
``services.verification``; this is what "passed" means.

Three decisions worth stating, because each one will look surprising once:

* **A category with no commands is ``SKIPPED``, not ``PASSED``.** A project
  that declares no lint step has not passed lint, and a report that said
  otherwise would let "all green" mean two different things.
* **A timeout is the category's own failure, not an infrastructure error.** A
  test suite that never finishes is a defect in the candidate far more often
  than it is a slow machine, and the evidence -- the command, the ceiling, the
  output up to the kill -- is exactly what the coder needs. The *worker* dying
  is different, raises, and is ``WORKER_FAILURE``'s business (section 49).
* **The feedback is assembled from captured output, never summarised by a
  model.** What goes back to the coder is what the command printed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from uuid import UUID

from .enums import (
    FailureReason,
    ScopePolicyDecision,
    VerificationStatus,
    VerificationType,
)

#: The pipeline, in section 17's order. ``SCOPE`` runs before anything is
#: executed -- a candidate that already broke its allowance should not get a
#: worker -- and ``DIFF_POLICY`` runs last, after the commands, because a
#: build can create files the coder never wrote.
PIPELINE_ORDER: tuple[VerificationType, ...] = (
    VerificationType.SCOPE,
    VerificationType.BUILD,
    VerificationType.LINT,
    VerificationType.TESTS,
    VerificationType.SECURITY,
    VerificationType.DIFF_POLICY,
)

#: Categories whose steps are project-configured commands run in a worker. The
#: other two are decided by the orchestrator from the diff itself.
COMMAND_CATEGORIES: tuple[VerificationType, ...] = (
    VerificationType.BUILD,
    VerificationType.LINT,
    VerificationType.TESTS,
    VerificationType.SECURITY,
)

#: One failure reason per category (section 49). Every failure is classified;
#: none falls through to a generic handler.
FAILURE_REASONS: Mapping[VerificationType, FailureReason] = {
    VerificationType.SCOPE: FailureReason.SCOPE_VIOLATION,
    VerificationType.BUILD: FailureReason.BUILD_FAILED,
    VerificationType.LINT: FailureReason.LINT_FAILED,
    VerificationType.TESTS: FailureReason.TEST_FAILED,
    VerificationType.SECURITY: FailureReason.SECURITY_FAILED,
    VerificationType.DIFF_POLICY: FailureReason.SCOPE_VIOLATION,
}

#: Statuses that mean the pipeline stops here.
FAILED_STATUSES: frozenset[VerificationStatus] = frozenset(
    {VerificationStatus.FAILED, VerificationStatus.ERROR, VerificationStatus.TIMEOUT}
)

#: Lines of captured output sent back with a failure. Enough to hold a stack
#: trace or a compiler's first complaints; not the whole suite.
FEEDBACK_OUTPUT_LINES = 60

#: Steps whose text goes into the feedback. A coder cannot act on the output
#: of the command that ran *before* the one that broke.
MAX_FEEDBACK_STEPS = 3


@dataclass(frozen=True, slots=True)
class VerificationProfile:
    """The commands a project declared, per category (section 18).

    Section 18's rule is the point: *commands must be project-controlled
    rather than invented freely by the model.* This dataclass is the whole of
    what may run, it comes from the manifest, and nothing adds to it at
    runtime.

    ``security`` is the hook section 19 asks for: a dependency audit or a
    scanner the project already trusts. The checks the orchestrator makes on
    the diff itself are in ``domain.security`` and run whether or not a
    project configured a command here.
    """

    build: tuple[str, ...] = ()
    lint: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    security: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any((self.build, self.lint, self.tests, self.security))

    def commands_for(self, category: VerificationType) -> tuple[str, ...]:
        return {
            VerificationType.BUILD: self.build,
            VerificationType.LINT: self.lint,
            VerificationType.TESTS: self.tests,
            VerificationType.SECURITY: self.security,
        }.get(category, ())

    def all_commands(self) -> tuple[str, ...]:
        return (*self.build, *self.lint, *self.tests, *self.security)

    def with_task_commands(self, commands: Sequence[str]) -> VerificationProfile:
        """Add a task's own ``verify`` commands to the test category.

        Section 6 has every task declare its verification commands and
        section 17 asks for *targeted* tests, which reads like an invitation
        to let a task replace the project's suite with a narrower one. It is
        not, and the specification's own example manifest shows why: build.md
        section 5 writes ``verify: [npm run compile, npm test]``, so a task
        that replaced the suite would run the compiler as a "test", and a
        task whose list happened to hold only ``npm run compile`` would run
        no tests at all while the report said ``TESTS: PASSED``.

        So a task's commands are **added**, never substituted: a task can ask
        for more verification than the project requires and never for less.
        Commands the profile already runs are dropped rather than repeated --
        that is what makes ``verify: [npm run compile, npm test]`` cost one
        extra run of the tests and nothing else.
        """
        already = set(self.all_commands())
        extra = tuple(
            command
            for command in dict.fromkeys(command.strip() for command in commands)
            if command and command not in already
        )
        return self if not extra else replace(self, tests=(*self.tests, *extra))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> VerificationProfile:
        """Build a profile from stored or parsed data, ignoring nothing.

        Unknown keys are the caller's problem to reject (the manifest parser
        does); this is the lenient direction, used when reading a row back.
        """
        if not payload:
            return cls()
        return cls(
            build=_string_tuple(payload.get("build")),
            lint=_string_tuple(payload.get("lint")),
            tests=_string_tuple(payload.get("tests")),
            security=_string_tuple(payload.get("security")),
        )

    def describe(self) -> dict[str, list[str]]:
        return {
            "build": list(self.build),
            "lint": list(self.lint),
            "tests": list(self.tests),
            "security": list(self.security),
        }


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """One check and what it returned.

    A step is the unit the ``verification_runs`` table stores (section 7), so
    everything a later reader needs -- what ran, what it returned, where the
    log is -- is here rather than only in the log file.
    """

    verification_type: VerificationType
    status: VerificationStatus
    #: The command, or a short description for the checks nothing executes.
    command: str
    exit_code: int | None = None
    duration_ms: int = 0
    log_artifact: str | None = None
    #: Whether a worker actually executed something. False for the checks the
    #: orchestrator decides from the diff and for a skipped category, which is
    #: what lets ``VerificationReport.verified`` tell "everything passed" from
    #: "nothing ran".
    executed: bool = False
    #: Why, for a human. Empty for an uneventful pass.
    detail: str = ""
    #: The tail of what the command printed, already redacted by the worker.
    output: str = ""

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES

    @property
    def failure_reason(self) -> FailureReason | None:
        return FAILURE_REASONS[self.verification_type] if self.failed else None

    def describe(self) -> dict[str, object]:
        return {
            "verification_type": self.verification_type.value,
            "status": self.status.value,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "log_artifact": self.log_artifact,
            "executed": self.executed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Everything the pipeline decided about one candidate.

    ``passed`` is not "the code is good" -- that is the reviewer's question
    (section 21). It is "every deterministic check the project declared was
    executed by the orchestrator and returned a pass", which is the thing a
    model is never asked to assert.
    """

    task_run_id: UUID
    external_task_id: str
    attempt: int = 1
    steps: tuple[VerificationStep, ...] = ()
    #: ``REQUIRE_REVIEW`` findings from the scope guard and the security
    #: checks: not failures, but not something to merge unseen either.
    human_review_reasons: tuple[str, ...] = ()
    artifacts: Mapping[str, str] = field(default_factory=dict)

    @property
    def performed(self) -> tuple[VerificationStep, ...]:
        """Every check that reached a verdict, executed or decided."""
        return tuple(
            step for step in self.steps if step.status is not VerificationStatus.SKIPPED
        )

    @property
    def commands_run(self) -> tuple[VerificationStep, ...]:
        """The steps a worker actually executed."""
        return tuple(step for step in self.steps if step.executed)

    @property
    def unverified_categories(self) -> tuple[VerificationType, ...]:
        """Command categories for which nothing was executed."""
        ran = {step.verification_type for step in self.commands_run}
        return tuple(category for category in COMMAND_CATEGORIES if category not in ran)

    @property
    def failures(self) -> tuple[VerificationStep, ...]:
        return tuple(step for step in self.steps if step.failed)

    @property
    def passed(self) -> bool:
        """Nothing that ran failed.

        Not the same as ``verified``: a project that configured no commands
        passes this trivially, having proven nothing. A caller deciding
        whether a candidate has earned a reviewer's time wants ``verified``.
        """
        return not self.failures

    @property
    def verified(self) -> bool:
        """Nothing failed **and** the orchestrator executed something.

        Section 17's guarantee in one property. ``passed`` alone is a weaker
        claim than it looks: with an empty profile every command category is
        ``SKIPPED``, nothing fails, and a caller that checked ``passed``
        would send a completely unverified candidate to review.
        """
        return self.passed and bool(self.commands_run)

    @property
    def requires_human_review(self) -> bool:
        return bool(self.human_review_reasons)

    @property
    def failure_reason(self) -> FailureReason | None:
        """The first failure's reason: the pipeline stops at the first one."""
        failures = self.failures
        return failures[0].failure_reason if failures else None

    @property
    def feedback(self) -> str | None:
        """What to send back to the coder (section 17), or ``None`` on a pass."""
        return render_feedback(self.failures) if self.failures else None

    def step_for(self, category: VerificationType) -> VerificationStep | None:
        """The first step of ``category``, or ``None`` if it never ran.

        A category can hold several steps -- one per configured command, and,
        for ``SECURITY``, the orchestrator's own scan beside any audit command
        the project declared. Use ``steps_for`` when all of them matter.
        """
        steps = self.steps_for(category)
        return steps[0] if steps else None

    def steps_for(self, category: VerificationType) -> tuple[VerificationStep, ...]:
        return tuple(step for step in self.steps if step.verification_type is category)

    def summary(self) -> str:
        if self.passed and not self.verified:
            skipped = ", ".join(
                category.value.casefold() for category in self.unverified_categories
            )
            return f"nothing was verified: no command configured for {skipped}"
        if self.passed:
            return (
                f"verification passed ({len(self.commands_run)} command(s) executed, "
                f"{len(self.performed)} check(s) in total)"
            )
        first = self.failures[0]
        return (
            f"{first.verification_type.value.casefold()} {first.status.value.casefold()}: "
            f"{first.detail or first.command}"
        )

    def describe(self) -> dict[str, object]:
        return {
            "task_run_id": str(self.task_run_id),
            "task": self.external_task_id,
            "attempt": self.attempt,
            "passed": self.passed,
            "verified": self.verified,
            "unverified_categories": [
                category.value for category in self.unverified_categories
            ],
            "failure_reason": self.failure_reason.value if self.failure_reason else None,
            "requires_human_review": self.requires_human_review,
            "human_review_reasons": list(self.human_review_reasons),
            "summary": self.summary(),
            "steps": [step.describe() for step in self.steps],
        }


def classify_command(
    category: VerificationType,
    *,
    exit_code: int | None,
    timed_out: bool,
) -> VerificationStatus:
    """The verdict for one executed command.

    A timeout is ``TIMEOUT`` rather than ``FAILED`` because it produced no
    exit code and therefore no verdict of its own: recording a failure with
    ``exit_code=0`` would let a caller that checks only the code read a killed
    suite as a pass. Both are failures of the same category, so both carry the
    same ``FailureReason`` -- see the module docstring.
    """
    if timed_out:
        return VerificationStatus.TIMEOUT
    if exit_code is None:
        return VerificationStatus.ERROR
    return VerificationStatus.PASSED if exit_code == 0 else VerificationStatus.FAILED


def status_for_decision(decision: ScopePolicyDecision) -> VerificationStatus:
    """The scope guard's three-way decision as a verification status.

    ``REQUIRE_REVIEW`` passes: it is a request for a human's attention
    (section 20), not a defect the coder can fix by trying again.
    """
    return (
        VerificationStatus.FAILED
        if decision is ScopePolicyDecision.BLOCK
        else VerificationStatus.PASSED
    )


def render_feedback(failures: Sequence[VerificationStep]) -> str:
    """The deterministic failure, written for the coder (section 17).

    No interpretation and no advice beyond the instruction to fix it: the
    command, what it returned, and what it printed. A model that is told what
    the compiler said can fix the code; a model that is told "the build
    failed" can only guess.
    """
    blocks: list[str] = [
        "Verification failed. The orchestrator ran the project's configured "
        "commands against your change; these are their real results. Fix the "
        "cause and return the corrected files."
    ]
    for step in failures[:MAX_FEEDBACK_STEPS]:
        header = f"[{step.verification_type.value}] {step.command}"
        outcome = (
            f"timed out after {step.duration_ms}ms"
            if step.status is VerificationStatus.TIMEOUT
            else f"exit code {step.exit_code}"
            if step.exit_code is not None
            else step.status.value.casefold()
        )
        body = tail(step.output, FEEDBACK_OUTPUT_LINES)
        blocks.append(
            "\n".join(
                part
                for part in (
                    header,
                    f"-> {outcome}" + (f": {step.detail}" if step.detail else ""),
                    body,
                )
                if part
            )
        )
    return "\n\n".join(blocks)


def tail(text: str, lines: int) -> str:
    """The last ``lines`` lines of ``text``, marked when anything was dropped."""
    if not text:
        return ""
    split = text.splitlines()
    if len(split) <= lines:
        return "\n".join(split)
    dropped = len(split) - lines
    return "\n".join([f"[... {dropped} earlier line(s) omitted ...]", *split[-lines:]])


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(entry).strip() for entry in value if str(entry).strip())
    return ()


__all__ = [
    "COMMAND_CATEGORIES",
    "FAILED_STATUSES",
    "FAILURE_REASONS",
    "FEEDBACK_OUTPUT_LINES",
    "MAX_FEEDBACK_STEPS",
    "PIPELINE_ORDER",
    "VerificationProfile",
    "VerificationReport",
    "VerificationStep",
    "classify_command",
    "render_feedback",
    "status_for_decision",
    "tail",
]
