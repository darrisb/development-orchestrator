"""The Context Builder (build.md section 15).

Goal, in the specification's words: give the coder the smallest useful slice of
the repository. This module gathers the candidate slices in section 15's
priority order and hands them to ``domain.context`` to fit into a budget; it
does the I/O, and the domain decides what fits.

Three properties are load-bearing:

* **Nothing is sent by default.** The whole repository is never a candidate.
  Every item is here because something -- the task's own file list, an import
  in one of those files, a keyword match, an ADR -- put it here, and the
  reason is recorded against it in ``context-manifest.json``.
* **The package is deterministic.** Same commit, same task, same settings ->
  same rendered text and the same ``context_hash``, which is what makes a run
  reconstructable (section 9) and a failure attributable to a prompt rather
  than to a lucky ordering.
* **Truncation is visible.** A clipped file says so in the prompt and in the
  manifest, and a dropped file is listed with the limit that dropped it.

No model is involved in selection. A model that chose its own context could
quietly ask for the whole repository, and the budget would stop being a bound.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.architecture import (
    ARCHITECTURE_OVERVIEW,
    DECISIONS_DIRECTORY,
    PROJECT_OVERVIEW,
    ArchitectureDecision,
    parse_decision,
    select_decisions,
)
from ..domain.context import (
    ContextBudget,
    ContextItem,
    ContextPackage,
    ContextPriority,
    assemble,
    render_bullet_list,
)
from ..domain.enums import RunEventType
from ..domain.lessons import RetrievedLesson, retrieval_prompt_lines
from ..domain.models import Project, RunEvent, Task
from ..domain.relevance import (
    extract_import_targets,
    extract_keywords,
    is_config_path,
    is_excluded,
    is_interface_path,
    is_test_path,
    is_text_path,
    matches_pattern,
    normalise_path,
    rank_paths,
    related_test_paths,
    resolve_import,
)
from ..domain.task_spec import TASK_SPEC_VERSION, render_task_specification
from ..repositories import (
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from . import artifact_store
from .git_service import CommitSummary, GitService
from .lessons import retrieve_ranked_lessons
from .workspace import TaskWorkspace, load_run_context

logger = get_logger(__name__)

#: Artifact name from section 9. The manifest is the record of what the coder
#: was given; ``context.md`` is the text itself.
CONTEXT_MANIFEST_ARTIFACT = "context-manifest.json"
CONTEXT_TEXT_ARTIFACT = "context.md"

#: Glob metacharacters. A declared path containing one is a pattern to expand
#: against the repository; anything else is a literal path.
_GLOB_CHARACTERS = frozenset("*?[")

#: How many keyword-matched files each discretionary category may contribute.
#: Small on purpose: these are guesses, and a guess should never crowd out a
#: file the task actually named.
_MAX_INTERFACE_MATCHES = 6
_MAX_TEST_MATCHES = 4

_LANGUAGE_BY_SUFFIX = {
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cs": "csharp", ".css": "css",
    ".go": "go", ".h": "c", ".hpp": "cpp", ".html": "html", ".ini": "ini",
    ".java": "java", ".js": "javascript", ".json": "json", ".jsx": "jsx",
    ".kt": "kotlin", ".kts": "kotlin", ".md": "markdown", ".mjs": "javascript",
    ".php": "php", ".py": "python", ".pyi": "python", ".rb": "ruby",
    ".rs": "rust", ".scss": "scss", ".sh": "bash", ".sql": "sql",
    ".swift": "swift", ".toml": "toml", ".ts": "typescript", ".tsx": "tsx",
    ".vue": "vue", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
}


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """A built package and where it was recorded."""

    package: ContextPackage
    context_hash: str
    manifest_path: str | None = None
    context_path: str | None = None

    @property
    def text(self) -> str:
        return self.package.render()


def budget_from_settings(settings: Settings | None = None) -> ContextBudget:
    """The configured budget (section 15: "configurable context budgets")."""
    config = settings or get_settings()
    return ContextBudget(
        max_tokens=config.context_token_budget(),
        max_item_tokens=config.context_max_item_tokens,
        max_files=config.context_max_files,
    )


def build_task_context(
    session: Session,
    task_run_id: UUID,
    *,
    workspace: TaskWorkspace | None = None,
    source_root: str | Path | None = None,
    settings: Settings | None = None,
    include_lessons: bool = True,
    record_artifacts: bool = True,
) -> ContextBuildResult:
    """Build, record and hash the context package for one run.

    Reads from the run's worktree when there is one, so the coder sees the
    tree it is about to change rather than the managed repository's HEAD.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        ContextBudgetTooSmall: the budget cannot hold the task instructions.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, task_run_id)

    root = Path(source_root) if source_root else _default_root(workspace, project)
    git = workspace.git if workspace else _optional_git(root, project, config)

    lessons: Sequence[RetrievedLesson] = ()
    if include_lessons and config.context_max_lessons:
        lessons = _retrieve_lessons(session, project, task, limit=config.context_max_lessons)

    package = build_context_package(
        task,
        root=root,
        git=git,
        project=project,
        lessons=lessons,
        dependency_titles=_dependency_titles(session, task),
        settings=config,
    )

    manifest_path: str | None = None
    context_path: str | None = None
    if record_artifacts:
        # Prefixed per attempt, like the prompts the package produces: a fix
        # attempt is given different context from the attempt that failed, and
        # a review cycle that cannot show what its coder was looking at cannot
        # explain its own outcome (section 34).
        prefix = artifact_store.attempt_prefix(run)
        manifest = artifact_store.write_json(
            session,
            task_run_id,
            prefix + CONTEXT_MANIFEST_ARTIFACT,
            package.manifest(),
            kind=CONTEXT_MANIFEST_ARTIFACT,
            settings=config,
        )
        context = artifact_store.write_text(
            session,
            task_run_id,
            prefix + CONTEXT_TEXT_ARTIFACT,
            package.render(),
            kind=CONTEXT_TEXT_ARTIFACT,
            settings=config,
        )
        manifest_path, context_path = manifest.relative_path, context.relative_path

    context_hash = package.content_hash
    TaskRunRepository(session).update_fields(
        task_run_id, context_hash=context_hash, prompt_version=TASK_SPEC_VERSION
    )
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=task_run_id,
            project_id=project.id,
            task_id=task.id,
            event_type=RunEventType.CONTEXT_BUILT,
            attempt=run.attempt_number,
            payload={
                "context_hash": context_hash,
                "estimated_tokens": package.estimated_tokens,
                "files": list(package.paths),
                "truncated": list(package.truncated_paths),
                "dropped": [item.path or item.label for item in package.dropped],
                "manifest_artifact": manifest_path,
            },
        )
    )
    logger.info(
        "context_built",
        run_id=str(task_run_id),
        task=task.external_task_id,
        context_hash=context_hash,
        estimated_tokens=package.estimated_tokens,
        budget=package.budget.max_tokens,
        files=package.file_count,
        dropped=len(package.dropped),
    )
    return ContextBuildResult(
        package=package,
        context_hash=context_hash,
        manifest_path=manifest_path,
        context_path=context_path,
    )


def build_context_package(
    task: Task,
    *,
    root: str | Path,
    git: GitService | None = None,
    project: Project | None = None,
    lessons: Sequence[RetrievedLesson] = (),
    dependency_titles: dict[str, str] | None = None,
    budget: ContextBudget | None = None,
    settings: Settings | None = None,
) -> ContextPackage:
    """Gather and budget a package without touching the database.

    Separate from ``build_task_context`` so the selection rules can be tested
    against a fixture directory, which is the whole of Phase F's exit
    condition.
    """
    config = settings or get_settings()
    reader = RepositoryReader(Path(root), settings=config, git=git)
    keywords = _task_keywords(task)
    warnings: list[str] = []

    declared = _declared_paths(task, reader)
    items: list[ContextItem] = [_task_item(task, dependency_titles)]
    items.extend(_declared_items(task, declared, reader, warnings))
    items.extend(_interface_items(declared, reader, keywords))
    items.extend(_test_items(declared, reader, keywords))
    items.extend(_configuration_items(reader))
    items.extend(_decision_items(reader, keywords, limit=config.context_max_decisions))
    items.extend(_recent_change_items(git, declared, limit=config.context_recent_commits))
    items.extend(_lesson_items(lessons))
    items.extend(_repository_map_items(reader, limit=config.context_map_max_entries))

    metadata: dict[str, object] = {
        "task": {
            "external_task_id": task.external_task_id,
            "title": task.title,
            "complexity": str(task.complexity),
            "risk_level": str(task.risk_level),
        },
        "prompt_version": TASK_SPEC_VERSION,
        "keywords": sorted(keywords),
        "source_commit": git.get_head_sha() if git else None,
        "warnings": [*warnings, *reader.warnings],
    }
    if project is not None:
        metadata["project"] = {
            "name": project.name,
            "external_project_id": project.external_project_id,
        }
    return assemble(
        items,
        budget or budget_from_settings(config),
        metadata=metadata,
        required_paths=_required_source_paths(task, declared, reader),
    )


# --------------------------------------------------------------- item builders


def _task_item(task: Task, dependency_titles: dict[str, str] | None) -> ContextItem:
    text = render_task_specification(task, dependency_titles=dependency_titles)
    return ContextItem(
        priority=ContextPriority.TASK_INSTRUCTIONS,
        label=f"Task {task.external_task_id}",
        content=text,
        reason="the task specification; always included first (section 15 priority 1)",
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_bytes=len(text.encode()),
        source_lines=text.count("\n") + 1,
    )


def _declared_items(
    task: Task,
    declared: _DeclaredPaths,
    reader: RepositoryReader,
    warnings: list[str],
) -> list[ContextItem]:
    """Priority 2: the files the task named.

    A declared path that does not exist is a warning, not an error: for a task
    that creates files it is the expected state, and for a stale manifest it
    is exactly the kind of drift the manifest should be showing an operator.
    """
    items: list[ContextItem] = []
    for path in declared.existing:
        writable = path in declared.writable
        purpose = "may modify" if writable else "to inspect"
        item = reader.file_item(
            path,
            priority=ContextPriority.DECLARED_FILE,
            reason=f"declared by task {task.external_task_id} ({purpose})",
            label_suffix=f"declared: {purpose}",
            # A file the task may replace must be shown whole or not asked for:
            # the edit contract wants its complete new contents (concern 55).
            requires_complete=writable,
        )
        if item is not None:
            items.append(item)
    for path in declared.missing:
        warnings.append(f"declared path not found in the repository: {path}")
    return items


def _interface_items(
    declared: _DeclaredPaths, reader: RepositoryReader, keywords: frozenset[str]
) -> list[ContextItem]:
    """Priority 3: what the declared files import, plus type-shaped matches."""
    items: list[ContextItem] = []
    seen: set[str] = set(declared.existing)

    for path in declared.existing:
        source = reader.text_of(path)
        if source is None:
            continue
        for specifier in extract_import_targets(source):
            target = resolve_import(specifier, from_path=path, known_paths=reader.paths)
            if target is None or target in seen:
                continue
            seen.add(target)
            item = reader.file_item(
                target,
                priority=ContextPriority.INTERFACE,
                reason=f"imported by {path}",
                label_suffix=f"imported by {path}",
            )
            if item is not None:
                items.append(item)

    candidates = [path for path in reader.paths if is_interface_path(path) and path not in seen]
    for path, score in rank_paths(candidates, keywords, limit=_MAX_INTERFACE_MATCHES):
        seen.add(path)
        item = reader.file_item(
            path,
            priority=ContextPriority.INTERFACE,
            reason=f"declares types or contracts matching the task (score {score})",
            label_suffix="likely interface",
        )
        if item is not None:
            items.append(item)
    return items


def _test_items(
    declared: _DeclaredPaths, reader: RepositoryReader, keywords: frozenset[str]
) -> list[ContextItem]:
    """Priority 4: the tests that already cover what this task touches."""
    items: list[ContextItem] = []
    seen: set[str] = set(declared.existing)

    for path in declared.existing:
        for test_path in related_test_paths(path, reader.paths):
            if test_path in seen:
                continue
            seen.add(test_path)
            item = reader.file_item(
                test_path,
                priority=ContextPriority.RELEVANT_TEST,
                reason=f"tests {path}",
                label_suffix=f"tests {path}",
            )
            if item is not None:
                items.append(item)

    candidates = [path for path in reader.paths if is_test_path(path) and path not in seen]
    for path, score in rank_paths(candidates, keywords, limit=_MAX_TEST_MATCHES):
        seen.add(path)
        item = reader.file_item(
            path,
            priority=ContextPriority.RELEVANT_TEST,
            reason=f"test matching the task (score {score})",
            label_suffix="related test",
        )
        if item is not None:
            items.append(item)
    return items


def _configuration_items(reader: RepositoryReader) -> list[ContextItem]:
    """Priority 5: how the project is built, linted and tested.

    Only near the root: a fixture's ``package.json`` matters, one buried in an
    example directory does not.
    """
    items: list[ContextItem] = []
    for path in reader.paths:
        if not is_config_path(path) or len(PurePosixPath(path).parts) > 2:
            continue
        item = reader.file_item(
            path,
            priority=ContextPriority.CONFIGURATION,
            reason="project build/lint/test configuration",
            label_suffix="configuration",
        )
        if item is not None:
            items.append(item)
    return items


def _decision_items(
    reader: RepositoryReader, keywords: frozenset[str], *, limit: int
) -> list[ContextItem]:
    """Priority 6: applicable architecture decisions (section 16)."""
    if limit < 1:
        return []
    items: list[ContextItem] = []

    overviews = (
        (PROJECT_OVERVIEW, "project memory"),
        (ARCHITECTURE_OVERVIEW, "architecture overview"),
    )
    for path, label in overviews:
        item = reader.file_item(
            path,
            priority=ContextPriority.ARCHITECTURE_DECISION,
            reason=f"{label} from the repository's .ai/ directory",
            label_suffix=label,
        )
        if item is not None:
            items.append(item)

    decisions = load_decisions(reader)
    # A repository with only a handful of records is telling the coder its
    # whole architecture; withholding one because its words did not match the
    # task title would be the wrong kind of frugality.
    selected = select_decisions(
        decisions, keywords, limit=limit, include_unmatched=len(decisions) <= limit
    )
    for decision in selected:
        matched = decision.relevance(keywords)
        items.append(
            ContextItem(
                priority=ContextPriority.ARCHITECTURE_DECISION,
                label=f"{decision.identifier}: {decision.title}",
                content=decision.body,
                reason=(
                    f"binding architecture decision ({decision.status})"
                    + (f", {matched} keyword match(es)" if matched else "")
                ),
                path=decision.path,
                sha256=hashlib.sha256(decision.body.encode()).hexdigest(),
                source_bytes=len(decision.body.encode()),
                source_lines=decision.body.count("\n") + 1,
                language="markdown",
            )
        )
    return items


def _recent_change_items(
    git: GitService | None, declared: _DeclaredPaths, *, limit: int
) -> list[ContextItem]:
    """Priority 7: what was accepted here recently.

    Restricted to the declared paths when the task named any, so the slice is
    "history of this area" rather than "the project's changelog".
    """
    if git is None or limit < 1:
        return []
    paths = tuple(declared.existing)
    commits: tuple[CommitSummary, ...] = git.recent_commits(limit=limit, paths=paths)
    if not commits and paths:
        commits = git.recent_commits(limit=limit)
    if not commits:
        return []
    scope = "touching the declared files" if paths else "on this branch"
    body = render_bullet_list(
        f"Recent accepted commits {scope}",
        [f"{commit.short_sha} {commit.date} {commit.subject}" for commit in commits],
    )
    return [
        ContextItem(
            priority=ContextPriority.RECENT_CHANGE,
            label="Recent accepted changes",
            content=body,
            reason="recent history of the area this task changes (section 15 priority 7)",
        )
    ]


def _lesson_items(lessons: Sequence[RetrievedLesson]) -> list[ContextItem]:
    """Priority 8: the lesson hook (sections 32 and 33).

    Lessons are retrieved by the caller and rendered here. They are advice
    from earlier reviews, so they are labelled as such: a lesson must never
    read like part of the task's requirements.

    Each entry carries the reason it was retrieved (section 32 rule 5). That is
    what distinguishes guidance someone chose for this task from boilerplate
    that happened to be in the database, and a coder that cannot tell the
    difference has no way to weigh what it is being told.
    """
    if not lessons:
        return []
    entries = [
        retrieval_prompt_lines([entry])[0] for entry in lessons
    ]
    body = render_bullet_list(
        "Lessons from previous reviews of this project (guidance, not requirements)",
        entries,
    )
    return [
        ContextItem(
            priority=ContextPriority.LESSON,
            label="Lessons",
            content=body,
            reason=f"{len(lessons)} approved lesson(s) retrieved for this project and task",
        )
    ]


def _repository_map_items(reader: RepositoryReader, *, limit: int) -> list[ContextItem]:
    """The orientation aid: which directories exist and how big they are."""
    if limit < 1 or not reader.paths:
        return []
    counts: dict[str, int] = {}
    for path in reader.paths:
        parent = PurePosixPath(path).parent.as_posix()
        counts[parent if parent != "." else "(root)"] = counts.get(
            parent if parent != "." else "(root)", 0
        ) + 1
    entries = [
        f"{directory}/ ({count} file{'s' if count != 1 else ''})"
        for directory, count in sorted(counts.items())
    ][:limit]
    body = render_bullet_list("Tracked directories", entries)
    if len(counts) > limit:
        body += f"\n\n[{len(counts) - limit} further directories omitted]"
    return [
        ContextItem(
            priority=ContextPriority.REPOSITORY_MAP,
            label="Repository map",
            content=body,
            reason="repository structure, for orientation only",
        )
    ]


# ------------------------------------------------------------------ gathering


@dataclass(frozen=True, slots=True)
class _DeclaredPaths:
    """The task's file list, resolved against what is actually there."""

    existing: tuple[str, ...]
    missing: tuple[str, ...]
    writable: frozenset[str]


def _declared_paths(task: Task, reader: RepositoryReader) -> _DeclaredPaths:
    existing: list[str] = []
    missing: list[str] = []
    writable: set[str] = set()

    for path in task.files_to_inspect + task.files_to_modify:
        matches = _expand(path, reader)
        if not matches:
            missing.append(path)
            continue
        for match in matches:
            if match not in existing:
                existing.append(match)
            if path in task.files_to_modify:
                writable.add(match)
    # Files the task will create are described in the task block, not loaded:
    # there is nothing to read, and listing them as missing context would be
    # misleading.
    return _DeclaredPaths(
        existing=tuple(existing), missing=tuple(missing), writable=frozenset(writable)
    )


def _required_source_paths(
    task: Task, declared: _DeclaredPaths, reader: RepositoryReader
) -> dict[str, str | None]:
    """Existing files the task may replace, mapped to why they cannot be read.

    ``declared.existing`` is not enough on its own. It is built from
    ``reader.paths``, which has already dropped anything excluded or
    non-textual, so a writable file that exists but was never a selection
    candidate looks exactly like a file the task is going to create. Asking the
    filesystem directly is what separates "there is nothing to supply" from
    "there is something and it did not arrive" (concern 58).
    """
    required: dict[str, str | None] = {}
    for declaration in task.files_to_modify:
        for path in _expand(declaration, reader) or [normalise_path(declaration)]:
            target = reader.resolve(path)
            if target is None or not target.is_file():
                continue
            reader.text_of(path)
            required[path] = reader.exclusions.get(path)
    return required


def _expand(declared: str, reader: RepositoryReader) -> list[str]:
    path = normalise_path(declared)
    if set(path) & _GLOB_CHARACTERS:
        return [candidate for candidate in reader.paths if matches_pattern(candidate, path)]
    if path in reader.paths:
        return [path]
    # A declared directory means everything tracked beneath it.
    prefix = f"{path}/"
    return [candidate for candidate in reader.paths if candidate.startswith(prefix)]


def load_decisions(reader: RepositoryReader) -> list[ArchitectureDecision]:
    decisions: list[ArchitectureDecision] = []
    prefix = f"{DECISIONS_DIRECTORY}/"
    for path in reader.paths:
        if not path.startswith(prefix) or not path.endswith(".md"):
            continue
        text = reader.text_of(path)
        if text is None:
            continue
        decision = parse_decision(text, path=path)
        reader.warnings.extend(
            f"{path}: {warning}" for warning in decision.warnings
        )
        decisions.append(decision)
    return decisions


class RepositoryReader:
    """Bounded, cached reads of one checkout.

    Every path this returns is repository-relative and every read is checked
    against ``CONTEXT_MAX_FILE_BYTES`` and a binary sniff, so a minified
    bundle or a stray ``.png`` can never reach a prompt.
    """

    def __init__(
        self, root: Path, *, settings: Settings, git: GitService | None = None
    ) -> None:
        self.root = root.expanduser().resolve()
        self.settings = settings
        self.git = git
        self.warnings: list[str] = []
        #: Why a path that exists could not be read, by path. A warning is for
        #: an operator reading the manifest; this is for the code that has to
        #: decide whether a file the coder may replace actually arrived.
        self.exclusions: dict[str, str] = {}
        self._cache: dict[str, str | None] = {}
        self.paths: tuple[str, ...] = self._list_paths()

    def _list_paths(self) -> tuple[str, ...]:
        if not self.root.is_dir():
            self.warnings.append(f"source root does not exist: {self.root}")
            return ()
        if self.git is not None:
            candidates: Iterable[str] = self.git.list_tracked_files()
        else:
            candidates = (
                entry.relative_to(self.root).as_posix()
                for entry in self.root.rglob("*")
                if entry.is_file()
            )
        return tuple(
            sorted(
                path
                for path in candidates
                if not is_excluded(path) and (is_text_path(path) or _is_memory_path(path))
            )
        )

    def resolve(self, path: str) -> Path | None:
        """Absolute path for ``path``, or ``None`` if it escapes the root."""
        candidate = (self.root / normalise_path(path)).resolve()
        if candidate == self.root or self.root not in candidate.parents:
            return None
        return candidate

    def text_of(self, path: str) -> str | None:
        """File contents, or ``None`` when unreadable, binary or too large."""
        if path in self._cache:
            return self._cache[path]
        text = self._read(path)
        self._cache[path] = text
        return text

    def _read(self, path: str) -> str | None:
        target = self.resolve(path)
        if target is None:
            self.warnings.append(f"refused to read outside the repository: {path}")
            self.exclusions[path] = "resolves outside the repository"
            return None
        if not target.is_file():
            return None
        size = target.stat().st_size
        if size > self.settings.context_max_file_bytes:
            reason = (
                f"{size} bytes exceeds CONTEXT_MAX_FILE_BYTES="
                f"{self.settings.context_max_file_bytes}"
            )
            self.warnings.append(f"skipped {path}: {reason}")
            self.exclusions[path] = reason
            return None
        data = target.read_bytes()
        if b"\x00" in data:
            self.warnings.append(f"skipped {path}: binary content")
            self.exclusions[path] = "binary content"
            return None
        return data.decode("utf-8", errors="replace")

    def file_item(
        self,
        path: str,
        *,
        priority: ContextPriority,
        reason: str,
        label_suffix: str | None = None,
        requires_complete: bool = False,
    ) -> ContextItem | None:
        """A context item for ``path``, or ``None`` if it could not be read."""
        text = self.text_of(path)
        if text is None:
            return None
        data = text.encode()
        return ContextItem(
            priority=priority,
            label=f"{path} ({label_suffix})" if label_suffix else path,
            content=text,
            reason=reason,
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            source_bytes=len(data),
            source_lines=text.count("\n") + (0 if text.endswith("\n") else 1),
            language=language_for(path),
            requires_complete=requires_complete,
        )


# --------------------------------------------------------------------- helpers


def _task_keywords(task: Task) -> frozenset[str]:
    return extract_keywords(
        task.external_task_id,
        task.title,
        task.instructions,
        " ".join(task.declared_paths).replace("/", " ").replace(".", " "),
    )


def _dependency_titles(session: Session, task: Task) -> dict[str, str]:
    if not task.depends_on:
        return {}
    tasks = TaskRepository(session)
    titles: dict[str, str] = {}
    for external_id in task.depends_on:
        dependency = tasks.get_by_external_id(task.project_id, external_id)
        if dependency is not None:
            titles[external_id] = dependency.title
    return titles


def _retrieve_lessons(
    session: Session, project: Project, task: Task, *, limit: int
) -> list[RetrievedLesson]:
    """The lesson hook (section 33), delegated to ``services.lessons``.

    Retrieval is counted there and only approved lessons can come back, which
    is why this is a call rather than a query: a candidate is a question for a
    person, and a proposed one reaching a coder's context would be section 32's
    rules unenforced.
    """
    retrieved = list(
        retrieve_ranked_lessons(
            session,
            project.id,
            keywords=sorted(_task_keywords(task)),
            limit=limit,
        )
    )
    logger.info(
        "lessons_retrieved",
        project_id=str(project.id),
        task=task.external_task_id,
        count=len(retrieved),
        lessons=[entry.lesson.title for entry in retrieved],
    )
    return retrieved


def _default_root(workspace: TaskWorkspace | None, project: Project) -> Path:
    return workspace.path if workspace else Path(project.repository_path)


def _optional_git(root: Path, project: Project, settings: Settings) -> GitService | None:
    """A ``GitService`` for ``root`` when it is a checkout, else ``None``.

    A context package can be built from a plain directory -- that is what the
    fixture tests do -- so a missing repository loses history and the tracked
    file list, not the whole build.
    """
    try:
        return GitService(root, default_branch=project.default_branch, settings=settings)
    except Exception as error:  # noqa: BLE001 - any Git failure degrades the same way
        logger.warning("context_git_unavailable", root=str(root), error=str(error))
        return None


def _is_memory_path(path: str) -> bool:
    return path.startswith(".ai/")


def language_for(path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.casefold())


__all__ = [
    "CONTEXT_MANIFEST_ARTIFACT",
    "CONTEXT_TEXT_ARTIFACT",
    "ContextBuildResult",
    "RepositoryReader",
    "budget_from_settings",
    "build_context_package",
    "build_task_context",
    "language_for",
    "load_decisions",
]
