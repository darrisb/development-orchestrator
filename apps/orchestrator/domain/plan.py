"""Plan mode and plan validation (build.md section 14).

For a medium- or high-complexity task the coder must say what it intends to do
before it is allowed to change anything. The plan is a structured object, not
prose, for one reason: a validated plan can be *refused*. Section 14 asks for
exactly that -- "reject or escalate suspicious plans such as a small task
proposing dozens of unrelated file changes" -- and a paragraph of intent
cannot be checked against a task's file allowance, while this can.

Validation reuses the scope guard (``domain.scope``) rather than reimplementing
the allowance rules, so a path the plan may not write is the same path the
applier will not write and the same path the guard would block after the fact.
Catching it here just saves an attempt.

Pure: no I/O, no model, no repository.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .context import render_bullet_list
from .enums import Complexity, ScopePolicyDecision
from .models import Task
from .relevance import normalise_path
from .scope import ScopePolicy, categorise_path, check_write_path, is_within_repository

#: Bumped when the plan contract changes, for the same reason
#: ``TASK_SPEC_VERSION`` exists: an outcome must be attributable to the prompt
#: and schema that produced it (section 34).
PLAN_SCHEMA_VERSION = "coding-plan/1"

#: The JSON schema sent to the endpoint. Field names are section 14's, exactly.
PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["filesToInspect", "filesToModify", "filesToCreate", "approach"],
    "properties": {
        "filesToInspect": {"type": "array", "items": {"type": "string"}},
        "filesToModify": {"type": "array", "items": {"type": "string"}},
        "filesToCreate": {"type": "array", "items": {"type": "string"}},
        "approach": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "expectedTests": {"type": "array", "items": {"type": "string"}},
    },
}

#: How many files a task that declared no allowance may plan to touch before a
#: human is asked. Not a hard limit -- the task's ``max_files_changed`` is that
#: -- but a plan this wide for an undeclared scope is the "dozens of unrelated
#: file changes" case, and a low-complexity task reaching it is more likely to
#: be a misread task than an ambitious one.
SUSPICIOUS_UNDECLARED_FILES: dict[Complexity, int] = {
    Complexity.LOW: 4,
    Complexity.MEDIUM: 8,
    Complexity.HIGH: 16,
}

#: Complexities that must plan before coding (section 14).
PLAN_REQUIRED_COMPLEXITIES: frozenset[Complexity] = frozenset(
    {Complexity.MEDIUM, Complexity.HIGH}
)


class PlanRejected(ValueError):
    """A plan may not proceed. Carries the assessment that refused it."""

    def __init__(self, assessment: PlanAssessment) -> None:
        super().__init__(assessment.summary())
        self.assessment = assessment


@dataclass(frozen=True, slots=True)
class PlanFinding:
    """One objection to a plan, with its own verdict."""

    decision: ScopePolicyDecision
    detail: str
    path: str | None = None

    def describe(self) -> dict[str, object]:
        return {"decision": self.decision.value, "detail": self.detail, "path": self.path}


@dataclass(frozen=True, slots=True)
class CodingPlan:
    """What the coder says it will do, before it does any of it."""

    files_to_inspect: tuple[str, ...] = ()
    files_to_modify: tuple[str, ...] = ()
    files_to_create: tuple[str, ...] = ()
    approach: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    expected_tests: tuple[str, ...] = ()

    @property
    def write_paths(self) -> tuple[str, ...]:
        """Every path the plan intends to write, de-duplicated, in order."""
        return tuple(dict.fromkeys([*self.files_to_modify, *self.files_to_create]))

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CodingPlan:
        """Read a plan out of a parsed model response.

        Tolerant about shape and strict about content: a string where a list
        was asked for becomes a one-item list, because that is a formatting
        slip with an unambiguous reading, while a path that cannot be made
        repository-relative is dropped and will be missing from the plan the
        validator then measures.
        """
        return cls(
            files_to_inspect=_paths(payload.get("filesToInspect")),
            files_to_modify=_paths(payload.get("filesToModify")),
            files_to_create=_paths(payload.get("filesToCreate")),
            approach=_strings(payload.get("approach")),
            risks=_strings(payload.get("risks")),
            expected_tests=_paths(payload.get("expectedTests")),
        )

    def render(self) -> str:
        """The plan as prompt text, to be sent back with the coding request."""
        sections = [
            render_bullet_list("Approved plan — approach", list(self.approach)),
            render_bullet_list("Approved plan — files to modify", list(self.files_to_modify)),
            render_bullet_list("Approved plan — files to create", list(self.files_to_create)),
        ]
        if self.expected_tests:
            sections.append(
                render_bullet_list("Approved plan — expected tests", list(self.expected_tests))
            )
        if self.risks:
            sections.append(render_bullet_list("Approved plan — risks", list(self.risks)))
        return "\n\n".join(section for section in sections if section)

    def describe(self) -> dict[str, object]:
        """The artifact form, in the schema's own field names."""
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "filesToInspect": list(self.files_to_inspect),
            "filesToModify": list(self.files_to_modify),
            "filesToCreate": list(self.files_to_create),
            "approach": list(self.approach),
            "risks": list(self.risks),
            "expectedTests": list(self.expected_tests),
        }


@dataclass(frozen=True, slots=True)
class PlanAssessment:
    """Whether a plan may proceed, and why not when it may not."""

    decision: ScopePolicyDecision
    findings: tuple[PlanFinding, ...] = ()

    @property
    def approved(self) -> bool:
        return self.decision is not ScopePolicyDecision.BLOCK

    @property
    def needs_human(self) -> bool:
        return self.decision is ScopePolicyDecision.REQUIRE_REVIEW

    def summary(self) -> str:
        if not self.findings:
            return f"{self.decision}: plan is within the task's declared scope"
        return f"{self.decision}: " + "; ".join(finding.detail for finding in self.findings)

    def describe(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "findings": [finding.describe() for finding in self.findings],
        }

    def feedback(self) -> str:
        """The objections as text the coder can act on in its next attempt."""
        return render_bullet_list(
            "The previous plan was refused for these reasons",
            [finding.detail for finding in self.findings],
        )


def requires_plan(task: Task) -> bool:
    """Whether this task must plan before coding (section 14)."""
    return task.complexity in PLAN_REQUIRED_COMPLEXITIES


def validate_plan(plan: CodingPlan, task: Task, policy: ScopePolicy) -> PlanAssessment:
    """Check a plan against the task's scope before any code is written.

    ``BLOCK`` means the plan may not be executed: it would write somewhere it
    is not allowed to, or it would not write anything at all. ``REQUIRE_REVIEW``
    means the plan is executable but a human should see it -- an unexpectedly
    wide change, or a sensitive area the task never mentioned.
    """
    findings: list[PlanFinding] = []

    if not plan.approach:
        findings.append(
            PlanFinding(
                decision=ScopePolicyDecision.BLOCK,
                detail="the plan states no approach, so there is nothing to validate",
            )
        )
    if not plan.write_paths:
        findings.append(
            PlanFinding(
                decision=ScopePolicyDecision.BLOCK,
                detail=(
                    "the plan modifies and creates no files, so it cannot implement "
                    f"{task.external_task_id}"
                ),
            )
        )

    findings.extend(_path_findings(plan, policy))

    planned = len(plan.write_paths)
    if planned > policy.max_files_changed:
        findings.append(
            PlanFinding(
                decision=ScopePolicyDecision.BLOCK,
                detail=(
                    f"the plan writes {planned} files; the task allows "
                    f"{policy.max_files_changed}"
                ),
            )
        )
    elif not policy.has_allowance:
        threshold = SUSPICIOUS_UNDECLARED_FILES.get(task.complexity, 8)
        if planned > threshold:
            findings.append(
                PlanFinding(
                    decision=ScopePolicyDecision.REQUIRE_REVIEW,
                    detail=(
                        f"the plan writes {planned} files for a {task.complexity} "
                        f"task that declared no file list; more than {threshold} is "
                        f"wide enough that the task may have been misread"
                    ),
                )
            )

    ignored = _declared_but_unplanned(task, plan)
    if ignored:
        # Recorded, not held against the plan: a narrower change than the
        # manifest anticipated is often the better one, and escalating it
        # would spend a human's attention on the coder being conservative.
        findings.append(
            PlanFinding(
                decision=ScopePolicyDecision.ALLOW,
                detail=(
                    "the plan leaves "
                    + ", ".join(ignored)
                    + " unchanged, although the task declared them writable"
                ),
            )
        )

    return PlanAssessment(decision=_strictest(findings), findings=tuple(findings))


def _path_findings(plan: CodingPlan, policy: ScopePolicy) -> list[PlanFinding]:
    findings: list[PlanFinding] = []
    for path in plan.write_paths:
        if not is_within_repository(path):
            findings.append(
                PlanFinding(
                    decision=ScopePolicyDecision.BLOCK,
                    detail=f"{path} is not a repository-relative path",
                    path=path,
                )
            )
            continue
        scope_finding = check_write_path(path, policy)
        if scope_finding is not None:
            findings.append(
                PlanFinding(
                    decision=scope_finding.decision,
                    detail=f"the plan would write {scope_finding.detail}",
                    path=path,
                )
            )
            continue
        declared = policy.has_allowance and policy.is_allowed(path)
        for category in categorise_path(path):
            if declared:
                continue
            findings.append(
                PlanFinding(
                    decision=ScopePolicyDecision.REQUIRE_REVIEW,
                    detail=(
                        f"the plan would change {path}, a "
                        f"{category.value.replace('_', ' ')} file the task did not declare"
                    ),
                    path=path,
                )
            )
    return findings


def _declared_but_unplanned(task: Task, plan: CodingPlan) -> list[str]:
    """Paths the task said to write that the plan leaves alone.

    Only literal paths are reported: a declared glob or directory cannot be
    compared to a concrete plan path without guessing what the coder meant,
    and a false objection here would cost a whole attempt.
    """
    planned = {normalise_path(path) for path in plan.write_paths}
    return [
        declared
        for declared in (normalise_path(path) for path in task.allowed_paths)
        if not any(character in declared for character in "*?[")
        and declared not in planned
    ]


def _strictest(findings: Sequence[PlanFinding]) -> ScopePolicyDecision:
    decisions = {finding.decision for finding in findings}
    if ScopePolicyDecision.BLOCK in decisions:
        return ScopePolicyDecision.BLOCK
    if ScopePolicyDecision.REQUIRE_REVIEW in decisions:
        return ScopePolicyDecision.REQUIRE_REVIEW
    return ScopePolicyDecision.ALLOW


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(
        entry.strip() for entry in value if isinstance(entry, str) and entry.strip()
    )


def _paths(value: object) -> tuple[str, ...]:
    """Normalised, de-duplicated paths, keeping anything that cannot be
    normalised so the validator can object to it by name."""
    candidates = _strings(value)
    normalised = [
        normalise_path(candidate) if is_within_repository(candidate) else candidate.strip()
        for candidate in candidates
    ]
    return tuple(dict.fromkeys(path for path in normalised if path))


__all__ = [
    "PLAN_REQUIRED_COMPLEXITIES",
    "PLAN_SCHEMA",
    "PLAN_SCHEMA_VERSION",
    "SUSPICIOUS_UNDECLARED_FILES",
    "CodingPlan",
    "PlanAssessment",
    "PlanFinding",
    "PlanRejected",
    "requires_plan",
    "validate_plan",
]
