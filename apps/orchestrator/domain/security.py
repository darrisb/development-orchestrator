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

#: The shapes that infer a credential from a *name* beside a value, rather
#: than from the material itself.
#:
#: The split is what makes the rule in ``_insufficient_evidence`` safe.
#: ``private_key`` and ``prefixed_key`` recognise credential material by its
#: own content -- a PEM body, an ``sk-``/``ghp_``/``AKIA``/JWT prefix -- so no
#: surrounding syntax can make such a value innocent and they are never
#: exempted. They are also the backstop: a line excused below is still offered
#: to them, so genuine material on an excused line still blocks.
_NAME_INFERRED_SHAPES: frozenset[str] = frozenset({"named_value", "auth_header"})

#: A value spelled as a plain identifier: optionally qualified, generic or an
#: array. Deliberately a shape, not a vocabulary -- it says nothing about which
#: identifiers are types in which language, only that credential material
#: cannot be spelled this way. A secret carries ``-``, ``/``, ``+``, ``=`` or a
#: provider prefix; an identifier carries none of those.
_IDENTIFIER_VALUE = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*"         # Foo
    r"(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"   # ...or java.time.Instant
    r"(?:<[^>]*>?)?"                     # ...or Observable<string>
    r"(?:\[\])*$"                        # ...or Foo[][]
)

#: What makes an identifier-shaped value a *member expression* -- ``usage.
#: totalTokens`` rather than ``Zx91fakefakevalue``. A dot means the value being
#: read lives on another object, so whatever the credential-shaped name is
#: being given, it is not spelled on this line.
#:
#: This is the structural form of something the scanner already asserts one
#: prefix at a time: ``_REFERENCE_MARKERS`` lists ``config.``, ``settings.``,
#: ``secrets.``, ``process.env`` and ``os.environ``, every one of which is a
#: member expression, allowed for exactly this reason. The list cannot
#: enumerate every object a project reads a count off, so the shape is read
#: instead of the prefix.
_MEMBER_ACCESS = "."

#: How long a member expression may be and still read as code.
#:
#: The dot alone is not quite enough, because some real credentials are dotted
#: too -- a JWT, a SendGrid or Airtable key. The JWT is caught by
#: ``prefixed_key`` whatever this rule says, but the others are not, and before
#: this narrowing they blocked on the ``=`` separator alone. Letting them
#: through would be a regression, not a refinement.
#:
#: What separates them is length, and not marginally: the longest property
#: chain in the production evidence is ``result.usage.totalTokens`` at 24
#: characters, and ``this.state.response.usage.totalTokens`` reaches 37, while
#: dotted key material starts around 59 because its segments are random. The
#: bound sits in that gap. A length threshold is the same kind of judgement
#: the scanner already makes in ``MIN_REDACTABLE_LENGTH`` and in the shapes'
#: own ``{6,}``.
_MAX_MEMBER_EXPRESSION_LENGTH = 48

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
            if name in _NAME_INFERRED_SHAPES and _insufficient_evidence(text, match):
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


def _insufficient_evidence(text: str, match: re.Match[str]) -> bool:
    """Whether a name-inferred match is too weak to block a candidate on.

    The third thing, after references and placeholders, that wears a
    credential's shape without being one: a type annotation. The campaign line
    was ``readonly totalTokens: number;`` -- ``TOKEN`` is a secret-name hint
    and must stay one, the shared shape accepts ``:`` as a separator, and
    ``number`` is six characters, is no environment lookup and is no known
    placeholder. Every ingredient of a secret was there except a secret: what
    follows the colon is the *type* of the value, and the value is not on the
    line.

    Rather than prove the line is a declaration -- which would mean knowing the
    grammar of every language the orchestrator builds for -- this asks the
    cheaper and sounder question: **is there enough evidence here to block?**
    Three structural facts, all taken from the match itself, and a genuine
    credential contradicts at least one:

    1. **The value is unquoted.** A quoted value is data. This alone keeps
       every JSON and YAML string credential, and every ``key = "sk-..."``.
    2. **The value is spelled as an identifier**, optionally qualified.
       Credential material is not: it carries punctuation or a provider
       prefix, and both are what the content-identifying shapes read. A call
       is not one either -- ``getToken()`` keeps its bracket, so
       ``apiToken = getToken()`` is left to block.
    3. **What the separator then licenses**, and this is where the two
       readings part:

       * After ``:`` a bare word is enough. ``totalTokens: number``,
         ``apiKey: string`` and every project type beside them annotate what
         the value *is*; the value itself is not on the line.
       * After ``=`` a bare word is **not** enough, because ``TOKEN=abc123``
         really is a literal. What is enough is a **member expression**:
         ``totalTokens = usage.totalTokens`` reads a property off another
         object, so again the value is not on the line -- provided it is short
         enough to be a property chain and not a dotted key. See
         ``_MEMBER_ACCESS`` and ``_MAX_MEMBER_EXPRESSION_LENGTH``.

    In both cases what is left is a credential-shaped *name* next to something
    that is not a credential-shaped *value*, and a name is not evidence of a
    value.

    This is a decision not to *block*, not a decision that the line is safe.
    The loop continues to the content-identifying shapes, so a real key on the
    same line still fails closed, and redaction -- which pays nothing for a
    false positive -- goes on masking all of it.
    """
    before = text[: match.start("secret")]
    # The shape consumes an opening quote before the value, so a quote here
    # means the value was quoted: data, not an annotation or a property read.
    if before.endswith(('"', "'")):
        return False
    value = match.group("secret")
    if not _IDENTIFIER_VALUE.match(value):
        return False
    separator = before.rstrip()[-1:]
    if separator == ":":
        # An annotation: the name is being told what type it has.
        return True
    if separator == "=":
        # An assignment, so a bare word here would be a literal and must
        # block. Only a member expression says the value is elsewhere -- and
        # only one short enough to be a property chain rather than a dotted
        # key.
        return (
            _MEMBER_ACCESS in value
            and len(value) <= _MAX_MEMBER_EXPRESSION_LENGTH
        )
    return False


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
