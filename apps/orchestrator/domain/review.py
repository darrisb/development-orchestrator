"""The structured review contract and its routing policy (build.md sections 21, 22, 37).

Section 21 fixes the reviewer's answer: three decisions, a confidence, a risk,
a summary, and a list of issues. Section 22 fixes what the reviewer is
answering *about* -- task compliance and architecture compliance -- and adds
the rule everything here is built around:

> Only blocking issues should force a retry.

So this module holds three things and no I/O.

* **The schema**, as the endpoint is asked to satisfy it, and the tolerant
  parse that turns whatever comes back into a ``ReviewResult``.
* **Reconciliation**, because a reviewer can contradict itself. ``APPROVED``
  with a ``CRITICAL`` issue attached is not an approval, and a model that
  writes one is not exercising judgement the orchestrator should defer to.
  The issues win, and the disagreement is recorded rather than smoothed over.
* **Routing**, which is where section 37 lands: confidence is advice, and the
  human-approval gate is policy. Section 21 says it outright -- *confidence
  must not override mandatory human-review policy* -- so the gate is applied
  after the decision is read, never as an input to it.

One asymmetry is deliberate and worth stating before it looks like a bug. The
section 37 gate applies to **acceptance only**. A candidate that touches
authentication and is sent back for changes still goes back to the coder; it
is the *merge* that a human must sign off, not every cycle of the loop.
Gating both would spend a person's attention on watching a model iterate.

Pure: no I/O, no model, no repository, no database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .context import render_bullet_list
from .enums import (
    FailureReason,
    IssueCategory,
    IssueSeverity,
    ReviewDecision,
    RiskLevel,
    TaskStatus,
)
from .escalation import EscalationIntent, EscalationOption
from .models import Review, ReviewIssue
from .scope import SensitiveCategory

#: Bumped whenever the schema or the parse below changes. An outcome must be
#: attributable to the contract that produced it (section 34).
REVIEW_SCHEMA_VERSION = "review/1"

#: The JSON schema sent to the reviewer. Field names are section 21's example,
#: exactly, so the specification's sample response validates unchanged.
REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "summary", "issues"],
    "properties": {
        "taskId": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": ["APPROVED", "CHANGES_REQUESTED", "HUMAN_REVIEW_REQUIRED"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "category", "problem", "requiredFix"],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [category.value for category in IssueCategory],
                    },
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "requirementId": {"type": "string"},
                    "problem": {"type": "string"},
                    "requiredFix": {"type": "string"},
                },
            },
        },
    },
}

#: Where an unrecognised severity lands. Blocking, on purpose: a reviewer that
#: invented a severity has still said something is wrong, and reading an
#: unknown word as ``INFO`` would let a defect through on a spelling mistake.
UNKNOWN_SEVERITY = IssueSeverity.MEDIUM

#: Where an unrecognised category lands. Categories only steer reporting and
#: lesson extraction, so the safe default is the general one.
UNKNOWN_CATEGORY = IssueCategory.CORRECTNESS

#: Sensitive areas that may not be accepted automatically (section 37).
#: ``LOCKFILE`` and ``DEPENDENCY_MANIFEST`` are here for section 37's "major
#: dependency upgrades": the guard cannot tell a patch bump from a major one,
#: and a human glancing at a version number is cheaper than the alternative.
APPROVAL_GATED_CATEGORIES: frozenset[SensitiveCategory] = frozenset(
    {
        SensitiveCategory.SECURITY,
        SensitiveCategory.PAYMENT,
        SensitiveCategory.MIGRATION,
        SensitiveCategory.CI_DEPLOYMENT,
        SensitiveCategory.DEPENDENCY_MANIFEST,
        SensitiveCategory.LOCKFILE,
    }
)

#: Issues quoted in a correction prompt. Section 23 asks for the *actionable
#: blocking* issues; a coder handed twenty findings at once fixes none of them
#: well, and the ones past this point come back on the next cycle anyway.
MAX_FEEDBACK_ISSUES = 8


class MalformedReview(ValueError):
    """A reviewer answered, but not with a review.

    Distinct from a low-quality review: this is a payload with no decision, or
    a decision that is not one of the three. The workflow treats it as an
    invalid model response rather than as a verdict.
    """


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """What every ``ReviewProvider`` returns, whatever it talked to (section 21).

    ``warnings`` is the record of what reconciliation had to change. A review
    whose stated decision disagreed with its own issues is still usable, but
    the disagreement is evidence about the reviewer and belongs in the
    artifact rather than in a log line nobody reads.
    """

    decision: ReviewDecision
    summary: str
    issues: tuple[ReviewIssue, ...] = ()
    confidence: float | None = None
    risk: RiskLevel | None = None
    #: What the reviewer said the task was. Compared against the real task id;
    #: a mismatch is a warning, not a rejection, because the diff is what was
    #: actually reviewed and the identifier is the model's transcription of it.
    reported_task_id: str | None = None
    warnings: tuple[str, ...] = ()
    #: Provider identity, filled in by the provider rather than the model.
    provider_id: str = ""
    model_name: str = ""
    duration_ms: int = 0

    @property
    def blocking_issues(self) -> tuple[ReviewIssue, ...]:
        """The issues that force a retry (section 22)."""
        return tuple(issue for issue in self.issues if issue.is_blocking)

    @property
    def observations(self) -> tuple[ReviewIssue, ...]:
        """Non-blocking findings: worth recording, never worth a retry."""
        return tuple(issue for issue in self.issues if not issue.is_blocking)

    def to_review(self, *, task_run_id, cycle: int = 1) -> Review:
        """The persistable record (section 7's ``Review``)."""
        return Review(
            task_run_id=task_run_id,
            reviewer_provider=self.provider_id,
            reviewer_model=self.model_name,
            decision=self.decision,
            summary=self.summary,
            confidence=self.confidence,
            risk=self.risk,
            cycle=cycle,
            issues=list(self.issues),
        )

    def describe(self) -> dict[str, object]:
        """The ``review.json`` artifact."""
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "decision": self.decision.value,
            "confidence": self.confidence,
            "risk": self.risk.value if self.risk else None,
            "summary": self.summary,
            "reported_task_id": self.reported_task_id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "duration_ms": self.duration_ms,
            "issues": [_issue_entry(issue) for issue in self.issues],
            "blocking_issues": len(self.blocking_issues),
            "warnings": list(self.warnings),
        }


def parse_review(
    payload: Mapping[str, object],
    *,
    external_task_id: str | None = None,
) -> ReviewResult:
    """Read a ``ReviewResult`` out of a parsed reviewer response.

    Tolerant about shape, strict about the decision. A missing confidence, an
    unknown severity, an issue without a file: all recoverable, all recorded
    in ``warnings``. A payload with no recognisable decision is not, because
    there is nothing to route on and guessing one would mean the orchestrator
    deciding whether code is acceptable -- which is the one thing section 21
    asks a reviewer for.

    Raises:
        MalformedReview: no usable decision, or ``issues`` is not a list.
    """
    warnings: list[str] = []
    decision = _decision(payload.get("decision"))

    issues_value = payload.get("issues", [])
    if isinstance(issues_value, (Mapping, str)):
        raise MalformedReview(
            f"'issues' must be a JSON array, got {type(issues_value).__name__}"
        )
    issues = _issues(issues_value, warnings)

    reported = payload.get("taskId")
    reported_task_id = reported.strip() if isinstance(reported, str) else None
    if external_task_id and reported_task_id and reported_task_id != external_task_id:
        warnings.append(
            f"the reviewer reported task '{reported_task_id}' but reviewed "
            f"{external_task_id}"
        )

    summary = payload.get("summary")
    summary_text = summary.strip() if isinstance(summary, str) else ""
    if not summary_text:
        summary_text = "(the reviewer gave no summary)"
        warnings.append("the review carries no summary")

    decision, reconciliation = reconcile(decision, issues)
    warnings.extend(reconciliation)

    return ReviewResult(
        decision=decision,
        summary=summary_text,
        issues=issues,
        confidence=_confidence(payload.get("confidence"), warnings),
        risk=_risk(payload.get("risk"), warnings),
        reported_task_id=reported_task_id,
        warnings=tuple(warnings),
    )


def reconcile(
    decision: ReviewDecision, issues: Sequence[ReviewIssue]
) -> tuple[ReviewDecision, list[str]]:
    """Make a review agree with itself, and say where it did not.

    Three contradictions are possible and each has one safe reading:

    * **Approved with blocking issues.** The issues win. A reviewer that lists
      a ``CRITICAL`` defect and then approves has described a defect; taking
      the word over the content would let anything through behind a confident
      sentence.
    * **Changes requested with no issues at all.** There is nothing to put in
      a correction prompt, so the coder would be sent an empty instruction and
      would burn a cycle guessing. That is a judgement nobody can act on
      automatically, so it becomes ``HUMAN_REVIEW_REQUIRED``.
    * **Changes requested with only non-blocking issues.** Left alone. The
      severities may simply be under-graded, and the findings are real and
      actionable; the fix loop sends them rather than approving over the
      reviewer's stated decision.
    """
    blocking = [issue for issue in issues if issue.is_blocking]
    if decision is ReviewDecision.APPROVED and blocking:
        severities = ", ".join(sorted({issue.severity.value for issue in blocking}))
        return ReviewDecision.CHANGES_REQUESTED, [
            f"the reviewer approved while reporting {len(blocking)} blocking "
            f"issue(s) ({severities}); the issues decide"
        ]
    if decision is ReviewDecision.CHANGES_REQUESTED and not issues:
        return ReviewDecision.HUMAN_REVIEW_REQUIRED, [
            "the reviewer requested changes without naming any issue, so there "
            "is nothing to send back to the coder"
        ]
    return decision, []


# ------------------------------------------------------------------- routing


@dataclass(frozen=True, slots=True)
class HumanApprovalPolicy:
    """When a human must sign off before a change is accepted (section 37).

    Attributes:
        gated_categories: sensitive areas whose presence in the diff blocks an
            automatic acceptance.
        min_confidence: a reviewer less sure than this does not get to approve
            on its own. ``None`` disables the check. It never works the other
            way: high confidence cannot clear a gate.
        max_deleted_files: section 37's "deleting significant files". More
            deletions than this in one task is a human's decision.
    """

    gated_categories: frozenset[SensitiveCategory] = APPROVAL_GATED_CATEGORIES
    min_confidence: float | None = 0.6
    max_deleted_files: int = 3

    def describe(self) -> dict[str, object]:
        return {
            "gated_categories": sorted(
                category.value for category in self.gated_categories
            ),
            "min_confidence": self.min_confidence,
            "max_deleted_files": self.max_deleted_files,
        }


@dataclass(frozen=True, slots=True)
class ReviewRouting:
    """Where one review sends the task, and why.

    The decision here can differ from the reviewer's: ``decision`` is what the
    orchestrator acted on and ``reviewer_decision`` is what the model said.
    Keeping both means an escalation can explain itself without a reader
    having to reconstruct the policy from the code.
    """

    decision: ReviewDecision
    reviewer_decision: ReviewDecision
    task_status: TaskStatus
    failure_reason: FailureReason | None = None
    blocking_issues: tuple[ReviewIssue, ...] = ()
    human_review_reasons: tuple[str, ...] = ()
    feedback: str | None = None
    retry_exhausted: bool = False
    #: The event this routing should emit (section 40).
    escalated_by_policy: bool = False

    @property
    def approved(self) -> bool:
        return self.decision is ReviewDecision.APPROVED

    @property
    def needs_human(self) -> bool:
        return self.decision is ReviewDecision.HUMAN_REVIEW_REQUIRED

    @property
    def needs_fix(self) -> bool:
        return self.decision is ReviewDecision.CHANGES_REQUESTED

    def summary(self) -> str:
        if self.decision is self.reviewer_decision:
            return f"{self.decision}: {len(self.blocking_issues)} blocking issue(s)"
        return (
            f"{self.decision} (reviewer said {self.reviewer_decision}): "
            + "; ".join(self.human_review_reasons or ("policy override",))
        )

    def describe(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reviewer_decision": self.reviewer_decision.value,
            "task_status": self.task_status.value,
            "failure_reason": self.failure_reason.value if self.failure_reason else None,
            "blocking_issues": [_issue_entry(issue) for issue in self.blocking_issues],
            "human_review_reasons": list(self.human_review_reasons),
            "retry_exhausted": self.retry_exhausted,
            "escalated_by_policy": self.escalated_by_policy,
        }


def route_review(
    result: ReviewResult,
    *,
    cycle: int,
    max_review_cycles: int,
    policy: HumanApprovalPolicy | None = None,
    sensitive: Sequence[SensitiveCategory] = (),
    deleted_paths: Sequence[str] = (),
    pending_review_reasons: Sequence[str] = (),
) -> ReviewRouting:
    """Turn a review into the task's next state (sections 21, 22, 23 and 37).

    Args:
        cycle: which review cycle this is, counting from 1.
        max_review_cycles: the task's ceiling (section 23). Reaching it turns
            a further change request into an escalation rather than a loop.
        sensitive: sensitive categories the scope guard found in the diff.
        deleted_paths: files the candidate deletes.
        pending_review_reasons: ``REQUIRE_REVIEW`` findings the verification
            pipeline already raised (scope guard, security scan). They are
            carried, not re-derived: the pipeline saw the whole diff and this
            function sees only what it is handed.

    Returns:
        The routing. Nothing is persisted and no state is changed here.
    """
    approval_policy = policy or HumanApprovalPolicy()
    gates = _approval_gates(
        result, approval_policy, sensitive, deleted_paths, pending_review_reasons
    )

    if result.decision is ReviewDecision.HUMAN_REVIEW_REQUIRED:
        return ReviewRouting(
            decision=ReviewDecision.HUMAN_REVIEW_REQUIRED,
            reviewer_decision=result.decision,
            task_status=TaskStatus.HUMAN_REVIEW,
            failure_reason=FailureReason.HUMAN_DECISION_REQUIRED,
            blocking_issues=result.blocking_issues,
            human_review_reasons=(
                gates or ("the reviewer asked for a human decision",)
            ),
        )

    if result.decision is ReviewDecision.CHANGES_REQUESTED:
        # The section 37 gate is not applied here: it governs acceptance, and
        # nothing is being accepted. What does apply is the cycle ceiling.
        if cycle >= max_review_cycles:
            return ReviewRouting(
                decision=ReviewDecision.HUMAN_REVIEW_REQUIRED,
                reviewer_decision=result.decision,
                task_status=TaskStatus.HUMAN_REVIEW,
                failure_reason=FailureReason.RETRY_EXHAUSTED,
                blocking_issues=result.blocking_issues,
                human_review_reasons=(
                    f"review cycle {cycle} of {max_review_cycles} still requests "
                    f"changes; the task's review budget is spent",
                    *gates,
                ),
                feedback=render_review_feedback(result),
                retry_exhausted=True,
            )
        return ReviewRouting(
            decision=ReviewDecision.CHANGES_REQUESTED,
            reviewer_decision=result.decision,
            task_status=TaskStatus.CHANGES_REQUESTED,
            failure_reason=FailureReason.REVIEW_CHANGES_REQUESTED,
            blocking_issues=result.blocking_issues,
            human_review_reasons=tuple(gates),
            feedback=render_review_feedback(result),
        )

    if gates:
        return ReviewRouting(
            decision=ReviewDecision.HUMAN_REVIEW_REQUIRED,
            reviewer_decision=result.decision,
            task_status=TaskStatus.HUMAN_REVIEW,
            failure_reason=FailureReason.HUMAN_DECISION_REQUIRED,
            human_review_reasons=tuple(gates),
            escalated_by_policy=True,
        )

    return ReviewRouting(
        decision=ReviewDecision.APPROVED,
        reviewer_decision=result.decision,
        task_status=TaskStatus.APPROVED,
    )


def _approval_gates(
    result: ReviewResult,
    policy: HumanApprovalPolicy,
    sensitive: Sequence[SensitiveCategory],
    deleted_paths: Sequence[str],
    pending_review_reasons: Sequence[str],
) -> tuple[str, ...]:
    """Every reason this candidate may not be accepted without a human.

    All of them are collected rather than the first one returned: an
    escalation that names one of three reasons invites a human to clear it and
    be surprised by the next.
    """
    reasons: list[str] = []
    for category in dict.fromkeys(sensitive):
        if category in policy.gated_categories:
            reasons.append(
                f"the change touches {category.value.replace('_', ' ')}, which "
                f"requires human approval before it is accepted (section 37)"
            )
    if len(deleted_paths) > policy.max_deleted_files:
        reasons.append(
            f"the change deletes {len(deleted_paths)} files, more than the "
            f"{policy.max_deleted_files} that may be accepted automatically"
        )
    if (
        policy.min_confidence is not None
        and result.confidence is not None
        and result.confidence < policy.min_confidence
    ):
        reasons.append(
            f"the reviewer's confidence of {result.confidence:.2f} is below the "
            f"{policy.min_confidence:.2f} required to accept without a human"
        )
    reasons.extend(pending_review_reasons)
    return tuple(dict.fromkeys(reasons))


# ------------------------------------------------------------------ feedback


def render_review_feedback(result: ReviewResult) -> str:
    """The correction prompt's content (section 23 item 2).

    Blocking issues only, because only those force a retry. When a review
    requested changes and graded everything below blocking, the non-blocking
    findings are sent instead -- the alternative is a correction prompt with
    nothing in it.
    """
    issues = result.blocking_issues or result.issues
    quoted = issues[:MAX_FEEDBACK_ISSUES]
    lines = [
        "A reviewer read your change against the task's requirements and asked "
        "for corrections. Fix exactly these findings and return the complete "
        "new contents of every file you change.",
        "",
        f"Reviewer summary: {result.summary}",
        "",
    ]
    for index, issue in enumerate(quoted, start=1):
        location = _location(issue)
        header = f"{index}. [{issue.severity}] {issue.category}"
        if issue.requirement_id:
            header += f" (requirement {issue.requirement_id})"
        lines.append(header)
        if location:
            lines.append(f"   Where: {location}")
        lines.append(f"   Problem: {issue.problem}")
        lines.append(f"   Required fix: {issue.required_fix}")
        lines.append("")
    if len(issues) > len(quoted):
        lines.append(
            f"({len(issues) - len(quoted)} further finding(s) were not included "
            f"here; fix these first.)"
        )
    lines.append(
        "Do not change anything the reviewer did not raise, and do not widen "
        "the task's file allowance to reach a fix."
    )
    return "\n".join(lines)


def render_unresolved_issues(issues: Sequence[ReviewIssue]) -> str:
    """Prior cycles' open findings, for a re-review's package (section 23).

    A reviewer that cannot see what it asked for last time cannot tell a fix
    from a coincidence, and will re-raise findings the coder already
    addressed.
    """
    return render_bullet_list(
        "Issues raised in an earlier review cycle and not yet marked resolved",
        [
            f"[{issue.severity}] {_location(issue) or 'unlocated'}: {issue.problem} "
            f"(required: {issue.required_fix})"
            for issue in issues
        ],
    )


# --------------------------------------------------- issues across cycles


def issue_fingerprint(issue: ReviewIssue) -> tuple[str, str, str, str]:
    """What makes two findings "the same finding" across review cycles.

    Not the wording: a reviewer that raises the same defect twice will
    describe it differently the second time, and matching on prose would
    treat every re-raise as a new issue and every earlier issue as resolved.
    So the identity is the requirement it cites, the file it points at and
    the category it was filed under -- the three things a reviewer restates
    consistently because it is reading them off the package rather than
    composing them.

    Blank fields are kept in the key rather than dropped. Two unlocated
    ``correctness`` findings on the same run do collide, and collapsing them
    is the right way round: the cost of treating one as the other is that an
    open issue is closed a cycle early, against a re-raise that will be
    recorded anyway.
    """
    requirement = (issue.requirement_id or "").strip().casefold()
    path = (issue.file or "").strip().casefold()
    fallback = "" if requirement or path else " ".join(issue.problem.casefold().split())
    return (requirement, path, str(issue.category), fallback)


def unreraised_issues(
    earlier: Sequence[ReviewIssue], current: Sequence[ReviewIssue]
) -> tuple[ReviewIssue, ...]:
    """Earlier findings this review did not raise again (section 23, concern 27).

    The honest reading of "resolved". A coder's claim to have fixed something
    is exactly the kind of claim this system does not believe, so nothing is
    closed because an attempt was made; an issue is closed when a reviewer
    that was shown it -- ``render_unresolved_issues`` puts every open finding
    in the package -- read the new diff and did not raise it.

    Without this the open list only grows, and by the third cycle the reviewer
    is reading every finding ever raised against the run, including the ones
    it can see were fixed. That wastes package budget and invites a re-raise.

    Args:
        earlier: still-open findings from previous cycles.
        current: what the cycle just completed raised.

    Returns:
        The subset of ``earlier`` no current finding matches, in the order
        they were given.
    """
    raised = {issue_fingerprint(issue) for issue in current}
    requirements = {
        fingerprint[0] for fingerprint in raised if fingerprint[0]
    }
    return tuple(
        issue
        for issue in earlier
        if issue_fingerprint(issue) not in raised
        # A finding whose requirement is cited again is being pursued, even
        # if the reviewer moved it to another file or another category.
        and (issue.requirement_id or "").strip().casefold() not in requirements
    )

def render_escalation(
    *,
    external_task_id: str,
    reason: str,
    requirement: str,
    routing: ReviewRouting,
    result: ReviewResult,
    attempts: Sequence[str] = (),
    options: Sequence[str] = (),
    restored_commit: str | None = None,
) -> str:
    """A human escalation in section 24's shape.

    Section 24's rule is the design brief: *the human should not have to
    reconstruct the history manually.* So the blocker, the attempts, the
    reviewer's own concern and the repository's current state are all on the
    page, and the options are stated as choices rather than as a question.
    """
    sections = [
        f"TASK {external_task_id} — HUMAN REVIEW REQUIRED",
        "",
        "Reason:",
        reason,
        "",
        "Requirement:",
        requirement,
        "",
        "Current blocker:",
        "\n".join(routing.human_review_reasons) or routing.summary(),
        "",
        render_bullet_list("Attempts", list(attempts)),
        "",
        "Reviewer concern:",
        result.summary,
    ]
    if result.blocking_issues:
        sections.extend(
            [
                "",
                render_bullet_list(
                    "Blocking findings",
                    [
                        f"[{issue.severity}] {_location(issue) or 'unlocated'}: "
                        f"{issue.problem}"
                        for issue in result.blocking_issues
                    ],
                ),
            ]
        )
    sections.extend(["", render_bullet_list("Options", list(options))])
    if restored_commit:
        # Section 24's example says "restored to", but at the point a review
        # escalates nothing has been rolled back: the candidate is still on
        # the run's task branch and the managed repository never moved. Saying
        # "restored" would tell a person the work is gone when it is not.
        sections.extend(
            [
                "",
                "Current repository:",
                f"The managed repository is unchanged at known-good SHA "
                f"{restored_commit}. The candidate is uncommitted on the run's "
                f"task branch and nothing has been pushed.",
            ]
        )
    return "\n".join(sections)


def escalation_options(routing: ReviewRouting) -> tuple[EscalationOption, ...]:
    """Default decisions to offer a human, in section 24's A/B/C style.

    Generic on purpose. The orchestrator knows what it cannot decide; it does
    not know the project's alternatives, and inventing them would be the
    system "inventing architectural decisions", which principle 7 forbids.

    Each option carries the intent the workflow acts on (concern 32). All
    three sets offer acceptance, because a review reached this point with a
    candidate that verified and is still on disk -- which is exactly the case
    where "accept it anyway" is a decision a person can reasonably take.
    """
    if routing.retry_exhausted:
        return (
            EscalationOption(
                "A",
                EscalationIntent.ACCEPT_CANDIDATE,
                "Accept the candidate as it stands and complete the task.",
            ),
            EscalationOption(
                "B",
                EscalationIntent.RETRY_TASK,
                "Reword or split the task and run it again from the known-good SHA.",
            ),
            EscalationOption(
                "C",
                EscalationIntent.ABANDON_TASK,
                "Abandon the task and roll the repository back.",
            ),
        )
    if routing.escalated_by_policy:
        return (
            EscalationOption(
                "A",
                EscalationIntent.ACCEPT_CANDIDATE,
                "Approve the change and let the task complete.",
            ),
            EscalationOption(
                "B",
                EscalationIntent.REQUEST_CHANGES,
                "Send it back to the coder with your own required changes.",
            ),
            EscalationOption(
                "C",
                EscalationIntent.ABANDON_TASK,
                "Reject the change and roll the repository back.",
            ),
        )
    return (
        EscalationOption(
            "A",
            EscalationIntent.REQUEST_CHANGES,
            "Answer the reviewer's question; your answer is sent to the coder.",
        ),
        EscalationOption(
            "B",
            EscalationIntent.RETRY_TASK,
            "Change the task specification and run it again.",
        ),
        EscalationOption(
            "C",
            EscalationIntent.ABANDON_TASK,
            "Abandon the task and roll the repository back.",
        ),
    )


# ------------------------------------------------------------------- parsing


def _decision(value: object) -> ReviewDecision:
    if not isinstance(value, str) or not value.strip():
        raise MalformedReview("The review carries no 'decision' field")
    candidate = value.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return ReviewDecision(candidate)
    except ValueError as error:
        allowed = ", ".join(decision.value for decision in ReviewDecision)
        raise MalformedReview(
            f"'{value}' is not a review decision; expected one of {allowed}"
        ) from error


def _issues(value: object, warnings: list[str]) -> tuple[ReviewIssue, ...]:
    if not isinstance(value, Sequence):
        warnings.append("the review's 'issues' field was not a list and was ignored")
        return ()
    issues: list[ReviewIssue] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            warnings.append(f"issue {index} was not an object and was dropped")
            continue
        issue = _issue(entry, index, warnings)
        if issue is not None:
            issues.append(issue)
    return tuple(issues)


def _issue(
    entry: Mapping[str, object], index: int, warnings: list[str]
) -> ReviewIssue | None:
    problem = _text(entry.get("problem"))
    required_fix = _text(entry.get("requiredFix")) or _text(entry.get("required_fix"))
    if not problem:
        warnings.append(f"issue {index} states no problem and was dropped")
        return None
    if not required_fix:
        # Kept, not dropped: a real defect described without a remedy is still
        # a defect, and the coder is better served by the problem alone than
        # by silence. The gap is recorded so a reviewer's prompt can be fixed.
        required_fix = "The reviewer did not state a fix; resolve the problem above."
        warnings.append(f"issue {index} states no required fix")
    return ReviewIssue(
        severity=_severity(entry.get("severity"), index, warnings),
        category=_category(entry.get("category"), index, warnings),
        problem=problem,
        required_fix=required_fix,
        file=_text(entry.get("file")) or None,
        line=_line(entry.get("line")),
        requirement_id=(
            _text(entry.get("requirementId")) or _text(entry.get("requirement_id")) or None
        ),
    )


def _severity(value: object, index: int, warnings: list[str]) -> IssueSeverity:
    if isinstance(value, str):
        try:
            return IssueSeverity(value.strip().upper())
        except ValueError:
            warnings.append(
                f"issue {index} has severity '{value}', read as {UNKNOWN_SEVERITY}"
            )
            return UNKNOWN_SEVERITY
    warnings.append(f"issue {index} has no severity, read as {UNKNOWN_SEVERITY}")
    return UNKNOWN_SEVERITY


def _category(value: object, index: int, warnings: list[str]) -> IssueCategory:
    if isinstance(value, str):
        try:
            return IssueCategory(value.strip().casefold())
        except ValueError:
            warnings.append(
                f"issue {index} has category '{value}', read as {UNKNOWN_CATEGORY}"
            )
            return UNKNOWN_CATEGORY
    warnings.append(f"issue {index} has no category, read as {UNKNOWN_CATEGORY}")
    return UNKNOWN_CATEGORY


def _confidence(value: object, warnings: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"confidence '{value}' is not a number and was ignored")
        return None
    confidence = float(value)
    if not 0.0 <= confidence <= 1.0:
        # A reviewer answering "93" means 93%. Clamping rather than dropping
        # keeps the approval gate working on the reading the model intended.
        rescaled = confidence / 100 if 1.0 < confidence <= 100.0 else None
        if rescaled is not None and 0.0 <= rescaled <= 1.0:
            warnings.append(f"confidence {confidence} read as {rescaled:.2f}")
            return rescaled
        warnings.append(f"confidence {confidence} is out of range and was ignored")
        return None
    return confidence


def _risk(value: object, warnings: list[str]) -> RiskLevel | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return RiskLevel(value.strip().casefold())
        except ValueError:
            warnings.append(f"risk '{value}' is not a risk level and was ignored")
            return None
    warnings.append(f"risk '{value}' is not a risk level and was ignored")
    return None


def _line(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    line = int(value)
    return line if line > 0 else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _location(issue: ReviewIssue) -> str:
    if issue.file and issue.line:
        return f"{issue.file}:{issue.line}"
    return issue.file or ""


def _issue_entry(issue: ReviewIssue) -> dict[str, object]:
    return {
        "severity": issue.severity.value,
        "category": issue.category.value,
        "file": issue.file,
        "line": issue.line,
        "requirementId": issue.requirement_id,
        "problem": issue.problem,
        "requiredFix": issue.required_fix,
        "blocking": issue.is_blocking,
        "resolved": issue.resolved,
    }


__all__ = [
    "APPROVAL_GATED_CATEGORIES",
    "MAX_FEEDBACK_ISSUES",
    "REVIEW_SCHEMA",
    "REVIEW_SCHEMA_VERSION",
    "UNKNOWN_CATEGORY",
    "UNKNOWN_SEVERITY",
    "HumanApprovalPolicy",
    "MalformedReview",
    "ReviewResult",
    "ReviewRouting",
    "escalation_options",
    "issue_fingerprint",
    "parse_review",
    "reconcile",
    "render_escalation",
    "render_review_feedback",
    "render_unresolved_issues",
    "route_review",
    "unreraised_issues",
]
