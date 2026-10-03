"""NEW_REGRESSION reaches the coder on its own (concern 78, stage 3).

Stage 2 left the orchestrator able to say "this candidate broke these three
tests, and the other forty-two were already broken" -- and then left that
sentence on a report. These tests pin the routing that follows it, which is the
whole autonomy claim: an ordinary regression becomes bounded evidence, becomes
one repair attempt, becomes another authoritative verification, and reaches
review when it holds. Nobody reads a log for any of that.

They also pin the three cases that must *not* become a repair instruction: a
baseline-only failure (the candidate caused nothing), an unclassified failure
(stage 2 refused to attribute it, so stage 3 does not either) and a lint
failure (no stable failure identities, existing semantics unchanged).

Everything runs on the subprocess worker backend with a real worktree and a
real git repository, as the phase H and J tests do. No model endpoint is
touched: the coder and reviewer answers are scripted, and the classification is
produced entirely by the orchestrator's own comparison against the baseline
``prepare_workspace`` certified.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.agents.fix_loop import LoopOutcome, run_fix_loop
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.enums import (
    Complexity,
    FailureReason,
    ModelPurpose,
    TaskStatus,
    VerificationClassification,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ModelRunRepository,
    ProjectRepository,
    TaskRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git
from tests.integration.test_fix_loop import ScriptedModel, _review, reviewer

pytestmark = pytest.mark.integration

PYTHON = "python3"

if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)


#: A runner whose failures the candidate decides, printing what a real one
#: prints -- a short summary and the closing banner the extractor reconciles
#: against. The chatter is there so a test can assert it never travels.
_TEST = """\
import sys

source = open("src/nav.py").read()
if "CRASH" in source:
    # The runner dies before collecting anything: no banner, so no failure
    # list that can be vouched for.
    print("the test harness crashed before collecting anything", file=sys.stderr)
    sys.exit(3)
failing = []
for line in source.splitlines():
    if line.startswith("# FAIL:"):
        failing = line.removeprefix("# FAIL:").split()
print("============ test session starts ============")
for index in range(120):
    print(f"src/module_{index}.py ..........")
print("=== short test summary info ===")
for name in failing:
    print(f"FAILED {name} - AssertionError: navigate returned None")
if failing:
    print(f"=== {len(failing)} failed, 9 passed in 0.10s ===")
    sys.exit(1)
print("=== 10 passed in 0.10s ===")
"""

#: A lint command that fails the way a linter fails: no test identities at all.
_LINT = """\
import sys
source = open("src/nav.py").read()
if "LINT ERROR" in source:
    print("src/nav.py:1:1: E999 invalid syntax", file=sys.stderr)
    sys.exit(1)
print("All checks passed!")
"""

TEST_COMMAND = f"{PYTHON} tools/test.py"
LINT_COMMAND = f"{PYTHON} tools/lint.py"

#: The failure the committed tree already has. Nothing the candidate does to it
#: is the candidate's fault, and no repair attempt may be asked for it.
PRE_EXISTING = "tests/test_legacy.py::test_old_route"

#: The failure a broken candidate introduces.
REGRESSION = "tests/test_nav.py::test_navigate"

COMMITTED = "def navigate(target):\n    return target\n"


def source(*failing: str, note: str = "") -> str:
    """A candidate whose suite fails exactly these tests."""
    body = (
        "def navigate(target):\n"
        f'    """Navigate to target.{note}"""\n'
        "    if target is None:\n"
        "        raise ValueError('target is required')\n"
        "    return target\n"
    )
    return f"{body}# FAIL: {' '.join(failing)}\n" if failing else body


def _code(text: str, *, summary: str = "Implemented navigate.") -> str:
    payload = {
        "summary": summary,
        "edits": [{"path": "src/nav.py", "operation": "update", "content": text}],
        "requirementsMet": ["navigate rejects a null target"],
        "testsAdded": ["tests/test_nav.py"],
        "followUps": [],
        "deviationsFromPlan": [],
    }
    return "```json\n" + json.dumps(payload) + "\n```"


@pytest.fixture
def loop_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=60,
    )


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tracestack"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    # The committed tree is already failing one test: the realistic case, and
    # the reason the baseline has to exist at all.
    (repo / "src" / "nav.py").write_text(f"{COMMITTED}# FAIL: {PRE_EXISTING}\n")
    (repo / "tools" / "test.py").write_text(_TEST)
    (repo / "tools" / "lint.py").write_text(_LINT)
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
            verification=VerificationProfile(
                lint=(LINT_COMMAND,), tests=(TEST_COMMAND,)
            ),
        )
    )


@pytest.fixture
def task_factory(session: Session, project: Project):
    def make(**overrides) -> Task:
        fields: dict[str, object] = {
            "project_id": project.id,
            "external_task_id": "TS-078",
            "title": "Reject a null navigation target",
            "instructions": "navigate must reject a null target.",
            "complexity": Complexity.LOW,
            "files_to_modify": ["src/nav.py"],
            "limits": TaskLimits(max_files_changed=3, max_diff_lines=200),
        }
        fields.update(overrides)
        tasks = TaskRepository(session)
        task = tasks.add(Task(**fields))  # type: ignore[arg-type]
        return tasks.transition(task.id, TaskStatus.READY)

    return make


@pytest.fixture
def task(task_factory) -> Task:
    return task_factory()


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return create_run(session, task.id)


@pytest.fixture
def workspace(session: Session, run: TaskRun, loop_settings: Settings) -> TaskWorkspace:
    # Certifies the baseline for the starting commit as a side effect: stage 2's
    # machinery, untouched and relied on rather than re-created here.
    return prepare_workspace(session, run.id, settings=loop_settings)


def _coding_calls(session: Session, run: TaskRun) -> list:
    return [
        call
        for call in ModelRunRepository(session).list_for_run(run.id)
        if call.purpose in {ModelPurpose.CODE, ModelPurpose.FIX}
    ]


# --- 5, 6, 7: the regression routes itself, is repaired, and goes to review --


@pytest.mark.asyncio
async def test_a_new_regression_is_repaired_without_a_human_and_reaches_review(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """The whole stage 3 claim, end to end.

    Attempt 1 introduces a regression. Nobody reads a log: the orchestrator
    classifies it, packages bounded evidence, spends one repair attempt, runs
    authoritative verification again and -- because the repair holds -- reaches
    the reviewer, which approves.
    """
    coder = ScriptedModel(
        _code(source(PRE_EXISTING, REGRESSION), summary="First attempt."),
        _code(source(PRE_EXISTING, note=" Fixed."), summary="Repaired the regression."),
    )

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    assert result.escalation is None
    # Two coding attempts, and the second was a repair.
    assert len(result.iterations) == 2
    assert [call.purpose for call in _coding_calls(session, run)] == [
        ModelPurpose.CODE,
        ModelPurpose.FIX,
    ]

    # Verification ran after the fix as well as before it, and the second run
    # was the one that cleared the candidate.
    first, second = result.iterations
    assert first.verification is not None and second.verification is not None
    assert (
        first.verification.classification is VerificationClassification.NEW_REGRESSION
    )
    assert second.verification.no_new_regressions
    # The pre-existing failure is still failing, and that did not stop review:
    # the repaired candidate is KNOWN_BASELINE_ONLY, not PASSED.
    assert (
        second.verification.classification
        is VerificationClassification.KNOWN_BASELINE_ONLY
    )
    assert second.review is not None and second.review.approved
    # Both verification runs are on the record: the authoritative pipeline ran
    # again after the repair rather than trusting it.
    assert len(VerificationRunRepository(session).list_for_run(run.id)) >= 2


@pytest.mark.asyncio
async def test_the_repair_attempt_is_sent_bounded_evidence_and_not_the_log(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """What the FIX model actually received, asserted on the request itself."""
    coder = ScriptedModel(
        _code(source(PRE_EXISTING, REGRESSION)),
        _code(source(PRE_EXISTING, note=" Fixed.")),
    )

    await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    feedback = coder.requests[1].review_feedback
    assert feedback is not None
    assert "new regression(s)" in feedback
    assert REGRESSION in feedback
    assert TEST_COMMAND in feedback

    # The failure the candidate did not cause never travels, and neither does
    # the suite's own chatter -- the whole log stays on disk and is referenced.
    assert PRE_EXISTING not in feedback
    assert "test session starts" not in feedback
    assert "src/module_90.py" not in feedback
    assert ".log" in feedback

    # Stage 1's division of labour is restated to the repair attempt, in both
    # halves of what it is served.
    served = " ".join(
        (
            coder.requests[1].system_instructions
            + "\n"
            + coder.requests[1].task_instructions
        ).split()
    ).lower()
    assert "targeted testing of your own change is allowed" in served
    assert "authoritative verification is not yours" in served
    assert "do not run the project's full verification workflow" in served
    assert "this is a repair request" in served


# --- 8: a baseline-only failure is not a regression to repair ---------------


@pytest.mark.asyncio
async def test_a_baseline_only_failure_does_not_trigger_a_repair_attempt(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    coder = ScriptedModel(_code(source(PRE_EXISTING, note=" Implemented.")))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    # One coding attempt: no repair was asked for, and no FIX call was made.
    assert [call.purpose for call in _coding_calls(session, run)] == [ModelPurpose.CODE]
    (iteration,) = result.iterations
    assert iteration.verification is not None
    assert (
        iteration.verification.classification
        is VerificationClassification.KNOWN_BASELINE_ONLY
    )
    assert iteration.verification.repair_evidence is None


# --- 9 and 10: what stage 2 would not attribute, stage 3 does not either -----


@pytest.mark.asyncio
async def test_an_unclassified_failure_is_not_routed_as_an_attributed_regression(
    session: Session,
    task_factory,
    loop_settings: Settings,
):
    """A failure with no comparable baseline evidence stays unattributed.

    The run keeps its existing behaviour -- the deterministic command output
    goes back and the attempt budget bounds it -- but nothing tells the coder
    the orchestrator proved this was its regression, and no repair evidence is
    built.
    """
    task = task_factory(
        external_task_id="TS-078-OPAQUE",
        limits=TaskLimits(max_attempts=1, max_files_changed=3),
    )
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=loop_settings)
    # The candidate makes its own failure list unreadable: the runner dies
    # before printing a banner, so the extractor refuses and the comparison is
    # unavailable. Exactly the state stage 2 fails closed on, reached without
    # touching a file outside the task's scope.
    coder = ScriptedModel(_code(source(note=" CRASH")))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(),
        settings=loop_settings,
    )

    (iteration,) = result.iterations
    assert iteration.verification is not None
    assert (
        iteration.verification.classification
        is VerificationClassification.UNCLASSIFIED_FAILURE
    )
    assert iteration.verification.repair_evidence is None
    feedback = iteration.feedback or ""
    assert "new regression" not in feedback
    # Existing retry/escalation semantics, unchanged: one attempt was allowed.
    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED


@pytest.mark.asyncio
async def test_a_lint_failure_keeps_its_own_semantics(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """Requirement 8: a nonzero lint command is not a code regression."""
    coder = ScriptedModel(
        _code(source(PRE_EXISTING, note=" LINT ERROR")),
        _code(source(PRE_EXISTING, note=" Fixed.")),
    )

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    first = result.iterations[0]
    assert first.verification is not None
    assert first.failure_reason is FailureReason.LINT_FAILED
    assert (
        first.verification.classification
        is VerificationClassification.UNCLASSIFIED_FAILURE
    )
    assert first.verification.repair_evidence is None
    feedback = first.feedback or ""
    assert feedback.startswith("Verification failed.")
    assert LINT_COMMAND in feedback
    assert "new regression" not in feedback
    # And it still goes back to the coder exactly as it did before stage 3.
    assert result.outcome is LoopOutcome.APPROVED


# --- 11: repair attempts stay inside the existing budget --------------------


@pytest.mark.asyncio
async def test_repeated_regressions_exhaust_the_existing_attempt_budget(
    session: Session,
    task_factory,
    loop_settings: Settings,
):
    """No second retry counter: the task's own attempt limit ends the repairs."""
    task = task_factory(
        external_task_id="TS-078-BUDGET",
        limits=TaskLimits(max_attempts=2, max_files_changed=3),
    )
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=loop_settings)
    coder = ScriptedModel(
        _code(source(PRE_EXISTING, REGRESSION, note=" One.")),
        _code(source(PRE_EXISTING, REGRESSION, note=" Two.")),
    )

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert result.attempts_used == 2
    assert len(result.iterations) == 2
    assert coder.answers == []
    # The escalation a person reads carries the evidence of the last attempt.
    assert result.escalation is not None
    assert all(
        iteration.verification is not None
        and iteration.verification.classification
        is VerificationClassification.NEW_REGRESSION
        for iteration in result.iterations
    )


# --- 13: the classification remains the orchestrator's, not the model's -----


@pytest.mark.asyncio
async def test_the_classification_comes_from_the_baseline_and_not_from_a_model(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """Stage 2's authority, re-asserted through the stage 3 path.

    The coder is told nothing about a baseline, is never asked what failed, and
    the only model calls made are code, fix and review. The comparison names the
    exact commit the candidate started from.
    """
    coder = ScriptedModel(
        _code(source(PRE_EXISTING, REGRESSION)),
        _code(source(PRE_EXISTING, note=" Fixed.")),
    )

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    purposes = {
        call.purpose for call in ModelRunRepository(session).list_for_run(run.id)
    }
    assert purposes <= {ModelPurpose.CODE, ModelPurpose.FIX, ModelPurpose.REVIEW}

    comparison = result.iterations[0].verification.comparison
    assert comparison is not None and comparison.available
    assert comparison.baseline_sha == workspace.starting_commit
    assert comparison.new == frozenset({REGRESSION})
    assert comparison.known == frozenset({PRE_EXISTING})
    assert comparison.extractors == ("pytest",)

    # Nothing in what the coder was served asks it to establish or compare one.
    for request in coder.requests:
        served = " ".join(
            (request.system_instructions + request.task_instructions).split()
        ).lower()
        assert "establish a baseline" not in served or "do not" in served
        assert "do not compare" in served or "do not run a baseline" in served
