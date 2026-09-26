"""The scope guard (build.md section 20).

Each test names the check from section 20's list it exercises. The interesting
property is not that a violation is noticed but that a *permitted* change stays
permitted: a guard that escalates every diff would be turned off within a week.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.orchestrator.domain.enums import ScopePolicyDecision
from apps.orchestrator.domain.git import ChangeType, DiffSummary, FileChange
from apps.orchestrator.domain.models import Project, Task, TaskLimits
from apps.orchestrator.domain.scope import (
    ScopeFindingKind,
    ScopePolicy,
    SensitiveCategory,
    categorise_path,
    check_write_path,
    evaluate_scope,
    is_within_repository,
    path_matches_declaration,
    path_matches_write_declaration,
)


@pytest.fixture
def task() -> Task:
    return Task(
        project_id=uuid4(),
        external_task_id="TS-004",
        title="Implement navigation tree",
        files_to_inspect=["src/widgets/tree.ts"],
        files_to_modify=["src/navigation.ts"],
        files_to_create=["src/navigationTree.ts"],
        limits=TaskLimits(max_files_changed=3, max_diff_lines=100),
    )


@pytest.fixture
def policy(task: Task) -> ScopePolicy:
    return ScopePolicy.for_task(
        task,
        Project(name="TraceStack", repository_path="/workspace/tracestack",
                protected_paths=["secrets/**", "infra/**"]),
    )


def _summary(*changes: FileChange) -> DiffSummary:
    return DiffSummary(changes)


def _change(path: str, **kwargs) -> FileChange:
    kwargs.setdefault("insertions", 10)
    kwargs.setdefault("deletions", 2)
    return FileChange(path=path, **kwargs)


# --- allowed paths -----------------------------------------------------------


def test_a_change_inside_the_declared_allowance_is_allowed(policy: ScopePolicy):
    assessment = evaluate_scope(
        _summary(
            _change("src/navigation.ts"),
            _change("src/navigationTree.ts", change_type=ChangeType.ADDED),
        ),
        policy,
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert assessment.findings == ()
    assert assessment.files_changed == 2


def test_a_path_outside_the_allowance_is_blocked(policy: ScopePolicy):
    assessment = evaluate_scope(_summary(_change("src/billing.ts")), policy)

    assert assessment.decision is ScopePolicyDecision.BLOCK
    kinds = {finding.kind for finding in assessment.blocking_findings}
    assert ScopeFindingKind.OUTSIDE_ALLOWANCE in kinds


def test_a_file_declared_for_inspection_may_not_be_modified(policy: ScopePolicy):
    """The distinction the manifest drew is the distinction enforced: a task
    that said "read this" did not say "change this"."""
    assessment = evaluate_scope(_summary(_change("src/widgets/tree.ts")), policy)

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert assessment.blocking_findings[0].kind is ScopeFindingKind.INSPECT_ONLY


def test_a_task_with_no_file_list_is_bounded_by_size_not_by_paths():
    task = Task(
        project_id=uuid4(),
        external_task_id="TS-009",
        title="Tidy the sidebar",
        limits=TaskLimits(max_files_changed=2, max_diff_lines=50),
    )
    policy = ScopePolicy.for_task(task)

    assert evaluate_scope(_summary(_change("src/anything.ts")), policy).decision is (
        ScopePolicyDecision.ALLOW
    )
    over = evaluate_scope(
        _summary(_change("a.ts"), _change("b.ts"), _change("c.ts")), policy
    )
    assert over.decision is ScopePolicyDecision.BLOCK
    assert over.blocking_findings[0].kind is ScopeFindingKind.TOO_MANY_FILES


# --- protected paths ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [".git/config", ".env", "config/.env", "secrets/api.key", "infra/main.tf",
     "build.tasks.yaml", "deploy/id_rsa"],
)
def test_a_protected_path_is_never_writable(policy: ScopePolicy, path: str):
    """The project's list and the built-in floor are both enforced, and the
    manifest itself is on the floor: a coder that can edit its own task can
    edit its own verification commands."""
    finding = check_write_path(path, policy)

    assert finding is not None
    assert finding.decision is ScopePolicyDecision.BLOCK


def test_a_protected_path_blocks_even_when_the_task_declared_it():
    task = Task(
        project_id=uuid4(),
        external_task_id="TS-010",
        title="Rotate credentials",
        files_to_modify=[".env"],
    )
    policy = ScopePolicy.for_task(task)

    assert check_write_path(".env", policy) is not None


def test_a_rename_out_of_a_protected_path_checks_the_original_name():
    policy = ScopePolicy(
        allowed_paths=("config/environment.txt",),
        max_files_changed=2,
        max_diff_lines=100,
    )
    assessment = evaluate_scope(
        _summary(
            _change(
                "config/environment.txt",
                original_path=".env",
                change_type=ChangeType.RENAMED,
            )
        ),
        policy,
    )

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert any(
        finding.kind is ScopeFindingKind.PROTECTED_PATH and finding.path == ".env"
        for finding in assessment.blocking_findings
    )


# --- limits ------------------------------------------------------------------


def test_too_many_diff_lines_blocks(policy: ScopePolicy):
    assessment = evaluate_scope(
        _summary(_change("src/navigation.ts", insertions=400, deletions=0)), policy
    )

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert assessment.blocking_findings[0].kind is ScopeFindingKind.TOO_MANY_DIFF_LINES


def test_an_empty_diff_blocks_rather_than_reaching_a_reviewer(policy: ScopePolicy):
    assessment = evaluate_scope(DiffSummary(), policy)

    assert assessment.decision is ScopePolicyDecision.BLOCK
    assert assessment.blocking_findings[0].kind is ScopeFindingKind.NO_CHANGES


# --- deletions, binaries and sensitive categories ----------------------------


def test_an_undeclared_deletion_asks_for_a_human(policy: ScopePolicy):
    task_policy = ScopePolicy(allowed_paths=(), max_files_changed=5, max_diff_lines=500)
    assessment = evaluate_scope(
        _summary(_change("src/old.ts", change_type=ChangeType.DELETED)), task_policy
    )

    assert assessment.decision is ScopePolicyDecision.REQUIRE_REVIEW
    assert assessment.findings[0].kind is ScopeFindingKind.DELETION


def test_a_declared_deletion_is_the_task_and_is_allowed():
    policy = ScopePolicy(
        allowed_paths=("src/old.ts",), max_files_changed=5, max_diff_lines=500
    )
    assessment = evaluate_scope(
        _summary(_change("src/old.ts", change_type=ChangeType.DELETED)), policy
    )

    assert assessment.decision is ScopePolicyDecision.ALLOW


def test_a_binary_file_asks_for_a_human_because_the_diff_cannot_be_read():
    policy = ScopePolicy(max_files_changed=5, max_diff_lines=500)
    assessment = evaluate_scope(
        _summary(FileChange(path="assets/icon.png", insertions=None, deletions=None)), policy
    )

    assert assessment.decision is ScopePolicyDecision.REQUIRE_REVIEW
    assert ScopeFindingKind.BINARY_FILE in {finding.kind for finding in assessment.findings}


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("package-lock.json", SensitiveCategory.LOCKFILE),
        ("pyproject.toml", SensitiveCategory.DEPENDENCY_MANIFEST),
        ("migrations/versions/0001_init.py", SensitiveCategory.MIGRATION),
        ("src/auth/session.ts", SensitiveCategory.SECURITY),
        ("src/billing/invoice.ts", SensitiveCategory.PAYMENT),
        (".github/workflows/ci.yml", SensitiveCategory.CI_DEPLOYMENT),
    ],
)
def test_every_category_section_20_names_is_recognised(path: str, category: SensitiveCategory):
    assert category in categorise_path(path)


def test_an_undeclared_sensitive_change_asks_for_a_human_and_a_declared_one_does_not():
    undeclared = ScopePolicy(max_files_changed=5, max_diff_lines=500)
    assert evaluate_scope(_summary(_change("package-lock.json")), undeclared).decision is (
        ScopePolicyDecision.REQUIRE_REVIEW
    )

    declared = ScopePolicy(
        allowed_paths=("package-lock.json",), max_files_changed=5, max_diff_lines=500
    )
    assessment = evaluate_scope(_summary(_change("package-lock.json")), declared)
    assert assessment.decision is ScopePolicyDecision.ALLOW
    assert SensitiveCategory.LOCKFILE in assessment.sensitive


def test_one_block_outweighs_any_number_of_softer_findings():
    policy = ScopePolicy(allowed_paths=("src/navigation.ts",), max_files_changed=5)
    assessment = evaluate_scope(
        _summary(_change("src/navigation.ts"), _change("package-lock.json"), _change(".env")),
        policy,
    )

    assert assessment.decision is ScopePolicyDecision.BLOCK


# --- path handling -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "declaration", "expected"),
    [
        ("src/navigation.ts", "src/navigation.ts", True),
        ("src/deep/file.ts", "src", True),
        ("src/deep/file.ts", "src/*.ts", False),
        ("src/file.ts", "src/*.ts", True),
        ("src/a/b.ts", "src/**", True),
        ("other/navigation.ts", "src/navigation.ts", False),
    ],
)
def test_a_declaration_is_read_the_same_way_the_context_builder_reads_it(
    path: str, declaration: str, expected: bool
):
    assert path_matches_declaration(path, declaration) is expected


def test_a_bare_write_declaration_does_not_match_the_same_name_at_any_depth():
    assert path_matches_write_declaration("config.ts", "config.ts")
    assert not path_matches_write_declaration("src/deep/config.ts", "config.ts")


@pytest.mark.parametrize(
    ("path", "declaration", "expected"),
    [
        ("src/deep/file.ts", "src", True),
        ("src/file.ts", "src/*.ts", True),
        ("src/deep/file.ts", "src/*.ts", False),
        ("src/deep/file.ts", "src/**", True),
        ("root.py", "*.py", True),
        ("src/nested.py", "*.py", False),
        ("src/nested.py", "**/*.py", True),
    ],
)
def test_write_allowances_only_widen_through_directories_or_explicit_globs(
    path: str, declaration: str, expected: bool
):
    assert path_matches_write_declaration(path, declaration) is expected


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "~/.ssh/config", "../outside.ts", "src/../../escape.ts", ""]
)
def test_a_path_that_leaves_the_repository_is_refused_not_normalised(path: str):
    assert is_within_repository(path) is False


@pytest.mark.parametrize("path", ["src/navigation.ts", "a.ts", "deep/nested/path.ts"])
def test_an_ordinary_relative_path_is_accepted(path: str):
    assert is_within_repository(path) is True
