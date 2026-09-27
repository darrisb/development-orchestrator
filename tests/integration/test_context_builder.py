"""Context builder (build.md sections 15, 16 and 45 phase F).

The phase F exit condition: a deterministic context package is produced for a
fixture task. These tests use a small repository that looks like a real one --
declared files, an imported module, a matching test, configuration, project
memory and history -- because the selection rules are only interesting when
there is something they could wrongly include.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.context import ContextBudgetTooSmall, ContextPriority
from apps.orchestrator.domain.enums import LessonConfidence, RunEventType, TaskStatus
from apps.orchestrator.domain.lessons import RetrievedLesson
from apps.orchestrator.domain.models import Lesson, Project, Task, TaskRun
from apps.orchestrator.domain.task_spec import TASK_SPEC_VERSION
from apps.orchestrator.repositories import (
    LessonRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.context_builder import (
    CONTEXT_MANIFEST_ARTIFACT,
    budget_from_settings,
    build_context_package,
    build_task_context,
)
from apps.orchestrator.services.git_service import GitService
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration


@pytest.fixture
def context_repo(tmp_path: Path) -> Path:
    """A fixture repository with everything the builder is meant to notice."""
    repository = tmp_path / "tracestack"
    (repository / "src" / "widgets").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / ".ai" / "decisions").mkdir(parents=True)
    (repository / "node_modules" / "left-pad").mkdir(parents=True)

    files = {
        "package.json": '{"name": "tracestack", "scripts": {"test": "vitest"}}\n',
        "README.md": "# TraceStack\n",
        "src/navigation.ts": (
            "import { TreeNode } from './widgets/tree';\n"
            "import { SidebarState } from './state';\n"
            "import leftPad from 'left-pad';\n\n"
            "export function navigation(nodes: TreeNode[]) {\n"
            "  return nodes.length;\n"
            "}\n"
        ),
        "src/widgets/tree.ts": "export interface TreeNode { id: string; }\n",
        "src/state.ts": "export interface SidebarState { open: boolean; }\n",
        "src/unrelated/payments.ts": "export const charge = () => 0;\n",
        "tests/navigation.test.ts": "import { navigation } from '../src/navigation';\n",
        "tests/payments.test.ts": "// unrelated\n",
        ".ai/project.md": "TraceStack is a VS Code extension.\n",
        ".ai/decisions/ADR-001.md": (
            "# ADR-001: Navigation is a tree\n\nStatus: Accepted\n\n"
            "## Context\nThe sidebar needs structure.\n\n"
            "## Decision\nRender navigation as a tree.\n\n"
            "## Consequences\nDeep nesting is possible.\n"
        ),
        ".ai/decisions/ADR-002.md": (
            "# ADR-002: Payments use Stripe\n\nStatus: Superseded\n\n"
            "## Decision\nUse Stripe.\n"
        ),
        "node_modules/left-pad/index.js": "module.exports = () => {};\n",
    }
    for name, content in files.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-001: scaffold navigation")
    return repository


@pytest.fixture
def project(session: Session, context_repo: Path) -> Project:
    return ProjectRepository(session).add(
        Project(name="TraceStack", repository_path=str(context_repo), default_branch="main")
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    tasks = TaskRepository(session)
    tasks.add(
        Task(
            project_id=project.id,
            external_task_id="TS-001",
            title="Scaffold navigation",
            status=TaskStatus.COMPLETE,
        )
    )
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="TS-004",
            title="Implement navigation tree",
            instructions="Render each node of the navigation tree in the sidebar.",
            depends_on=["TS-001"],
            verify_commands=["npm test"],
            files_to_inspect=["src/widgets/tree.ts"],
            files_to_modify=["src/navigation.ts"],
            files_to_create=["src/navigationTree.ts"],
        )
    )
    return tasks.transition(task.id, TaskStatus.READY)


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return create_run(session, task.id)


def _package(task: Task, context_repo: Path, settings: Settings, **overrides):
    return build_context_package(
        task,
        root=context_repo,
        git=GitService(context_repo, settings=settings),
        settings=settings,
        **overrides,
    )


# --- selection ---------------------------------------------------------------


def test_the_package_leads_with_the_task_and_its_declared_files(
    task: Task, context_repo: Path, git_settings: Settings
):
    package = _package(task, context_repo, git_settings)

    assert package.items[0].priority is ContextPriority.TASK_INSTRUCTIONS
    assert "Implement navigation tree" in package.items[0].content
    declared = [
        item.path for item in package.items if item.priority is ContextPriority.DECLARED_FILE
    ]
    assert declared == ["src/navigation.ts", "src/widgets/tree.ts"]


def test_the_whole_repository_is_not_sent(
    task: Task, context_repo: Path, git_settings: Settings
):
    """Section 15: do not automatically send the entire repository."""
    package = _package(task, context_repo, git_settings)

    assert "src/unrelated/payments.ts" not in package.paths
    assert "tests/payments.test.ts" not in package.paths
    assert not any(path.startswith("node_modules/") for path in package.paths)


def test_an_imported_module_arrives_as_an_interface_and_a_package_does_not(
    task: Task, context_repo: Path, git_settings: Settings
):
    package = _package(task, context_repo, git_settings)
    interfaces = {
        item.path: item.reason
        for item in package.items
        if item.priority is ContextPriority.INTERFACE
    }

    assert interfaces["src/state.ts"] == "imported by src/navigation.ts"
    # tree.ts is imported too, but the task declared it: a file is sent once,
    # at its highest priority.
    assert "src/widgets/tree.ts" not in interfaces
    # left-pad is a third-party package and must resolve to nothing at all.
    assert "node_modules/left-pad/index.js" not in package.paths


def test_the_test_that_covers_a_declared_file_is_included_with_its_reason(
    task: Task, context_repo: Path, git_settings: Settings
):
    package = _package(task, context_repo, git_settings)
    tests = {
        item.path: item.reason
        for item in package.items
        if item.priority is ContextPriority.RELEVANT_TEST
    }

    assert tests["tests/navigation.test.ts"] == "tests src/navigation.ts"
    assert "tests/payments.test.ts" not in tests


def test_configuration_and_project_memory_are_included(
    task: Task, context_repo: Path, git_settings: Settings
):
    package = _package(task, context_repo, git_settings)

    assert "package.json" in package.paths
    assert ".ai/project.md" in package.paths


def test_a_binding_decision_is_retrieved_and_a_superseded_one_is_not(
    task: Task, context_repo: Path, git_settings: Settings
):
    """Section 16: the context builder retrieves relevant decisions."""
    package = _package(task, context_repo, git_settings)
    decisions = [
        item.label
        for item in package.items
        if item.priority is ContextPriority.ARCHITECTURE_DECISION and item.path
        and item.path.startswith(".ai/decisions/")
    ]

    assert decisions == ["ADR-001: Navigation is a tree"]


def test_recent_history_of_the_declared_area_is_included(
    task: Task, context_repo: Path, git_settings: Settings
):
    package = _package(task, context_repo, git_settings)
    recent = next(
        item for item in package.items if item.priority is ContextPriority.RECENT_CHANGE
    )

    assert "TS-001: scaffold navigation" in recent.content


def test_lessons_are_labelled_as_guidance_not_requirements(
    task: Task, context_repo: Path, git_settings: Settings
):
    lesson = Lesson(
        category="testing",
        title="Assert on behaviour",
        lesson="Tests should assert on rendered output, not internals.",
        confidence=LessonConfidence.HIGH,
    )
    package = _package(
        task,
        context_repo,
        git_settings,
        lessons=[RetrievedLesson(lesson, score=7, reasons=("category testing",))],
    )
    item = next(item for item in package.items if item.priority is ContextPriority.LESSON)

    assert "guidance, not requirements" in item.content
    assert "Assert on behaviour" in item.content


def test_a_retrieved_lesson_says_why_it_was_chosen(
    task: Task, context_repo: Path, git_settings: Settings
):
    """Section 32 rule 5: the reason is part of what the coder is shown.

    Without it, guidance a project chose for this task is indistinguishable
    from boilerplate that happened to be in the database.
    """
    lesson = Lesson(
        category="testing",
        title="Assert on behaviour",
        lesson="Tests should assert on rendered output, not internals.",
    )
    package = _package(
        task,
        context_repo,
        git_settings,
        lessons=[RetrievedLesson(lesson, score=7, reasons=("keyword match: navigation",))],
    )
    item = next(item for item in package.items if item.priority is ContextPriority.LESSON)

    assert "selected because keyword match: navigation" in item.content


def test_a_declared_path_that_does_not_exist_is_a_recorded_warning(
    session: Session, task: Task, context_repo: Path, git_settings: Settings
):
    task = TaskRepository(session).update_fields(
        task.id, files_to_inspect=["src/missing.ts"]
    )
    package = _package(task, context_repo, git_settings)

    assert any("src/missing.ts" in warning for warning in package.manifest()["warnings"])
    assert "src/missing.ts" not in package.paths


def test_a_directory_or_glob_in_the_file_list_expands_to_tracked_files(
    session: Session, task: Task, context_repo: Path, git_settings: Settings
):
    task = TaskRepository(session).update_fields(
        task.id, files_to_inspect=["src/widgets"], files_to_modify=["src/*.ts"]
    )
    package = _package(task, context_repo, git_settings)
    declared = [
        item.path for item in package.items if item.priority is ContextPriority.DECLARED_FILE
    ]

    assert declared == ["src/navigation.ts", "src/state.ts", "src/widgets/tree.ts"]


def test_a_binary_or_oversized_file_never_reaches_a_prompt(
    session: Session, task: Task, context_repo: Path, git_settings: Settings
):
    (context_repo / "src" / "blob.ts").write_bytes(b"export const x = 1;\x00\x01binary")
    (context_repo / "src" / "huge.ts").write_text("x = 1\n" * 100_000, encoding="utf-8")
    run_git(context_repo, "add", "-A")
    run_git(context_repo, "commit", "--quiet", "-m", "add blobs")
    task = TaskRepository(session).update_fields(
        task.id, files_to_inspect=["src/blob.ts", "src/huge.ts"]
    )

    package = _package(task, context_repo, git_settings)
    warnings = " ".join(package.manifest()["warnings"])

    assert "src/blob.ts" not in package.paths
    assert "src/huge.ts" not in package.paths
    assert "binary content" in warnings
    assert "CONTEXT_MAX_FILE_BYTES" in warnings


# --- budget and determinism ---------------------------------------------------


def test_the_same_task_and_commit_produce_the_same_hash(
    task: Task, context_repo: Path, git_settings: Settings
):
    first = _package(task, context_repo, git_settings)
    second = _package(task, context_repo, git_settings)

    assert first.content_hash == second.content_hash
    assert first.manifest() == second.manifest()


def test_changing_a_file_changes_the_hash(
    task: Task, context_repo: Path, git_settings: Settings
):
    before = _package(task, context_repo, git_settings)
    (context_repo / "src" / "navigation.ts").write_text("// rewritten\n", encoding="utf-8")
    after = _package(task, context_repo, git_settings)

    assert before.content_hash != after.content_hash


def test_a_tight_budget_keeps_the_task_and_its_files_and_drops_the_rest(
    task: Task, context_repo: Path, git_settings: Settings
):
    settings = git_settings.model_copy(update={"context_max_tokens": 400})
    package = _package(task, context_repo, settings)

    assert package.estimated_tokens <= 400
    assert "src/navigation.ts" in package.paths
    assert package.dropped
    # Whatever was dropped ranks below what was kept.
    assert min(item.priority for item in package.dropped) >= max(
        item.priority for item in package.items
    )


def test_a_budget_that_cannot_hold_the_task_is_a_configuration_error(
    task: Task, context_repo: Path, git_settings: Settings
):
    settings = git_settings.model_copy(update={"context_max_tokens": 5})

    with pytest.raises(ContextBudgetTooSmall):
        _package(task, context_repo, settings)


def test_the_budget_follows_the_served_context_window(git_settings: Settings):
    settings = git_settings.model_copy(
        update={"local_model_context_window": 8192, "context_window_share": 0.5}
    )

    assert budget_from_settings(settings).max_tokens == 4096


# --- recording ----------------------------------------------------------------


def test_building_records_a_manifest_an_event_and_the_hash(
    session: Session, run: TaskRun, task: Task, git_settings: Settings
):
    result = build_task_context(session, run.id, settings=git_settings)

    stored = TaskRunRepository(session).get(run.id)
    assert stored.context_hash == result.context_hash
    assert stored.prompt_version == TASK_SPEC_VERSION

    manifest_file = git_settings.artifact_root / result.manifest_path
    manifest = json.loads(manifest_file.read_text())
    assert manifest["context_hash"] == result.context_hash
    assert manifest["task"]["external_task_id"] == "TS-004"
    assert manifest["source_commit"]
    assert any(entry["path"] == "src/navigation.ts" for entry in manifest["items"])

    events = [event for event in RunEventRepository(session).list_for_run(run.id)]
    built = next(event for event in events if event.event_type == RunEventType.CONTEXT_BUILT)
    assert built.payload["context_hash"] == result.context_hash
    assert "src/navigation.ts" in built.payload["files"]
    assert built.payload["manifest_artifact"].endswith(CONTEXT_MANIFEST_ARTIFACT)


def test_the_rendered_context_is_written_beside_its_manifest(
    session: Session, run: TaskRun, task: Task, git_settings: Settings
):
    result = build_task_context(session, run.id, settings=git_settings)

    written = (git_settings.artifact_root / result.context_path).read_text()
    assert written == result.text
    assert "## Task TS-004" in written


def test_a_second_attempt_keeps_the_first_attempts_context(
    session: Session, run: TaskRun, task: Task, git_settings: Settings
):
    """A fix attempt is given different context from the attempt that failed.
    Losing the earlier manifest would make a review cycle unable to explain
    its own outcome (concern 10).

    The directory is named for the cycle this attempt belongs to -- the second,
    which is the one whose review will judge it -- and not for the one already
    recorded on the run, which counts the cycles that have finished
    (concern 33)."""
    first = build_task_context(session, run.id, settings=git_settings)
    TaskRunRepository(session).update_fields(run.id, attempt_number=2, review_cycle=1)

    second = build_task_context(session, run.id, settings=git_settings)

    assert first.manifest_path != second.manifest_path
    assert "attempt-2-cycle-2/" in second.manifest_path
    assert (git_settings.artifact_root / first.manifest_path).exists()
    assert (git_settings.artifact_root / second.manifest_path).exists()


def test_retrieved_lessons_are_counted(
    session: Session, run: TaskRun, task: Task, project: Project, git_settings: Settings
):
    lessons = LessonRepository(session)
    lesson = lessons.add(
        Lesson(
            project_id=project.id,
            category="testing",
            title="Assert on behaviour",
            lesson="Assert on rendered output.",
        )
    )
    lessons.approve(lesson.id)

    build_task_context(session, run.id, settings=git_settings)

    assert lessons.get(lesson.id).times_retrieved == 1


def test_an_unapproved_lesson_never_reaches_a_coder(
    session: Session, run: TaskRun, project: Project, git_settings: Settings
):
    """Section 32: a candidate is a question for a person, not guidance.

    The lesson below is proposed and never approved, and its text is about the
    very thing the task does. A context package that included it would be
    putting unreviewed advice in front of a coder as if it had been vetted.
    """
    lessons = LessonRepository(session)
    lessons.add(
        Lesson(
            project_id=project.id,
            category="testing",
            title="Do not write tests",
            lesson="Skip the test suite for navigation changes.",
        )
    )

    result = build_task_context(session, run.id, settings=git_settings)

    assert "Do not write tests" not in result.text
    assert all(
        item.priority is not ContextPriority.LESSON for item in result.package.items
    )


def test_the_context_is_built_from_the_run_worktree_not_the_managed_repository(
    session: Session, run: TaskRun, task: Task, git_settings: Settings, context_repo: Path
):
    """The coder must see the tree it is about to change (section 10 rule 7)."""
    workspace = prepare_workspace(session, run.id, settings=git_settings)
    (workspace.path / "src" / "navigation.ts").write_text(
        "// changed in the worktree\n", encoding="utf-8"
    )

    result = build_task_context(session, run.id, workspace=workspace, settings=git_settings)

    assert "// changed in the worktree" in result.text
    assert (context_repo / "src" / "navigation.ts").read_text() != "// changed in the worktree\n"


def test_a_directory_that_is_not_a_repository_still_builds(
    session: Session, run: TaskRun, task: Task, tmp_path: Path, git_settings: Settings
):
    """History and the tracked-file list are lost; the package is not."""
    plain = tmp_path / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "navigation.ts").write_text("export const a = 1;\n", encoding="utf-8")

    result = build_task_context(session, run.id, source_root=plain, settings=git_settings)

    assert "src/navigation.ts" in result.package.paths
    assert result.package.manifest()["source_commit"] is None


# --- writable files must arrive whole (concerns 55 and 56) -------------------


def test_a_declared_writable_file_larger_than_the_per_item_cap_arrives_whole(
    task: Task, context_repo: Path, git_settings: Settings
):
    """The TS-106 case, at the size that produced it.

    The cumulative TraceStack run grew src/test/navigation-stack.test.ts from
    2985 bytes at the baseline to 8110 after TS-105. At 8110 bytes the file
    estimated 2034 tokens against CONTEXT_MAX_ITEM_TOKENS=2000, so TS-106 was
    shown 242 of its 276 lines and refused before a model was called -- with
    twelve thousand tokens of the total budget unused.
    """
    writable = context_repo / "src" / "navigation.ts"
    body = writable.read_text(encoding="utf-8")
    body += "".join(
        f"export function helper{index}(): number {{ return {index}; }}\n"
        for index in range(220)
    )
    writable.write_text(body, encoding="utf-8")
    assert len(body.encode()) > 8_000
    run_git(context_repo, "add", "-A")
    run_git(context_repo, "commit", "--quiet", "-m", "TS-105: grow navigation.ts")

    package = _package(task, context_repo, git_settings)

    included = next(item for item in package.items if item.path == "src/navigation.ts")
    assert included.estimated_tokens > git_settings.context_max_item_tokens
    assert not included.truncated
    assert included.content == body
    assert package.metadata["required_complete"]["honoured"] is True
    assert package.metadata["required_complete"]["paths"] == ["src/navigation.ts"]
    # The file the task may only read is not exempted by association.
    inspected = next(item for item in package.items if item.path == "src/widgets/tree.ts")
    assert inspected.requires_complete is False
    assert package.estimated_tokens <= package.budget.max_tokens


def test_a_writable_file_that_cannot_fit_is_still_clipped_and_recorded(
    task: Task, context_repo: Path, tmp_path: Path
):
    """Requirement 5. A total budget too small for the complete file leaves the
    old behaviour in place: clipped, marked, and refused downstream."""
    writable = context_repo / "src" / "navigation.ts"
    writable.write_text(
        "".join(f"export const value{index} = {index};\n" for index in range(4_000)),
        encoding="utf-8",
    )
    run_git(context_repo, "add", "-A")
    run_git(context_repo, "commit", "--quiet", "-m", "TS-105: grow navigation.ts hugely")
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_tokens=3_000,
    )

    package = _package(task, context_repo, settings)

    included = next(item for item in package.items if item.path == "src/navigation.ts")
    assert included.truncated
    assert included.requires_complete
    assert package.metadata["required_complete"]["honoured"] is False
    assert any(
        "raise CONTEXT_MAX_TOKENS" in warning for warning in package.metadata["warnings"]
    )
    assert package.estimated_tokens <= package.budget.max_tokens


def test_a_writable_file_over_the_byte_cap_is_recorded_as_incomplete(
    task: Task, context_repo: Path, tmp_path: Path
):
    """Concern 58. Over CONTEXT_MAX_FILE_BYTES the file is not clipped, it is
    never read at all -- so there is no truncated item to notice, and the
    package has to say so itself."""
    writable = context_repo / "src" / "navigation.ts"
    writable.write_text(
        "".join(f"export const value{index} = {index};\n" for index in range(2_000)),
        encoding="utf-8",
    )
    run_git(context_repo, "add", "-A")
    run_git(context_repo, "commit", "--quiet", "-m", "TS-105: grow navigation.ts past the cap")
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_file_bytes=1_024,
    )

    package = _package(task, context_repo, settings)

    assert "src/navigation.ts" not in package.paths
    assert package.truncated_paths == ()
    incomplete = package.incomplete_required
    assert [source.path for source in incomplete] == ["src/navigation.ts"]
    assert "CONTEXT_MAX_FILE_BYTES" in (incomplete[0].reason or "")


def test_a_writable_file_that_is_binary_is_recorded_as_incomplete(
    session: Session, project: Project, context_repo: Path, git_settings: Settings
):
    """Any exclusion path, not a list of the ones known today: a binary file is
    refused by the reader long before the budget is consulted."""
    blob = context_repo / "src" / "blob.ts"
    blob.write_bytes(b"export const x = 1;\n\x00\x00binary\n")
    run_git(context_repo, "add", "-A")
    run_git(context_repo, "commit", "--quiet", "-m", "TS-105: add a binary-looking source")
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="TS-006",
            title="Rewrite the blob",
            instructions="Replace the contents of the blob module.",
            files_to_modify=["src/blob.ts"],
        )
    )

    package = _package(task, context_repo, git_settings)

    assert [source.path for source in package.incomplete_required] == ["src/blob.ts"]
    assert package.incomplete_required[0].reason == "binary content"


def test_a_writable_file_that_is_read_whole_is_recorded_complete(
    task: Task, context_repo: Path, git_settings: Settings
):
    package = _package(task, context_repo, git_settings)

    sources = {source.path: source for source in package.required_sources}
    assert sources["src/navigation.ts"].complete is True
    assert sources["src/navigation.ts"].reason is None
    assert package.incomplete_required == ()
    # A file the task only reads is not a required source at all.
    assert "src/widgets/tree.ts" not in sources
    # And a file the task will create has nothing to supply.
    assert "src/navigationTree.ts" not in sources
