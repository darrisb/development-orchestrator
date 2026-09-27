"""The Fix Loop (build.md sections 23-25, phase J).

The phase J exit condition, in one sentence: *a deliberately flawed fixture can
be corrected and approved, while an unfixable fixture escalates.* The first two
tests are that sentence, one clause each, and they run the whole loop -- a
scripted coder writing real files into a real worktree, the project's own
commands executed by a real worker, a scripted reviewer reading the real diff.
Nothing between the coder and the reviewer is stubbed, because what phase J adds
is precisely the wiring between them.

The commands run on the subprocess worker backend, as the phase D and H tests
do, so the suite still passes on a machine without Docker.

The rest of the file is the loop's bounds and its bookkeeping: that it cannot
run longer than the task permits, that a rejected candidate is rolled back
rather than reviewed, that a correction attempt is not made to re-plan, and that
findings a re-review let go are closed (concern 27).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.agents.fix_loop import (
    FIX_LOOP_ARTIFACT,
    LoopOutcome,
    run_fix_loop,
)
from apps.orchestrator.agents.review_agent import ESCALATION_ARTIFACT
from apps.orchestrator.agents.review_prompts import (
    REVIEWER_SYSTEM_PROMPT,
    render_review_instructions,
)
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.enums import (
    Complexity,
    EscalationStatus,
    FailureReason,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.providers import (
    ConnectionReport,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    TokenUsage,
)
from apps.orchestrator.providers.review import ModelReviewProvider
from apps.orchestrator.providers.structured import parse_structured, strip_reasoning
from apps.orchestrator.repositories import (
    ArtifactRepository,
    EscalationRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration

PYTHON = "python3"

if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)


# --- the project's own tooling, standing in for npm ---------------------------
#
# Each reads the candidate and fails the way a real tool would, so a broken
# fixture fails for the reason the fixture is broken.

_COMPILE = """\
import sys
source = open("src/nav.py").read()
if "SYNTAX ERROR" in source:
    print("src/nav.py:1: invalid syntax", file=sys.stderr)
    sys.exit(2)
print("compiled 1 file")
"""

_TEST = """\
import sys
source = open("src/nav.py").read()
if "return None" in source:
    print("FAILED tests/test_nav.py::test_navigate - AssertionError: got None", file=sys.stderr)
    sys.exit(1)
print("2 passed")
"""


# --- a coder and a reviewer that answer from a script -------------------------


class ScriptedModel:
    """A ``ModelProvider`` replaying prepared answers, parsing its own text.

    Text rather than objects, as in the phase G and I tests: the JSON has to
    survive a reasoning block and a Markdown fence exactly as it does from a
    real endpoint, so the parsing path under test is the real one.
    """

    def __init__(self, *answers: str | Exception, role: ModelRole = ModelRole.CODER) -> None:
        self.config = ProviderConfig(
            provider_id=f"scripted-{role.value.casefold()}",
            base_url="http://stub/v1",
            model_name=f"{role.value.casefold()}-test",
            role=role,
            context_window=32768,
        )
        self.answers: list[str | Exception] = list(answers)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        assert self.answers, "the model was asked for more answers than the test scripted"
        raw = self.answers.pop(0)
        if isinstance(raw, Exception):
            raise raw
        text = strip_reasoning(raw)
        data = (
            parse_structured(text, request.schema.schema, schema_name=request.schema.name)
            if request.schema
            else None
        )
        return ModelResponse(
            text=text,
            raw_text=raw,
            model_name=self.config.model_name,
            provider_id=self.config.provider_id,
            data=data,
            finish_reason="stop",
            usage=TokenUsage(input_tokens=1200, output_tokens=180),
            duration_ms=37,
        )

    async def check_connection(self) -> ConnectionReport:
        return ConnectionReport(provider_id=self.config.provider_id, reachable=True)

    async def aclose(self) -> None:
        return None

    @property
    def purposes(self) -> list[str]:
        """What each request was for, in order: ``plan`` or ``code``."""
        return [str(request.metadata.get("purpose", "")) for request in self.requests]

    @property
    def feedback_sent(self) -> list[str | None]:
        return [request.review_feedback for request in self.requests]


def reviewer(*answers: str | Exception) -> ModelReviewProvider:
    return ModelReviewProvider(
        ScriptedModel(*answers, role=ModelRole.REVIEWER),
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        instruction_renderer=render_review_instructions,
    )


# --- what the coder writes ----------------------------------------------------

#: What the repository starts with. Deliberately none of the candidates below:
#: an edit that writes back a file's existing contents produces an empty diff,
#: which the scope guard blocks, and a fixture whose first attempt failed for
#: that reason would be testing the guard rather than the loop.
STUB = "def navigate(target):\n    pass  # TODO: TS-004\n"
BROKEN = "def navigate(target):\n    return None\n"
WORKING = "def navigate(target):\n    return target\n"
REVIEWED = (
    "def navigate(target):\n"
    "    if target is None:\n"
    "        raise ValueError('target is required')\n"
    "    return target\n"
)


def _code(source: str, *, path: str = "src/nav.py", summary: str = "Implemented navigate.") -> str:
    """One edit, wrapped the way a reasoning model wraps one."""
    payload = {
        "summary": summary,
        "edits": [{"path": path, "operation": "update", "content": source}],
        "requirementsMet": ["navigate returns its target"],
        "testsAdded": [],
        "followUps": [],
        "deviationsFromPlan": [],
    }
    return "<think>Reading the task.</think>\n```json\n" + json.dumps(payload) + "\n```"


def _plan() -> str:
    payload = {
        "filesToInspect": [],
        "filesToModify": ["src/nav.py"],
        "filesToCreate": [],
        "approach": ["Read the current implementation", "Return the target", "Run the tests"],
        "risks": [],
        "expectedTests": ["tests/test_nav.py"],
    }
    return "```json\n" + json.dumps(payload) + "\n```"


def _review(**overrides) -> str:
    payload = {
        "taskId": "TS-004",
        "decision": "APPROVED",
        "confidence": 0.9,
        "risk": "LOW",
        "summary": "navigate returns its target and the tests cover it.",
        "issues": [],
    }
    payload.update(overrides)
    return (
        "<think>Checking the diff against the task.</think>\n"
        "```json\n" + json.dumps(payload) + "\n```"
    )


_MISSING_GUARD = {
    "severity": "HIGH",
    "category": "requirement",
    "file": "src/nav.py",
    "line": 2,
    "requirementId": "TS-004-R2",
    "problem": "A null target is returned instead of being rejected.",
    "requiredFix": "Raise when target is None.",
}

_UNTESTED = {
    "severity": "HIGH",
    "category": "testing",
    "file": "tests/test_nav.py",
    "requirementId": "TS-004-R9",
    "problem": "The guard has no test.",
    "requiredFix": "Add a test for the null target.",
}


# --- the fixture project -----------------------------------------------------


@pytest.fixture
def loop_settings(tmp_path: Path) -> Settings:
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
    (repo / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (repo / "tools" / "compile.py").write_text(_COMPILE)
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
            protected_paths=[".env", "secrets/**"],
            verification=VerificationProfile(
                build=(f"{PYTHON} tools/compile.py",),
                tests=(f"{PYTHON} tools/test.py",),
            ),
        )
    )


@pytest.fixture
def task_factory(session: Session, project: Project):
    def make(**overrides) -> Task:
        fields: dict[str, object] = {
            "project_id": project.id,
            "external_task_id": "TS-004",
            "title": "Reject a null navigation target",
            "instructions": "navigate must return its target and reject a null one.",
            # Low: most of these tests are about the loop, not the planner, and
            # a task that plans on every fixture would put a plan answer in
            # front of every scripted code answer. The task that *is* about
            # planning asks for a complexity that requires one.
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
    return prepare_workspace(session, run.id, settings=loop_settings)


def _status(session: Session, task: Task) -> TaskStatus:
    return TaskRepository(session).get(task.id).status


def _events(session: Session, run: TaskRun) -> list[RunEventType]:
    return [event.event_type for event in RunEventRepository(session).list_for_run(run.id)]


def _artifact(settings: Settings, path: str) -> str:
    return (settings.artifact_root / path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_runtime_exhaustion_is_not_retry_exhaustion_and_reports_evidence(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    coder = ScriptedModel()
    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(),
        settings=loop_settings,
        deadline=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert result.failure_reason is FailureReason.RUNTIME_EXHAUSTED
    assert result.attempts_used == 0
    assert result.cycles_used == 0
    assert result.iterations == ()
    assert coder.requests == []
    assert result.remaining_runtime_ms == 0
    assert result.escalation is not None
    assert "runtime, not retries, was exhausted" in result.escalation.summary
    assert "0 coding attempt(s)" in result.escalation.summary
    assert "0 of the task's" not in result.escalation.summary


@pytest.mark.asyncio
async def test_worker_invocation_timeout_is_not_model_or_run_runtime_exhaustion(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    loop_settings: Settings,
):
    coder = ScriptedModel()
    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(),
        settings=loop_settings,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        worker_deadline=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert result.failure_reason is FailureReason.WORKER_FAILURE
    assert result.failure_reason is not FailureReason.MODEL_TIMEOUT
    assert result.failure_reason is not FailureReason.RUNTIME_EXHAUSTED
    assert coder.requests == []


# --- the exit condition, clause one ------------------------------------------


@pytest.mark.asyncio
async def test_a_flawed_fixture_is_corrected_and_approved(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """Phase J's exit condition, first half. Three turns: a candidate whose
    tests fail, a candidate a reviewer sends back, and the fix it asked for.

    Both kinds of correction are exercised in one run on purpose -- a
    deterministic failure and a review finding travel the same wire, and the
    thing worth proving is that the loop can tell them apart and keep counting.
    """
    coder = ScriptedModel(
        _code(BROKEN),  # tests fail
        _code(WORKING),  # verified, but the reviewer wants the guard
        _code(REVIEWED),  # the fix
    )

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(
            _review(
                decision="CHANGES_REQUESTED",
                summary="A null target is still accepted.",
                issues=[_MISSING_GUARD],
            ),
            _review(summary="The guard rejects a null target."),
        ),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    assert result.approved
    assert result.attempts_used == 3
    assert result.cycles_used == 2
    assert result.task_status is TaskStatus.APPROVED
    assert _status(session, task) is TaskStatus.APPROVED
    assert result.escalation is None
    assert not result.rolled_back

    # The corrected code is what is actually in the worktree.
    assert (workspace.path / "src" / "nav.py").read_text() == REVIEWED

    # Each correction carried the real evidence, not a summary of it. The first
    # attempt had nothing to be told; the second got the failing test's own
    # output; the third got the reviewer's finding.
    first, second, third = coder.feedback_sent
    assert first is None
    assert "AssertionError: got None" in second
    assert f"{PYTHON} tools/test.py" in second
    assert "A null target is returned instead of being rejected." in third
    assert "TS-004-R2" in third

    # The turns are what the report says they are.
    assert [iteration.stage for iteration in result.iterations] == [
        "verification",
        "review",
        "review",
    ]
    assert result.iterations[0].failure_reason is FailureReason.TEST_FAILED
    assert result.iterations[1].failure_reason is FailureReason.REVIEW_CHANGES_REQUESTED
    assert result.iterations[2].failure_reason is None


@pytest.mark.asyncio
async def test_the_approved_run_leaves_a_readable_history(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    loop_settings: Settings,
):
    """Every turn has to be reconstructible afterwards: section 34's point, and
    what phase L will read. One directory per turn, and one summary over them."""
    coder = ScriptedModel(_code(BROKEN), _code(WORKING))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    summary = json.loads(_artifact(loop_settings, result.artifacts[FIX_LOOP_ARTIFACT]))
    assert summary["outcome"] == "APPROVED"
    assert summary["attempts_used"] == 2
    assert [turn["attempt"] for turn in summary["iterations"]] == [1, 2]

    # The first turn writes section 9's unprefixed names; the second is filed
    # under the attempt and the cycle it belongs to (concern 33).
    assert result.iterations[0].coding.artifacts["prompt.txt"].endswith(
        "/prompt.txt"
    )
    assert "attempt-2-cycle-1/" in result.iterations[1].coding.artifacts["prompt.txt"]
    assert "attempt-2-cycle-1/" in result.iterations[1].review.artifacts["review.json"]

    events = _events(session, run)
    assert RunEventType.FIX_STARTED in events
    assert RunEventType.APPROVED in events
    # The fix event sits between the failure that caused it and the retry.
    assert events.index(RunEventType.BUILD_FAILED) < events.index(RunEventType.FIX_STARTED)


def _outcomes_recorded(session: Session, run: TaskRun) -> list[str]:
    """Every outcome value the run's event log recorded, in order."""
    return [
        str(event.payload.get("outcome"))
        for event in RunEventRepository(session).list_for_run(run.id)
        # StrEnum compares equal to its value; the domain model carries a str.
        if event.event_type == RunEventType.OUTCOME_RECORDED
    ]


@pytest.mark.asyncio
async def test_an_approved_run_is_never_recorded_as_rejected(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    loop_settings: Settings,
):
    """Concern 48: the loop must not write a terminal outcome for an approval.

    An approved run has not settled when the loop ends -- its candidate is
    uncommitted and delivery is the workflow's next step -- and the function
    that records a settled outcome maps everything that is not an escalation to
    "rejected". So an approval used to acquire a durable "rejected" record that
    delivery then corrected, and a crash in between left the history asserting
    the opposite of what happened.
    """
    result = await run_fix_loop(
        session,
        workspace,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    recorded = _outcomes_recorded(session, run)
    assert "rejected" not in recorded
    # The provisional record stands instead, which is what a run awaiting
    # delivery actually is. Delivery replaces it with "accepted".
    assert recorded == ["in_progress"]


@pytest.mark.asyncio
async def test_a_rejected_run_is_still_recorded_as_rejected(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """The other direction: concern 48's fix must not silence a real rejection."""
    changes = _review(
        decision="CHANGES_REQUESTED",
        summary="Still wrong.",
        issues=[_MISSING_GUARD],
    )
    result = await run_fix_loop(
        session,
        workspace,
        coder=ScriptedModel(_code(WORKING), _code(WORKING), _code(WORKING)),
        reviewer=reviewer(changes, changes, changes),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.ESCALATED
    assert "escalated" in _outcomes_recorded(session, run)


# --- the exit condition, clause two -----------------------------------------


@pytest.mark.asyncio
async def test_an_unfixable_fixture_escalates_when_the_attempts_run_out(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """Phase J's exit condition, second half. A coder that never fixes the
    failing test spends the task's three attempts and stops, with an escalation
    a person can act on -- not a fourth attempt."""
    coder = ScriptedModel(_code(BROKEN), _code(BROKEN), _code(BROKEN))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        # The reviewer is never reached: nothing ever verified.
        reviewer=reviewer(),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert result.attempts_used == 3 == task.limits.max_attempts
    assert result.cycles_used == 0
    assert not coder.answers, "the loop stopped before spending a fourth attempt"

    assert _status(session, task) is TaskStatus.HUMAN_REVIEW
    stored_run = TaskRunRepository(session).get(run.id)
    assert stored_run.status is RunStatus.FAILED
    assert stored_run.failure_reason == FailureReason.RETRY_EXHAUSTED.value
    assert stored_run.completed_at is not None

    # Section 24: the whole history, without a person reconstructing it.
    (escalation,) = EscalationRepository(session).list_open(task_id=task.id)
    assert escalation.status is EscalationStatus.OPEN
    assert escalation.reason == FailureReason.RETRY_EXHAUSTED.value
    assert "TASK TS-004" in escalation.summary
    assert escalation.summary.count("attempt ") >= 3
    assert f"{PYTHON} tools/test.py" in escalation.summary
    assert "navigate must return its target" in escalation.summary
    assert len(escalation.options) == 3

    # The candidate is preserved, not rolled back: it is what a person has been
    # asked to look at.
    assert not result.rolled_back
    assert (workspace.path / "src" / "nav.py").read_text() == BROKEN
    assert stored_run.starting_commit in escalation.summary
    assert RunEventType.HUMAN_REVIEW_REQUIRED in _events(session, run)


@pytest.mark.asyncio
async def test_repeated_identical_findings_escalate_before_spending_the_full_budget(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """An unchanged blocking set twice is evidence the run is not converging."""
    coder = ScriptedModel(_code(WORKING), _code(REVIEWED), _code(WORKING))
    changes = _review(
        decision="CHANGES_REQUESTED",
        summary="Still not what the requirement asks for.",
        issues=[_MISSING_GUARD],
    )

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(changes, changes, changes),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert result.cycles_used == 2 < task.limits.max_review_cycles
    assert _status(session, task) is TaskStatus.HUMAN_REVIEW
    assert len(coder.answers) == 1

    (escalation,) = EscalationRepository(session).list_open(task_id=task.id)
    assert "2 of the task's 3 permitted attempts" in escalation.summary
    assert "A null target is returned instead of being rejected." in escalation.summary
    assert result.escalation.id == escalation.id


@pytest.mark.asyncio
async def test_a_later_correction_still_carries_the_findings_from_earlier_cycles(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """Concern 39: a coder told only the newest cycle's findings can regress an
    earlier fix while addressing a later one, and by concern 27's rule the
    original was already marked resolved -- so it comes back as a new finding
    rather than as a regression, a cycle later.
    """
    coder = ScriptedModel(_code(WORKING), _code(REVIEWED), _code(REVIEWED))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(
            _review(
                decision="CHANGES_REQUESTED",
                summary="A null target is still accepted.",
                issues=[_MISSING_GUARD],
            ),
            _review(
                decision="CHANGES_REQUESTED",
                summary="The guard is untested.",
                issues=[_UNTESTED],
            ),
            _review(summary="Guarded and tested."),
        ),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    _first, second, third = coder.feedback_sent

    # Cycle two's finding is the newest evidence and leads.
    assert "The guard has no test." in third
    # Cycle one's finding is still there, under a heading that says why.
    assert "A null target is returned instead of being rejected." in third
    assert "do not regress" in third
    assert third.index("The guard has no test.") < third.index("do not regress")
    # And the second turn was not given anything earlier, because there was
    # nothing earlier: the history is carried, never invented.
    assert "do not regress" not in second


@pytest.mark.asyncio
async def test_carrying_earlier_guidance_can_be_turned_off(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """`MAX_FEEDBACK_ISSUES` exists for a real reason -- a coder handed twenty
    findings fixes none of them well -- so the history has a budget an operator
    can set to zero."""
    coder = ScriptedModel(_code(WORKING), _code(REVIEWED), _code(REVIEWED))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(
            _review(
                decision="CHANGES_REQUESTED",
                summary="A null target is still accepted.",
                issues=[_MISSING_GUARD],
            ),
            _review(
                decision="CHANGES_REQUESTED",
                summary="The guard is untested.",
                issues=[_UNTESTED],
            ),
            _review(summary="Guarded and tested."),
        ),
        settings=loop_settings.model_copy(
            update={"fix_loop_feedback_history_limit": 0}
        ),
    )

    assert result.outcome is LoopOutcome.APPROVED
    third = coder.feedback_sent[2]
    assert "The guard has no test." in third
    assert "do not regress" not in third


# --- the loop's bounds -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_single_attempt_task_escalates_after_one_turn(
    session: Session,
    task_factory,
    loop_settings: Settings,
):
    """Never loop indefinitely: the ceiling is the task's, whatever it is."""
    task = task_factory(limits=TaskLimits(max_attempts=1, max_files_changed=3))
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=loop_settings)
    coder = ScriptedModel(_code(BROKEN))

    result = await run_fix_loop(
        session, workspace, coder=coder, reviewer=reviewer(), settings=loop_settings
    )

    assert result.outcome is LoopOutcome.ESCALATED
    assert result.attempts_used == 1
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED


@pytest.mark.asyncio
async def test_max_attempts_can_be_lowered_for_a_run(
    session: Session,
    workspace: TaskWorkspace,
    loop_settings: Settings,
):
    """A caller may spend less of a task's budget than the manifest allows."""
    coder = ScriptedModel(_code(BROKEN), _code(BROKEN))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(),
        settings=loop_settings,
        max_attempts=2,
    )

    assert result.attempts_used == 2
    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED


@pytest.mark.asyncio
async def test_max_attempts_cannot_be_raised_above_the_tasks_own_ceiling(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    loop_settings: Settings,
):
    """The manifest is the authority on how much of a repository's time a task
    may spend. A caller asking for more gets the task's number."""
    coder = ScriptedModel(*[_code(BROKEN)] * task.limits.max_attempts)

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(),
        settings=loop_settings,
        max_attempts=99,
    )

    assert result.attempts_used == task.limits.max_attempts
    assert not coder.answers, "the loop asked for exactly the attempts the task allows"


@pytest.mark.asyncio
async def test_a_candidate_that_breaks_its_scope_is_rolled_back_not_reviewed(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    loop_settings: Settings,
):
    """``ROLLBACK`` ends the run. Section 25: reset the disposable worktree,
    mark the run failed, and do not hand an unsafe candidate to a reviewer."""
    coder = ScriptedModel(_code(WORKING, path=".env", summary="Added configuration."))

    result = await run_fix_loop(
        session, workspace, coder=coder, reviewer=reviewer(), settings=loop_settings
    )

    assert result.outcome is LoopOutcome.FAILED
    assert result.failure_reason is FailureReason.SCOPE_VIOLATION
    assert result.rolled_back
    assert result.escalation is None
    assert result.cycles_used == 0
    assert _status(session, task) is TaskStatus.FAILED
    assert TaskRunRepository(session).get(run.id).status is RunStatus.FAILED
    # The worktree is back at the run's starting commit, and the protected file
    # the coder aimed at was never written.
    assert not (workspace.path / ".env").exists()
    assert (workspace.path / "src" / "nav.py").read_text() == STUB


# --- bookkeeping ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_finding_the_next_review_dropped_is_marked_resolved(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    loop_settings: Settings,
):
    """Concern 27. The first review raises two findings, the second re-raises
    one: the one it let go is closed, so the third cycle's reviewer is not shown
    a finding it can see was fixed."""
    coder = ScriptedModel(_code(WORKING), _code(REVIEWED), _code(REVIEWED))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(
            _review(
                decision="CHANGES_REQUESTED",
                summary="Two problems.",
                issues=[_MISSING_GUARD, _UNTESTED],
            ),
            _review(
                decision="CHANGES_REQUESTED",
                summary="The guard is there; the test is not.",
                issues=[_UNTESTED],
            ),
            _review(summary="Both addressed."),
        ),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    reviews = ReviewRepository(session).list_for_run(run.id)
    assert [review.cycle for review in reviews] == [1, 2, 3]

    first_cycle = {issue.requirement_id: issue for issue in reviews[0].issues}
    second_cycle = {issue.requirement_id: issue for issue in reviews[1].issues}

    # The second review dropped the guard finding and re-raised the test one, so
    # exactly one issue was closed at that point -- which is the behaviour worth
    # pinning down, because it is what keeps the third package small.
    guard = first_cycle["TS-004-R2"]
    assert guard.resolved
    assert result.iterations[1].resolved_issues == (guard.id,)

    # The approval that followed closed what was still open: the test finding as
    # the first cycle raised it, and again as the second re-raised it. Nothing
    # was closed because an attempt was made; each was closed by a reviewer that
    # had it in front of it and did not raise it.
    assert first_cycle["TS-004-R9"].resolved
    assert second_cycle["TS-004-R9"].resolved
    assert set(result.iterations[2].resolved_issues) == {
        first_cycle["TS-004-R9"].id,
        second_cycle["TS-004-R9"].id,
    }


@pytest.mark.asyncio
async def test_the_first_review_resolves_nothing(
    session: Session,
    workspace: TaskWorkspace,
    loop_settings: Settings,
):
    """There is nothing earlier to close, and an approving first review must
    not be read as having resolved findings that were never raised."""
    result = await run_fix_loop(
        session,
        workspace,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    assert result.iterations[0].resolved_issues == ()


@pytest.mark.asyncio
async def test_a_correction_attempt_does_not_replan(
    session: Session,
    task_factory,
    loop_settings: Settings,
):
    """A complex task plans once. The reviewer's findings are what the fix
    attempt is following, and re-planning would spend a call on a plan nobody
    asked for -- and risk refusing the attempt over an approach that is not the
    subject of the correction."""
    task = task_factory(complexity=Complexity.HIGH)
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=loop_settings)
    coder = ScriptedModel(_plan(), _code(BROKEN), _code(WORKING))

    result = await run_fix_loop(
        session,
        workspace,
        coder=coder,
        reviewer=reviewer(_review()),
        settings=loop_settings,
    )

    assert result.outcome is LoopOutcome.APPROVED
    assert coder.purposes == ["plan", "code", "code"]
    assert RunEventType.PLAN_CREATED in [
        event.event_type for event in RunEventRepository(session).list_for_run(run.id)
    ]


@pytest.mark.asyncio
async def test_an_escalated_run_keeps_every_attempts_evidence(
    session: Session,
    workspace: TaskWorkspace,
    run: TaskRun,
    loop_settings: Settings,
):
    """Section 25: preserve the artifacts. Three attempts that failed the same
    way still have three diffs, three prompts and three verification reports,
    and the escalation text is on disk beside them."""
    coder = ScriptedModel(_code(BROKEN), _code(BROKEN), _code(BROKEN))

    result = await run_fix_loop(
        session, workspace, coder=coder, reviewer=reviewer(), settings=loop_settings
    )

    assert result.outcome is LoopOutcome.ESCALATED
    directories = {
        iteration.coding.artifacts["candidate.patch"].rsplit("/", 1)[0]
        for iteration in result.iterations
    }
    assert len(directories) == 3
    for iteration in result.iterations:
        patch = _artifact(loop_settings, iteration.coding.artifacts["candidate.patch"])
        assert "return None" in patch
        assert iteration.verification is not None
        assert iteration.verification.artifacts["verification.json"]

    kinds = {
        artifact.kind: artifact.path
        for artifact in ArtifactRepository(session).list_for_run(run.id)
    }
    escalation_text = _artifact(loop_settings, kinds[ESCALATION_ARTIFACT])
    assert "HUMAN REVIEW REQUIRED" in escalation_text
    # Filed with the attempt that ended the run, not at the run's root.
    assert "attempt-3-cycle-1/" in kinds[ESCALATION_ARTIFACT]


@pytest.mark.asyncio
async def test_a_run_that_continues_earlier_work_does_not_reuse_its_attempt_number(
    session: Session,
    task: Task,
    loop_settings: Settings,
):
    """A run may be opened at attempt 2 -- a retry of work already tried once.
    Its turns have to continue that numbering, or the first two would share an
    artifact directory and the second would overwrite the first."""
    run = create_run(session, task.id, attempt_number=2)
    workspace = prepare_workspace(session, run.id, settings=loop_settings)
    coder = ScriptedModel(_code(BROKEN), _code(BROKEN))

    result = await run_fix_loop(
        session, workspace, coder=coder, reviewer=reviewer(), settings=loop_settings
    )

    # Two attempts remained of the task's three, and they were numbered 2 and 3.
    assert [iteration.attempt for iteration in result.iterations] == [2, 3]
    assert [iteration.number for iteration in result.iterations] == [1, 2]
    directories = {
        iteration.coding.artifacts["candidate.patch"].rsplit("/", 1)[0]
        for iteration in result.iterations
    }
    assert len(directories) == 2
    assert result.outcome is LoopOutcome.ESCALATED
    assert result.failure_reason is FailureReason.RETRY_EXHAUSTED
