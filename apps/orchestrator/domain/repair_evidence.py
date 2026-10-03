"""Bounded repair evidence for an attributed regression (concern 78, stage 3).

Stage 2 ends with a verdict: this candidate broke *these* tests, and here is
the recorded baseline that proves the others were already broken. Stage 3 is
what happens next, and the only thing it adds is packaging. The classification
is already deterministic, so nothing here asks a model why verification failed;
this module turns the evidence stage 2 already holds into the one artefact the
repair attempt is given.

Three properties are the whole design.

* **Provenance survives.** ``FailureComparison`` aggregates: it answers "which
  identities are new" across every command that was compared, because that is
  the only question set arithmetic can answer safely. A coder handed that union
  cannot tell which runner produced which failure. So the evidence is split per
  failing command again, by the one deterministic means available -- an
  identity was extracted *from* a command's output, so the command whose output
  mentions it is the command it came from. No prose merges two runners'
  failures into one story.
* **Everything is bounded before it is rendered, not after.** The number of
  commands, the number of identities (overall and per command), the lines of
  excerpt and its characters each have an explicit ceiling, and what was
  dropped is stated rather than silently lost. The full log is never a field:
  it stays on disk and travels as ``artifact_reference``, a path.
* **It is data, not a string.** ``describe`` is the serialisable form for an
  artifact or a run record, ``render`` is the prompt text, and both read the
  same bounded fields -- so what a later reader can audit is exactly what the
  model was shown.

A ``RepairEvidence`` exists only for ``NEW_REGRESSION``. ``build`` returns
``None`` for every other classification, which is stage 2's fail-closed rule
expressed as a constructor: a failure nobody could classify has no attributed
regression to repair, and must not be handed to a coder as though it did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .enums import VerificationClassification
from .failure_identity import FailureComparison

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .verification import VerificationStep

#: Failing commands whose evidence is carried. A candidate that broke tests in
#: four different runners is a candidate with a single cause far more often
#: than four, and the pipeline stops at the first failing category anyway.
MAX_EVIDENCE_COMMANDS = 3

#: Regression identities named in total, and per command. The same ceiling as
#: stage 2's rendered samples: a list longer than this is not read, and the
#: count of what was dropped carries the rest of the information.
MAX_EVIDENCE_IDENTITIES = 20

#: Lines of captured output carried per failing command, and the character
#: ceiling that holds when those lines are long (a single assertion diff can be
#: thousands of characters wide).
MAX_EVIDENCE_EXCERPT_LINES = 40
MAX_EVIDENCE_EXCERPT_CHARS = 4_000

#: The whole rendered instruction's ceiling. Every field above is already
#: bounded, so this is a backstop rather than the working limit: it is what
#: makes "no unbounded text reaches a model" true by construction instead of
#: true by inspection of the fields that happen to exist today.
MAX_EVIDENCE_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class RepairCommandEvidence:
    """One failing command, the regressions attributed to it, and its excerpt."""

    verification_type: str
    command: str
    exit_code: int | None = None
    #: Bounded, sorted. The new failures whose identity appears in this
    #: command's own output.
    failure_identities: tuple[str, ...] = ()
    #: Attributed identities not listed above.
    omitted_identities: int = 0
    #: The lines of this command's output that mention one of them, bounded.
    excerpt: str = ""
    #: Where the complete log is. A path, never the log.
    artifact_reference: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "verification_type": self.verification_type,
            "command": self.command,
            "exit_code": self.exit_code,
            "failure_identities": list(self.failure_identities),
            "omitted_identities": self.omitted_identities,
            "excerpt": self.excerpt,
            "artifact_reference": self.artifact_reference,
        }

    def render(self) -> str:
        header = f"[{self.verification_type}] `{self.command}`"
        if self.exit_code is not None:
            header += f" -> exit code {self.exit_code}"
        if self.artifact_reference:
            header += f" (full log on disk: {self.artifact_reference})"
        parts = [header]
        if self.failure_identities:
            listed = [f"- {identity}" for identity in self.failure_identities]
            if self.omitted_identities:
                listed.append(f"- [... {self.omitted_identities} more ...]")
            parts.append("New failure(s) from this command:\n" + "\n".join(listed))
        if self.excerpt:
            parts.append("Relevant output:\n" + self.excerpt)
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class RepairEvidence:
    """What a repair attempt is given for an attributed regression.

    Deterministic: built from the failing steps and the comparison, with no
    clock, no randomness and no set iteration order reaching the output.
    """

    classification: VerificationClassification
    baseline_sha: str | None = None
    #: Bounded, sorted union of the new failure identities.
    failure_identities: tuple[str, ...] = ()
    #: New identities not listed above.
    omitted_identities: int = 0
    commands: tuple[RepairCommandEvidence, ...] = field(default_factory=tuple)
    #: Failing commands whose evidence was dropped by ``MAX_EVIDENCE_COMMANDS``.
    omitted_commands: int = 0
    #: New identities no failing command's captured output mentioned. Named,
    #: because the alternative is attributing them to the wrong command: the
    #: step's output is a tail, so an identity read from the full log can be
    #: absent from the text carried here.
    unattributed_identities: tuple[str, ...] = ()
    #: Pre-existing failures this candidate happens to have fixed. A count
    #: only -- it is information, never an instruction.
    resolved_count: int = 0

    def describe(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "baseline_sha": self.baseline_sha,
            "failure_identities": list(self.failure_identities),
            "omitted_identities": self.omitted_identities,
            "unattributed_identities": list(self.unattributed_identities),
            "resolved_count": self.resolved_count,
            "omitted_commands": self.omitted_commands,
            "commands": [command.describe() for command in self.commands],
        }

    def render(self) -> str:
        """The repair instruction, assembled from the bounded fields only."""
        total = len(self.failure_identities) + self.omitted_identities
        blocks: list[str] = [
            f"Authoritative verification found {total} new regression(s) "
            "relative to the established baseline for the tree this change "
            "started from"
            + (f" ({self.baseline_sha})." if self.baseline_sha else "."),
            "New failure(s):\n" + _identity_lines(
                self.failure_identities, self.omitted_identities
            ),
        ]
        blocks.extend(command.render() for command in self.commands)
        if self.omitted_commands:
            blocks.append(
                f"[... {self.omitted_commands} further failing command(s) not "
                "shown ...]"
            )
        if self.unattributed_identities:
            blocks.append(
                "No captured output was available for these new failure(s); "
                "the full logs referenced above hold their detail:\n"
                + _identity_lines(self.unattributed_identities, 0)
            )
        if self.resolved_count:
            blocks.append(
                "For information only -- this change also resolved "
                f"{self.resolved_count} pre-existing failure(s). Leave them "
                "fixed."
            )
        blocks.append(
            "Repair only the regression(s) above, in the supplied candidate for "
            "this task. You may run the specific tests directly affected by what "
            "you changed to confirm the repair; the full verification suite will "
            "be run by the orchestrator after you return the corrected candidate, "
            "so do not reproduce it, do not establish a baseline and do not "
            "compare one. Failures outside the list above are not yours to chase. "
            "Return the corrected edits once the repair and its targeted tests "
            "are done."
        )
        rendered = "\n\n".join(blocks)
        if len(rendered) <= MAX_EVIDENCE_CHARS:
            return rendered
        return rendered[:MAX_EVIDENCE_CHARS] + "\n[... evidence truncated ...]"


def build(
    failures: Sequence[VerificationStep],
    comparison: FailureComparison | None,
    *,
    classification: VerificationClassification,
) -> RepairEvidence | None:
    """Bounded evidence for an attributed regression, or ``None``.

    ``None`` for anything that is not ``NEW_REGRESSION``: a pass, a
    baseline-only failure and -- the one that matters -- an
    ``UNCLASSIFIED_FAILURE``, which stage 2 refused to attribute and stage 3
    must not attribute on its behalf. Infrastructure, build and lint failures
    reach here only as unclassified, because a category with no stable failure
    identities cannot produce an available comparison.
    """
    if classification is not VerificationClassification.NEW_REGRESSION:
        return None
    if comparison is None or not comparison.available or not comparison.new:
        return None

    new_failures = sorted(comparison.new)
    commands: list[RepairCommandEvidence] = []
    attributed: set[str] = set()
    considered = [step for step in failures if step.output or step.log_artifact]
    for step in considered[:MAX_EVIDENCE_COMMANDS]:
        mine = [identity for identity in new_failures if identity in step.output]
        excerpt = _excerpt_for(step.output, mine)
        if not mine and not excerpt:
            continue
        attributed.update(mine)
        shown = mine[:MAX_EVIDENCE_IDENTITIES]
        commands.append(
            RepairCommandEvidence(
                verification_type=step.verification_type.value,
                command=step.command,
                exit_code=step.exit_code,
                failure_identities=tuple(shown),
                omitted_identities=len(mine) - len(shown),
                excerpt=excerpt,
                artifact_reference=step.log_artifact,
            )
        )
    omitted_commands = max(len(considered) - MAX_EVIDENCE_COMMANDS, 0)

    listed = new_failures[:MAX_EVIDENCE_IDENTITIES]
    unattributed = [
        identity for identity in new_failures if identity not in attributed
    ]
    return RepairEvidence(
        classification=classification,
        baseline_sha=comparison.baseline_sha,
        failure_identities=tuple(listed),
        omitted_identities=len(new_failures) - len(listed),
        commands=tuple(commands),
        omitted_commands=omitted_commands,
        unattributed_identities=tuple(unattributed[:MAX_EVIDENCE_IDENTITIES]),
        resolved_count=len(comparison.resolved),
    )


def _identity_lines(identities: Sequence[str], omitted: int) -> str:
    lines = [f"- {identity}" for identity in identities]
    if omitted:
        lines.append(f"- [... {omitted} more ...]")
    return "\n".join(lines)


def _excerpt_for(output: str, identities: Sequence[str]) -> str:
    """The lines of ``output`` naming one of ``identities``, line- and char-bounded.

    A substring match rather than a parse: the identity came out of this text,
    and the lines that name it -- the short-summary line, the traceback header
    -- are the ones worth sending. Long lines are clipped individually so that
    one enormous assertion diff cannot consume the whole character ceiling.
    """
    if not output or not identities:
        return ""
    kept = [
        line
        for line in output.splitlines()
        if any(identity in line for identity in identities)
    ]
    if not kept:
        return ""
    dropped_lines = max(len(kept) - MAX_EVIDENCE_EXCERPT_LINES, 0)
    kept = kept[:MAX_EVIDENCE_EXCERPT_LINES]
    if dropped_lines:
        kept.append(f"[... {dropped_lines} more line(s) ...]")
    excerpt = "\n".join(kept)
    if len(excerpt) <= MAX_EVIDENCE_EXCERPT_CHARS:
        return excerpt
    return excerpt[:MAX_EVIDENCE_EXCERPT_CHARS] + "\n[... excerpt truncated ...]"


__all__ = [
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_COMMANDS",
    "MAX_EVIDENCE_EXCERPT_CHARS",
    "MAX_EVIDENCE_EXCERPT_LINES",
    "MAX_EVIDENCE_IDENTITIES",
    "RepairCommandEvidence",
    "RepairEvidence",
    "build",
]
