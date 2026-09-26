"""The Scope Guard (build.md section 20).

Before a candidate reaches review, its actual changes are measured against the
task's declared scope. Every check in section 20 -- file count, diff lines,
protected paths, allowed paths, deletions, dependency and lockfile changes,
migrations, security, payment and CI files -- happens here, and the answer is
one of three policy decisions: ``ALLOW``, ``REQUIRE_REVIEW`` or ``BLOCK``.

Pure functions over paths and a ``DiffSummary``: no filesystem, no repository,
no model. Two properties matter.

* **The allowance is code, not a prompt.** The task's file lists are read as a
  write allowance and enforced against the diff, so a coder that was asked
  politely not to touch a file and did so anyway is stopped rather than
  reviewed. The same rule is applied *before* each edit is written
  (``services.code_edits``), so a forbidden path is normally never touched at
  all; this module is the audit that proves it.
* **A weak specification is not permission.** A task that declared no file
  list gets no allowance check -- there is nothing to check against -- but it
  still gets the size limits, the protected paths and the sensitive-category
  checks. The absence of a boundary never widens one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath

from .enums import ScopePolicyDecision
from .git import ChangeType, DiffSummary, FileChange
from .models import Project, Task
from .relevance import matches_pattern, normalise_path

#: Paths no task may ever write, whatever its manifest says. These are the
#: floor beneath a project's own ``protected_paths``: the repository's own
#: plumbing, its credentials, and -- deliberately -- the task manifest itself,
#: because a coder that can edit ``build.tasks.yaml`` can edit its own
#: verification commands and its own scope.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".git/**",
    ".gitignore",
    ".env",
    ".env.*",
    "**/.env",
    "secrets/**",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    ".ssh/**",
    "build.tasks.yaml",
)


class SensitiveCategory(StrEnum):
    """Change categories section 20 asks about by name.

    A change here is not wrong -- a migration task is meant to add a migration
    -- but it is never *incidental*. Touching one of these without the task
    declaring it is the difference between ``ALLOW`` and ``REQUIRE_REVIEW``.
    """

    LOCKFILE = "lockfile"
    DEPENDENCY_MANIFEST = "dependency_manifest"
    MIGRATION = "migration"
    SECURITY = "security"
    PAYMENT = "payment"
    CI_DEPLOYMENT = "ci_deployment"


SENSITIVE_PATTERNS: dict[SensitiveCategory, tuple[str, ...]] = {
    SensitiveCategory.LOCKFILE: (
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
    ),
    SensitiveCategory.DEPENDENCY_MANIFEST: (
        "package.json",
        "pyproject.toml",
        "requirements*.txt",
        "Pipfile",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
        "Gemfile",
    ),
    SensitiveCategory.MIGRATION: (
        "**/migrations/**",
        "**/migration/**",
        "**/alembic/versions/**",
        "**/db/migrate/**",
        "**/flyway/**",
        "**/liquibase/**",
    ),
    SensitiveCategory.SECURITY: (
        "**/auth/**",
        "**/authentication/**",
        "**/authorization/**",
        "**/security/**",
        "**/credentials/**",
        "**/passwords/**",
        "**/tokens/**",
        "**/sessions/**",
    ),
    SensitiveCategory.PAYMENT: (
        "**/payment*/**",
        "**/billing/**",
        "**/checkout/**",
        "**/*payment*.*",
        "**/*billing*.*",
        "**/*invoice*.*",
        "**/*stripe*.*",
    ),
    SensitiveCategory.CI_DEPLOYMENT: (
        ".github/**",
        ".gitlab-ci.yml",
        ".circleci/**",
        "Jenkinsfile",
        "azure-pipelines.yml",
        "Dockerfile*",
        "docker-compose*.yml",
        "**/k8s/**",
        "**/kubernetes/**",
        "**/helm/**",
        "**/terraform/**",
        "*.tf",
        "**/deploy/**",
        "Procfile",
    ),
}


class ScopeFindingKind(StrEnum):
    """Why the guard has something to say about a change."""

    PROTECTED_PATH = "protected_path"
    OUTSIDE_ALLOWANCE = "outside_allowance"
    INSPECT_ONLY = "inspect_only"
    DELETION = "deletion"
    BINARY_FILE = "binary_file"
    SENSITIVE_CATEGORY = "sensitive_category"
    TOO_MANY_FILES = "too_many_files"
    TOO_MANY_DIFF_LINES = "too_many_diff_lines"
    NO_CHANGES = "no_changes"


@dataclass(frozen=True, slots=True)
class ScopeFinding:
    """One thing the guard noticed, and what it costs.

    ``decision`` is this finding's own verdict. The assessment's decision is
    the strictest of them, never an average: one ``BLOCK`` blocks.
    """

    kind: ScopeFindingKind
    decision: ScopePolicyDecision
    detail: str
    path: str | None = None
    category: SensitiveCategory | None = None

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "decision": self.decision.value,
            "detail": self.detail,
            "path": self.path,
            "category": self.category.value if self.category else None,
        }


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """What this task is allowed to change.

    Attributes:
        allowed_paths: the task's ``modify`` and ``create`` lists. Empty means
            the task declared no allowance, which disables the allowance check
            and nothing else.
        inspect_only_paths: named by the task as *read* material. Writing one
            is a distinct finding from writing an undeclared path: it is not
            an oversight in the manifest, it is the coder ignoring a boundary
            the task stated.
        protected_paths: the project's list, on top of
            ``DEFAULT_PROTECTED_PATHS``.
        max_files_changed: hard ceiling from the task's limits.
        max_diff_lines: hard ceiling from the task's limits.
    """

    allowed_paths: tuple[str, ...] = ()
    inspect_only_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    sensitive_path_exceptions: tuple[str, ...] = ()
    max_files_changed: int = 12
    max_diff_lines: int = 1200

    @classmethod
    def for_task(cls, task: Task, project: Project | None = None) -> ScopePolicy:
        """The policy for one task, merging the project's protected paths in."""
        protected = [*DEFAULT_PROTECTED_PATHS]
        if project is not None:
            protected.extend(
                pattern for pattern in project.protected_paths if pattern not in protected
            )
        return cls(
            allowed_paths=tuple(normalise_path(path) for path in task.allowed_paths),
            inspect_only_paths=tuple(normalise_path(path) for path in task.files_to_inspect),
            protected_paths=tuple(protected),
            sensitive_path_exceptions=tuple(
                project.sensitive_path_exceptions if project is not None else ()
            ),
            max_files_changed=task.limits.max_files_changed,
            max_diff_lines=task.limits.max_diff_lines,
        )

    @property
    def has_allowance(self) -> bool:
        return bool(self.allowed_paths)

    def is_protected(self, path: str) -> bool:
        return any(matches_pattern(path, pattern) for pattern in self.protected_paths)

    def is_allowed(self, path: str) -> bool:
        """Whether ``path`` is inside the write allowance.

        ``True`` for every path when the task declared none: see the module
        docstring -- a missing allowance is a weaker specification, and the
        size and protected-path checks still apply.
        """
        if not self.has_allowance:
            return True
        return any(
            path_matches_write_declaration(path, declaration)
            for declaration in self.allowed_paths
        )

    def is_inspect_only(self, path: str) -> bool:
        """Declared as read material and not also declared as writable.

        Checked against ``allowed_paths`` directly rather than through
        ``is_allowed``: a task that lists inspect files and no writable ones
        has still said "read this", and that statement should hold even though
        there is no allowance to measure the rest of the diff against.
        """
        if not any(
            path_matches_declaration(path, declaration)
            for declaration in self.inspect_only_paths
        ):
            return False
        return not any(
            path_matches_write_declaration(path, declaration)
            for declaration in self.allowed_paths
        )


@dataclass(frozen=True, slots=True)
class ScopeAssessment:
    """The guard's verdict on one candidate diff."""

    decision: ScopePolicyDecision
    findings: tuple[ScopeFinding, ...] = ()
    files_changed: int = 0
    diff_lines: int = 0
    paths: tuple[str, ...] = ()
    sensitive: tuple[SensitiveCategory, ...] = field(default=())

    @property
    def allowed(self) -> bool:
        return self.decision is not ScopePolicyDecision.BLOCK

    @property
    def blocking_findings(self) -> tuple[ScopeFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.decision is ScopePolicyDecision.BLOCK
        )

    def summary(self) -> str:
        """One line for a log, an event payload or an escalation."""
        if not self.findings:
            return f"{self.decision}: {self.files_changed} file(s), {self.diff_lines} line(s)"
        return f"{self.decision}: " + "; ".join(finding.detail for finding in self.findings)

    def describe(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "files_changed": self.files_changed,
            "diff_lines": self.diff_lines,
            "paths": list(self.paths),
            "sensitive": [category.value for category in self.sensitive],
            "findings": [finding.describe() for finding in self.findings],
        }


def path_matches_declaration(path: str, declaration: str) -> bool:
    """Whether ``path`` is covered by a declared path, glob or directory.

    The same reading as the context builder's expansion of ``files:``, on
    purpose: the list a coder was shown as its reading list and the list its
    writes are measured against must mean the same thing, or the task
    specification would be describing a boundary that is not the one enforced.
    """
    candidate = normalise_path(path)
    declared = normalise_path(declaration)
    if not declared:
        return False
    if candidate == declared:
        return True
    if matches_pattern(candidate, declared):
        return True
    # A declared directory covers everything beneath it.
    return candidate.startswith(f"{declared}/")


def path_matches_write_declaration(path: str, declaration: str) -> bool:
    """Whether a write declaration covers ``path`` without bare-name widening.

    Context selection treats ``config.ts`` as a repository-wide search. A
    write allowance must not: a root filename may not silently grant access to
    the same filename at arbitrary depth. Directories and explicit globs are
    the only widening forms.
    """
    candidate = normalise_path(path)
    declared = normalise_path(declaration)
    if not declared:
        return False
    if candidate == declared:
        return True
    has_glob = any(character in declared for character in "*?[")
    if has_glob:
        if "/" not in declared and "/" in candidate:
            return False
        return matches_pattern(candidate, declared)
    return candidate.startswith(f"{declared}/")


def categorise_path(
    path: str, *, exceptions: tuple[str, ...] = ()
) -> tuple[SensitiveCategory, ...]:
    """Every sensitive category ``path`` falls into, in enum order."""
    candidate = normalise_path(path)
    if any(matches_pattern(candidate, pattern) for pattern in exceptions):
        return ()
    return tuple(
        category
        for category, patterns in SENSITIVE_PATTERNS.items()
        if any(matches_pattern(candidate, pattern) for pattern in patterns)
    )


def check_write_path(path: str, policy: ScopePolicy) -> ScopeFinding | None:
    """The pre-write check: may this path be written at all?

    Returns the blocking finding, or ``None`` when the write is permitted.
    Only the two checks that can be decided from the path alone are made here
    -- protected and outside-the-allowance -- because they are the two that
    must stop a write from happening rather than be reported after it has.
    """
    candidate = normalise_path(path)
    if policy.is_protected(candidate):
        return ScopeFinding(
            kind=ScopeFindingKind.PROTECTED_PATH,
            decision=ScopePolicyDecision.BLOCK,
            detail=f"{candidate} is a protected path and may never be written",
            path=candidate,
        )
    if policy.is_inspect_only(candidate):
        return ScopeFinding(
            kind=ScopeFindingKind.INSPECT_ONLY,
            decision=ScopePolicyDecision.BLOCK,
            detail=(
                f"{candidate} was declared for inspection only; the task does not "
                f"allow it to be modified"
            ),
            path=candidate,
        )
    if not policy.is_allowed(candidate):
        return ScopeFinding(
            kind=ScopeFindingKind.OUTSIDE_ALLOWANCE,
            decision=ScopePolicyDecision.BLOCK,
            detail=(
                f"{candidate} is outside the task's declared allowance "
                f"({', '.join(policy.allowed_paths)})"
            ),
            path=candidate,
        )
    return None


def evaluate_scope(summary: DiffSummary, policy: ScopePolicy) -> ScopeAssessment:
    """Measure a candidate diff against ``policy`` (section 20).

    An empty diff is ``BLOCK``: a run that changed nothing has not done the
    task, and sending it to a reviewer would spend a review cycle discovering
    that.
    """
    findings: list[ScopeFinding] = []
    sensitive: list[SensitiveCategory] = []

    for change in summary.files:
        findings.extend(_findings_for_change(change, policy, sensitive))

    if summary.files_changed > policy.max_files_changed:
        findings.append(
            ScopeFinding(
                kind=ScopeFindingKind.TOO_MANY_FILES,
                decision=ScopePolicyDecision.BLOCK,
                detail=(
                    f"changed {summary.files_changed} files, the task allows "
                    f"{policy.max_files_changed}"
                ),
            )
        )
    if summary.line_count > policy.max_diff_lines:
        findings.append(
            ScopeFinding(
                kind=ScopeFindingKind.TOO_MANY_DIFF_LINES,
                decision=ScopePolicyDecision.BLOCK,
                detail=(
                    f"diff is {summary.line_count} lines, the task allows "
                    f"{policy.max_diff_lines}"
                ),
            )
        )
    if not summary.files:
        findings.append(
            ScopeFinding(
                kind=ScopeFindingKind.NO_CHANGES,
                decision=ScopePolicyDecision.BLOCK,
                detail="the candidate changes nothing, so there is nothing to review",
            )
        )

    return ScopeAssessment(
        decision=_strictest(findings),
        findings=tuple(findings),
        files_changed=summary.files_changed,
        diff_lines=summary.line_count,
        paths=summary.paths,
        sensitive=tuple(dict.fromkeys(sensitive)),
    )


def _findings_for_change(
    change: FileChange, policy: ScopePolicy, sensitive: list[SensitiveCategory]
) -> list[ScopeFinding]:
    findings: list[ScopeFinding] = []
    path = normalise_path(change.path)

    path_finding = check_write_path(path, policy)
    if path_finding is not None:
        findings.append(path_finding)

    # A rename writes both names: Git reports the destination as ``path`` and
    # the removed source as ``original_path``. Checking only the destination
    # lets a protected file be moved to an innocuous name.
    original_path = normalise_path(change.original_path) if change.original_path else None
    if original_path and original_path != path:
        original_finding = check_write_path(original_path, policy)
        if original_finding is not None:
            findings.append(original_finding)

    if change.change_type is ChangeType.DELETED:
        # A deletion the task named is the task; one it did not is either a
        # mistake or a refactor nobody asked for, and a human should look.
        declared = policy.has_allowance and policy.is_allowed(path)
        findings.append(
            ScopeFinding(
                kind=ScopeFindingKind.DELETION,
                decision=(
                    ScopePolicyDecision.ALLOW if declared else ScopePolicyDecision.REQUIRE_REVIEW
                ),
                detail=(
                    f"{path} was deleted"
                    + ("" if declared else "; the task did not declare a deletion here")
                ),
                path=path,
            )
        )

    if change.is_binary:
        findings.append(
            ScopeFinding(
                kind=ScopeFindingKind.BINARY_FILE,
                decision=ScopePolicyDecision.REQUIRE_REVIEW,
                detail=f"{path} is binary, so the diff cannot be read by a reviewer",
                path=path,
            )
        )

    categories = tuple(
        dict.fromkeys(
            [
                *categorise_path(path, exceptions=policy.sensitive_path_exceptions),
                *(
                    categorise_path(
                        original_path,
                        exceptions=policy.sensitive_path_exceptions,
                    )
                    if original_path
                    else ()
                ),
            ]
        )
    )
    for category in categories:
        sensitive.append(category)
        declared = policy.has_allowance and policy.is_allowed(path)
        findings.append(
            ScopeFinding(
                kind=ScopeFindingKind.SENSITIVE_CATEGORY,
                decision=(
                    ScopePolicyDecision.ALLOW if declared else ScopePolicyDecision.REQUIRE_REVIEW
                ),
                detail=(
                    f"{path} is a {category.value.replace('_', ' ')} change"
                    + (
                        " the task declared"
                        if declared
                        else "; the task did not declare it, so a human decides"
                    )
                ),
                path=path,
                category=category,
            )
        )
    return findings


def _strictest(findings: list[ScopeFinding]) -> ScopePolicyDecision:
    decisions = {finding.decision for finding in findings}
    if ScopePolicyDecision.BLOCK in decisions:
        return ScopePolicyDecision.BLOCK
    if ScopePolicyDecision.REQUIRE_REVIEW in decisions:
        return ScopePolicyDecision.REQUIRE_REVIEW
    return ScopePolicyDecision.ALLOW


def is_within_repository(path: str) -> bool:
    """Whether ``path`` stays inside the repository when resolved textually.

    A cheap structural check for a model-supplied path, made before any
    filesystem call: absolute paths, ``~`` and any ``..`` segment are refused
    rather than normalised away, because normalising ``src/../../etc/passwd``
    produces a path that looks perfectly reasonable.
    """
    raw = path.strip()
    if not raw or raw.startswith(("/", "~", "\\")) or ":" in raw.split("/")[0]:
        return False
    parts = PurePosixPath(raw.replace("\\", "/")).parts
    return ".." not in parts


__all__ = [
    "DEFAULT_PROTECTED_PATHS",
    "SENSITIVE_PATTERNS",
    "ScopeAssessment",
    "ScopeFinding",
    "ScopeFindingKind",
    "ScopePolicy",
    "SensitiveCategory",
    "categorise_path",
    "check_write_path",
    "evaluate_scope",
    "is_within_repository",
    "path_matches_declaration",
    "path_matches_write_declaration",
]
