"""Serializing baseline certification (concern 78, stage 2 completion).

The race the previous pass left open: two preparations for the same project and
the same not-yet-certified baseline both read the evidence as missing, and both
go on to reset and run commands in the *same* supervisor integration worktree
(``integration_worktree_path(project.id)``). The unique constraint on
``verification_baselines`` protects the row the measurement ends with; it does
nothing about the checkout the measurement is gathered from, so the loser would
record evidence read out of a tree the winner was resetting underneath it.

The fix is the repository's own concurrency mechanism -- ``SELECT ... FOR
UPDATE`` on the project row, bounded by ``lock_timeout`` and surfaced as
``LockWaitTimeout`` (concern 57) -- with a **double-checked** read around it.

These tests are deterministic rather than threaded. The default test engine is
SQLite, where ``with_for_update()`` is a no-op, so real threads would prove
nothing about the lock and would be flaky besides. What *is* worth pinning, and
what these tests pin, is the logic the lock protects: that the lock is taken
before the worktree is ever reached, that the mandatory post-lock re-check
exists and turns a lost race into a reuse, that the steady-state path takes no
lock at all, that the lock identity is the project's own row, and that failing
to get the lock leaves the shared directory alone.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.models import VerificationBaselineRow
from apps.orchestrator.domain.enums import TaskStatus, WorkerProfile
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    TaskRepository,
    VerificationBaselineRepository,
)
from apps.orchestrator.services import integration as integration_service
from apps.orchestrator.services.errors import LockWaitTimeout
from apps.orchestrator.services.integration import certify_baseline
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import verify_candidate
from apps.orchestrator.services.verification_baseline import is_recorded
from apps.orchestrator.services.workspace import (
    TaskWorkspace,
    prepare_workspace,
    repository_service,
)
from tests.conftest import run_git

pytestmark = pytest.mark.integration

PYTHON = "python3"

pytest.importorskip("sqlalchemy")
if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)


_TEST = """\
import sys

source = open("src/nav.py").read()
failing = []
for line in source.splitlines():
    if line.startswith("# FAIL:"):
        failing = line.removeprefix("# FAIL:").split()
for name in failing:
    print(f"FAILED {name} - AssertionError: nope")
if failing:
    print(f"=== {len(failing)} failed, 7 passed in 0.10s ===")
    sys.exit(1)
print("=== 8 passed in 0.10s ===")
"""

BASELINE_FAILURES = ("tests/test_a.py::test_one", "tests/test_b.py::test_two")
COMMITTED = "def navigate(target):\n    return target\n"
TEST_COMMAND = f"{PYTHON} tools/test.py"


def source(*failing: str) -> str:
    body = 'def navigate(target):\n    """Navigate to target."""\n    return target\n'
    return f"{body}# FAIL: {' '.join(failing)}\n" if failing else body


@pytest.fixture
def verification_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "nav.py").write_text(
        f"{COMMITTED}# FAIL: {' '.join(BASELINE_FAILURES)}\n"
    )
    (repo / "tools" / "test.py").write_text(_TEST)
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


def make_project(session: Session, repo: Path, name: str) -> Project:
    return ProjectRepository(session).add(
        Project(
            name=name,
            repository_path=str(repo),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=VerificationProfile(tests=(TEST_COMMAND,)),
        )
    )


def make_run(session: Session, project: Project, external_id: str) -> TaskRun:
    tasks = TaskRepository(session)
    task: Task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id=external_id,
            title="Implement navigation",
            files_to_modify=["src/nav.py"],
        )
    )
    tasks.transition(task.id, TaskStatus.READY)
    created = create_run(session, task.id)
    tasks.transition(task.id, TaskStatus.CODING)
    return created


@pytest.fixture
def project(session: Session, tmp_path: Path) -> Project:
    return make_project(session, make_repo(tmp_path, "tracestack"), "TraceStack")


@pytest.fixture
def run(session: Session, project: Project) -> TaskRun:
    return make_run(session, project, "TS-078")


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the order of the two operations whose interleaving is the bug.

    ``lock`` must always precede ``worktree``. Anything else means a worker
    reached the shared checkout without exclusive access.
    """
    events: list[str] = []
    real_lock = ProjectRepository.lock
    real_worktree = integration_service._integration_worktree

    def traced_lock(self: ProjectRepository, project_id: UUID):
        events.append(f"lock:{project_id}")
        return real_lock(self, project_id)

    def traced_worktree(*args, **kwargs):
        events.append("worktree")
        return real_worktree(*args, **kwargs)

    monkeypatch.setattr(ProjectRepository, "lock", traced_lock)
    monkeypatch.setattr(
        integration_service, "_integration_worktree", traced_worktree
    )
    return events


def certify(
    session: Session, project: Project, run: TaskRun, sha: str, settings: Settings
):
    return certify_baseline(
        session, project, run, baseline_sha=sha, settings=settings
    )


def clear_baselines(session: Session, project: Project) -> None:
    """Put the project back into the not-yet-certified state."""
    for row in session.scalars(
        select(VerificationBaselineRow).where(
            VerificationBaselineRow.project_id == project.id
        )
    ).all():
        session.delete(row)
    session.flush()


def baseline_sha(project: Project, settings: Settings) -> str:
    """The commit a run would start from, without preparing a workspace."""
    return integration_service.integration_baseline(
        repository_service(project, settings=settings), project
    )


def baseline_rows(session: Session, project: Project, sha: str):
    return VerificationBaselineRepository(session).list_for_sha(project.id, sha)


# --- the race ----------------------------------------------------------------


def test_the_lock_is_acquired_before_the_shared_worktree_is_touched(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    trace: list[str],
):
    """Requirement 3. The lock covers the execution window, not just the write.

    If the worktree were reached first -- or the lock only taken around the
    database insert -- the serialization would be decorative: both workers
    would already have reset the same checkout by the time either wrote a row.
    """
    sha = baseline_sha(project, verification_settings)

    result = certify(session, project, run, sha, verification_settings)

    assert result.certified and not result.reused
    assert trace == [f"lock:{project.id}", "worktree"]


def test_the_loser_of_the_race_re_checks_after_the_lock_and_reuses(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirements 1 and 2, and the mandatory post-lock re-check.

    The interleaving is forced rather than raced: worker B's *lock acquisition*
    is where worker A's certification completes, which is exactly the window
    the double check exists for -- B read the evidence as missing, then waited,
    and by the time it holds the lock the tree has been measured. Without the
    second check B would run the whole suite again.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)
    sha = workspace.starting_commit
    # ``prepare_workspace`` already certified this tree; wipe the evidence so
    # that both callers below start from "missing", which is the only state the
    # race can happen in.
    clear_baselines(session, project)
    assert not is_recorded(session, project, sha)

    captures: list[str] = []
    real_capture = integration_service.capture_baseline

    def counted_capture(*args, **kwargs):
        captures.append("ran")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(integration_service, "capture_baseline", counted_capture)

    # Worker A finishes while worker B is blocked on the lock. ``raced`` is set
    # before the inner call so that worker A's own lock acquisition does not
    # re-enter this hook -- A is the worker that wins, not another racer.
    real_lock = ProjectRepository.lock
    raced: list[str] = []

    def lock_that_loses_the_race(self: ProjectRepository, project_id: UUID):
        acquired = real_lock(self, project_id)
        if not raced:
            raced.append("yes")
            certify_baseline(
                self.session,
                project,
                run,
                baseline_sha=sha,
                settings=verification_settings,
            )
        return acquired

    monkeypatch.setattr(ProjectRepository, "lock", lock_that_loses_the_race)

    result = certify(session, project, run, sha, verification_settings)

    # Exactly one baseline verification execution across both callers.
    assert len(captures) == 1
    # And the one that lost reused rather than re-measuring.
    assert result.certified
    assert result.reused
    assert result.commands_measured == 0
    assert is_recorded(session, project, sha)


def test_two_sequential_certifications_measure_the_tree_once(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirement 1, in the plain case: one measurement, whatever the order."""
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    def refuse(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the baseline suite was measured twice")

    monkeypatch.setattr(integration_service, "capture_baseline", refuse)

    again = certify(
        session, project, run, workspace.starting_commit, verification_settings
    )
    assert again.reused and again.commands_measured == 0
    assert len(baseline_rows(session, project, workspace.starting_commit)) == 1


# --- what the lock must not cost --------------------------------------------


def test_the_steady_state_path_takes_no_lock_at_all(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    trace: list[str],
):
    """Requirement 4, and the reason the first check is deliberately unlocked.

    A project-wide lock on every preparation would serialize the start of every
    task in the project behind every other, to discover each time that the
    evidence was already there. The unlocked first read is what keeps the
    steady state free, and it can only ever be wrong in the direction of taking
    the lock unnecessarily.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)
    assert trace == [f"lock:{project.id}", "worktree"]
    trace.clear()

    second_run = make_run(session, project, "TS-079")
    prepare_workspace(session, second_run.id, settings=verification_settings)

    assert trace == []
    assert is_recorded(session, project, workspace.starting_commit)


def test_different_projects_lock_their_own_rows_and_do_not_serialize(
    session: Session,
    tmp_path: Path,
    verification_settings: Settings,
    trace: list[str],
):
    """Requirement 5. The identity is the project row, so there is no global lock."""
    first = make_project(session, make_repo(tmp_path, "alpha"), "Alpha")
    second = make_project(session, make_repo(tmp_path, "beta"), "Beta")

    prepare_workspace(
        session, make_run(session, first, "AL-1").id, settings=verification_settings
    )
    prepare_workspace(
        session, make_run(session, second, "BE-1").id, settings=verification_settings
    )

    locked = [event for event in trace if event.startswith("lock:")]
    assert locked == [f"lock:{first.id}", f"lock:{second.id}"]
    assert first.id != second.id
    # Each project measured its own tree; neither waited on the other's row.
    assert trace.count("worktree") == 2


# --- failure ----------------------------------------------------------------


def test_a_lock_timeout_leaves_the_shared_worktree_alone_and_fails_closed(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    trace: list[str],
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirement 6. Existing lock-timeout semantics, and no shared mutation.

    ``LockWaitTimeout`` is the application's one name for a bounded lock wait
    (concern 57). Certification answers it the way it answers every other
    failure -- no evidence, not certified, not fatal -- and crucially does not
    go on to reset the directory another worker is currently using.
    """

    def time_out(self: ProjectRepository, project_id: UUID):
        trace.append(f"lock-timeout:{project_id}")
        raise LockWaitTimeout("gave up waiting for a database lock after 30s")

    monkeypatch.setattr(ProjectRepository, "lock", time_out)

    baseline = baseline_sha(project, verification_settings)

    result = certify(session, project, run, baseline, verification_settings)

    assert not result.certified
    assert not result.reused
    assert "could not acquire the baseline certification lock" in result.detail
    # The shared worktree was never reached.
    assert "worktree" not in trace
    assert baseline_rows(session, project, baseline) == []
    assert not is_recorded(session, project, baseline)


def test_a_task_whose_baseline_could_not_be_locked_still_runs_and_fails_closed(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirements 6 and 8 together: unchanged candidate behaviour.

    A lock it could not get must not stop a task. The run proceeds, its
    candidate verification executes exactly once, and with no baseline on
    record the classification is the fail-closed ``UNCLASSIFIED_FAILURE`` --
    which is how this behaved before stage 2 existed.
    """

    def time_out(self: ProjectRepository, project_id: UUID):
        raise LockWaitTimeout("gave up waiting for a database lock after 30s")

    monkeypatch.setattr(ProjectRepository, "lock", time_out)

    workspace: TaskWorkspace = prepare_workspace(
        session, run.id, settings=verification_settings
    )
    assert baseline_rows(session, project, workspace.starting_commit) == []

    (workspace.path / "src" / "nav.py").write_text(
        source(*BASELINE_FAILURES), encoding="utf-8"
    )
    report = verify_candidate(session, workspace, settings=verification_settings)

    assert not report.no_new_regressions
    assert report.classification.value == "UNCLASSIFIED_FAILURE"
    assert report.comparison is not None and not report.comparison.available


def test_no_model_call_is_made_anywhere_in_the_locked_path(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """Requirement 7. Locking and certifying are deterministic throughout."""
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    assert is_recorded(session, project, workspace.starting_commit)
    assert ModelRunRepository(session).list_for_run(run.id) == []
