"""Project manifest model and parser (build.md section 5).

The manifest (`build.tasks.yaml` in each managed repository) is the declared
project/task graph. Parsing is strict and total: a manifest either yields a
fully validated ``ProjectManifest`` or raises ``ManifestError``. Nothing
partially valid reaches the importer, because a typo such as ``verifiy:``
would otherwise silently import a task with no verification commands.

This module takes plain Python data, not a file path, so it stays free of YAML
and I/O concerns; see ``services.manifest_loader`` for loading.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .dependencies import validate_graph
from .enums import Complexity, RiskLevel, TaskStatus, WorkerProfile
from .errors import ManifestError
from .models import TaskLimits
from .relevance import normalise_path
from .verification import VerificationProfile

#: Manifest schema versions this build understands.
SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})

#: Statuses a manifest may assert. Everything else is runtime state owned by
#: the orchestrator: the database is authoritative after import (section 5).
#: ``COMPLETE`` exists so an adopted repository can mark already-done work.
IMPORTABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PENDING, TaskStatus.COMPLETE}
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "project",
        "runtime",
        "model_policy",
        "verification",
        "protected_paths",
        "sensitive_path_exceptions",
        "generated_path_exceptions",
        "dependency_paths",
        "dependency_bootstrap_commands",
        "approval_gated_categories",
        "tasks",
    }
)
_PROJECT_KEYS = frozenset({"id", "name", "repository", "default_branch"})
_RUNTIME_KEYS = frozenset({"worker_profile", "max_parallel_tasks"})
_MODEL_POLICY_KEYS = frozenset({"default_coder", "high_complexity_coder", "reviewer"})
_VERIFICATION_KEYS = frozenset({"build", "lint", "tests", "security", "milestone_interval"})
_TASK_KEYS = frozenset(
    {
        "id",
        "section",
        "title",
        "instructions",
        "status",
        "complexity",
        "risk_level",
        "depends_on",
        "files",
        "limits",
        "verify",
    }
)
#: Section 6: a task declares what to read and what it may write. The lists
#: are the coder's reading list and, later, the scope guard's allowance.
_FILE_KEYS = frozenset({"inspect", "modify", "create"})
_LIMIT_KEYS = frozenset(
    {
        "max_attempts",
        "max_review_cycles",
        "max_runtime_minutes",
        "max_files_changed",
        "max_diff_lines",
    }
)


@dataclass(frozen=True, slots=True)
class ManifestModelPolicy:
    """Declared model preferences, resolved to real models in a later phase."""

    default_coder: str | None = None
    high_complexity_coder: str | None = None
    reviewer: str | None = None


@dataclass(frozen=True, slots=True)
class ManifestTask:
    external_id: str
    title: str
    section: int | None = None
    instructions: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    complexity: Complexity = Complexity.MEDIUM
    risk_level: RiskLevel = RiskLevel.LOW
    depends_on: tuple[str, ...] = ()
    verify_commands: tuple[str, ...] = ()
    files_to_inspect: tuple[str, ...] = ()
    files_to_modify: tuple[str, ...] = ()
    files_to_create: tuple[str, ...] = ()
    limits: TaskLimits = field(default_factory=TaskLimits)


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    version: int
    external_id: str
    name: str
    repository_path: str
    tasks: tuple[ManifestTask, ...]
    default_branch: str = "main"
    worker_profile: WorkerProfile = WorkerProfile.NODE
    max_parallel_tasks: int = 1
    model_policy: ManifestModelPolicy = field(default_factory=ManifestModelPolicy)
    #: The project's verification commands, per category (section 18). The
    #: orchestrator runs exactly these; a model never supplies one.
    verification: VerificationProfile = field(default_factory=VerificationProfile)
    milestone_interval: int | None = None
    protected_paths: tuple[str, ...] = ()
    sensitive_path_exceptions: tuple[str, ...] = ()
    generated_path_exceptions: tuple[str, ...] = ()
    dependency_paths: tuple[str, ...] = ()
    dependency_bootstrap_commands: tuple[str, ...] = ()
    approval_gated_categories: tuple[str, ...] | None = None

    @property
    def dependency_graph(self) -> dict[str, tuple[str, ...]]:
        return {task.external_id: task.depends_on for task in self.tasks}


def parse_manifest(data: Any) -> ProjectManifest:
    """Validate ``data`` and return the manifest it describes.

    Raises:
        ManifestError: the document is structurally invalid, including a
            dependency on an unknown task or a dependency cycle.
    """
    document = _require_mapping(data, "manifest")
    _reject_unknown_keys(document, _TOP_LEVEL_KEYS, "manifest")

    version = _require_int(document.get("version"), "version", minimum=1)
    if version not in SUPPORTED_VERSIONS:
        supported = ", ".join(str(candidate) for candidate in sorted(SUPPORTED_VERSIONS))
        raise ManifestError(f"Unsupported manifest version {version}; supported: {supported}")

    project = _require_mapping(document.get("project"), "project")
    _reject_unknown_keys(project, _PROJECT_KEYS, "project")

    runtime = _optional_mapping(document.get("runtime"), "runtime")
    _reject_unknown_keys(runtime, _RUNTIME_KEYS, "runtime")

    verification = _optional_mapping(document.get("verification"), "verification")
    _reject_unknown_keys(verification, _VERIFICATION_KEYS, "verification")

    milestone_interval = verification.get("milestone_interval")
    tasks = _parse_tasks(document.get("tasks"))
    manifest = ProjectManifest(
        version=version,
        external_id=_require_str(project.get("id"), "project.id"),
        name=_require_str(project.get("name"), "project.name"),
        repository_path=_require_str(project.get("repository"), "project.repository"),
        default_branch=_optional_str(project.get("default_branch"), "project.default_branch")
        or "main",
        worker_profile=_parse_enum(
            runtime.get("worker_profile"),
            WorkerProfile,
            "runtime.worker_profile",
            WorkerProfile.NODE,
        ),
        max_parallel_tasks=(
            1
            if runtime.get("max_parallel_tasks") is None
            else _require_int(
                runtime["max_parallel_tasks"], "runtime.max_parallel_tasks", minimum=1
            )
        ),
        model_policy=_parse_model_policy(document.get("model_policy")),
        verification=_parse_verification_profile(verification),
        milestone_interval=(
            None
            if milestone_interval is None
            else _require_int(milestone_interval, "verification.milestone_interval", minimum=1)
        ),
        protected_paths=_parse_string_list(
            document.get("protected_paths"), "protected_paths"
        ),
        sensitive_path_exceptions=_parse_string_list(
            document.get("sensitive_path_exceptions"), "sensitive_path_exceptions"
        ),
        generated_path_exceptions=_parse_string_list(
            document.get("generated_path_exceptions"), "generated_path_exceptions"
        ),
        dependency_paths=_parse_string_list(
            document.get("dependency_paths"), "dependency_paths"
        ),
        dependency_bootstrap_commands=_parse_string_list(
            document.get("dependency_bootstrap_commands"),
            "dependency_bootstrap_commands",
        ),
        approval_gated_categories=(
            None
            if "approval_gated_categories" not in document
            else _parse_string_list(
                document.get("approval_gated_categories"),
                "approval_gated_categories",
            )
        ),
        tasks=tasks,
    )
    # Dependency validation is part of parsing: an invalid graph must never
    # reach the database (Phase B, step 4).
    validate_graph(manifest.dependency_graph)
    return manifest


def _parse_verification_profile(verification: Mapping[str, Any]) -> VerificationProfile:
    """The project's command profile (section 18).

    Each category is a list of command strings, like section 18's examples.
    A bare scalar is refused rather than wrapped: ``build: npm run compile``
    and ``build: [npm run compile]`` would then both be legal spellings of
    the same thing, and a manifest is easier to review when there is one.
    """
    return VerificationProfile(
        build=_parse_string_list(verification.get("build"), "verification.build"),
        lint=_parse_string_list(verification.get("lint"), "verification.lint"),
        tests=_parse_string_list(verification.get("tests"), "verification.tests"),
        security=_parse_string_list(verification.get("security"), "verification.security"),
    )


def _parse_model_policy(value: Any) -> ManifestModelPolicy:
    policy = _optional_mapping(value, "model_policy")
    _reject_unknown_keys(policy, _MODEL_POLICY_KEYS, "model_policy")
    return ManifestModelPolicy(
        default_coder=_optional_str(policy.get("default_coder"), "model_policy.default_coder"),
        high_complexity_coder=_optional_str(
            policy.get("high_complexity_coder"), "model_policy.high_complexity_coder"
        ),
        reviewer=_optional_str(policy.get("reviewer"), "model_policy.reviewer"),
    )


def _parse_tasks(value: Any) -> tuple[ManifestTask, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ManifestError("tasks must be a list")
    if not value:
        raise ManifestError("tasks must contain at least one task")

    tasks: list[ManifestTask] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        task = _parse_task(entry, index)
        if task.external_id in seen:
            raise ManifestError(f"Duplicate task id {task.external_id}")
        seen.add(task.external_id)
        tasks.append(task)
    return tuple(tasks)


def _parse_task(value: Any, index: int) -> ManifestTask:
    entry = _require_mapping(value, f"tasks[{index}]")
    # Name the task in every message when it has a usable id; the list position
    # is a poor way to point an author at the line they mistyped.
    label = entry.get("id") if isinstance(entry.get("id"), str) else None
    where = f"task {label.strip()}" if label and label.strip() else f"tasks[{index}]"
    _reject_unknown_keys(entry, _TASK_KEYS, where)
    task_id = _require_str(entry.get("id"), f"{where}.id")

    section = entry.get("section")
    depends_on = _parse_string_list(entry.get("depends_on"), f"{where}.depends_on")
    if task_id in depends_on:
        raise ManifestError(f"Task {task_id} depends on itself")

    status = _parse_enum(entry.get("status"), TaskStatus, f"{where}.status", TaskStatus.PENDING)
    if status not in IMPORTABLE_STATUSES:
        importable = ", ".join(sorted(IMPORTABLE_STATUSES))
        raise ManifestError(
            f"Task {task_id} declares status {status}; a manifest may only declare: {importable}"
        )

    return ManifestTask(
        external_id=task_id,
        title=_require_str(entry.get("title"), f"{where}.title"),
        section=None if section is None else _require_int(section, f"{where}.section", minimum=0),
        instructions=_optional_str(entry.get("instructions"), f"{where}.instructions"),
        status=status,
        complexity=_parse_enum(
            entry.get("complexity"), Complexity, f"{where}.complexity", Complexity.MEDIUM
        ),
        risk_level=_parse_enum(
            entry.get("risk_level"), RiskLevel, f"{where}.risk_level", RiskLevel.LOW
        ),
        depends_on=depends_on,
        verify_commands=_parse_string_list(entry.get("verify"), f"{where}.verify"),
        **_parse_files(entry.get("files"), where),
        limits=_parse_limits(entry.get("limits"), where),
    )


def _parse_files(value: Any, where: str) -> dict[str, tuple[str, ...]]:
    """The task's declared files (section 6).

    Paths are normalised to repository-relative POSIX form and an absolute or
    escaping path is rejected outright: these lists become the coder's reading
    list and the scope guard's allowance, so ``../../etc/passwd`` must never
    survive parsing.
    """
    files = _optional_mapping(value, f"{where}.files")
    _reject_unknown_keys(files, _FILE_KEYS, f"{where}.files")
    return {
        f"files_to_{key}": tuple(
            _require_repository_path(path, f"{where}.files.{key}[{index}]")
            for index, path in enumerate(
                _parse_string_list(files.get(key), f"{where}.files.{key}")
            )
        )
        for key in ("inspect", "modify", "create")
    }


def _require_repository_path(value: str, where: str) -> str:
    candidate = normalise_path(value)
    if not candidate or candidate == ".":
        raise ManifestError(f"{where} must be a repository-relative path")
    if PurePosixPath(value).is_absolute() or value.startswith("~"):
        raise ManifestError(f"{where} must be relative to the repository, not {value!r}")
    if ".." in PurePosixPath(candidate).parts:
        raise ManifestError(f"{where} must stay inside the repository: {value!r}")
    return candidate


def _parse_limits(value: Any, where: str) -> TaskLimits:
    limits = _optional_mapping(value, f"{where}.limits")
    _reject_unknown_keys(limits, _LIMIT_KEYS, f"{where}.limits")
    defaults = TaskLimits()
    resolved = {
        key: (
            getattr(defaults, key)
            if limits.get(key) is None
            else _require_int(limits[key], f"{where}.limits.{key}", minimum=1)
        )
        for key in _LIMIT_KEYS
    }
    return TaskLimits(**resolved)


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{where} must be a mapping")
    return value


def _optional_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _require_mapping(value, where)


def _reject_unknown_keys(mapping: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise ManifestError(f"Unknown key(s) in {where}: {', '.join(unknown)}")


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where)


def _require_int(value: Any, where: str, *, minimum: int) -> int:
    # bool is an int subclass; a boolean here is always a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{where} must be an integer")
    if value < minimum:
        raise ManifestError(f"{where} must be >= {minimum}")
    return value


def _parse_string_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ManifestError(f"{where} must be a list")
    return tuple(_require_str(item, f"{where}[{index}]") for index, item in enumerate(value))


_EnumT = Any


def _parse_enum(value: Any, enum_type: Any, where: str, default: _EnumT) -> _EnumT:
    """Resolve a manifest string to an enum member, ignoring case.

    Manifests are hand-written, and the enums mix conventions (``pending``
    complexity values are lower case, task statuses upper case), so matching is
    case-insensitive rather than forcing authors to remember which is which.
    """
    if value is None:
        return default
    text = _require_str(value, where)
    for member in enum_type:
        if member.value.casefold() == text.casefold():
            return member
    allowed = ", ".join(member.value for member in enum_type)
    raise ManifestError(f"{where} must be one of: {allowed}")
