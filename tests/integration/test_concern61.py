"""Concern 61 regression tests.

RUN-20260927-000018 proved two related defects:

1. A model response containing multiple edits can have one edit rejected during
   CodeChangeSet parsing while other edits survive. The rejected edit became
   only a warning and disappeared from change_set.edits, so the coding attempt
   proceeded through verification and review with a partial candidate.

2. The whole-file replacement ceiling was derived from CONTEXT_MAX_ITEM_TOKENS
   (2000 tokens -> ~8750 bytes), even when the Orchestrator had supplied the
   complete 8110-byte writable file to the coder.

These tests pin the fix:

A. Parse-level rejected requested edits are represented structurally and cause
   the coding attempt to fail closed with INVALID_MODEL_RESPONSE.
B. A complete writable file gets an output allowance derived from its source
   size, not from the per-item input ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.agents.coding_agent import (
    run_coding_attempt,
)
from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.edits import (
    EDIT_SIZE_HEADROOM,
    MAX_EDIT_BYTES,
    CodeChangeSet,
    MalformedChangeSet,
    max_edit_bytes_for_context,
    per_path_edit_allowance,
)
from apps.orchestrator.domain.enums import (
    Complexity,
    FailureReason,
    ModelRole,
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
from apps.orchestrator.providers.structured import parse_structured, strip_reasoning
from apps.orchestrator.repositories import (
    ProjectRepository,
    TaskRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration


# --- a coder that answers from a script --------------------------------------


class ScriptedCoder:
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


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> Path:
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


# --- 1. Multi-edit payload: one valid + one oversized, structurally recorded -


def test_multi_edit_payload_with_one_oversized_records_the_rejection_structurally():
    """A model response with two edits, one valid and one oversized, must
    represent the oversized one as a RejectedParseEdit with path and reason."""
    huge = "x" * (MAX_EDIT_BYTES + 1)
    payload = {
        "summary": "two edits",
        "edits": [
            {"path": "src/good.ts", "operation": "create", "content": "ok\n"},
            {"path": "src/huge.ts", "operation": "create", "content": huge},
        ],
    }

    change_set = CodeChangeSet.from_payload(payload)

    assert len(change_set.edits) == 1
    assert change_set.edits[0].path == "src/good.ts"
    assert change_set.has_parse_rejections
    assert len(change_set.rejected_parse_edits) == 1
    rejection = change_set.rejected_parse_edits[0]
    assert rejection.path == "src/huge.ts"
    assert rejection.operation == "create"
    assert "over the" in rejection.reason


def test_multi_edit_payload_with_one_bad_path_records_the_rejection():
    """A model response with one valid edit and one with an absolute path."""
    payload = {
        "summary": "two edits",
        "edits": [
            {"path": "src/good.ts", "operation": "create", "content": "ok\n"},
            {"path": "/etc/passwd", "operation": "update", "content": "x"},
        ],
    }

    change_set = CodeChangeSet.from_payload(payload)

    assert len(change_set.edits) == 1
    assert change_set.has_parse_rejections
    rejection = change_set.rejected_parse_edits[0]
    assert "/etc/passwd" in (rejection.path or "")
    assert "repository-relative" in rejection.reason


# --- 2. Coding attempt with valid + rejected parse-level edit fails closed ---


@pytest.mark.asyncio
async def test_coding_attempt_with_one_valid_and_one_rejected_edit_fails_closed(
    session: Session,
    workspace: TaskWorkspace,
    git_settings: Settings,
    task_factory,
):
    """A model response with one valid edit and one that is parse-rejected
    (oversized) must fail the attempt with INVALID_MODEL_RESPONSE, not proceed
    with the valid edit as a partial candidate."""
    task = task_factory(
        external_task_id="TS-061a",
        complexity=Complexity.LOW,
    )
    created = create_run(session, task.id)
    ws = prepare_workspace(session, created.id, settings=git_settings)

    # Must exceed the default max_edit_bytes_for_context(2000) = 8750
    huge = "x" * 10_000
    coder = ScriptedCoder(
        json.dumps({
            "summary": "two edits, one oversized",
            "edits": [
                {
                    "path": "src/navigation.ts",
                    "operation": "update",
                    "content": "export const updated = 1;\n",
                },
                {
                    "path": "src/navigationTree.ts",
                    "operation": "create",
                    "content": huge,
                },
            ],
        })
    )

    attempt = await run_coding_attempt(
        session, ws, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert "src/navigationTree.ts" in attempt.feedback
    assert "over the" in attempt.feedback or "limit" in attempt.feedback
    # The valid edit must NOT have been applied as a partial candidate.
    assert attempt.changed_paths == ()
    assert attempt.application is None
    # The model was asked, so there is one call.
    assert len(coder.requests) == 1


@pytest.mark.asyncio
async def test_candidate_verification_does_not_run_after_parse_rejection(
    session: Session,
    workspace: TaskWorkspace,
    git_settings: Settings,
    task_factory,
):
    """When a parse-level edit is rejected, the attempt must not proceed to
    diff capture, scope evaluation or completion report."""
    task = task_factory(
        external_task_id="TS-061b",
        complexity=Complexity.LOW,
    )
    created = create_run(session, task.id)
    ws = prepare_workspace(session, created.id, settings=git_settings)

    # Must exceed the default max_edit_bytes_for_context(2000) = 8750
    huge = "x" * 10_000
    coder = ScriptedCoder(
        json.dumps({
            "summary": "partial",
            "edits": [
                {"path": "src/navigation.ts", "operation": "update", "content": "ok\n"},
                {"path": "src/navigationTree.ts", "operation": "create", "content": huge},
            ],
        })
    )

    attempt = await run_coding_attempt(
        session, ws, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert attempt.diff is None
    assert attempt.scope is None
    assert attempt.report is None


# --- 3. TS-106 shape: large complete writable file accepted ------------------


@pytest.mark.asyncio
async def test_ts106_shape_large_complete_writable_file_is_accepted(
    session: Session,
    repository: Path,
    task_factory,
    tmp_path: Path,
):
    """The exact TS-106 shape: an existing writable file larger than the old
    per-item input threshold (8750 bytes from CONTEXT_MAX_ITEM_TOKENS=2000),
    supplied completely to the coder, with a model replacement that is
    proportionally larger. Under the old ceiling this was rejected; under the
    new per-path allowance it is accepted."""
    source_content = "// grown by the tasks before this one\n" + "".join(
        f"export const value{index} = {index};\n" for index in range(400)
    )
    source_bytes = len(source_content.encode())
    old_ceiling = max_edit_bytes_for_context(2000)
    assert source_bytes > old_ceiling, (
        f"source {source_bytes} must exceed old ceiling {old_ceiling}"
    )

    long_file = repository / "src" / "navigation.ts"
    long_file.write_text(source_content, encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-102: grow navigation.ts")

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        context_max_item_tokens=50,
    )
    task = task_factory(
        external_task_id="TS-061d",
        complexity=Complexity.LOW,
        limits=TaskLimits(max_files_changed=3, max_diff_lines=2_000),
    )
    created = create_run(session, task.id)
    workspace = prepare_workspace(session, created.id, settings=settings)

    replacement = source_content + "// reasonable added content\n" + "".join(
        f"export const extra{index} = {index};\n" for index in range(50)
    )
    replacement_bytes = len(replacement.encode())
    small_ceiling = max_edit_bytes_for_context(50)
    assert replacement_bytes > small_ceiling, (
        f"replacement {replacement_bytes} must exceed small ceiling {small_ceiling}"
    )

    coder = ScriptedCoder(
        json.dumps({
            "summary": "extended navigation",
            "edits": [
                {
                    "path": "src/navigation.ts",
                    "operation": "update",
                    "content": replacement,
                },
            ],
        })
    )

    attempt = await run_coding_attempt(
        session, workspace, provider=coder, settings=settings
    )

    assert attempt.succeeded
    assert attempt.failure_reason is None
    assert "src/navigation.ts" in attempt.changed_paths


# --- 4. New/small file remains bounded --------------------------------------


def test_new_file_remains_bounded_by_default_ceiling():
    """A new file (not in path_max_bytes) is bounded by the default ceiling.
    When the only edit is rejected, MalformedChangeSet is raised with the
    rejection reason in the message."""
    huge = "x" * (MAX_EDIT_BYTES + 1)
    payload = {
        "summary": "s",
        "edits": [{"path": "new.ts", "operation": "create", "content": huge}],
    }

    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload(payload)

    assert "new.ts" in str(error.value)
    assert "over the" in str(error.value)


def test_small_file_with_default_ceiling_is_accepted():
    """A small new file under the default ceiling is accepted."""
    payload = {
        "summary": "s",
        "edits": [{"path": "new.ts", "operation": "create", "content": "small\n"}],
    }

    change_set = CodeChangeSet.from_payload(payload)

    assert not change_set.has_parse_rejections
    assert len(change_set.edits) == 1


# --- 5. Grossly oversized output remains rejected ----------------------------


def test_grossly_oversized_output_is_rejected_even_with_per_path_allowance():
    """Even with a per-path allowance derived from a large source file,
    grossly oversized output (beyond the outer ceiling) is rejected.
    When the only edit is rejected, MalformedChangeSet is raised."""
    source_bytes = 10_000
    allowance = per_path_edit_allowance(source_bytes, outer_ceiling=262_144)
    huge = "x" * (allowance + 1)
    payload = {
        "summary": "s",
        "edits": [
            {
                "path": "src/large.ts",
                "operation": "update",
                "content": huge,
            }
        ],
    }

    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload(
            payload,
            path_max_bytes={"src/large.ts": allowance},
        )

    assert "over the" in str(error.value)


def test_outer_ceiling_caps_the_per_path_allowance():
    """The outer ceiling (CONTEXT_MAX_FILE_BYTES) caps the per-path allowance."""
    huge_source = 1_000_000
    allowance = per_path_edit_allowance(huge_source, outer_ceiling=262_144)
    assert allowance == 262_144


# --- 6. Per-path allowance derivation ----------------------------------------


def test_per_path_allowance_is_proportional_to_source():
    """The allowance for a complete writable file is source_bytes * headroom."""
    source_bytes = 8110
    allowance = per_path_edit_allowance(source_bytes)
    assert allowance == int(source_bytes * EDIT_SIZE_HEADROOM)
    assert allowance > max_edit_bytes_for_context(2000)


def test_per_path_allowance_has_a_floor():
    """A tiny file still gets at least MAX_EDIT_BYTES."""
    allowance = per_path_edit_allowance(100)
    assert allowance >= MAX_EDIT_BYTES


def test_per_path_allowance_respects_outer_ceiling():
    """The outer ceiling caps the allowance."""
    allowance = per_path_edit_allowance(1_000_000, outer_ceiling=50_000)
    assert allowance == 50_000


# --- 7. Informational warnings are not treated as rejections -----------------


def test_delete_with_content_is_a_warning_not_a_rejection():
    """Content sent with a delete is an informational warning, not a rejection.
    The edit is still accepted; the content is just ignored."""
    payload = {
        "summary": "s",
        "edits": [
            {"path": "a.ts", "operation": "delete", "content": "stale"},
        ],
    }

    change_set = CodeChangeSet.from_payload(payload)

    assert len(change_set.edits) == 1
    assert not change_set.has_parse_rejections
    assert "content sent with a delete was ignored" in change_set.warnings[0]


def test_duplicate_path_is_a_warning_not_a_rejection():
    """Two edits to one path: the first is kept, the second is a warning."""
    payload = {
        "summary": "s",
        "edits": [
            {"path": "a.ts", "operation": "update", "content": "first\n"},
            {"path": "a.ts", "operation": "update", "content": "second\n"},
        ],
    }

    change_set = CodeChangeSet.from_payload(payload)

    assert len(change_set.edits) == 1
    assert not change_set.has_parse_rejections
    assert "edited more than once" in change_set.warnings[0]


# --- 8. Existing application-layer semantics preserved -----------------------


@pytest.mark.asyncio
async def test_application_layer_scope_refusal_still_fails_the_attempt(
    session: Session,
    workspace: TaskWorkspace,
    git_settings: Settings,
    task_factory,
):
    """An edit that parses successfully but is refused at application time
    (scope violation) still fails the attempt with SCOPE_VIOLATION, not
    INVALID_MODEL_RESPONSE. The parse-layer and application-layer are distinct."""
    task = task_factory(
        external_task_id="TS-061c",
        complexity=Complexity.LOW,
    )
    created = create_run(session, task.id)
    ws = prepare_workspace(session, created.id, settings=git_settings)

    coder = ScriptedCoder(
        json.dumps({
            "summary": "scope violation",
            "edits": [
                {
                    "path": "src/navigation.ts",
                    "operation": "update",
                    "content": "ok\n",
                },
                {
                    "path": ".env",
                    "operation": "update",
                    "content": "API_TOKEN=leaked\n",
                },
            ],
        })
    )

    attempt = await run_coding_attempt(
        session, ws, provider=coder, settings=git_settings
    )

    assert attempt.failure_reason is FailureReason.SCOPE_VIOLATION
    assert ".env" in attempt.feedback


# --- Discrimination checks ---------------------------------------------------


def test_restoring_silent_drop_must_fail_the_rejection_test():
    """If the silent `if edit is None: continue` behavior were restored, the
    multi-edit rejection test would fail because rejected_parse_edits would
    be empty. This test asserts that property directly.

    When there is at least one valid edit alongside a rejected one, the
    CodeChangeSet must carry the rejection structurally."""
    huge = "x" * (MAX_EDIT_BYTES + 1)
    payload = {
        "summary": "two edits",
        "edits": [
            {"path": "src/good.ts", "operation": "create", "content": "ok\n"},
            {"path": "src/huge.ts", "operation": "create", "content": huge},
        ],
    }

    change_set = CodeChangeSet.from_payload(payload)

    # This assertion would fail if rejections were silently dropped.
    assert change_set.has_parse_rejections, (
        "A parse-rejected edit must be represented structurally"
    )
    # And when ALL edits are rejected, MalformedChangeSet is raised with the reason.
    all_rejected = {
        "summary": "one bad edit",
        "edits": [
            {"path": "src/huge.ts", "operation": "create", "content": huge},
        ],
    }
    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload(all_rejected)
    assert "over the" in str(error.value)


def test_restoring_fixed_8750_byte_ceiling_must_fail_ts106_test():
    """If the stale fixed 8750-byte output ceiling were restored, a TS-106
    shape replacement would be rejected. This test asserts the per-path
    allowance is larger than the old ceiling for a file of TS-106 size."""
    ts106_source_bytes = 8110
    old_ceiling = max_edit_bytes_for_context(2000)
    new_allowance = per_path_edit_allowance(ts106_source_bytes)

    assert new_allowance > old_ceiling, (
        "The per-path allowance for a complete writable file must exceed the "
        "old fixed ceiling derived from CONTEXT_MAX_ITEM_TOKENS"
    )
    assert new_allowance == int(ts106_source_bytes * EDIT_SIZE_HEADROOM)
