"""The Coding Agent (build.md section 14, phase G).

The phase G exit condition: *a fixture task can cause an isolated worktree
change without touching forbidden paths.* The first test is that sentence. The
rest are the ways it could be satisfied dishonestly -- a plan that proposes a
forbidden path, an edit that reaches one anyway, an answer that claims work it
did not do -- and what happens instead.

The coder is a stub, and it returns text rather than an object on purpose: the
JSON has to survive a reasoning block and a Markdown fence exactly as it does
from a real local endpoint, so the parsing path under test is the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.agents.coding_agent import (
    CANDIDATE_PATCH_ARTIFACT,
    CODER_PROMPT_ARTIFACT,
    CODER_RESPONSE_ARTIFACT,
    CODING_PROMPT_CONTRACT,
    PLAN_ARTIFACT,
    run_coding_attempt,
)
from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.completion import COMPLETION_REPORT_ARTIFACT
from apps.orchestrator.domain.enums import (
    Complexity,
    FailureReason,
    ModelPurpose,
    ModelRole,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.providers import (
    ConnectionReport,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    TokenUsage,
)
from apps.orchestrator.providers.errors import ModelUnavailable
from apps.orchestrator.providers.structured import parse_structured, strip_reasoning
from apps.orchestrator.repositories import (
    ModelRepository,
    ModelRunRepository,
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration


# --- a coder that answers from a script --------------------------------------


class ScriptedCoder:
    """A ``ModelProvider`` that replays prepared answers.

    It parses its own text the way ``OpenAICompatibleProvider`` does, so an
    answer wrapped in a reasoning block or a Markdown fence is exercised here
    rather than assumed to work.
    """

    def __init__(self, *answers: str) -> None:
        self.config = ProviderConfig(
            provider_id="scripted-coder",
            base_url="http://stub/v1",
            model_name="qwen-coder-test",
            role=ModelRole.CODER,
            context_window=32768,
        )
        self.answers = list(answers)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        assert self.answers, "the coder was asked for more answers than the test scripted"
        raw = self.answers.pop(0)
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
            usage=TokenUsage(input_tokens=1000, output_tokens=200),
            duration_ms=42,
        )

    async def check_connection(self) -> ConnectionReport:
        return ConnectionReport(provider_id=self.config.provider_id, reachable=True)

    async def aclose(self) -> None:
        return None


def _plan_answer(**overrides) -> str:
    """A plan, wrapped the way a Qwen-class model wraps one."""
    payload = {
        "filesToInspect": ["src/widgets/tree.ts"],
        "filesToModify": ["src/navigation.ts"],
        "filesToCreate": ["src/navigationTree.ts"],
        "approach": ["Read the tree widget", "Render each node", "Add a test"],
        "risks": ["deep nesting"],
        "expectedTests": ["tests/navigation.test.ts"],
    }
    payload.update(overrides)
    return "<think>The task names two files.</think>\n```json\n" + json.dumps(payload) + "\n```"


def _code_answer(**overrides) -> str:
    payload = {
        "summary": "Rendered the navigation tree from the tree widget.",
        "edits": [
            {
                "path": "src/navigation.ts",
                "operation": "update",
                "content": (
                    "import { renderTree } from './navigationTree';\n"
                    "export function navigation(nodes) { return renderTree(nodes); }\n"
                ),
            },
            {
                "path": "src/navigationTree.ts",
                "operation": "create",
                "content": "export const renderTree = (nodes) => nodes.length;\n",
            },
        ],
        "requirementsMet": ["each node of the navigation tree is rendered"],
        "testsAdded": [],
        "followUps": [],
        "deviationsFromPlan": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- the fixture project -----------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository with a declared file, a read-only file and a secret."""
    repo = tmp_path / "tracestack"
    (repo / "src" / "widgets").mkdir(parents=True)
    files = {
        "package.json": '{"name": "tracestack", "scripts": {"test": "vitest"}}\n',
        "src/navigation.ts": (
            "import { TreeNode } from './widgets/tree';\n"
            "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n"
        ),
        "src/widgets/tree.ts": "export interface TreeNode { id: string; }\n",
        ".env": "API_TOKEN=do-not-touch\n",
    }
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "TS-001: scaffold")
    return repo


@pytest.fixture
def project(session: Session, repository: Path) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            repository_path=str(repository),
            default_branch="main",
            protected_paths=[".env", "secrets/**"],
        )
    )


@pytest.fixture
def task_factory(session: Session, project: Project):
    def make(**overrides) -> Task:
        fields: dict[str, object] = {
            "project_id": project.id,
            "external_task_id": "TS-004",
            "title": "Implement navigation tree",
            "instructions": "Render each node of the navigation tree in the sidebar.",
            "complexity": Complexity.MEDIUM,
            "verify_commands": ["npm test"],
            "files_to_inspect": ["src/widgets/tree.ts"],
            "files_to_modify": ["src/navigation.ts"],
            "files_to_create": ["src/navigationTree.ts"],
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
def workspace(session: Session, run: TaskRun, git_settings: Settings) -> TaskWorkspace:
    return prepare_workspace(session, run.id, settings=git_settings)


def _events(session: Session, run: TaskRun) -> list[RunEventType]:
    return [event.event_type for event in RunEventRepository(session).list_for_run(run.id)]


# --- the exit condition ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fixture_task_changes_the_worktree_without_touching_forbidden_paths(
    session: Session,
    workspace: TaskWorkspace,
    repository: Path,
    git_settings: Settings,
):
    """Phase G's exit condition, in one test."""
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.succeeded
    assert attempt.failure_reason is None
    # The declared files changed, in the worktree.
    assert set(attempt.changed_paths) == {"src/navigation.ts", "src/navigationTree.ts"}
    assert (workspace.path / "src" / "navigationTree.ts").is_file()
    assert "renderTree" in (workspace.path / "src" / "navigation.ts").read_text()
    # Nothing forbidden was touched, and the diff says so.
    assert ".env" not in attempt.diff.summary.paths
    assert (workspace.path / ".env").read_text() == "API_TOKEN=do-not-touch\n"
    # The managed repository never moved: it is still on its own commit with a
    # clean tree, and the change lives on the run's branch alone.
    assert (repository / "src" / "navigationTree.ts").exists() is False
    assert workspace.repository.is_clean()
    assert workspace.repository.get_current_branch() == "main"


@pytest.mark.asyncio
async def test_the_candidate_is_measured_and_reported_but_not_judged_correct(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.scope.files_changed == 2
    assert attempt.scope.diff_lines > 0
    assert attempt.report.usable
    # The report separates what the coder said from what was measured.
    described = attempt.report.describe()
    assert described["claimed"]["requirements_met"]
    assert described["measured"]["applied_paths"] == [
        "src/navigation.ts",
        "src/navigationTree.ts",
    ]


@pytest.mark.asyncio
async def test_every_artifact_of_the_attempt_is_recorded(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    """Section 9: the run must be reconstructable. Prompt, answer, plan, patch
    and completion report all land in the run directory."""
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    expected = {
        PLAN_ARTIFACT,
        CODER_PROMPT_ARTIFACT,
        CODER_RESPONSE_ARTIFACT,
        CANDIDATE_PATCH_ARTIFACT,
        COMPLETION_REPORT_ARTIFACT,
    }
    assert expected <= set(attempt.artifacts)
    for name in expected:
        assert (git_settings.artifact_root / attempt.artifacts[name]).is_file()
    patch = (git_settings.artifact_root / attempt.artifacts[CANDIDATE_PATCH_ARTIFACT]).read_text()
    assert "src/navigationTree.ts" in patch


@pytest.mark.asyncio
async def test_the_workflow_events_tell_the_story_of_the_attempt(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    events = _events(session, run)
    assert RunEventType.PLAN_CREATED in events
    assert events.index(RunEventType.CODING_STARTED) < events.index(
        RunEventType.CODING_COMPLETED
    )


# --- plan mode ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_low_complexity_task_is_not_asked_to_plan(
    session: Session, task_factory, git_settings: Settings
):
    task = task_factory(external_task_id="TS-005", complexity=Complexity.LOW)
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=git_settings)
    coder = ScriptedCoder(_code_answer())

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.plan is None
    assert len(coder.requests) == 1
    assert RunEventType.PLAN_CREATED not in _events(session, run)


@pytest.mark.asyncio
async def test_a_plan_that_would_write_a_protected_path_never_reaches_the_code_step(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(_plan_answer(filesToModify=["src/navigation.ts", ".env"]))

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.SCOPE_VIOLATION
    assert len(coder.requests) == 1  # no code was ever requested
    assert attempt.change_set is None
    assert ".env" in attempt.feedback
    assert (workspace.path / ".env").read_text() == "API_TOKEN=do-not-touch\n"


@pytest.mark.asyncio
async def test_a_suspiciously_wide_plan_is_escalated_rather_than_refused(
    session: Session, task_factory, git_settings: Settings
):
    """Section 14: reject *or escalate*. A task that declared no files cannot
    say the plan is out of bounds, only that it is surprisingly wide."""
    task = task_factory(
        external_task_id="TS-006",
        complexity=Complexity.MEDIUM,
        files_to_inspect=[],
        files_to_modify=[],
        files_to_create=[],
        limits=TaskLimits(max_files_changed=20),
    )
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=git_settings)
    coder = ScriptedCoder(
        _plan_answer(
            filesToModify=[f"src/module{index}.ts" for index in range(12)],
            filesToCreate=[],
        )
    )

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.HUMAN_DECISION_REQUIRED
    assert attempt.plan_assessment.needs_human
    assert len(coder.requests) == 1


# --- the scope check on the edits themselves ---------------------------------


@pytest.mark.asyncio
async def test_an_edit_outside_the_allowance_is_refused_and_fails_the_attempt(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    """The plan promised one thing and the edits did another. Neither the
    protected file nor the undeclared one is written, and the attempt does not
    get to go on to verification as though it were in scope."""
    coder = ScriptedCoder(
        _plan_answer(),
        _code_answer(
            edits=[
                {"path": "src/navigation.ts", "operation": "update", "content": "ok\n"},
                {"path": ".env", "operation": "update", "content": "API_TOKEN=leaked\n"},
                {"path": "src/billing.ts", "operation": "create", "content": "charge()\n"},
            ]
        ),
    )

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.SCOPE_VIOLATION
    assert (workspace.path / ".env").read_text() == "API_TOKEN=do-not-touch\n"
    assert not (workspace.path / "src" / "billing.ts").exists()
    assert ".env" in attempt.feedback
    # The refusals are on the record, not only in the log.
    assert {edit.path for edit in attempt.report.rejected_edits} == {".env", "src/billing.ts"}


@pytest.mark.asyncio
async def test_a_file_declared_for_inspection_only_is_not_writable(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(
        _plan_answer(),
        _code_answer(
            edits=[
                {
                    "path": "src/widgets/tree.ts",
                    "operation": "update",
                    "content": "export interface TreeNode { id: number; }\n",
                }
            ]
        ),
    )

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.SCOPE_VIOLATION
    assert (
        workspace.path / "src" / "widgets" / "tree.ts"
    ).read_text() == "export interface TreeNode { id: string; }\n"


# --- answers that cannot be used --------------------------------------------


@pytest.mark.asyncio
async def test_an_unusable_answer_becomes_feedback_rather_than_an_exception(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(
        _plan_answer(),
        json.dumps({"summary": "I could not decide what to change.", "edits": []}),
    )

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert "no usable edits" in attempt.feedback
    assert workspace.git.is_clean()


@pytest.mark.asyncio
async def test_an_update_to_a_file_that_does_not_exist_is_told_to_the_coder(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(
        _plan_answer(),
        _code_answer(
            edits=[
                {
                    "path": "src/navigationTree.ts",
                    "operation": "update",
                    "content": "export const renderTree = () => 0;\n",
                }
            ]
        ),
    )

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert "create" in attempt.feedback
    assert not (workspace.path / "src" / "navigationTree.ts").exists()


# --- what the coder is actually sent ----------------------------------------


@pytest.mark.asyncio
async def test_the_coder_is_sent_the_task_the_context_and_the_approved_plan(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    coding_request = coder.requests[1]
    prompt = "\n".join(message.content for message in coding_request.messages())
    assert "Task: TS-004" in prompt  # the task specification, from the context
    assert "src/widgets/tree.ts" in prompt  # the declared file it may read
    assert "Approved plan" in prompt
    assert "never run commands" in prompt  # the system rules
    assert "API_TOKEN" not in prompt  # .env is not context


@pytest.mark.asyncio
async def test_review_feedback_reaches_a_fix_attempt_unchanged(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(
        session,
        workspace,
        provider=coder,
        settings=git_settings,
        review_feedback="The tree must not render a node twice.",
    )

    prompt = "\n".join(message.content for message in coder.requests[1].messages())
    assert "The tree must not render a node twice." in prompt


@pytest.mark.asyncio
async def test_the_run_records_the_prompt_contract_it_was_served_under(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    """Section 34: an outcome must be attributable to the prompt and schema that
    produced it, not only to the model."""
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    stored = TaskRunRepository(session).get(run.id)
    assert stored.prompt_version == CODING_PROMPT_CONTRACT
    assert stored.status is RunStatus.RUNNING
    assert stored.context_hash


# --- what each call cost (sections 34 and 35, concern 3) ---------------------


@pytest.mark.asyncio
async def test_every_model_call_leaves_a_model_run_row(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    """A call that is not recorded here did not happen as far as training
    capture and model evaluation are concerned."""
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)
    calls = ModelRunRepository(session).list_for_run(run.id)

    assert [call.purpose for call in calls] == [ModelPurpose.PLAN, ModelPurpose.CODE]
    for call in calls:
        assert call.status is RunStatus.SUCCEEDED
        assert call.input_tokens == 1000
        assert call.output_tokens == 200
        assert call.duration_ms == 42
        assert call.prompt_artifact and call.response_artifact


@pytest.mark.asyncio
async def test_a_provider_configured_from_the_environment_gets_a_models_row(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    """`model_runs.model_id` is non-nullable, and the default coder has no row
    until one is made for it -- concern 3."""
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    stored = TaskRunRepository(session).get(run.id)
    model = ModelRepository(session).get(stored.coder_model_id)
    assert model.model_name == "qwen-coder-test"
    assert model.role is ModelRole.CODER
    assert model.metadata["source"] == "environment"
    # One row, however many calls it served.
    assert len(ModelRepository(session).list()) == 1


@pytest.mark.asyncio
async def test_a_fix_attempt_is_recorded_as_a_fix_rather_than_as_new_code(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    """Section 35 compares first attempts with corrections, which only works
    if the two are distinguishable in the table."""
    coder = ScriptedCoder(_plan_answer(), _code_answer())

    await run_coding_attempt(
        session,
        workspace,
        provider=coder,
        settings=git_settings,
        review_feedback="The tree does not render empty children.",
    )
    purposes = [call.purpose for call in ModelRunRepository(session).list_for_run(run.id)]

    assert ModelPurpose.FIX in purposes
    assert ModelPurpose.CODE not in purposes


@pytest.mark.asyncio
async def test_a_call_that_fails_is_recorded_before_the_error_is_raised(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    """How often an endpoint fails is exactly what section 35 wants to ask,
    and a table of only the calls that worked cannot answer it."""

    class UnreachableCoder(ScriptedCoder):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            raise ModelUnavailable("the endpoint refused the connection")

    with pytest.raises(ModelUnavailable):
        await run_coding_attempt(
            session, workspace, provider=UnreachableCoder(), settings=git_settings
        )

    calls = ModelRunRepository(session).list_for_run(run.id)
    assert [call.status for call in calls] == [RunStatus.FAILED]
    assert calls[0].prompt_artifact
    assert calls[0].response_artifact is None


@pytest.mark.asyncio
async def test_a_clipped_writable_file_refuses_the_attempt_before_any_model_call(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """Concern 1, closed, and still closed after concerns 55 and 56.

    The edit contract asks for the complete new contents of every file the coder
    changes. For a writable file it has only seen the start of, that cannot be
    met -- it would return the part it read and the rest would land in the diff
    as a deletion. So the attempt is refused, and refused *before* a model is
    asked: no feedback the coder could act on would change the outcome.

    What changed with concern 56 is when this arises. A writable file is now
    exempt from the per-item cap, so reaching this guard takes a *total* budget
    that cannot hold the file -- which is the only case where the refusal was
    ever the right answer. The guard itself is untouched, and is the defence in
    depth behind the budgeting: if the reservation is ever wrong, an incomplete
    writable file still never reaches a model."""
    long_file = repository / "src" / "navigation.ts"
    long_file.write_text(
        "// a file larger than the whole context budget\n"
        + "".join(f"export const value{index} = {index};\n" for index in range(4_000)),
        encoding="utf-8",
    )
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-002: grow navigation.ts")

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_item_tokens=50,
        # No room for the complete file, so it cannot be supplied whole and
        # cannot be asked for whole either.
        context_max_tokens=3_000,
    )
    task = task_factory()
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)
    coder = ScriptedCoder()  # scripted with no answers: asking one would fail

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=settings
    )

    assert not attempt.succeeded
    assert attempt.failure_reason is FailureReason.HUMAN_DECISION_REQUIRED
    assert coder.requests == []
    assert "src/navigation.ts" in attempt.feedback
    # The remedy named is the one that would actually work: the per-item cap no
    # longer applies to a writable file, so raising it would change nothing.
    assert "CONTEXT_MAX_TOKENS" in attempt.feedback
    # Nothing was written, so there is nothing to undo.
    assert attempt.changed_paths == ()
    assert ModelRunRepository(session).list_for_run(created.id) == []


@pytest.mark.asyncio
async def test_a_clipped_file_the_task_only_reads_does_not_refuse_the_attempt(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """The refusal is about files the coder must reproduce whole. A long file it
    was only asked to read is what clipping is *for*."""
    long_file = repository / "src" / "widgets" / "tree.ts"
    long_file.write_text(
        "".join(f"export interface Node{index} {{ id: string; }}\n" for index in range(400)),
        encoding="utf-8",
    )
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-002: grow tree.ts")

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_item_tokens=50,
    )
    task = task_factory(complexity=Complexity.LOW)
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)

    attempt = await run_coding_attempt(
        session, workspace, provider=ScriptedCoder(_code_answer()), settings=settings
    )

    assert attempt.failure_reason is not FailureReason.HUMAN_DECISION_REQUIRED
    assert attempt.succeeded


@pytest.mark.asyncio
async def test_a_writable_file_over_the_per_item_cap_is_coded_not_refused(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """Concern 56. The same file that used to refuse the attempt is now coded.

    This is the TS-106 shape: a writable file past CONTEXT_MAX_ITEM_TOKENS with
    most of the total budget unspent. Before, the per-item cap clipped it and
    the guard above refused -- a task that was entirely possible became
    impossible because an earlier task in the chain had made the file longer.
    Now the complete file is supplied, and the coder is asked.
    """
    long_file = repository / "src" / "navigation.ts"
    body = "// grown by the tasks before this one\n" + "".join(
        f"export const value{index} = {index};\n" for index in range(400)
    )
    long_file.write_text(body, encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-002: grow navigation.ts")

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_item_tokens=50,
    )
    # Replacing a 400-line file is a large diff by construction; the size
    # ceiling is not what this test is about.
    task = task_factory(
        complexity=Complexity.LOW,
        limits=TaskLimits(max_files_changed=3, max_diff_lines=2_000),
    )
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)
    coder = ScriptedCoder(_code_answer())

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=settings
    )

    assert attempt.succeeded
    assert attempt.failure_reason is not FailureReason.HUMAN_DECISION_REQUIRED
    # The point of the exemption: the coder saw every line it was asked to
    # replace, so "complete new contents" is a question it can answer.
    assert len(coder.requests) == 1
    context = coder.requests[0].context or ""
    assert body in context
    # Only the writable file is exempt: the section for it carries no marker,
    # while the supporting files this tiny per-item cap clips still do.
    section = context.split("## src/navigation.ts (declared: may modify)")[1]
    assert "truncated by orchestrator" not in section.split("\n## ")[0]
    assert "truncated by orchestrator" in context


# --- writable files that never arrive at all (concern 58) --------------------
#
# Concern 56 made a writable file exempt from the per-item cap. That closed the
# case where the file was clipped, and left the case where it was never a
# candidate: over CONTEXT_MAX_FILE_BYTES, binary, or filtered out of selection.
# Those leave no truncated item behind, so a guard that reasons from truncation
# sees nothing and the coder is asked to replace a file it has never read.


@pytest.mark.asyncio
async def test_a_writable_file_over_the_byte_cap_refuses_the_attempt(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """The file is not clipped here -- it is absent. Nothing in the package
    says so on its own, which is the whole point of recording it."""
    oversized = repository / "src" / "navigation.ts"
    oversized.write_text(
        "".join(f"export const value{index} = {index};\n" for index in range(2_000)),
        encoding="utf-8",
    )
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-002: grow navigation.ts past the cap")

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_file_bytes=1_024,
    )
    task = task_factory()
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)
    coder = ScriptedCoder()  # no answers: being asked at all would fail here

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=settings
    )

    assert not attempt.succeeded
    assert attempt.failure_reason is FailureReason.HUMAN_DECISION_REQUIRED
    # Requirement: the model is not called when the required source is missing.
    assert coder.requests == []
    assert ModelRunRepository(session).list_for_run(created.id) == []
    assert "src/navigation.ts" in attempt.feedback
    assert "CONTEXT_MAX_FILE_BYTES" in attempt.feedback
    assert attempt.changed_paths == ()


@pytest.mark.asyncio
async def test_an_oversized_file_the_task_only_reads_is_still_just_omitted(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """Requirement: ordinary exclusion policy is unchanged for everything the
    coder is not going to reproduce."""
    oversized = repository / "src" / "widgets" / "tree.ts"
    oversized.write_text(
        "".join(f"export interface Node{index} {{ id: string; }}\n" for index in range(2_000)),
        encoding="utf-8",
    )
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-002: grow tree.ts past the cap")

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_file_bytes=1_024,
    )
    task = task_factory(complexity=Complexity.LOW)
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)

    attempt = await run_coding_attempt(
        session, workspace, provider=ScriptedCoder(_code_answer()), settings=settings
    )

    assert attempt.succeeded
    assert attempt.failure_reason is not FailureReason.HUMAN_DECISION_REQUIRED


@pytest.mark.asyncio
async def test_an_ordinary_writable_file_still_proceeds(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """The guard has to stay quiet on the ordinary case, or it is just a brake."""
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
    )
    task = task_factory(complexity=Complexity.LOW)
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)
    coder = ScriptedCoder(_code_answer())

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=settings
    )

    assert attempt.succeeded
    assert len(coder.requests) == 1
    assert "src/navigation.ts" in attempt.changed_paths
