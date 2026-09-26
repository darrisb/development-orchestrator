"""The verification pipeline (build.md section 17, phase H).

The phase H exit condition: *intentionally broken fixture implementations
fail before review.* The first group of tests is that sentence -- a fixture
that does not compile, one that fails lint, one whose tests fail, and one
that smuggles a secret past a passing build -- each stopping the pipeline and
sending the real output back to the coder.

These run on the subprocess worker backend, like the phase D tests, so the
suite passes on a machine without Docker. Everything that is not the
container's own flags is exercised: the profile, the order, the classification,
the rows, the logs and the artifact.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.enums import (
    FailureReason,
    RunEventType,
    TaskStatus,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import (
    VERIFICATION_ARTIFACT,
    resolve_profile,
    verify_candidate,
)
from apps.orchestrator.services.workspace import (
    TaskWorkspace,
    capture_diff,
    prepare_workspace,
)
from tests.conftest import run_git

pytestmark = pytest.mark.integration

PYTHON = "python3"

pytest.importorskip("sqlalchemy")
if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)

#: Stand-ins for `npm run compile`, `npm run lint` and `npm test`. Each reads
#: the candidate file and fails the way a real tool would, so a broken fixture
#: fails for the reason the fixture is broken.
_COMPILE = """\
import sys
source = open("src/nav.py").read()
if "SYNTAX ERROR" in source:
    print("src/nav.py:1: invalid syntax", file=sys.stderr)
    sys.exit(2)
print("compiled 1 file")
"""

_LINT = """\
import sys
source = open("src/nav.py").read()
if "unused_import" in source:
    print("src/nav.py:1: F401 'os' imported but unused", file=sys.stderr)
    sys.exit(1)
print("no lint findings")
"""

_TEST = """\
import sys
source = open("src/nav.py").read()
if "return None" in source:
    print("FAILED tests/test_nav.py::test_navigate - AssertionError", file=sys.stderr)
    sys.exit(1)
print("2 passed")
"""


@pytest.fixture
def verification_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    """A managed repository with a working implementation and its tooling."""
    repo = tmp_path / "tracestack"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "nav.py").write_text("def navigate(target):\n    return target\n")
    (repo / "tools" / "compile.py").write_text(_COMPILE)
    (repo / "tools" / "lint.py").write_text(_LINT)
    (repo / "tools" / "test.py").write_text(_TEST)
    (repo / "tools" / "hang.py").write_text("import time\ntime.sleep(30)\n")
    # A build that writes into the worktree, for the post-command diff check.
    (repo / "tools" / "generate.py").write_text(
        "import pathlib\n"
        "pathlib.Path('dist').mkdir(exist_ok=True)\n"
        "pathlib.Path('dist/bundle.js').write_text('console.log(1)\\n')\n"
    )
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


@pytest.fixture
def profile() -> VerificationProfile:
    return VerificationProfile(
        build=(f"{PYTHON} tools/compile.py",),
        lint=(f"{PYTHON} tools/lint.py",),
        tests=(f"{PYTHON} tools/test.py",),
    )


@pytest.fixture
def project(
    session: Session, project_repo: Path, profile: VerificationProfile
) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            repository_path=str(project_repo),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=profile,
        )
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="TS-004",
            title="Implement navigation",
            files_to_modify=["src/nav.py"],
        )
    )
    return tasks.transition(task.id, TaskStatus.READY)


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    """A run whose coding attempt has happened: the pipeline's own entry state."""
    created = create_run(session, task.id)
    TaskRepository(session).transition(task.id, TaskStatus.CODING)
    task.status = TaskStatus.CODING
    return created


@pytest.fixture
def workspace(
    session: Session, run: TaskRun, verification_settings: Settings
) -> TaskWorkspace:
    return prepare_workspace(session, run.id, settings=verification_settings)


def candidate(workspace: TaskWorkspace, source: str) -> None:
    """Write what a coding attempt would have left in the worktree."""
    (workspace.path / "src" / "nav.py").write_text(source, encoding="utf-8")


def verify(session: Session, workspace: TaskWorkspace, settings: Settings):
    return verify_candidate(session, workspace, settings=settings)


# --- the exit condition ------------------------------------------------------


def test_a_candidate_that_does_not_compile_fails_before_review(
    session: Session, workspace: TaskWorkspace, task: Task, verification_settings: Settings
):
    """Phase H's exit condition. The build fails, the deterministic output
    goes back to the coder, and the reviewer is never reached."""
    candidate(workspace, "def navigate(target):  # SYNTAX ERROR\n    return\n")

    report = verify(session, workspace, verification_settings)

    assert not report.passed
    assert report.failure_reason is FailureReason.BUILD_FAILED
    assert "invalid syntax" in report.feedback
    assert f"{PYTHON} tools/compile.py" in report.feedback
    # The task was not advanced towards a reviewer.
    assert TaskRepository(session).get(task.id).status is TaskStatus.VERIFYING


def test_a_candidate_that_fails_lint_fails_before_review(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    candidate(workspace, "import os  # unused_import\n\ndef navigate(target):\n    return target\n")

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.LINT_FAILED
    assert "F401" in report.feedback
    assert report.step_for(VerificationType.BUILD).passed


def test_a_candidate_whose_tests_fail_fails_before_review(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    candidate(workspace, "def navigate(target):\n    return None\n")

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.TEST_FAILED
    assert "AssertionError" in report.feedback


def test_a_candidate_that_smuggles_a_secret_past_a_passing_build_is_stopped(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    """Section 19: the commands can all pass and the change still not be
    safe to review."""
    candidate(
        workspace,
        'TOKEN = "ghp_ZZfakefakefakefakefakefake123456"\n\n'
        "def navigate(target):\n    return target\n",
    )

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.SECURITY_FAILED
    assert report.step_for(VerificationType.TESTS).passed
    assert "credential" in report.failures[0].detail


def test_a_working_candidate_reaches_review_pending(
    session: Session, workspace: TaskWorkspace, task: Task, verification_settings: Settings
):
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    report = verify(session, workspace, verification_settings)

    assert report.passed, report.summary()
    assert report.feedback is None
    assert TaskRepository(session).get(task.id).status is TaskStatus.REVIEW_PENDING


# --- the order (section 17) --------------------------------------------------


def test_the_pipeline_stops_at_the_first_failing_category(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    """A candidate that does not compile has nothing useful to say about its
    own tests, and running them would spend the time to find that out."""
    candidate(workspace, "import os  # unused_import SYNTAX ERROR\n")

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.BUILD_FAILED
    assert report.step_for(VerificationType.LINT) is None
    assert report.step_for(VerificationType.TESTS) is None


def test_a_blocked_scope_fails_before_a_worker_is_started(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    verification_settings: Settings,
):
    """Scope validation is first in section 17's order: a candidate that
    already broke its allowance should not be given a container."""
    candidate(workspace, "def navigate(target):\n    return target\n")
    (workspace.path / "src" / "billing.py").write_text("SECRET_RATE = 0.3\n")

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.SCOPE_VIOLATION
    assert report.step_for(VerificationType.SCOPE).status is VerificationStatus.FAILED
    # Nothing was executed: no build row, no build log.
    assert report.step_for(VerificationType.BUILD) is None


def test_a_change_that_touches_nothing_never_reaches_a_reviewer(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.SCOPE_VIOLATION
    assert "changes nothing" in report.summary()


# --- what is executed, and what is recorded ----------------------------------


def test_a_task_adds_to_the_projects_suite_without_replacing_it(
    session: Session, project: Project, task: Task
):
    """Sections 6 and 18 together: the project owns the profile, a task may
    ask for more. It may never ask for less -- concern 18."""
    task.verify_commands = [f"{PYTHON} tools/test.py --only nav"]

    resolved = resolve_profile(project, task)

    assert resolved.tests == (
        f"{PYTHON} tools/test.py",
        f"{PYTHON} tools/test.py --only nav",
    )
    assert resolved.build == project.verification.build


def test_the_security_scan_runs_even_with_no_audit_command_configured(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    """Section 19's checks on the diff itself are the orchestrator's, not a
    project's: they happen whether or not `npm audit` was configured."""
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    report = verify(session, workspace, verification_settings)
    steps = report.steps_for(VerificationType.SECURITY)

    assert [step.status for step in steps] == [VerificationStatus.PASSED]
    assert steps[0].command == "security scan (orchestrator)"


def test_every_executed_check_leaves_a_row_and_a_log(
    session: Session, workspace: TaskWorkspace, run: TaskRun, verification_settings: Settings
):
    """Section 17's rule made auditable: the claim that a command passed is
    backed by a row saying what ran and a log saying what it printed."""
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    report = verify(session, workspace, verification_settings)
    rows = VerificationRunRepository(session).list_for_run(run.id)
    by_type = {row.verification_type for row in rows}

    assert report.passed
    assert {
        VerificationType.SCOPE,
        VerificationType.BUILD,
        VerificationType.LINT,
        VerificationType.TESTS,
        VerificationType.SECURITY,
        VerificationType.DIFF_POLICY,
    } <= by_type
    for row in rows:
        if row.verification_type in (VerificationType.BUILD, VerificationType.TESTS):
            assert row.exit_code == 0
            assert (verification_settings.artifact_root / row.stdout_artifact).exists()


def test_a_failing_command_is_recorded_with_its_real_exit_code(
    session: Session, workspace: TaskWorkspace, run: TaskRun, verification_settings: Settings
):
    candidate(workspace, "def navigate(target):\n    return None\n")

    verify(session, workspace, verification_settings)
    failures = VerificationRunRepository(session).list_failures(run.id)

    assert [row.verification_type for row in failures] == [VerificationType.TESTS]
    assert failures[0].exit_code == 1


def test_the_run_keeps_the_pipelines_own_report(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    candidate(workspace, "def navigate(target):\n    return None\n")

    report = verify(session, workspace, verification_settings)
    stored = verification_settings.artifact_root / report.artifacts[VERIFICATION_ARTIFACT]

    assert stored.exists()
    assert FailureReason.TEST_FAILED.value in stored.read_text()


def test_the_run_stream_records_the_outcome(
    session: Session, workspace: TaskWorkspace, run: TaskRun, verification_settings: Settings
):
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    verify(session, workspace, verification_settings)
    events = [event.event_type for event in RunEventRepository(session).list_for_run(run.id)]

    assert RunEventType.BUILD_STARTED in events
    assert RunEventType.TESTS_PASSED in events


def test_a_project_with_no_profile_passes_nothing_silently(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    run: TaskRun,
    verification_settings: Settings,
):
    """The honest outcome for an unconfigured project: every command category
    is recorded as SKIPPED, so "it passed" cannot be read as "it was tested"."""
    ProjectRepository(session).update_fields(project.id, verification_profile={})
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    report = verify(session, workspace, verification_settings)
    skipped = [
        step.verification_type
        for step in report.steps
        if step.status is VerificationStatus.SKIPPED
    ]

    assert report.passed
    assert set(skipped) == {
        VerificationType.BUILD,
        VerificationType.LINT,
        VerificationType.TESTS,
    }
    # Passed, but nothing was verified: no command was executed at all.
    assert not report.verified
    assert report.commands_run == ()
    assert len(report.performed) == 3  # scope, the security scan, diff policy
    assert "nothing was verified" in report.summary()


# --- human review (section 20) -----------------------------------------------


def test_an_undeclared_sensitive_change_asks_for_a_human_without_failing(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    task: Task,
    verification_settings: Settings,
):
    """A task that declared no file list has no allowance to measure against
    (section 20), so an authentication file it touched is a question for a
    human rather than a failure the coder can fix."""
    TaskRepository(session).update_fields(task.id, files_to_modify=[])
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")
    (workspace.path / "src" / "auth").mkdir()
    (workspace.path / "src" / "auth" / "session.py").write_text("TIMEOUT = 900\n")

    report = verify(session, workspace, verification_settings)

    assert report.passed
    assert report.requires_human_review
    assert any("security" in reason for reason in report.human_review_reasons)


# --- diff limits and protected paths (section 20, phase H items 6 and 7) -----


def test_a_diff_larger_than_the_task_allows_is_blocked(
    session: Session, workspace: TaskWorkspace, task: Task, verification_settings: Settings
):
    TaskRepository(session).update_fields(task.id, max_diff_lines=5)
    candidate(workspace, "".join(f"x{index} = {index}\n" for index in range(50)))

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.SCOPE_VIOLATION
    assert "the task allows 5" in report.step_for(VerificationType.SCOPE).detail


def test_a_write_to_a_protected_path_is_blocked_whatever_the_manifest_says(
    session: Session, workspace: TaskWorkspace, verification_settings: Settings
):
    """`build.tasks.yaml` is protected by default: a coder that can edit its
    own task can edit its own verification commands."""
    candidate(workspace, "def navigate(target):\n    return target\n")
    (workspace.path / "build.tasks.yaml").write_text("version: 1\n")

    report = verify(session, workspace, verification_settings)

    assert report.failure_reason is FailureReason.SCOPE_VIOLATION
    assert "protected path" in report.step_for(VerificationType.SCOPE).detail


def test_files_a_build_generated_are_removed_after_the_commands_run(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """Build products may feed later commands but never enter the candidate."""
    ProjectRepository(session).update_fields(
        project.id,
        verification_profile={"build": [f"{PYTHON} tools/generate.py"]},
    )
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    report = verify(session, workspace, verification_settings)

    assert report.passed
    assert report.failure_reason is None
    assert report.step_for(VerificationType.BUILD).passed
    assert not (workspace.path / "dist" / "bundle.js").exists()
    assert [change.path for change in capture_diff(workspace).summary.files] == [
        "src/nav.py"
    ]


def test_a_command_that_never_finishes_fails_its_category_without_an_exit_code(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    run: TaskRun,
    tmp_path: Path,
):
    """A test suite that hangs is a defect in the candidate far more often
    than it is a slow machine, so a timeout is the category's own failure --
    recorded with no exit code, because it produced no verdict."""
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=1,
    )
    ProjectRepository(session).update_fields(
        project.id,
        verification_profile={"tests": [f"{PYTHON} tools/hang.py"]},
    )
    candidate(workspace, "def navigate(target):\n    return target.strip()\n")

    report = verify(session, workspace, settings)
    step = report.step_for(VerificationType.TESTS)

    assert report.failure_reason is FailureReason.TEST_FAILED
    assert step.status is VerificationStatus.TIMEOUT
    assert step.exit_code is None
    assert "timed out" in report.feedback
