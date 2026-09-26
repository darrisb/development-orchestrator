"""Security verification over a candidate diff (build.md section 19).

Section 19 asks for modest checks in V1 with the abstraction in place, and
names them: dependency audit, secret scanning, protected-path checks,
forbidden file detection, unexpected binary detection, diff-size checks,
suspicious generated files, configuration policy checks.

They are not all in one place, on purpose:

* **Protected paths, diff size and deletions are the scope guard's**
  (``domain.scope``). They are measured from the structured diff and they are
  the same checks whether the question is "may this be written" or "is this
  candidate safe", so they exist once.
* **A dependency audit is a command**, not a pattern: ``npm audit`` knows
  things this process does not. It belongs in the project's verification
  profile (section 18) and runs in the worker like any other command.
* **What is left is content**, and that is this module: what the added lines
  actually contain, and what kind of file was added. Nothing here needs a
  repository, a worker or a model -- a diff, its structure, and the answer.

One property matters more than the checks themselves. **A clipped diff is
reported, never scanned quietly.** If the captured text was truncated, the
lines that were dropped were not scanned, and a scan that says "no secrets
found" over material it never read is worse than no scan: it is a false
assurance attached to the run record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .enums import ScopePolicyDecision
from .git import DiffSummary
from .redaction import PLACEHOLDER, SHAPE_PATTERNS
from .relevance import matches_pattern, normalise_path

#: Paths that are build output, dependency trees or caches. A coder that
#: commits one has usually run a build in its worktree and handed back what
#: the build produced; either way the repository should not carry it, and the
#: reviewer should not be asked to read it.
GENERATED_PATTERNS: tuple[str, ...] = (
    "**/node_modules/**",
    "**/bower_components/**",
    "**/vendor/**",
    "**/dist/**",
    "**/build/**",
    "**/out/**",
    "**/target/**",
    "**/.next/**",
    "**/.nuxt/**",
    "**/coverage/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/.mypy_cache/**",
    "**/__pycache__/**",
    "**/.venv/**",
    "**/venv/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.pyc",
    "**/*.class",
    "**/*.o",
    "**/*.so",
    "**/*.log",
    "**/.DS_Store",
)

#: Files that are credential material by their very existence, whatever they
#: contain. ``domain.scope`` refuses to *write* these; this catches one that
#: reached the diff another way, for instance through a rename.
FORBIDDEN_FILE_PATTERNS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.keystore",
    "**/*.jks",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.npmrc",
    "**/.pypirc",
    "**/.netrc",
    "**/credentials.json",
    "**/service-account*.json",
)

#: Values that are a *reference* to a credential rather than the credential:
#: reading a key from the environment is the thing a coder should be doing,
#: and flagging ``API_KEY = process.env.API_KEY`` would teach it to stop.
_REFERENCE_MARKERS: tuple[str, ...] = (
    "process.env",
    "import.meta.env",
    "os.environ",
    "getenv",
    "System.getenv",
    "${",
    "{{",
    "config.",
    "settings.",
    "secrets.",
    "vault",
    "$env:",
)

#: Obvious stand-ins. A template that ships ``PASSWORD=changeme`` is not a
#: leak, and refusing it would make ``.env.example`` unwritable.
_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "changeme",
    "change-me",
    "your-",
    "your_",
    "xxxx",
    "placeholder",
    "example",
    "redacted",
    "dummy",
    PLACEHOLDER.casefold(),
)

#: A PEM header on its own line. See ``_secret_findings``.
_PEM_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

#: Added lines longer than this are not scanned line by line for credential
#: shapes: a minified bundle is one 300KB line, and running every shape
#: pattern over it costs more than it finds. The path checks still see it.
MAX_SCANNED_LINE_LENGTH = 2000


class SecurityFindingKind(StrEnum):
    """What the scan noticed."""

    SECRET_MATERIAL = "secret_material"
    FORBIDDEN_FILE = "forbidden_file"
    GENERATED_ARTIFACT = "generated_artifact"
    UNEXPECTED_BINARY = "unexpected_binary"
    DIFF_NOT_SCANNED = "diff_not_scanned"


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    """One thing the scan found, and what it costs.

    ``decision`` follows section 20's vocabulary so that the scope guard and
    the security scan can be combined without translating between two scales.
    """

    kind: SecurityFindingKind
    decision: ScopePolicyDecision
    detail: str
    path: str | None = None
    line: int | None = None

    @property
    def blocking(self) -> bool:
        return self.decision is ScopePolicyDecision.BLOCK

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "decision": self.decision.value,
            "detail": self.detail,
            "path": self.path,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class SecurityAssessment:
    """The scan's verdict on one candidate."""

    decision: ScopePolicyDecision = ScopePolicyDecision.ALLOW
    findings: tuple[SecurityFinding, ...] = ()
    scanned_lines: int = 0
    complete: bool = True

    @property
    def blocked(self) -> bool:
        return self.decision is ScopePolicyDecision.BLOCK

    @property
    def blocking_findings(self) -> tuple[SecurityFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    def summary(self) -> str:
        if not self.findings:
            return f"no security findings over {self.scanned_lines} added line(s)"
        return "; ".join(finding.detail for finding in self.findings)

    def describe(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "scanned_lines": self.scanned_lines,
            "complete": self.complete,
            "findings": [finding.describe() for finding in self.findings],
        }


def scan_candidate(
    diff_text: str,
    summary: DiffSummary,
    *,
    truncated: bool = False,
    generated_path_exceptions: tuple[str, ...] = (),
) -> SecurityAssessment:
    """Run every content check section 19 asks of a diff.

    Args:
        diff_text: the unified diff. Only *added* lines are scanned: a secret
            being deleted is the candidate doing the right thing, and
            flagging it would teach the coder not to remove one.
        summary: the structured diff, for the path and binary checks. These
            are made from the summary rather than from the text so a clipped
            diff still gets them in full.
        truncated: whether ``diff_text`` was clipped. A clipped scan is
            reported as incomplete; see the module docstring.
    """
    findings: list[SecurityFinding] = []

    added = added_lines(diff_text)
    findings.extend(_secret_findings(added))
    findings.extend(
        _path_findings(
            summary, generated_path_exceptions=generated_path_exceptions
        )
    )

    if truncated:
        findings.append(
            SecurityFinding(
                kind=SecurityFindingKind.DIFF_NOT_SCANNED,
                decision=ScopePolicyDecision.REQUIRE_REVIEW,
                detail=(
                    "the captured diff was clipped, so part of the change was never "
                    "scanned for credentials; a human should read the rest"
                ),
            )
        )

    return SecurityAssessment(
        decision=_strictest(findings),
        findings=tuple(findings),
        scanned_lines=len(added),
        complete=not truncated,
    )


def added_lines(diff_text: str) -> tuple[tuple[int, str], ...]:
    """The added lines of a unified diff, numbered by position in the text.

    The number is the line's index in the diff, not in the file it belongs
    to: this is evidence for a human reading ``candidate.patch``, and a file
    line number would have to be reconstructed from hunk headers that a
    clipped diff may not contain.
    """
    collected: list[tuple[int, str]] = []
    for index, line in enumerate(diff_text.splitlines(), start=1):
        if line.startswith("+") and not line.startswith("+++"):
            collected.append((index, line[1:]))
    return tuple(collected)


def _secret_findings(added: tuple[tuple[int, str], ...]) -> list[SecurityFinding]:
    """Credential shapes in added lines, reusing the redactor's patterns.

    The same patterns that mask a secret in a captured log identify one in a
    diff. Sharing them means a shape the orchestrator knows to hide is also a
    shape it knows to refuse, rather than two lists that drift apart.
    """
    findings: list[SecurityFinding] = []
    for number, text in added:
        if len(text) > MAX_SCANNED_LINE_LENGTH:
            findings.append(
                SecurityFinding(
                    kind=SecurityFindingKind.DIFF_NOT_SCANNED,
                    decision=ScopePolicyDecision.REQUIRE_REVIEW,
                    detail=(
                        f"line {number} of the diff is too long for the built-in "
                        "credential scan; a project scanner or human must inspect it"
                    ),
                    line=number,
                )
            )
            continue
        # The redactor matches a PEM block across its whole body, which a
        # line-by-line scan never sees; the header alone is conclusive.
        if _PEM_HEADER.search(text):
            findings.append(
                SecurityFinding(
                    kind=SecurityFindingKind.SECRET_MATERIAL,
                    decision=ScopePolicyDecision.BLOCK,
                    detail=(
                        f"line {number} of the diff adds a private key block; the "
                        f"key itself is not recorded here"
                    ),
                    line=number,
                )
            )
            continue
        for name, pattern in SHAPE_PATTERNS:
            match = pattern.search(text)
            if match is None or not _is_literal_secret(match.group("secret")):
                continue
            # One finding per line, named for the first shape that matched:
            # a key that is both "prefixed" and "named" is still one secret,
            # and reporting it twice makes a diff look worse than it is.
            findings.append(
                SecurityFinding(
                    kind=SecurityFindingKind.SECRET_MATERIAL,
                    decision=ScopePolicyDecision.BLOCK,
                    detail=(
                        f"line {number} of the diff adds something shaped like a "
                        f"credential ({name.replace('_', ' ')}); the value itself is "
                        f"not recorded here"
                    ),
                    line=number,
                )
            )
            break
    return findings


def _is_literal_secret(value: str) -> bool:
    """Whether a matched value looks like a credential rather than a reference.

    The redactor's patterns are deliberately eager -- masking one value too
    many in a log costs nothing. Blocking a candidate does cost something, so
    the same match is held to a higher bar here: an environment lookup or an
    obvious placeholder is not a leak.
    """
    lowered = value.casefold()
    if any(marker.casefold() in lowered for marker in _REFERENCE_MARKERS):
        return False
    return not any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _path_findings(
    summary: DiffSummary, *, generated_path_exceptions: tuple[str, ...] = ()
) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for change in summary.files:
        path = normalise_path(change.path)
        if any(matches_pattern(path, pattern) for pattern in FORBIDDEN_FILE_PATTERNS):
            findings.append(
                SecurityFinding(
                    kind=SecurityFindingKind.FORBIDDEN_FILE,
                    decision=ScopePolicyDecision.BLOCK,
                    detail=f"{path} is credential material and does not belong in a diff",
                    path=path,
                )
            )
        elif (
            any(matches_pattern(path, pattern) for pattern in GENERATED_PATTERNS)
            and not any(
                matches_pattern(path, pattern)
                for pattern in generated_path_exceptions
            )
        ):
            findings.append(
                SecurityFinding(
                    kind=SecurityFindingKind.GENERATED_ARTIFACT,
                    decision=ScopePolicyDecision.BLOCK,
                    detail=(
                        f"{path} is build output or a dependency tree; commit the "
                        f"source, not what a build produces"
                    ),
                    path=path,
                )
            )
        elif change.is_binary:
            findings.append(
                SecurityFinding(
                    kind=SecurityFindingKind.UNEXPECTED_BINARY,
                    decision=ScopePolicyDecision.REQUIRE_REVIEW,
                    detail=(
                        f"{path} is binary, so neither this scan nor a reviewer can "
                        f"read what it contains"
                    ),
                    path=path,
                )
            )
    return findings


def _strictest(findings: list[SecurityFinding]) -> ScopePolicyDecision:
    decisions = {finding.decision for finding in findings}
    if ScopePolicyDecision.BLOCK in decisions:
        return ScopePolicyDecision.BLOCK
    if ScopePolicyDecision.REQUIRE_REVIEW in decisions:
        return ScopePolicyDecision.REQUIRE_REVIEW
    return ScopePolicyDecision.ALLOW


__all__ = [
    "FORBIDDEN_FILE_PATTERNS",
    "GENERATED_PATTERNS",
    "MAX_SCANNED_LINE_LENGTH",
    "SecurityAssessment",
    "SecurityFinding",
    "SecurityFindingKind",
    "added_lines",
    "scan_candidate",
]
