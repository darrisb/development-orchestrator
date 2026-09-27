"""Task workspace lifecycle (build.md sections 10, 40 and 45 phase C).

The phase C exit condition, end to end: create an isolated task worktree,
modify a fixture repository, capture the diff, commit, and clean up.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.enums import RunEventType, TaskStatus
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.repositories import (
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.errors import EntityNotFound
from apps.orchestrator.services.git_errors import DirtyWorktree, NothingToCommit
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import (
    TaskWorkspace,
    capture_diff,
    checkpoint_workspace,
    commit_task_work,
    prepare_workspace,
    push_task_branch,
    release_workspace,
    repository_service,
    rollback_workspace,
)
from tests.conftest import run_git

pytestmark = pytest.mark.integration


@pytest.fixture
def project(session: Session, fixture_repo: Path) -> Project:
    return ProjectRepository(session).add(
        Project(name="Fixture", repository_path=str(fixture_repo), default_branch="main")
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    task = TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="TS-001",
            title="Fix the answer",
            verify_commands=["npm test"],
        )
    )
    return TaskRepository(session).transition(task.id, TaskStatus.READY)


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return create_run(session, task.id)


@pytest.fixture
def workspace(
    session: Session, run: TaskRun, git_settings: Settings
) -> TaskWorkspace:
    return prepare_workspace(session, run.id, settings=git_settings)


# --- preparation ------------------------------------------------------------


def test_prepare_creates_an_isolated_worktree_on_a_named_branch(
    workspace: TaskWorkspace, fixture_repo: Path, git_settings: Settings
):
    assert workspace.branch == "agent/TS-001-fix-the-answer"
    assert workspace.path.is_dir()
    assert git_settings.worktree_root in workspace.path.parents
    assert workspace.path != fixture_repo
    assert (workspace.path / "src" / "app.js").exists()
    assert workspace.git.get_current_branch() == workspace.branch


def test_prepare_records_the_starting_commit_on_the_run_before_any_work(
    session: Session, workspace: TaskWorkspace, fixture_repo: Path
):
    """Section 10 rule 3, and the precondition for rule 8."""
    head = run_git(fixture_repo, "rev-parse", "main").strip()

    stored = TaskRunRepository(session).get(workspace.task_run_id)

    assert workspace.starting_commit == head
    assert stored is not None
    assert stored.starting_commit == head
    assert stored.branch_name == workspace.branch


def test_prepare_records_a_workspace_created_event(
    session: Session, workspace: TaskWorkspace
):
    events = RunEventRepository(session).list_for_run(workspace.task_run_id)

    assert [event.event_type for event in events] == [
        RunEventType.TASK_SELECTED,
        RunEventType.WORKSPACE_CREATED,
    ]
    payload = events[-1].payload
    assert payload["branch"] == workspace.branch
    assert payload["starting_commit"] == workspace.starting_commit
    assert payload["worktree_path"] == str(workspace.path)


def test_prepare_refuses_a_dirty_managed_repository(
    session: Session, run: TaskRun, fixture_repo: Path, git_settings: Settings
):
    (fixture_repo / "README.md").write_text("# Edited by hand\n", encoding="utf-8")

    with pytest.raises(DirtyWorktree):
        prepare_workspace(session, run.id, settings=git_settings)


def test_prepare_may_be_allowed_to_start_dirty(
    session: Session, run: TaskRun, fixture_repo: Path, git_settings: Settings
):
    (fixture_repo / "README.md").write_text("# Edited by hand\n", encoding="utf-8")

    workspace = prepare_workspace(session, run.id, settings=git_settings, allow_dirty=True)

    assert workspace.path.is_dir()


def test_prepare_rejects_an_unknown_run(session: Session, git_settings: Settings):
    with pytest.raises(EntityNotFound):
        prepare_workspace(session, uuid4(), settings=git_settings)


def test_a_second_run_of_the_same_task_gets_its_own_worktree(
    session: Session, task: Task, workspace: TaskWorkspace, git_settings: Settings
):
    """A retry must not inherit the previous attempt's directory."""
    release_workspace(workspace, delete_branch=True)
    second_run = create_run(session, task.id, attempt_number=2)

    second = prepare_workspace(session, second_run.id, settings=git_settings)

    assert second.path != workspace.path
    assert second.path.name.endswith("-run2")


# --- diff capture -----------------------------------------------------------


def test_capture_diff_sees_uncommitted_and_untracked_work(workspace: TaskWorkspace):
    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )
    (workspace.path / "src" / "answer.test.js").write_text("// tests\n", encoding="utf-8")

    capture = capture_diff(workspace)

    assert capture.files_changed == 2
    assert set(capture.summary.paths) == {"src/answer.test.js", "src/app.js"}
    assert "export const answer = 42;" in capture.text
    assert capture.line_count == 3
    assert not capture.truncated


def test_capture_diff_marks_truncation(workspace: TaskWorkspace):
    (workspace.path / "src" / "big.js").write_text("x\n" * 4000, encoding="utf-8")

    capture = capture_diff(workspace, max_bytes=512)

    assert capture.truncated
    # The structured summary is unaffected by text truncation.
    assert capture.summary.files_changed == 1


def test_capture_diff_is_empty_before_any_work(workspace: TaskWorkspace):
    capture = capture_diff(workspace)

    assert capture.text == ""
    assert capture.files_changed == 0


# --- commit -----------------------------------------------------------------


def test_commit_uses_the_task_id_and_records_the_candidate(
    session: Session, workspace: TaskWorkspace, task: Task
):
    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )

    sha = commit_task_work(session, workspace, task)

    assert run_git(workspace.path, "log", "-1", "--pretty=%s").strip() == (
        "TS-001: Fix the answer"
    )
    stored = TaskRunRepository(session).get(workspace.task_run_id)
    assert stored is not None
    assert stored.candidate_commit == sha
    events = RunEventRepository(session).list_for_run(workspace.task_run_id)
    assert events[-1].event_type == RunEventType.COMMIT_CREATED
    assert events[-1].payload == {"commit": sha, "branch": workspace.branch}


def test_committing_leaves_the_managed_repository_alone(
    session: Session, workspace: TaskWorkspace, task: Task, fixture_repo: Path
):
    """The default branch only ever moves by an explicit human decision."""
    main_before = run_git(fixture_repo, "rev-parse", "main").strip()
    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )

    commit_task_work(session, workspace, task)

    assert run_git(fixture_repo, "rev-parse", "main").strip() == main_before
    assert repository_service(
        ProjectRepository(session).get(workspace.project_id), settings=workspace.repository.settings
    ).is_clean()


def test_committing_nothing_is_an_error(session: Session, workspace: TaskWorkspace, task: Task):
    with pytest.raises(NothingToCommit):
        commit_task_work(session, workspace, task)


# --- rollback, checkpoints, release -----------------------------------------


def test_rollback_returns_the_worktree_to_the_starting_commit(
    session: Session, workspace: TaskWorkspace, task: Task
):
    (workspace.path / "src" / "app.js").write_text("broken\n", encoding="utf-8")
    (workspace.path / "src" / "stray.js").write_text("junk\n", encoding="utf-8")
    commit_task_work(session, workspace, task)

    rollback_workspace(workspace)

    assert workspace.git.get_head_sha() == workspace.starting_commit
    assert not (workspace.path / "src" / "stray.js").exists()
    assert capture_diff(workspace).files_changed == 0


def test_checkpoint_tags_the_current_head(session: Session, workspace: TaskWorkspace, task: Task):
    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )
    sha = commit_task_work(session, workspace, task)

    assert checkpoint_workspace(workspace, attempt=1) == sha
    assert "checkpoint/TS-001/attempt-1" in run_git(workspace.path, "tag", "--list")


def test_a_failed_checkpoint_does_not_raise(workspace: TaskWorkspace):
    """It runs on the escalation path, where the real failure must survive."""
    checkpoint_workspace(workspace, attempt=1)

    assert checkpoint_workspace(workspace, attempt=1) is None


def test_release_removes_the_worktree_but_keeps_the_branch(
    session: Session, workspace: TaskWorkspace, task: Task
):
    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )
    sha = commit_task_work(session, workspace, task)

    release_workspace(workspace)

    assert not workspace.path.exists()
    assert workspace.repository.branch_exists(workspace.branch)
    assert workspace.repository.resolve_sha(workspace.branch) == sha
    assert workspace.repository.is_clean()


def test_release_can_delete_an_abandoned_branch(workspace: TaskWorkspace):
    release_workspace(workspace, delete_branch=True)

    assert not workspace.path.exists()
    assert not workspace.repository.branch_exists(workspace.branch)


# --- push policy ------------------------------------------------------------


def test_push_is_skipped_rather_than_failed_when_disabled(
    session: Session, workspace: TaskWorkspace
):
    """Pushing is never required for a task to complete."""
    assert push_task_branch(session, workspace) is False
    assert RunEventRepository(session).list_for_run(workspace.task_run_id)[-1].event_type == (
        RunEventType.WORKSPACE_CREATED
    )


def test_push_records_an_event_when_enabled(
    session: Session, run: TaskRun, fixture_repo: Path, tmp_path: Path, git_settings: Settings
):
    remote = tmp_path / "remote.git"
    run_git(fixture_repo, "init", "--bare", "--quiet", str(remote))
    run_git(fixture_repo, "remote", "add", "origin", str(remote))
    enabled = git_settings.model_copy(update={"git_push_enabled": True})
    workspace = prepare_workspace(session, run.id, settings=enabled)

    assert push_task_branch(session, workspace) is True

    assert workspace.branch in run_git(fixture_repo, "ls-remote", "--heads", "origin")
    events = RunEventRepository(session).list_for_run(workspace.task_run_id)
    assert events[-1].event_type == RunEventType.PUSH_COMPLETED


# --- the phase C exit condition ---------------------------------------------


def test_full_task_workspace_round_trip(
    session: Session, run: TaskRun, task: Task, fixture_repo: Path, git_settings: Settings
):
    workspace = prepare_workspace(session, run.id, settings=git_settings)

    (workspace.path / "src" / "app.js").write_text(
        "export const answer = 42;\n", encoding="utf-8"
    )
    (workspace.path / "src" / "answer.test.js").write_text(
        "import { answer } from './app.js';\n", encoding="utf-8"
    )
    capture = capture_diff(workspace)
    assert capture.files_changed == 2

    sha = commit_task_work(session, workspace, task)
    committed = workspace.repository.get_changed_files_between(workspace.starting_commit, sha)
    assert [change.path for change in committed] == ["src/answer.test.js", "src/app.js"]

    release_workspace(workspace)

    assert not workspace.path.exists()
    assert workspace.repository.is_clean()
    assert run_git(fixture_repo, "rev-parse", "main").strip() == workspace.starting_commit
    assert run_git(fixture_repo, "rev-parse", workspace.branch).strip() == sha
    assert [event.event_type for event in RunEventRepository(session).list_for_run(run.id)] == [
        RunEventType.TASK_SELECTED,
        RunEventType.WORKSPACE_CREATED,
        RunEventType.COMMIT_CREATED,
    ]


# --- declared dependencies (concern 12) -------------------------------------


def test_declared_git_ignored_dependencies_are_copied_into_a_new_worktree(
    session: Session, fixture_repo: Path, git_settings: Settings
):
    """Concern 12: a worker has no network, so `npm ci` cannot run in one.

    A repository that keeps its installed tree out of Git has to get it into
    the worktree some other way, or its verification commands fail in a way
    that reads as a broken worker rather than as the network policy it is.
    """
    (fixture_repo / "node_modules" / "left-pad").mkdir(parents=True)
    (fixture_repo / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports = () => {};\n", encoding="utf-8"
    )
    (fixture_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    run_git(fixture_repo, "add", "-A")
    run_git(fixture_repo, "commit", "--quiet", "-m", "Ignore node_modules")

    project = ProjectRepository(session).add(
        Project(
            name="Fixture",
            repository_path=str(fixture_repo),
            default_branch="main",
            dependency_paths=["node_modules"],
        )
    )
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="TS-020", title="Fix the answer")
    )
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    created = create_run(session, task.id)

    workspace = prepare_workspace(session, created.id, settings=git_settings)

    assert (workspace.path / "node_modules" / "left-pad" / "index.js").exists()
    # And the copy is invisible to the candidate: it is ignored, so it is not
    # in the diff and cannot be mistaken for something the coder wrote.
    assert capture_diff(workspace).summary.files == ()


def test_a_dependency_path_git_does_not_ignore_is_refused(
    session: Session, fixture_repo: Path, git_settings: Settings
):
    """A tracked path is already in the worktree; copying over it would mean
    the worker ran against something other than the commit it was given."""
    project = ProjectRepository(session).add(
        Project(
            name="Fixture",
            repository_path=str(fixture_repo),
            default_branch="main",
            dependency_paths=["src"],
        )
    )
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="TS-021", title="Fix the answer")
    )
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    created = create_run(session, task.id)

    with pytest.raises(ValueError, match="must be ignored by Git"):
        prepare_workspace(session, created.id, settings=git_settings)


@pytest.mark.parametrize("declared", ["/etc/passwd", "../outside", ""])
def test_a_dependency_path_that_leaves_the_repository_is_refused(
    session: Session, fixture_repo: Path, git_settings: Settings, declared: str
):
    project = ProjectRepository(session).add(
        Project(
            name="Fixture",
            repository_path=str(fixture_repo),
            default_branch="main",
            dependency_paths=[declared],
        )
    )
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="TS-022", title="Fix the answer")
    )
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    created = create_run(session, task.id)

    with pytest.raises(ValueError):
        prepare_workspace(session, created.id, settings=git_settings)
