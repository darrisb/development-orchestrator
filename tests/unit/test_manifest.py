"""Manifest parsing (build.md section 5)."""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.enums import Complexity, RiskLevel, TaskStatus, WorkerProfile
from apps.orchestrator.domain.errors import (
    DependencyCycleError,
    ManifestError,
    UnknownDependencyError,
)
from apps.orchestrator.domain.manifest import parse_manifest


def test_the_documented_example_parses(manifest_document: dict):
    manifest = parse_manifest(manifest_document)

    assert manifest.version == 1
    assert manifest.external_id == "tracestack"
    assert manifest.name == "TraceStack"
    assert manifest.repository_path == "/workspace/tracestack"
    assert manifest.default_branch == "main"
    assert manifest.worker_profile is WorkerProfile.NODE
    assert manifest.max_parallel_tasks == 1
    assert manifest.milestone_interval == 5
    assert manifest.protected_paths == (".git/**", ".env", "secrets/**")
    assert manifest.model_policy.default_coder == "qwen-coder-14b"
    assert manifest.model_policy.high_complexity_coder == "qwen-coder-30b"
    assert manifest.model_policy.reviewer == "primary-reviewer"

    first, second = manifest.tasks
    assert first.external_id == "TS-001"
    assert first.section == 1
    assert first.complexity is Complexity.LOW
    assert first.status is TaskStatus.PENDING
    assert first.verify_commands == ("npm run compile", "npm test")
    assert first.limits.max_diff_lines == 1200
    assert second.depends_on == ("TS-001",)
    assert manifest.dependency_graph == {"TS-001": (), "TS-002": ("TS-001",)}


def _minimal(**overrides) -> dict:
    document = {
        "version": 1,
        "project": {"id": "p", "name": "P", "repository": "/workspace/p"},
        "tasks": [{"id": "T-1", "title": "First"}],
    }
    document.update(overrides)
    return document


def test_optional_sections_fall_back_to_documented_defaults():
    manifest = parse_manifest(_minimal())
    task = manifest.tasks[0]

    assert manifest.default_branch == "main"
    assert manifest.worker_profile is WorkerProfile.NODE
    assert manifest.max_parallel_tasks == 1
    assert manifest.milestone_interval is None
    assert manifest.protected_paths == ()
    assert task.status is TaskStatus.PENDING
    assert task.complexity is Complexity.MEDIUM
    assert task.risk_level is RiskLevel.LOW
    assert task.depends_on == ()
    assert task.verify_commands == ()
    # Section 23 defaults.
    assert (task.limits.max_attempts, task.limits.max_review_cycles) == (3, 3)
    assert task.limits.max_runtime_minutes == 30
    assert task.limits.max_files_changed == 12
    assert task.limits.max_diff_lines == 1200


def test_partial_limits_keep_the_defaults_for_the_rest():
    manifest = parse_manifest(
        _minimal(tasks=[{"id": "T-1", "title": "First", "limits": {"max_attempts": 5}}])
    )
    limits = manifest.tasks[0].limits
    assert limits.max_attempts == 5
    assert limits.max_diff_lines == 1200


def test_enum_values_are_matched_case_insensitively():
    """Manifests are hand-written; `status: pending` and `PENDING` both work."""
    manifest = parse_manifest(
        _minimal(
            runtime={"worker_profile": "PYTHON"},
            tasks=[
                {
                    "id": "T-1",
                    "title": "First",
                    "status": "PENDING",
                    "complexity": "HIGH",
                    "risk_level": "High",
                }
            ],
        )
    )
    assert manifest.worker_profile is WorkerProfile.PYTHON
    assert manifest.tasks[0].complexity is Complexity.HIGH
    assert manifest.tasks[0].risk_level is RiskLevel.HIGH


def test_a_task_may_be_declared_already_complete():
    """So an adopted repository can record work finished before onboarding."""
    manifest = parse_manifest(
        _minimal(tasks=[{"id": "T-1", "title": "First", "status": "COMPLETE"}])
    )
    assert manifest.tasks[0].status is TaskStatus.COMPLETE


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("not a mapping", "manifest must be a mapping"),
        ({"project": {}, "tasks": []}, "version must be an integer"),
        ({"version": 99, "project": {}, "tasks": []}, "Unsupported manifest version"),
        ({"version": 1, "tasks": []}, "project must be a mapping"),
    ],
)
def test_structural_problems_are_rejected(document, message):
    with pytest.raises(ManifestError, match=message):
        parse_manifest(document)


def test_a_misspelled_key_is_rejected_rather_than_ignored():
    """`verifiy:` would otherwise import a task with no verification at all."""
    with pytest.raises(ManifestError, match="Unknown key\\(s\\) in task T-1: verifiy"):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "verifiy": ["npm test"]}]))


def test_an_unknown_top_level_key_is_rejected():
    with pytest.raises(ManifestError, match="Unknown key\\(s\\) in manifest: runtimes"):
        parse_manifest(_minimal(runtimes={}))


def test_tasks_must_not_be_empty():
    with pytest.raises(ManifestError, match="at least one task"):
        parse_manifest(_minimal(tasks=[]))


def test_duplicate_task_ids_are_rejected():
    with pytest.raises(ManifestError, match="Duplicate task id T-1"):
        parse_manifest(
            _minimal(tasks=[{"id": "T-1", "title": "A"}, {"id": "T-1", "title": "B"}])
        )


def test_a_task_may_not_declare_a_runtime_status():
    with pytest.raises(ManifestError, match="may only declare"):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "status": "CODING"}]))


def test_an_unknown_status_lists_the_allowed_values():
    with pytest.raises(ManifestError, match="status must be one of"):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "status": "done"}]))


def test_self_dependency_is_rejected_with_a_clear_message():
    with pytest.raises(ManifestError, match="Task T-1 depends on itself"):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "depends_on": ["T-1"]}]))


def test_dependency_validation_runs_at_parse_time():
    with pytest.raises(UnknownDependencyError):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "depends_on": ["T-9"]}]))


def test_a_dependency_cycle_never_reaches_the_importer():
    with pytest.raises(DependencyCycleError):
        parse_manifest(
            _minimal(
                tasks=[
                    {"id": "T-1", "title": "A", "depends_on": ["T-2"]},
                    {"id": "T-2", "title": "B", "depends_on": ["T-1"]},
                ]
            )
        )


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"max_attempts": 0}, "must be >= 1"),
        ({"max_attempts": True}, "must be an integer"),
        ({"max_attempts": "3"}, "must be an integer"),
        ({"max_atempts": 3}, "Unknown key"),
    ],
)
def test_invalid_limits_are_rejected(limits, message):
    with pytest.raises(ManifestError, match=message):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "limits": limits}]))


def test_blank_required_strings_are_rejected():
    with pytest.raises(ManifestError, match="title must be a non-empty string"):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "   "}]))


def test_a_string_where_a_list_belongs_is_rejected():
    with pytest.raises(ManifestError, match="depends_on must be a list"):
        parse_manifest(_minimal(tasks=[{"id": "T-1", "title": "T", "depends_on": "T-0"}]))


def test_milestone_interval_must_be_positive():
    with pytest.raises(ManifestError, match="milestone_interval must be >= 1"):
        parse_manifest(_minimal(verification={"milestone_interval": 0}))


def test_max_parallel_tasks_is_accepted_but_must_be_positive():
    assert parse_manifest(_minimal(runtime={"max_parallel_tasks": 4})).max_parallel_tasks == 4
    with pytest.raises(ManifestError, match="max_parallel_tasks must be >= 1"):
        parse_manifest(_minimal(runtime={"max_parallel_tasks": 0}))


# --- Task-declared files (section 6) ----------------------------------------


def _with_files(files: dict) -> dict:
    return _minimal(tasks=[{"id": "T-1", "title": "First", "files": files}])


def test_a_task_declares_what_to_read_and_what_it_may_write():
    task = parse_manifest(
        _with_files({"inspect": ["./src/a.ts"], "modify": ["src/b.ts"], "create": ["src/c.ts"]})
    ).tasks[0]

    assert task.files_to_inspect == ("src/a.ts",)
    assert task.files_to_modify == ("src/b.ts",)
    assert task.files_to_create == ("src/c.ts",)


def test_a_task_without_a_files_block_declares_nothing():
    task = parse_manifest(_minimal()).tasks[0]

    assert task.files_to_inspect == ()
    assert task.files_to_modify == ()
    assert task.files_to_create == ()


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.ts", "~/secrets.env"])
def test_a_path_that_leaves_the_repository_is_rejected(path: str):
    """These lists become the coder's reading list and the scope guard's allowance."""
    with pytest.raises(ManifestError, match="files.modify"):
        parse_manifest(_with_files({"modify": [path]}))


def test_an_unknown_files_key_is_rejected_like_any_other_typo():
    with pytest.raises(ManifestError, match="files"):
        parse_manifest(_with_files({"modifiy": ["src/a.ts"]}))
