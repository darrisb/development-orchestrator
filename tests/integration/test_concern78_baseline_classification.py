"""Deterministic failure classification in the pipeline (concern 78, stage 2).

Stage 1 stopped the coder measuring the project's baseline. These tests pin the
half that replaces it: the orchestrator runs the authoritative suite, compares
the failures it identifies against durably recorded baseline evidence for the
exact tree the candidate started from, and says which of three things happened
-- or says it does not know.

Everything runs on the subprocess worker backend, like the rest of phase H, and
no model is involved at any point. ``_TEST`` is a stand-in for a real runner:
it prints pytest-shaped output, including the closing banner, and which tests
"fail" is written into the candidate file, so a candidate can introduce a
regression, resolve a pre-existing failure, or do both at once.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.models import VerificationBaselineRow
from apps.orchestrator.domain.enums import (
    RunEventType,
    TaskStatus,
    VerificationClassification,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.models import (
    Project,
    Task,
    TaskRun,
    VerificationBaseline,
)
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    VerificationBaselineRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import verify_candidate
from apps.orchestrator.services.verification_baseline import capture_baseline
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration

PYTHON = "python3"

pytest.importorskip("sqlalchemy")
if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)


#: A runner whose failures are decided by the candidate, and which prints what
#: a real one prints: a short summary and a closing banner with the counts.
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

#: A runner that fails without naming anything comparable: the fail-closed path.
_OPAQUE_TEST = """\
import sys
print("the test harness crashed before collecting anything", file=sys.stderr)
sys.exit(3)
"""

#: Three pre-existing failures in the tree every candidate starts from.
BASELINE_FAILURES = (
    "tests/test_a.py::test_one",
    "tests/test_b.py::test_two",
    "tests/test_c.py::test_three",
)


#: The committed implementation. A candidate always differs from it -- an empty
#: diff is a scope violation and would never reach the commands.
COMMITTED = "def navigate(target):\n    return target\n"


def source(*failing: str) -> str:
    """A candidate whose suite fails exactly these tests."""
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
    # The committed tree already fails three tests: the realistic case, and the
    # one stage 2 exists for.
    (repo / "src" / "nav.py").write_text(
        f"{COMMITTED}# FAIL: {' '.join(BASELINE_FAILURES)}\n"
    )
    (repo / "tools" / "test.py").write_text(_TEST)
    (repo / "tools" / "opaque.py").write_text(_OPAQUE_TEST)
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


#: The project's one declared test command. Its exact text is half the
#: provenance a baseline row is matched on.
TEST_COMMAND = f"{PYTHON} tools/test.py"


@pytest.fixture
def profile() -> VerificationProfile:
    return VerificationProfile(tests=(TEST_COMMAND,))


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
            external_task_id="TS-078",
            title="Implement navigation",
            files_to_modify=["src/nav.py"],
        )
    )
    return tasks.transition(task.id, TaskStatus.READY)


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    created = create_run(session, task.id)
    TaskRepository(session).transition(task.id, TaskStatus.CODING)
    task.status = TaskStatus.CODING
    return created


@pytest.fixture
def workspace(
    session: Session, run: TaskRun, verification_settings: Settings
) -> TaskWorkspace:
    return prepare_workspace(session, run.id, settings=verification_settings)


def candidate(workspace: TaskWorkspace, text: str) -> None:
    (workspace.path / "src" / "nav.py").write_text(text, encoding="utf-8")


def clear_baselines(session: Session, project: Project) -> None:
    """Remove every recorded baseline for this project.

    ``prepare_workspace`` now certifies the baseline, so the ``workspace``
    fixture arrives with valid evidence already on record -- which is the
    steady state and the point of the correction. A test about *missing* or
    *stale* evidence has to put the system back into the state where
    certification has not happened or could not produce a usable answer, and
    this is that state.
    """
    for row in session.scalars(
        select(VerificationBaselineRow).where(
            VerificationBaselineRow.project_id == project.id
        )
    ).all():
        session.delete(row)
    session.flush()


def record_baseline(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    *,
    identities: tuple[str, ...] = BASELINE_FAILURES,
    sha: str | None = None,
    command: str = TEST_COMMAND,
    status: VerificationStatus = VerificationStatus.FAILED,
    failures_available: bool = True,
) -> VerificationBaseline:
    """Durable evidence for one command against one tree."""
    return VerificationBaselineRepository(session).record(
        VerificationBaseline(
            project_id=project.id,
            baseline_sha=sha or workspace.starting_commit,
            verification_type=VerificationType.TESTS,
            command=command,
            worker_profile=project.worker_profile,
            status=status,
            failure_identities=list(identities),
            failures_available=failures_available,
            extractor="pytest" if failures_available else None,
            exit_code=1 if status is VerificationStatus.FAILED else 0,
        )
    )


# --- the classification ------------------------------------------------------


def test_a_clean_candidate_still_passes_through_the_same_pipeline(
    session: Session,
    project: Project,
    task: Task,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """Nothing about a passing candidate changes, and the commands still ran."""
    record_baseline(session, project, workspace)
    candidate(workspace, source())

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.passed
    assert report.verified
    assert report.classification is VerificationClassification.PASSED
    assert report.no_new_regressions
    assert report.comparison is None
    assert report.feedback is None
    assert TaskRepository(session).get(task.id).status is TaskStatus.REVIEW_PENDING
    # Authoritative execution is unchanged: the orchestrator ran the command.
    executed = [step for step in report.commands_run if step.executed]
    assert any(step.verification_type is VerificationType.TESTS for step in executed)


def test_failures_identical_to_the_baseline_produce_no_new_failures(
    session: Session,
    project: Project,
    task: Task,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    record_baseline(session, project, workspace)
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert not report.passed
    assert report.classification is VerificationClassification.KNOWN_BASELINE_ONLY
    assert report.no_new_regressions
    comparison = report.comparison
    assert comparison is not None and comparison.available
    assert comparison.new == frozenset()
    assert comparison.known == frozenset(BASELINE_FAILURES)
    assert comparison.resolved == frozenset()
    assert comparison.baseline_sha == workspace.starting_commit
    # Carried forward to review, not approved here.
    assert TaskRepository(session).get(task.id).status is TaskStatus.REVIEW_PENDING


def test_one_additional_failure_identifies_exactly_that_regression(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    regression = "tests/test_nav.py::test_navigate"
    record_baseline(session, project, workspace)
    candidate(workspace, source(*BASELINE_FAILURES, regression))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.NEW_REGRESSION
    assert not report.no_new_regressions
    comparison = report.comparison
    assert comparison is not None
    assert comparison.new == frozenset({regression})
    assert comparison.known == frozenset(BASELINE_FAILURES)
    feedback = report.feedback
    assert feedback is not None
    assert regression in feedback
    assert not any(known in feedback for known in BASELINE_FAILURES)


def test_equal_failure_counts_with_different_identities_are_not_equivalent(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """One baseline failure gone, one new one in its place. Same count."""
    regression = "tests/test_nav.py::test_navigate"
    record_baseline(session, project, workspace)
    candidate(workspace, source(*BASELINE_FAILURES[1:], regression))

    report = verify_candidate(session, workspace, settings=verification_settings)

    comparison = report.comparison
    assert comparison is not None
    assert len(comparison.known) + len(comparison.new) == len(BASELINE_FAILURES)
    assert comparison.new == frozenset({regression})
    assert comparison.resolved == frozenset({BASELINE_FAILURES[0]})
    assert report.classification is VerificationClassification.NEW_REGRESSION


def test_resolving_a_baseline_failure_is_recorded_and_is_not_a_regression(
    session: Session,
    project: Project,
    task: Task,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    record_baseline(session, project, workspace)
    candidate(workspace, source(*BASELINE_FAILURES[1:]))

    report = verify_candidate(session, workspace, settings=verification_settings)

    comparison = report.comparison
    assert comparison is not None
    assert comparison.resolved == frozenset({BASELINE_FAILURES[0]})
    assert comparison.new == frozenset()
    assert report.classification is VerificationClassification.KNOWN_BASELINE_ONLY
    assert TaskRepository(session).get(task.id).status is TaskStatus.REVIEW_PENDING


def test_a_candidate_that_fixes_everything_passes_and_resolves_the_baseline(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """The command now exits zero, so there is nothing to compare at all.

    ``PASSED`` without a comparison is the right answer here: the resolved set
    is interesting, but it is not needed to decide anything, and computing it
    would mean reading a baseline on the success path for no verdict.
    """
    record_baseline(session, project, workspace)
    candidate(workspace, source())

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.PASSED
    assert report.comparison is None


# --- fail closed -------------------------------------------------------------


def test_a_missing_baseline_fails_closed_rather_than_assuming_the_failures_are_known(
    session: Session,
    project: Project,
    task: Task,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """No usable row. The three failures are real as far as anyone knows."""
    clear_baselines(session, project)
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert not report.no_new_regressions
    comparison = report.comparison
    assert comparison is not None and not comparison.available
    assert "no baseline evidence" in comparison.detail
    assert TaskRepository(session).get(task.id).status is not TaskStatus.REVIEW_PENDING
    # And the feedback is the pre-stage-2 deterministic output.
    assert report.feedback is not None
    assert report.feedback.startswith("Verification failed.")


def test_a_baseline_for_another_commit_is_not_used(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """Stale provenance. It describes a different tree, so it describes nothing."""
    clear_baselines(session, project)
    record_baseline(session, project, workspace, sha="0" * 40)
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert report.comparison is not None
    assert workspace.starting_commit in report.comparison.detail


def test_a_baseline_for_a_different_command_is_not_used(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """The command text is provenance too: a changed suite is a new baseline."""
    clear_baselines(session, project)
    record_baseline(session, project, workspace, command=f"{PYTHON} tools/old.py")
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.UNCLASSIFIED_FAILURE


def test_a_baseline_without_identifiable_failures_cannot_make_anything_known(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    clear_baselines(session, project)
    record_baseline(
        session, project, workspace, identities=(), failures_available=False
    )
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert report.comparison is not None
    assert "without identifiable failures" in report.comparison.detail


def test_a_non_zero_command_with_no_readable_failures_fails_closed(
    session: Session,
    project: Project,
    task: Task,
    verification_settings: Settings,
    run: TaskRun,
):
    """A runner whose output no adapter reads. The baseline cannot help.

    This is the case that must not be waved through: a valid baseline exists,
    the candidate's command failed, and the orchestrator cannot tell what
    failed. Falling back on "the baseline failed too, so this is fine" would be
    the whole fail-closed rule inverted.
    """
    opaque = f"{PYTHON} tools/opaque.py"
    ProjectRepository(session).update_fields(
        project_id=task.project_id,
        verification_profile={"tests": [opaque]},
    )
    project = ProjectRepository(session).get(task.project_id)
    assert project is not None
    workspace = prepare_workspace(session, run.id, settings=verification_settings)
    record_baseline(session, project, workspace, command=opaque)
    candidate(workspace, source())

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.UNCLASSIFIED_FAILURE
    assert report.comparison is not None
    assert "could not be identified" in report.comparison.detail


# --- evidence ----------------------------------------------------------------


def test_the_raw_log_is_still_on_disk_and_the_feedback_only_points_at_it(
    session: Session,
    project: Project,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    regression = "tests/test_nav.py::test_navigate"
    record_baseline(session, project, workspace)
    candidate(workspace, source(*BASELINE_FAILURES, regression))

    report = verify_candidate(session, workspace, settings=verification_settings)

    step = report.steps_for(VerificationType.TESTS)[0]
    assert step.log_artifact is not None
    log = (verification_settings.artifact_root / step.log_artifact).read_text()
    # Everything is in the log, including the failures the coder is not shown.
    for identity in (*BASELINE_FAILURES, regression):
        assert identity in log
    # And the verification artifact carries the classification for a later reader.
    stored = json.loads(
        (
            verification_settings.artifact_root
            / report.artifacts["verification.json"]
        ).read_text()
    )
    assert stored["classification"] == "NEW_REGRESSION"
    assert stored["comparison"]["new"] == [regression]


def test_the_classification_is_on_the_run_event_and_the_rows_are_unchanged(
    session: Session,
    project: Project,
    run: TaskRun,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    record_baseline(session, project, workspace)
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    events = RunEventRepository(session).list_for_run(run.id)
    kinds = [event.event_type for event in events]
    # Both are true and both are recorded: the tests category really did fail,
    # and the candidate really did earn a reviewer. The per-category
    # ``BUILD_FAILED`` is the pipeline's existing record of the command's own
    # verdict and is left exactly as it was.
    assert RunEventType.BUILD_FAILED in kinds
    assert RunEventType.TESTS_PASSED in kinds
    passed = [
        event for event in events if event.event_type == RunEventType.TESTS_PASSED
    ]
    assert passed[-1].payload["classification"] == "KNOWN_BASELINE_ONLY"
    assert passed[-1].payload["comparison"]["counts"]["new"] == 0

    # ``verification_runs`` still records the command's real verdict. The
    # classification does not rewrite what the command returned.
    rows = VerificationRunRepository(session).list_for_run(run.id)
    tests = [row for row in rows if row.verification_type is VerificationType.TESTS]
    assert tests and all(row.status is VerificationStatus.FAILED for row in tests)
    assert report.classification is VerificationClassification.KNOWN_BASELINE_ONLY


# --- capture -----------------------------------------------------------------


def test_capture_baseline_measures_a_tree_and_records_what_it_found(
    session: Session,
    project: Project,
    run: TaskRun,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """The deliberate capture path, over a tree the orchestrator already has.

    No Git state is moved and the candidate is never disturbed: the worktree
    handed in is already at the commit named, and that commit is the provenance
    every later classification is matched against.
    """
    recorded = capture_baseline(
        session,
        project,
        run,
        worktree_path=workspace.path,
        baseline_sha=workspace.starting_commit,
        settings=verification_settings,
    )

    assert len(recorded) == 1
    entry = recorded[0]
    assert entry.baseline_sha == workspace.starting_commit
    assert entry.verification_type is VerificationType.TESTS
    assert entry.command == TEST_COMMAND
    assert entry.status is VerificationStatus.FAILED
    assert entry.usable
    assert entry.extractor == "pytest"
    assert entry.identities == frozenset(BASELINE_FAILURES)
    assert entry.stdout_artifact is not None
    assert entry.source_task_run_id == run.id


def test_a_captured_baseline_is_what_the_next_candidate_is_classified_against(
    session: Session,
    project: Project,
    run: TaskRun,
    task: Task,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """Capture once per tree, reuse for every candidate that starts from it."""
    capture_baseline(
        session,
        project,
        run,
        worktree_path=workspace.path,
        baseline_sha=workspace.starting_commit,
        settings=verification_settings,
    )
    candidate(workspace, source(*BASELINE_FAILURES))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.KNOWN_BASELINE_ONLY
    assert TaskRepository(session).get(task.id).status is TaskStatus.REVIEW_PENDING


def test_recapturing_the_same_tree_replaces_the_evidence_rather_than_duplicating_it(
    session: Session,
    project: Project,
    run: TaskRun,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    for _ in range(2):
        capture_baseline(
            session,
            project,
            run,
            worktree_path=workspace.path,
            baseline_sha=workspace.starting_commit,
            settings=verification_settings,
        )

    rows = VerificationBaselineRepository(session).list_for_sha(
        project.id, workspace.starting_commit
    )
    assert len(rows) == 1


def test_classification_costs_no_model_call(
    session: Session,
    project: Project,
    run: TaskRun,
    workspace: TaskWorkspace,
    verification_settings: Settings,
):
    """Stage 2 is deterministic, and this is what that claim means in rows.

    Stage 3 may put a local model in front of a failure it cannot classify.
    Stage 2 must not, because the evidence has to be trustworthy before
    anything is asked to interpret it -- and because a verification that costs
    a model call costs it on every attempt of every task.
    """
    regression = "tests/test_nav.py::test_navigate"
    capture_baseline(
        session,
        project,
        run,
        worktree_path=workspace.path,
        baseline_sha=workspace.starting_commit,
        settings=verification_settings,
    )
    candidate(workspace, source(*BASELINE_FAILURES, regression))

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.classification is VerificationClassification.NEW_REGRESSION
    assert ModelRunRepository(session).list_for_run(run.id) == []
