"""The Review Agent (build.md sections 21-24, phase I).

The phase I exit condition: *fixture reviews route correctly.* The first
three tests are that sentence, once per decision -- approve, request changes,
ask for a human -- and each checks the whole consequence: the task's state,
the persisted review and its issues, the events, and the artifacts a later
reader would need.

The rest are the ways a reviewer can be wrong or unavailable, and what
happens instead of a verdict being invented.

The reviewer is a stub that answers with text rather than an object, for the
same reason the coding agent's stub does: the JSON has to survive a reasoning
block and a Markdown fence exactly as it does from a real endpoint, so the
parsing path under test is the real one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.agents.review_agent import (
    ESCALATION_ARTIFACT,
    REVIEW_ARTIFACT,
    REVIEW_PACKAGE_MANIFEST_ARTIFACT,
    REVIEW_PROMPT_ARTIFACT,
    REVIEW_RESPONSE_ARTIFACT,
    run_review,
)
from apps.orchestrator.agents.review_prompts import (
    REVIEWER_SYSTEM_PROMPT,
    render_review_instructions,
)
from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.completion import CompletionReport
from apps.orchestrator.domain.enums import (
    EscalationStatus,
    FailureReason,
    IssueSeverity,
    ModelPurpose,
    ModelRole,
    ReviewDecision,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.domain.redaction import PLACEHOLDER
from apps.orchestrator.domain.review_package import REVIEW_PACKAGE_ARTIFACT
from apps.orchestrator.providers import (
    ConnectionReport,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    TokenUsage,
)
from apps.orchestrator.providers.errors import InvalidModelResponse, ModelUnavailable
from apps.orchestrator.providers.review import (
    ModelReviewProvider,
    ReviewerUnavailable,
)
from apps.orchestrator.providers.structured import parse_structured, strip_reasoning
from apps.orchestrator.repositories import (
    EscalationRepository,
    ModelRunRepository,
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


# --- a reviewer that answers from a script -----------------------------------


class ScriptedReviewer:
    """A ``ModelProvider`` that replays prepared reviews, or raises."""

    def __init__(self, *answers: str | Exception) -> None:
        self.config = ProviderConfig(
            provider_id="scripted-reviewer",
            base_url="http://stub/v1",
            model_name="reviewer-test",
            role=ModelRole.REVIEWER,
            context_window=65536,
        )
        self.answers: list[str | Exception] = list(answers)
        self.requests: list[ModelRequest] = []
        self.finish_reason = "stop"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        assert self.answers, "the reviewer was asked for more answers than the test scripted"
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = strip_reasoning(answer)
        data = (
            parse_structured(text, request.schema.schema, schema_name=request.schema.name)
            if request.schema
            else None
        )
        return ModelResponse(
            text=text,
            raw_text=answer,
            model_name=self.config.model_name,
            provider_id=self.config.provider_id,
            data=data,
            finish_reason=self.finish_reason,
            usage=TokenUsage(input_tokens=4000, output_tokens=300),
            duration_ms=120,
        )

    async def check_connection(self) -> ConnectionReport:
        return ConnectionReport(provider_id=self.config.provider_id, reachable=True)

    async def aclose(self) -> None:
        return None


def reviewer(*answers: str | Exception) -> ModelReviewProvider:
    return ModelReviewProvider(
        ScriptedReviewer(*answers),
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        instruction_renderer=render_review_instructions,
    )


def _review_answer(**overrides) -> str:
    """A review, wrapped the way a reasoning model wraps one."""
    payload = {
        "taskId": "TS-004",
        "decision": "APPROVED",
        "confidence": 0.91,
        "risk": "LOW",
        "summary": "The change renders each node and adds a test for it.",
        "issues": [],
    }
    payload.update(overrides)
    return (
        "<think>Checking each acceptance criterion against the diff.</think>\n"
        "```json\n" + json.dumps(payload) + "\n```"
    )


_BLOCKING_ISSUE = {
    "severity": "HIGH",
    "category": "requirement",
    "file": "src/navigation.ts",
    "line": 4,
    "requirementId": "TS-004-R7",
    "problem": "Saved selection is not restored.",
    "requiredFix": "Restore the stored selection after opening the document.",
}


# --- the fixture project -----------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository with a declared file, a contract to read, and one ADR."""
    repo = tmp_path / "tracestack"
    files = {
        "package.json": '{"name": "tracestack", "scripts": {"test": "vitest"}}\n',
        "src/navigation.ts": (
            "import { TreeNode } from './widgets/tree';\n"
            "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n"
        ),
        "src/widgets/tree.ts": "export interface TreeNode { id: string; }\n",
        ".ai/decisions/adr-001-navigation.md": (
            "# ADR-001: Navigation state lives in the provider\n\n"
            "Status: ACCEPTED\n\n"
            "## Decision\n\nNavigation and selection state belong to the tree "
            "provider, never to the view.\n"
        ),
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
            "title": "Restore the saved selection",
            "instructions": "Restore the stored selection after opening a document.",
            "files_to_inspect": ["src/widgets/tree.ts"],
            "files_to_modify": ["src/navigation.ts"],
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
    """A run whose candidate has been coded and verified: review's entry state."""
    created = create_run(session, task.id)
    tasks = TaskRepository(session)
    for status in (TaskStatus.CODING, TaskStatus.VERIFYING, TaskStatus.REVIEW_PENDING):
        tasks.transition(task.id, status)
    task.status = TaskStatus.REVIEW_PENDING
    return created


@pytest.fixture
def workspace(session: Session, run: TaskRun, git_settings: Settings) -> TaskWorkspace:
    prepared = prepare_workspace(session, run.id, settings=git_settings)
    candidate(prepared)
    return prepared


def candidate(workspace: TaskWorkspace, source: str | None = None) -> None:
    """Write what a coding attempt would have left in the worktree."""
    (workspace.path / "src" / "navigation.ts").write_text(
        source
        or (
            "import { TreeNode } from './widgets/tree';\n"
            "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n"
            "export function restoreSelection(id: string) { return id; }\n"
        ),
        encoding="utf-8",
    )


def _events(session: Session, run: TaskRun) -> list[RunEventType]:
    return [event.event_type for event in RunEventRepository(session).list_for_run(run.id)]


def _status(session: Session, task: Task) -> TaskStatus:
    return TaskRepository(session).get(task.id).status


# --- the exit condition: each decision routes correctly ----------------------


@pytest.mark.asyncio
async def test_an_approval_routes_the_task_to_approved(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    git_settings: Settings,
):
    outcome = await run_review(
        session, workspace, provider=reviewer(_review_answer()), settings=git_settings
    )

    assert outcome.approved
    assert outcome.routing.task_status is TaskStatus.APPROVED
    assert _status(session, task) is TaskStatus.APPROVED
    assert outcome.feedback is None
    assert outcome.escalation is None

    stored = ReviewRepository(session).list_for_run(run.id)
    assert [review.decision for review in stored] == [ReviewDecision.APPROVED]
    assert stored[0].cycle == 1
    assert stored[0].reviewer_model == "reviewer-test"
    assert RunEventType.APPROVED in _events(session, run)


@pytest.mark.asyncio
async def test_a_change_request_routes_back_to_the_coder_with_actionable_findings(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    git_settings: Settings,
):
    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(
            _review_answer(
                decision="CHANGES_REQUESTED",
                summary="Misses selection restoration.",
                issues=[_BLOCKING_ISSUE],
            )
        ),
        settings=git_settings,
    )

    assert outcome.routing.needs_fix
    assert outcome.routing.failure_reason is FailureReason.REVIEW_CHANGES_REQUESTED
    assert _status(session, task) is TaskStatus.CHANGES_REQUESTED
    # The correction prompt carries the finding, its location and its fix.
    assert "Saved selection is not restored." in outcome.feedback
    assert "src/navigation.ts:4" in outcome.feedback
    assert "TS-004-R7" in outcome.feedback

    (stored,) = ReviewRepository(session).list_for_run(run.id)
    (issue,) = stored.issues
    assert issue.severity is IssueSeverity.HIGH
    assert issue.is_blocking
    assert not issue.resolved
    assert RunEventType.CHANGES_REQUESTED in _events(session, run)


@pytest.mark.asyncio
async def test_a_human_review_request_stops_and_leaves_an_escalation(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    git_settings: Settings,
):
    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(
            _review_answer(
                decision="HUMAN_REVIEW_REQUIRED",
                summary="The task does not say which selection to restore.",
            )
        ),
        settings=git_settings,
    )

    assert outcome.routing.needs_human
    assert _status(session, task) is TaskStatus.HUMAN_REVIEW
    assert RunEventType.HUMAN_REVIEW_REQUIRED in _events(session, run)

    escalation = outcome.escalation
    assert escalation is not None
    assert escalation.status is EscalationStatus.OPEN
    assert escalation.options
    # Section 24: the human should not have to reconstruct the history.
    assert "TASK TS-004 — HUMAN REVIEW REQUIRED" in escalation.summary
    assert "which selection to restore" in escalation.summary
    assert workspace.starting_commit in escalation.summary
    assert EscalationRepository(session).list_open(task_id=task.id)


# --- the reviewer cannot be taken at its word --------------------------------


@pytest.mark.asyncio
async def test_an_approval_that_lists_a_blocking_issue_does_not_approve(
    session: Session, workspace: TaskWorkspace, task: Task, git_settings: Settings
):
    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(_review_answer(decision="APPROVED", issues=[_BLOCKING_ISSUE])),
        settings=git_settings,
    )

    assert outcome.routing.needs_fix
    assert _status(session, task) is TaskStatus.CHANGES_REQUESTED
    assert any("approved while reporting" in warning for warning in outcome.result.warnings)


@pytest.mark.asyncio
async def test_a_low_confidence_approval_goes_to_a_human_instead(
    session: Session, workspace: TaskWorkspace, task: Task, git_settings: Settings
):
    # Confidence is self-reported, so it is opt-in policy rather than a safe
    # default. Exercise the explicit policy here.
    git_settings.review_min_confidence = 0.6
    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(_review_answer(confidence=0.2)),
        settings=git_settings,
    )

    assert outcome.routing.needs_human
    assert outcome.routing.reviewer_decision is ReviewDecision.APPROVED
    assert outcome.routing.escalated_by_policy
    assert _status(session, task) is TaskStatus.HUMAN_REVIEW


@pytest.mark.asyncio
async def test_an_approval_touching_a_high_risk_area_is_not_accepted_automatically(
    session: Session,
    task_factory,
    git_settings: Settings,
):
    """Section 37: authentication is never accepted without a human."""
    task = task_factory(
        external_task_id="TS-010",
        title="Refresh the session token",
        files_to_modify=["src/auth/session.ts"],
    )
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=git_settings)
    tasks = TaskRepository(session)
    for status in (TaskStatus.CODING, TaskStatus.VERIFYING, TaskStatus.REVIEW_PENDING):
        tasks.transition(task.id, status)
    (workspace.path / "src" / "auth").mkdir(parents=True, exist_ok=True)
    (workspace.path / "src" / "auth" / "session.ts").write_text(
        "export function refresh() { return true; }\n", encoding="utf-8"
    )

    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(_review_answer(taskId="TS-010", confidence=1.0)),
        settings=git_settings,
    )

    assert outcome.routing.needs_human
    assert any("security" in reason for reason in outcome.routing.human_review_reasons)
    assert _status(session, task) is TaskStatus.HUMAN_REVIEW


@pytest.mark.asyncio
async def test_the_last_review_cycle_escalates_rather_than_looping(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    git_settings: Settings,
):
    """Section 23: after the limit, create a human escalation. Never loop."""
    TaskRunRepository(session).update_fields(run.id, review_cycle=2)

    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(
            _review_answer(decision="CHANGES_REQUESTED", issues=[_BLOCKING_ISSUE])
        ),
        settings=git_settings,
    )

    assert outcome.cycle == 3
    assert outcome.routing.retry_exhausted
    assert outcome.routing.failure_reason is FailureReason.RETRY_EXHAUSTED
    assert _status(session, task) is TaskStatus.HUMAN_REVIEW
    assert "review budget is spent" in outcome.escalation.summary


@pytest.mark.asyncio
async def test_an_unreachable_reviewer_is_not_a_verdict(
    session: Session, workspace: TaskWorkspace, task: Task, run: TaskRun, git_settings: Settings
):
    """A verified candidate with no review is a state the workflow handles,
    not one to paper over with a default decision."""
    with pytest.raises(ReviewerUnavailable):
        await run_review(
            session,
            workspace,
            provider=reviewer(ModelUnavailable("connection refused")),
            settings=git_settings,
        )

    assert not ReviewRepository(session).list_for_run(run.id)
    assert _status(session, task) is TaskStatus.REVIEWING
    # The failed call is still counted (section 35).
    (recorded,) = ModelRunRepository(session).list_for_run(run.id)
    assert recorded.purpose is ModelPurpose.REVIEW
    assert recorded.status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_review_is_rejected(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    with pytest.raises(InvalidModelResponse):
        await run_review(
            session,
            workspace,
            provider=reviewer('{"summary": "seems fine to me", "issues": []}'),
            settings=git_settings,
        )
    assert not ReviewRepository(session).list_for_run(run.id)


@pytest.mark.asyncio
async def test_a_truncated_review_is_discarded_rather_than_salvaged(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    """The issues list may have been cut mid-way, so an "approval" could be
    the findings not having been reached yet."""
    provider = reviewer(_review_answer())
    provider._provider.finish_reason = "length"  # noqa: SLF001 - stubbing the endpoint

    with pytest.raises(InvalidModelResponse, match="incomplete"):
        await run_review(session, workspace, provider=provider, settings=git_settings)


# --- the record the review leaves --------------------------------------------


@pytest.mark.asyncio
async def test_every_artifact_of_the_review_is_recorded(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    outcome = await run_review(
        session,
        workspace,
        provider=reviewer(_review_answer(decision="HUMAN_REVIEW_REQUIRED")),
        settings=git_settings,
    )

    for name in (
        REVIEW_PACKAGE_ARTIFACT,
        REVIEW_PACKAGE_MANIFEST_ARTIFACT,
        REVIEW_PROMPT_ARTIFACT,
        REVIEW_RESPONSE_ARTIFACT,
        REVIEW_ARTIFACT,
        ESCALATION_ARTIFACT,
    ):
        assert name in outcome.artifacts
        assert (git_settings.artifact_root / outcome.artifacts[name]).exists()

    stored = json.loads(
        (git_settings.artifact_root / outcome.artifacts[REVIEW_ARTIFACT]).read_text()
    )
    assert stored["decision"] == ReviewDecision.HUMAN_REVIEW_REQUIRED.value
    assert stored["package_hash"] == outcome.package.content_hash
    assert stored["routing"]["task_status"] == TaskStatus.HUMAN_REVIEW.value


@pytest.mark.asyncio
async def test_the_package_holds_what_section_21_asks_for_and_not_the_repository(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    provider = reviewer(_review_answer())
    outcome = await run_review(session, workspace, provider=provider, settings=git_settings)
    rendered = outcome.package.render()

    # What section 21 lists.
    assert "TS-004" in rendered
    assert workspace.starting_commit in rendered
    assert "restoreSelection" in rendered  # the candidate diff
    assert "src/navigation.ts" in rendered  # the changed-file list
    assert "ADR-001" in rendered  # the applicable architecture decision
    assert "src/widgets/tree.ts" in rendered  # source needed to read the diff

    # What it excludes: a file that is neither changed, declared nor imported.
    assert "package.json" not in rendered


@pytest.mark.asyncio
async def test_the_successful_call_is_counted_against_the_reviewer_model(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    await run_review(
        session, workspace, provider=reviewer(_review_answer()), settings=git_settings
    )

    (recorded,) = ModelRunRepository(session).list_for_run(run.id)
    assert recorded.purpose is ModelPurpose.REVIEW
    assert recorded.status is RunStatus.SUCCEEDED
    assert recorded.input_tokens == 4000
    assert recorded.prompt_artifact and recorded.response_artifact


@pytest.mark.asyncio
async def test_the_reviewer_is_told_what_is_a_fact_and_what_is_a_claim(
    session: Session, workspace: TaskWorkspace, git_settings: Settings
):
    provider = reviewer(_review_answer())
    await run_review(
        session,
        workspace,
        provider=provider,
        completion_report=CompletionReport(
            external_task_id="TS-004",
            attempt=1,
            summary="Restored the selection and added a test.",
            tests_claimed=("tests/nav.test.ts",),
            applied_paths=("src/navigation.ts",),
        ),
        settings=git_settings,
    )

    (request,) = provider._provider.requests  # noqa: SLF001 - inspecting the stub
    prompt = "\n".join(message.content for message in request.messages())
    assert "Restored the selection and added a test." in prompt
    assert "claims the test tests/nav.test.ts" in prompt  # the measured discrepancy
    assert "These are claims, not facts." in prompt


@pytest.mark.asyncio
async def test_a_re_review_is_shown_the_previous_cycles_open_findings(
    session: Session,
    workspace: TaskWorkspace,
    task: Task,
    run: TaskRun,
    git_settings: Settings,
):
    first = reviewer(
        _review_answer(decision="CHANGES_REQUESTED", issues=[_BLOCKING_ISSUE])
    )
    await run_review(session, workspace, provider=first, settings=git_settings)

    TaskRepository(session).transition(task.id, TaskStatus.CODING)
    candidate(
        workspace,
        "import { TreeNode } from './widgets/tree';\n"
        "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n"
        "export function restoreSelection(id: string) { return open(id); }\n",
    )
    TaskRepository(session).transition(task.id, TaskStatus.VERIFYING)
    TaskRepository(session).transition(task.id, TaskStatus.REVIEW_PENDING)

    second = reviewer(_review_answer(summary="The selection is now restored."))
    outcome = await run_review(session, workspace, provider=second, settings=git_settings)

    assert outcome.cycle == 2
    rendered = outcome.package.render()
    assert "earlier review cycles" in rendered
    assert "Saved selection is not restored." in rendered
    assert "This is a re-review." in "\n".join(
        message.content for message in second._provider.requests[0].messages()  # noqa: SLF001
    )
    assert outcome.approved


@pytest.mark.asyncio
async def test_an_unreachable_reviewer_does_not_spend_a_review_cycle(
    session: Session, workspace: TaskWorkspace, run: TaskRun, git_settings: Settings
):
    """Section 23 counts reviews, not attempts to reach a reviewer. Three
    outages must not exhaust a task's budget before anyone has read the
    change."""
    with pytest.raises(ReviewerUnavailable):
        await run_review(
            session,
            workspace,
            provider=reviewer(ModelUnavailable("connection refused")),
            settings=git_settings,
        )

    assert TaskRunRepository(session).get(run.id).review_cycle == 0

    outcome = await run_review(
        session, workspace, provider=reviewer(_review_answer()), settings=git_settings
    )
    assert outcome.cycle == 1
    assert TaskRunRepository(session).get(run.id).review_cycle == 1


# --- redaction (build.md section 36, concern 28) ------------------------------


@pytest.fixture
def remote_reviewer_settings(tmp_path: Path) -> Settings:
    """Settings whose reviewer is not on this machine."""
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        review_base_url="https://reviewer.example.com/v1",
    )


_SECRET = "ghp_ZZfakefakefakefakefakefake123456"


@pytest.mark.asyncio
async def test_a_remote_reviewer_is_not_sent_the_raw_diff(
    session: Session,
    run: TaskRun,
    remote_reviewer_settings: Settings,
):
    """Concern 28. Worker output and log artifacts already go through
    ``Redactor``; the package carries the raw diff and the raw contents of
    supporting files to whatever ``REVIEW_BASE_URL`` points at, and did not.

    A candidate can reach a reviewer carrying a credential even though the
    security scan blocks the obvious cases: the scan reads added lines only, it
    is a heuristic, a clipped diff is reported unscanned, and ``run_review`` can
    be called with no verification report at all -- as it is here."""
    workspace = prepare_workspace(session, run.id, settings=remote_reviewer_settings)
    candidate(
        workspace,
        "import { TreeNode } from './widgets/tree';\n"
        f"const token = '{_SECRET}';\n"
        "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n",
    )
    provider = reviewer(_review_answer())

    outcome = await run_review(
        session, workspace, provider=provider, settings=remote_reviewer_settings
    )

    assert outcome.package.redacted
    # Not in what was sent...
    prompt = (remote_reviewer_settings.artifact_root / outcome.artifacts[
        REVIEW_PROMPT_ARTIFACT
    ]).read_text()
    assert _SECRET not in prompt
    assert PLACEHOLDER in prompt
    # ...nor in the package as it was rendered, hashed and stored, because the
    # three have to be the same bytes for the hash to mean anything.
    stored = (remote_reviewer_settings.artifact_root / outcome.artifacts[
        REVIEW_PACKAGE_ARTIFACT
    ]).read_text()
    assert _SECRET not in stored
    assert outcome.package.content_hash == hashlib.sha256(
        outcome.package.render().encode()
    ).hexdigest()
    # And the manifest says it happened, so a masked finding is readable later
    # as a finding about a masked line.
    manifest = json.loads(
        (remote_reviewer_settings.artifact_root / outcome.artifacts[
            REVIEW_PACKAGE_MANIFEST_ARTIFACT
        ]).read_text()
    )
    assert manifest["redacted"] is True


@pytest.mark.asyncio
async def test_a_reviewer_on_this_machine_sees_the_real_diff_by_default(
    session: Session,
    workspace: TaskWorkspace,
    git_settings: Settings,
):
    """Masking costs the reviewer the ability to comment on a masked line. For a
    model on this host nothing left the machine, so the default does not pay
    that cost -- an operator who disagrees sets the flag."""
    candidate(
        workspace,
        "import { TreeNode } from './widgets/tree';\n"
        f"const token = '{_SECRET}';\n"
        "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n",
    )

    outcome = await run_review(
        session, workspace, provider=reviewer(_review_answer()), settings=git_settings
    )

    assert not outcome.package.redacted
    prompt = (git_settings.artifact_root / outcome.artifacts[REVIEW_PROMPT_ARTIFACT]).read_text()
    assert _SECRET in prompt


@pytest.mark.asyncio
async def test_redaction_can_be_forced_on_for_a_local_reviewer(
    session: Session,
    run: TaskRun,
    tmp_path: Path,
):
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        review_base_url="http://localhost:11434/v1",
        review_redact_package=True,
    )
    workspace = prepare_workspace(session, run.id, settings=settings)
    candidate(workspace, f"const token = '{_SECRET}';\n")

    outcome = await run_review(
        session, workspace, provider=reviewer(_review_answer()), settings=settings
    )

    assert outcome.package.redacted
