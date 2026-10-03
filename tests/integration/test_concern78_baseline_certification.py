"""Who owns establishing the baseline (concern 78, stage 2 completion).

Stage 2 made the comparison deterministic but left the evidence with no
lifecycle owner: a project with pre-existing failures had no path to a recorded
baseline at all, because the only automatic source was a cumulative gate that
such a project never passes.

The owner is ``prepare_workspace``. It is the one function that decides what
commit a run starts from, it runs as its own graph node in its own transaction
*before* ``execute``, and it is therefore the only place where "the state this
run starts from" and "before any model is invoked" are the same moment.

What these tests pin is the economics as much as the correctness. The baseline
suite must run when the **baseline** changes, not when a **task** starts:

    baseline not yet measured  -> one baseline suite, at prepare time
    baseline already measured  -> nothing runs, for this and every later task
    baseline changed           -> measured again, once

and a candidate still costs exactly one authoritative verification.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.enums import (
    TaskStatus,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    TaskRepository,
    VerificationBaselineRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services import verification_baseline
from apps.orchestrator.services.integration import certify_baseline
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import verify_candidate
from apps.orchestrator.services.verification_baseline import is_recorded
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

#: The committed tree is *not* green. This is the case the correction exists
#: for: forty-two-pre-existing-failures, in miniature.
BASELINE_FAILURES = (
    "tests/test_a.py::test_one",
    "tests/test_b.py::test_two",
    "tests/test_c.py::test_three",
)

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


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tracestack"
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


@pytest.fixture
def project(session: Session, project_repo: Path) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            repository_path=str(project_repo),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=VerificationProfile(tests=(TEST_COMMAND,)),
        )
    )


def make_task(session: Session, project: Project, external_id: str) -> Task:
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id=external_id,
            title="Implement navigation",
            files_to_modify=["src/nav.py"],
        )
    )
    return tasks.transition(task.id, TaskStatus.READY)


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    return make_task(session, project, "TS-078")


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    created = create_run(session, task.id)
    TaskRepository(session).transition(task.id, TaskStatus.CODING)
    return created


def baselines(session: Session, project: Project, sha: str):
    return VerificationBaselineRepository(session).list_for_sha(project.id, sha)


def candidate(workspace: TaskWorkspace, text: str) -> None:
    (workspace.path / "src" / "nav.py").write_text(text, encoding="utf-8")


# --- the lifecycle boundary --------------------------------------------------


def test_preparing_a_workspace_certifies_a_baseline_that_has_none(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """Requirement 1 and 3: missing evidence reaches the chosen boundary.

    Nothing was recorded before this call, and the evidence exists after it --
    produced by the orchestrator's own command execution, at the moment the
    starting commit was decided, with no model anywhere near it.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    recorded = baselines(session, project, workspace.starting_commit)
    assert len(recorded) == 1
    assert is_recorded(session, project, workspace.starting_commit)


def test_a_failing_baseline_is_recorded_rather_than_refused(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """The essential case: a baseline that is not green is still evidence.

    Three failing tests with readable node ids say something true and useful
    about this tree. Requiring the suite to pass before recording it would
    leave exactly the projects that need stage 2 without a baseline.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    entry = baselines(session, project, workspace.starting_commit)[0]
    assert entry.status is VerificationStatus.FAILED
    assert entry.usable
    assert entry.extractor == "pytest"
    assert entry.identities == frozenset(BASELINE_FAILURES)
    assert entry.baseline_sha == workspace.starting_commit
    assert entry.worker_profile is WorkerProfile.PYTHON
    assert entry.source_task_run_id == run.id
    # And certifying a baseline integrates nothing: the ref did not move.
    assert workspace.starting_commit == workspace.repository.resolve_sha(
        "agent/integration"
    )


def test_an_already_certified_baseline_is_reused_without_running_anything(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirement 2: the baseline suite is not rerun for a measured tree."""
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    def refuse(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the baseline suite was run a second time")

    monkeypatch.setattr(verification_baseline, "capture_baseline", refuse)
    monkeypatch.setattr(
        "apps.orchestrator.services.integration.capture_baseline", refuse
    )

    again = certify_baseline(
        session,
        project,
        run,
        baseline_sha=workspace.starting_commit,
        settings=verification_settings,
    )
    assert again.certified
    assert again.reused
    assert again.commands_measured == 0


def test_a_second_task_from_the_same_baseline_runs_no_baseline_suite(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirement 4, and the economics the whole correction turns on.

    One baseline suite per baseline, not per task. The hundredth task from an
    unchanged tree costs nothing at prepare time.
    """
    first = prepare_workspace(session, run.id, settings=verification_settings)
    before = len(baselines(session, project, first.starting_commit))

    def refuse(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("a second task triggered another baseline suite")

    monkeypatch.setattr(
        "apps.orchestrator.services.integration.capture_baseline", refuse
    )

    second_task = make_task(session, project, "TS-079")
    second_run = create_run(session, second_task.id)
    TaskRepository(session).transition(second_task.id, TaskStatus.CODING)
    second = prepare_workspace(session, second_run.id, settings=verification_settings)

    assert second.starting_commit == first.starting_commit
    assert len(baselines(session, project, second.starting_commit)) == before


def test_a_changed_verification_command_is_recertified_not_reused(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """Requirement 4's other half: stale evidence is not reused.

    The command text is provenance, so changing the project's suite leaves the
    old row unmatched and the tree uncertified -- and the next prepare measures
    it again rather than comparing against a command that no longer runs.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)
    assert is_recorded(session, project, workspace.starting_commit)

    changed = VerificationProfile(tests=(f"{PYTHON} tools/test.py --strict",))
    assert not is_recorded(
        session, project, workspace.starting_commit, profile=changed
    )


def test_a_changed_worker_profile_invalidates_the_baseline(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """The provenance field added by this pass.

    The same command under a different image is a different measurement, not a
    stale one, and must not be reused: a suite that passes under the Python
    worker says nothing about what it does under the Node one.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)
    assert is_recorded(session, project, workspace.starting_commit)

    project.worker_profile = WorkerProfile.NODE
    assert not is_recorded(session, project, workspace.starting_commit)


def test_certification_failure_is_not_fatal_and_leaves_no_evidence(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirement 10: fail closed, and never block a task that needs no baseline.

    A baseline that could not be measured leaves the run exactly where stage 2
    found it: candidate verification will find nothing and classify as
    ``UNCLASSIFIED_FAILURE``.
    """

    def explode(*args, **kwargs):
        raise OSError("the baseline worktree is on a full disk")

    monkeypatch.setattr(
        "apps.orchestrator.services.integration._integration_worktree", explode
    )

    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    assert baselines(session, project, workspace.starting_commit) == []
    assert not is_recorded(session, project, workspace.starting_commit)


# --- what certification must not do -----------------------------------------


def test_certification_does_not_touch_the_candidate_worktree(
    session: Session,
    run: TaskRun,
    verification_settings: Settings,
):
    """Requirement 5. The measurement runs in the supervisor's own worktree.

    The candidate's tree arrives exactly as Git created it: an empty diff
    against its starting commit, with nothing the baseline suite wrote in it.
    An artefact left here would be classified as the coder's change and would
    turn an otherwise-clean candidate into a scope violation.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    diff = capture_diff(workspace)
    assert diff.text == ""
    assert diff.summary.files == ()
    assert workspace.path.is_dir()


def test_no_model_call_is_made_while_certifying(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """Requirements 6, 7 and 9, as rows.

    ``prepare_workspace`` takes no provider -- there is no argument through
    which a model could be reached -- and it is a graph node of its own ahead
    of ``execute``, so the coder is not invoked and does not wait. The empty
    ``model_runs`` table is that argument made checkable.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)

    assert baselines(session, project, workspace.starting_commit)
    assert ModelRunRepository(session).list_for_run(run.id) == []


def test_a_candidate_still_costs_exactly_one_authoritative_verification(
    session: Session,
    project: Project,
    run: TaskRun,
    verification_settings: Settings,
):
    """Requirement 8, and the invariant the whole design is for.

    One baseline suite, already paid for, reused. One candidate verification.
    The test command is executed once against the candidate -- not twice, and
    not once per comparison -- and the classification is deterministic.
    """
    workspace = prepare_workspace(session, run.id, settings=verification_settings)
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    candidate_runs = [
        row
        for row in VerificationRunRepository(session).list_for_run(run.id)
        if row.verification_type is VerificationType.TESTS
    ]
    assert len(candidate_runs) == 1
    assert report.no_new_regressions
    assert report.comparison is not None
    assert report.comparison.new == frozenset()
    assert ModelRunRepository(session).list_for_run(run.id) == []
