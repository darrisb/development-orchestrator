"""The completion report (build.md sections 6 and 14, phase G item 5).

A task's specification requires a completion report, and section 52 says the
model is never responsible for proving that its own work succeeded. Both hold
at once here: the report has two halves, and they are kept apart.

* **Claimed** -- the summary, the requirements the coder says it met, the tests
  it says it added, the deviations it admits to. Recorded verbatim, believed by
  nobody. It is the coder's account, and it is what a reviewer reads *against*
  the diff.
* **Measured** -- which edits were actually written and which were refused,
  how many files and lines actually changed, what the scope guard decided.
  None of it comes from the model.

A discrepancy between the two is itself a finding: a coder claiming a test it
never wrote is exactly the failure mode a completion report exists to catch,
and ``discrepancies`` names those without a reviewer having to notice.

Pure: no I/O, no model, no repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .context import render_bullet_list
from .edits import CodeChangeSet
from .enums import ScopePolicyDecision
from .relevance import is_test_path, normalise_path
from .scope import ScopeAssessment, path_matches_write_declaration

#: Bumped when the report's shape changes, so a stored report can be read
#: against the contract that produced it (section 34).
COMPLETION_REPORT_VERSION = "completion-report/1"

COMPLETION_REPORT_ARTIFACT = "completion-report.json"


@dataclass(frozen=True, slots=True)
class RejectedEdit:
    """An edit the orchestrator refused to write, and why."""

    path: str
    operation: str
    reason: str

    def describe(self) -> dict[str, object]:
        return {"path": self.path, "operation": self.operation, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class CompletionReport:
    """What the coder says it did, beside what it actually did."""

    external_task_id: str
    attempt: int
    #: --- claimed ---
    summary: str = ""
    requirements_met: tuple[str, ...] = ()
    tests_claimed: tuple[str, ...] = ()
    follow_ups: tuple[str, ...] = ()
    deviations_from_plan: tuple[str, ...] = ()
    #: --- measured ---
    applied_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    rejected_edits: tuple[RejectedEdit, ...] = ()
    files_changed: int = 0
    diff_lines: int = 0
    scope_decision: ScopePolicyDecision = ScopePolicyDecision.ALLOW
    scope_findings: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())
    planned: bool = False
    planned_paths: tuple[str, ...] = ()

    @property
    def tests_written(self) -> tuple[str, ...]:
        """Applied paths that look like tests. Measured, not claimed."""
        return tuple(path for path in self.applied_paths if is_test_path(path))

    @property
    def discrepancies(self) -> tuple[str, ...]:
        """Where the coder's account and the measured change disagree."""
        found: list[str] = []
        written = {normalise_path(path) for path in self.applied_paths}
        for claimed in self.tests_claimed:
            if normalise_path(claimed) not in written:
                found.append(
                    f"claims the test {claimed}, which is not among the files written"
                )
        if self.rejected_edits:
            found.append(
                f"{len(self.rejected_edits)} proposed edit(s) were refused and are "
                f"not part of the candidate"
            )
        if not self.applied_paths:
            found.append("no file was written, so the attempt changed nothing")
        if self.unplanned_paths:
            found.append(
                "changed files outside the approved plan: "
                + ", ".join(self.unplanned_paths)
            )
        return tuple(found)

    @property
    def unplanned_paths(self) -> tuple[str, ...]:
        if not self.planned:
            return ()
        return tuple(
            path
            for path in self.applied_paths
            if not any(
                path_matches_write_declaration(path, planned)
                for planned in self.planned_paths
            )
        )

    @property
    def usable(self) -> bool:
        """Whether this candidate may go on to verification.

        Being usable is not being correct: it means something was written and
        the scope guard did not block it. Whether the code works is decided by
        the verification pipeline (phase H), never here and never by the coder.
        """
        return bool(self.applied_paths) and self.scope_decision is not ScopePolicyDecision.BLOCK

    def describe(self) -> dict[str, object]:
        """The ``completion-report.json`` artifact."""
        return {
            "schema_version": COMPLETION_REPORT_VERSION,
            "task": self.external_task_id,
            "attempt": self.attempt,
            "planned": self.planned,
            "claimed": {
                "summary": self.summary,
                "requirements_met": list(self.requirements_met),
                "tests": list(self.tests_claimed),
                "follow_ups": list(self.follow_ups),
                "deviations_from_plan": list(self.deviations_from_plan),
            },
            "measured": {
                "applied_paths": list(self.applied_paths),
                "deleted_paths": list(self.deleted_paths),
                "rejected_edits": [edit.describe() for edit in self.rejected_edits],
                "tests_written": list(self.tests_written),
                "files_changed": self.files_changed,
                "diff_lines": self.diff_lines,
                "scope_decision": self.scope_decision.value,
                "scope_findings": list(self.scope_findings),
                "planned_paths": list(self.planned_paths),
                "unplanned_paths": list(self.unplanned_paths),
            },
            "discrepancies": list(self.discrepancies),
            "warnings": list(self.warnings),
            "usable": self.usable,
        }

    def render(self) -> str:
        """The report as text, for the reviewer's package (section 21)."""
        sections = [
            f"# Completion report — {self.external_task_id} (attempt {self.attempt})",
            "## Reported by the coder (unverified)",
            self.summary or "(no summary given)",
            render_bullet_list("Requirements it says it met", list(self.requirements_met)),
            render_bullet_list("Tests it says it added", list(self.tests_claimed)),
            render_bullet_list(
                "Deviations from the approved plan", list(self.deviations_from_plan)
            ),
            render_bullet_list("Follow-ups it suggests", list(self.follow_ups)),
            "## Measured by the orchestrator",
            render_bullet_list("Files written", list(self.applied_paths)),
            render_bullet_list("Files deleted", list(self.deleted_paths)),
            render_bullet_list(
                "Edits refused",
                [f"{edit.path} ({edit.operation}): {edit.reason}" for edit in self.rejected_edits],
            ),
            f"Diff: {self.files_changed} file(s), {self.diff_lines} line(s).",
            f"Scope guard: {self.scope_decision}.",
            render_bullet_list("Scope findings", list(self.scope_findings)),
            render_bullet_list("Files outside the approved plan", list(self.unplanned_paths)),
            render_bullet_list("Discrepancies", list(self.discrepancies)),
        ]
        return "\n\n".join(sections)


def build_completion_report(
    *,
    external_task_id: str,
    attempt: int,
    change_set: CodeChangeSet,
    applied_paths: tuple[str, ...],
    deleted_paths: tuple[str, ...],
    rejected_edits: tuple[RejectedEdit, ...],
    scope: ScopeAssessment,
    planned: bool,
    planned_paths: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> CompletionReport:
    """Assemble a report from the coder's answer and the measured outcome."""
    return CompletionReport(
        external_task_id=external_task_id,
        attempt=attempt,
        summary=change_set.summary,
        requirements_met=change_set.requirements_met,
        tests_claimed=change_set.tests_added,
        follow_ups=change_set.follow_ups,
        deviations_from_plan=change_set.deviations_from_plan,
        applied_paths=applied_paths,
        deleted_paths=deleted_paths,
        rejected_edits=rejected_edits,
        files_changed=scope.files_changed,
        diff_lines=scope.diff_lines,
        scope_decision=scope.decision,
        scope_findings=tuple(finding.detail for finding in scope.findings),
        warnings=tuple([*change_set.warnings, *warnings]),
        planned=planned,
        planned_paths=planned_paths,
    )


__all__ = [
    "COMPLETION_REPORT_ARTIFACT",
    "COMPLETION_REPORT_VERSION",
    "CompletionReport",
    "RejectedEdit",
    "build_completion_report",
]
