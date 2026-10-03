"""Concern 70 regression tests: bounded targeted edits.

RUN-000007 exposed a structural weakness in ``code-edits/2``. The contract had
one representation -- the complete new contents of every file the coder
changes -- so the model's *output* was forced to be the size of the *baseline*
it was editing:

* attempt 1 returned a faithful 15,106-byte replacement of an 11,813-byte test
  file against a 14,766-byte allowance. Two hundred and thirty-four percent of
  nothing: the change was about seven lines, the other eleven thousand bytes
  were unchanged source the model had to spend output allowance reproducing.
* attempt 2 was told to fit the limit, and fitted it by deleting 33 existing
  tests. The resulting diff was 413 lines against ``max_diff_lines=150``.

Both refusals were correct. The parser, the allowance and the scope guard all
did exactly their jobs; the representation was wrong.

These tests pin a fourth operation, ``replace``: ``oldText`` must occur exactly
once in the file as it stands and is spliced out, ``newText`` is spliced in,
and everything else is preserved byte for byte. No fuzzy matching, no line
numbers, no patch program. Every downstream guard is unchanged and still
measures the resulting file and the resulting Git diff.

Numbered below against the concern's own list:

1  targeted exact replacement succeeds
2  zero-match rejection
3  multiple-match rejection
4  malformed targeted edit rejection
5  invalid / path-traversal rejection
6  disallowed task path rejection
7  multiple targeted edits apply atomically
8  one bad edit fails the whole response closed
9  targeted edit beside a whole-file edit
10 resulting max_files_changed enforcement
11 resulting max_diff_lines enforcement
12 resulting file / output safety enforcement
13 unchanged surrounding content preserved byte for byte
14 existing content cannot disappear by being omitted
15 existing whole-file replacement still supported
16 FIX prompt advertises targeted edits
17 prompt tells the model to preserve unrelated content
S  the synthetic RUN-000007 structural case
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.agents.coding_agent import run_coding_attempt
from apps.orchestrator.agents.prompts import (
    CODER_PROMPT_VERSION,
    CODER_SYSTEM_PROMPT,
    render_coding_instructions,
)
from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.edits import (
    EDIT_SCHEMA,
    EDIT_SCHEMA_VERSION,
    MAX_EDIT_BYTES,
    MAX_TARGETED_EDIT_PAYLOAD_BYTES,
    CodeChangeSet,
    EditOperation,
    FileEdit,
    MalformedChangeSet,
    per_path_edit_allowance,
)
from apps.orchestrator.domain.enums import (
    Complexity,
    FailureReason,
    ModelRole,
    ScopePolicyDecision,
    TaskStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.domain.scope import ScopePolicy
from apps.orchestrator.providers import (
    ConnectionReport,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    TokenUsage,
)
from apps.orchestrator.providers.structured import parse_structured, strip_reasoning
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services.code_edits import apply_change_set
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
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "test").mkdir()
    files = {
        "package.json": '{"name": "tracestack", "scripts": {"test": "vitest"}}\n',
        "src/navigation.ts": (
            "import { TreeNode } from './tree';\n"
            "export function navigation(nodes: TreeNode[]) { return nodes.length; }\n"
        ),
        "src/test/navigation-stack.test.ts": (
            "import { navigation } from '../navigation';\n"
            "\n"
            "test('renders an empty tree', () => {\n"
            "  expect(navigation([])).toBe(0);\n"
            "});\n"
        ),
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
            "external_task_id": "TS-070",
            "title": "Extend the navigation stack tests",
            "instructions": "Cover the stack layout for a nested tree.",
            "complexity": Complexity.LOW,
            "verify_commands": ["npm test"],
            "files_to_inspect": ["src/navigation.ts"],
            "files_to_modify": ["src/test/navigation-stack.test.ts"],
            "files_to_create": [],
            "limits": TaskLimits(max_files_changed=2, max_diff_lines=150),
        }
        fields.update(overrides)
        tasks = TaskRepository(session)
        task = tasks.add(Task(**fields))  # type: ignore[arg-type]
        return tasks.transition(task.id, TaskStatus.READY)

    return make


@pytest.fixture
def workspace_factory(session: Session, task_factory, git_settings: Settings):
    def make(**overrides) -> tuple[Task, TaskRun, TaskWorkspace]:
        task = task_factory(**overrides)
        run = create_run(session, task.id)
        return task, run, prepare_workspace(session, run.id, settings=git_settings)

    return make


# --- a worktree for the application-level tests ------------------------------


@pytest.fixture
def worktree(repository: Path) -> Path:
    """The checkout a change set is applied into, as the coding agent sees it.

    Rooted at the repository rather than at a subdirectory: the paths in these
    tests are repository-relative, and a root that made them something else
    would be testing a path-normalisation accident rather than the operation.
    """
    return repository


NAV_TEST = "src/test/navigation-stack.test.ts"
EXISTING_TEST_NAME = "renders an empty tree"


def _policy(**overrides) -> ScopePolicy:
    fields: dict[str, object] = {
        "allowed_paths": ("src/navigation.ts", NAV_TEST, "src/new.ts"),
        "max_files_changed": 2,
        "max_diff_lines": 150,
    }
    fields.update(overrides)
    return ScopePolicy(**fields)  # type: ignore[arg-type]


def _change_set(*edits: FileEdit) -> CodeChangeSet:
    return CodeChangeSet(summary="test change", edits=edits)


def _replace(path: str, old: str, new: str) -> FileEdit:
    return FileEdit(
        path=path, operation=EditOperation.REPLACE, old_text=old, new_text=new
    )


def _payload_edit(**overrides) -> dict:
    entry = {"path": "a.ts", "operation": "replace", "content": "", "oldText": "x", "newText": "y"}
    entry.update(overrides)
    return entry


# ===========================================================================
# 1. A targeted exact replacement succeeds
# ===========================================================================


def test_a_targeted_replacement_succeeds_and_writes_only_its_region(worktree: Path):
    application = apply_change_set(
        _change_set(
            _replace(
                NAV_TEST,
                "test('renders an empty tree', () => {\n  expect(navigation([])).toBe(0);\n});",
                "test('renders an empty tree', () => {\n  expect(navigation([])).toBe(0);\n});\n"
                "test('counts a nested tree', () => {\n"
                "  expect(navigation([{ id: 'a' }, { id: 'b' }])).toBe(2);\n"
                "});",
            )
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    assert application.rejected == ()
    assert application.targeted == (NAV_TEST,)
    assert application.changed_anything

    text = (worktree / NAV_TEST).read_text(encoding="utf-8")
    assert "counts a nested tree" in text
    assert "renders an empty tree" in text


def test_a_targeted_replacement_can_remove_the_text_it_matches(worktree: Path):
    existing = "\ntest('renders an empty tree', () => {\n  expect(navigation([])).toBe(0);\n});\n"
    application = apply_change_set(
        _change_set(_replace(NAV_TEST, existing, "")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    assert (worktree / NAV_TEST).read_text(encoding="utf-8") == (
        "import { navigation } from '../navigation';\n"
    )


def test_several_targeted_edits_to_one_file_compose_in_the_order_given(worktree: Path):
    application = apply_change_set(
        _change_set(
            _replace(NAV_TEST, "import { navigation }", "import { navigation, tree }"),
            _replace(NAV_TEST, "tree } from '../navigation'", "tree, layout } from '../layout'"),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    assert (worktree / NAV_TEST).read_text(encoding="utf-8").startswith(
        "import { navigation, tree, layout } from '../layout';"
    )


# ===========================================================================
# 2. Zero-match rejection
# ===========================================================================


def test_a_targeted_edit_whose_text_is_absent_is_refused_and_writes_nothing(worktree: Path):
    before = (worktree / NAV_TEST).read_text(encoding="utf-8")
    application = apply_change_set(
        _change_set(_replace(NAV_TEST, "this line is not in the file\n", "replacement\n")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.refused_whole
    assert "does not occur" in application.rejected[0].reason
    assert (worktree / NAV_TEST).read_text(encoding="utf-8") == before


def test_a_targeted_edit_on_a_file_that_does_not_exist_is_refused(worktree: Path):
    application = apply_change_set(
        _change_set(_replace("src/new.ts", "anything", "replacement")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert "does not exist" in application.rejected[0].reason
    assert not (worktree / "src" / "new.ts").exists()


# ===========================================================================
# 3. Multiple-match rejection
# ===========================================================================


def test_a_targeted_edit_whose_text_occurs_twice_is_refused(worktree: Path):
    (worktree / NAV_TEST).write_text(
        "const flag = true;\nif (flag) { go(); }\nif (flag) { go(); }\n", encoding="utf-8"
    )
    application = apply_change_set(
        _change_set(_replace(NAV_TEST, "if (flag) { go(); }", "if (flag) { stop(); }")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.refused_whole
    assert "occurs 2 times" in application.rejected[0].reason
    assert (worktree / NAV_TEST).read_text(encoding="utf-8").count("go();") == 2


# ===========================================================================
# 4. Malformed targeted edit rejection
# ===========================================================================


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"path": "a.ts", "operation": "replace", "content": "", "oldText": "x"},
            "'newText'",
        ),
        (_payload_edit(oldText=""), "'oldText'"),
        (_payload_edit(newText=7), "must both be strings"),
        (_payload_edit(content="whole file\n"), "must be ''"),
        (_payload_edit(operation="update"), "belong to operation 'replace'"),
        (_payload_edit(operation="delete", newText=""), "belong to operation 'replace'"),
        ({"path": "a.ts", "operation": "replace", "content": ""}, "both 'oldText'"),
    ],
)
def test_a_malformed_targeted_edit_is_rejected_with_a_reason(entry: dict, expected: str):
    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload({"summary": "s", "edits": [entry]})

    assert expected in str(error.value)


def test_the_targeted_emission_ceiling_is_eight_kilobytes_of_old_text_plus_new_text():
    """The limit is pinned by literal, not by the constant it polices.

    The other two tests in this group build payloads *from*
    ``MAX_TARGETED_EDIT_PAYLOAD_BYTES``, so they would pass unchanged if the
    limit were raised to anything. This one is the only assertion that notices,
    and it can only notice if it does not ask the implementation what the limit
    is.
    """
    assert MAX_TARGETED_EDIT_PAYLOAD_BYTES == 8_000


def test_a_targeted_payload_over_the_flat_ceiling_is_refused():
    """The emission ceiling for a targeted edit does not scale with the file.

    Reached by a small file and a large one alike, which is the property: the
    model is bounded on what it may *send*, and the resulting file is bounded
    separately by ``result_max_bytes``.
    """
    huge = "x" * 8_001
    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload(
            {
                "summary": "s",
                "edits": [_payload_edit(oldText=huge, newText="")],
            }
        )

    assert "8000" in str(error.value)


def test_a_targeted_payload_at_the_ceiling_is_accepted():
    half = "x" * 4_000
    change_set = CodeChangeSet.from_payload(
        {"summary": "s", "edits": [_payload_edit(oldText=half, newText=half)]}
    )

    assert change_set.edits[0].size_bytes == 8_000


def test_one_malformed_edit_among_several_is_a_structural_rejection(worktree: Path):
    """Concern 61's rule still holds for the new operation: a model-requested
    edit that cannot be parsed does not disappear while the others proceed."""
    change_set = CodeChangeSet.from_payload(
        {
            "summary": "s",
            "edits": [
                _payload_edit(path="src/navigation.ts", oldText="navigation"),
                _payload_edit(path="src/tree.ts", operation="rewrite"),
            ],
        }
    )

    assert change_set.has_parse_rejections
    assert change_set.rejected_parse_edits[0].path == "src/tree.ts"


# ===========================================================================
# 5. Invalid / path-traversal rejection
# ===========================================================================


def test_a_targeted_edit_outside_the_repository_is_refused(tmp_path: Path):
    root = tmp_path / "worktree"
    root.mkdir()
    (tmp_path / "sibling.ts").write_text("original\n", encoding="utf-8")

    for bad in ("../sibling.ts", "/etc/passwd", "~/notes.ts", "src/../../escape.ts"):
        application = apply_change_set(
            _change_set(_replace(bad, "original", "tampered")),
            root=root,
            policy=_policy(),
        )
        assert application.written == (), bad
        assert "not repository-relative" in application.rejected[0].reason, bad

    assert (tmp_path / "sibling.ts").read_text(encoding="utf-8") == "original\n"


def test_a_targeted_edit_through_a_symlink_is_refused(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.ts").write_text("original\n", encoding="utf-8")
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    application = apply_change_set(
        _change_set(_replace("linked/target.ts", "original", "tampered")),
        root=root,
        policy=_policy(),
    )

    assert application.written == ()
    assert (outside / "target.ts").read_text(encoding="utf-8") == "original\n"


# ===========================================================================
# 6. Disallowed task path rejection
# ===========================================================================


def test_a_targeted_edit_outside_the_task_allowance_is_refused(worktree: Path):
    (worktree / "src" / "billing.ts").write_text("const price = 1;\n", encoding="utf-8")
    application = apply_change_set(
        _change_set(_replace("src/billing.ts", "price = 1", "price = 2")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.refused_whole
    assert application.scope_refusals
    assert (worktree / "src" / "billing.ts").read_text(encoding="utf-8") == "const price = 1;\n"


def test_a_targeted_edit_of_a_protected_path_is_refused(worktree: Path):
    application = apply_change_set(
        _change_set(_replace(".env", "API_TOKEN=do-not-touch", "API_TOKEN=leaked")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.scope_refusals
    assert (worktree / ".env").read_text(encoding="utf-8") == "API_TOKEN=do-not-touch\n"


# ===========================================================================
# 7. Multiple targeted edits apply atomically
# ===========================================================================


def test_several_targeted_edits_across_files_all_apply(worktree: Path):
    application = apply_change_set(
        _change_set(
            _replace(NAV_TEST, "renders an empty tree", "renders a bare tree"),
            _replace("src/navigation.ts", "nodes.length", "nodes.length + 1"),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ("src/test/navigation-stack.test.ts", "src/navigation.ts")
    assert application.rejected == ()
    assert "renders a bare tree" in (worktree / NAV_TEST).read_text(encoding="utf-8")
    assert "nodes.length + 1" in (worktree / "src" / "navigation.ts").read_text(encoding="utf-8")


def test_a_later_bad_edit_rolls_the_whole_response_back(worktree: Path):
    """No partial application: a targeted change set is planned in full first.

    The first edit here would have succeeded on its own. It is not written,
    because the second one cannot be applied, and a candidate holding half of
    what the model asked for is not a candidate anybody can review.
    """
    nav_before = (worktree / "src" / "navigation.ts").read_text(encoding="utf-8")
    nav_test_before = (worktree / NAV_TEST).read_text(encoding="utf-8")

    application = apply_change_set(
        _change_set(
            _replace("src/navigation.ts", "nodes.length", "nodes.length + 1"),
            _replace(NAV_TEST, "text that is not in the file", "anything"),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.deleted == ()
    assert application.refused_whole
    assert not application.changed_anything
    assert (worktree / "src" / "navigation.ts").read_text(encoding="utf-8") == nav_before
    assert (worktree / NAV_TEST).read_text(encoding="utf-8") == nav_test_before


def test_the_refusal_accounts_for_every_edit_in_the_response(worktree: Path):
    application = apply_change_set(
        _change_set(
            _replace("src/navigation.ts", "nodes.length", "nodes.length + 1"),
            _replace(NAV_TEST, "not in the file", "anything"),
        ),
        root=worktree,
        policy=_policy(),
    )

    reasons = {edit.path: edit.reason for edit in application.rejected}
    assert "does not occur" in reasons[NAV_TEST]
    assert "nothing was written" in reasons["src/navigation.ts"]


def test_a_whole_file_change_set_keeps_its_per_edit_refusal_semantics(worktree: Path):
    """The atomic boundary is drawn at the targeted operation, not everywhere.

    A whole-file edit applies to whatever the file happens to hold, so a
    refusal of one of them says nothing about the others and the original
    behaviour is unchanged.
    """
    application = apply_change_set(
        _change_set(
            FileEdit(path="../.env", operation=EditOperation.UPDATE, content="SECRET=leaked\n"),
            FileEdit(
                path=NAV_TEST,
                operation=EditOperation.UPDATE,
                content="// rewritten whole\n",
            ),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    assert not application.refused_whole
    assert len(application.rejected) == 1


# ===========================================================================
# 9. A targeted edit beside a whole-file edit
# ===========================================================================


def test_a_targeted_edit_and_a_whole_file_edit_apply_together(worktree: Path):
    application = apply_change_set(
        _change_set(
            _replace(NAV_TEST, "renders an empty tree", "renders a bare tree"),
            FileEdit(
                path="src/new.ts",
                operation=EditOperation.CREATE,
                content="export const a = 1;\n",
            ),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST, "src/new.ts")
    assert application.targeted == (NAV_TEST,)
    assert (worktree / "src" / "new.ts").read_text(encoding="utf-8") == "export const a = 1;\n"


def test_a_targeted_edit_may_follow_a_whole_file_edit_of_the_same_path(worktree: Path):
    """Both are kept, and the targeted one matches the *staged* content.

    This is the order a coder writes in: rewrite a file, then adjust a line of
    the version it just wrote. The overlay is what makes that deterministic
    rather than a race against the filesystem.
    """
    application = apply_change_set(
        _change_set(
            FileEdit(path=NAV_TEST, operation=EditOperation.UPDATE, content="alpha\nbeta\n"),
            _replace(NAV_TEST, "beta", "gamma"),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    assert (worktree / NAV_TEST).read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_a_bad_whole_file_edit_still_stops_a_targeted_one_in_the_same_response(worktree: Path):
    application = apply_change_set(
        _change_set(
            FileEdit(path="src/missing.ts", operation=EditOperation.UPDATE, content="x\n"),
            _replace(NAV_TEST, "renders an empty tree", "renders a bare tree"),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.refused_whole
    assert "renders an empty tree" in (worktree / NAV_TEST).read_text(encoding="utf-8")


# ===========================================================================
# 10. Resulting max_files_changed enforcement
# ===========================================================================


def test_a_targeted_change_set_wider_than_the_task_allows_is_refused_whole(worktree: Path):
    application = apply_change_set(
        _change_set(
            _replace(NAV_TEST, "renders an empty tree", "renders a bare tree"),
            _replace("src/navigation.ts", "nodes.length", "nodes.length + 1"),
            _replace("src/new.ts", "x", "y"),
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == ()
    assert application.refused_whole
    assert len(application.rejected) == 3
    assert "the task allows 2" in application.rejected[0].reason
    assert (worktree / "src" / "new.ts").exists() is False


def test_several_targeted_edits_to_one_file_count_as_one_file(worktree: Path):
    """``max_files_changed`` is a statement about files, not about edits.

    Three regions of one file is one file, and refusing that against a limit of
    one would be measuring the model's chosen representation rather than the
    change it proposes.
    """
    application = apply_change_set(
        _change_set(
            _replace(NAV_TEST, "renders an empty tree", "renders a bare tree"),
            _replace(
                NAV_TEST,
                "expect(navigation([])).toBe(0);",
                "expect(navigation([])).toBe(0); // bare",
            ),
            _replace(NAV_TEST, "import { navigation }", "import { navigation as nav }"),
        ),
        root=worktree,
        policy=_policy(max_files_changed=1),
    )

    assert application.written == (NAV_TEST,)
    assert application.rejected == ()


# ===========================================================================
# 12. Resulting file / output safety enforcement
# ===========================================================================


def test_a_targeted_edit_past_the_result_ceiling_is_refused(worktree: Path):
    """The resulting file has its own bound, because the emission bound does
    not cover it.

    A 200-byte ``oldText`` and a 200-byte ``newText`` are both comfortably
    inside ``MAX_TARGETED_EDIT_PAYLOAD_BYTES``; a 900KB result is not inside
    anything. ``result_max_bytes`` is that bound, and the caller supplies the
    largest file the context builder will read into a prompt at all.
    """
    before = (worktree / NAV_TEST).read_text(encoding="utf-8")
    application = apply_change_set(
        _change_set(_replace(NAV_TEST, "renders an empty tree", "renders a bare tree" * 100)),
        root=worktree,
        policy=_policy(),
        result_max_bytes=200,
    )

    assert application.written == ()
    assert application.refused_whole
    assert "byte limit for one file" in application.rejected[0].reason
    assert (worktree / NAV_TEST).read_text(encoding="utf-8") == before


def test_a_resulting_file_inside_the_ceiling_is_written(worktree: Path):
    application = apply_change_set(
        _change_set(_replace(NAV_TEST, "renders an empty tree", "renders a bare tree")),
        root=worktree,
        policy=_policy(),
        result_max_bytes=10_000,
    )

    assert application.written == (NAV_TEST,)


def test_a_whole_file_edit_is_not_measured_against_the_result_ceiling(worktree: Path):
    """The two bounds answer two different questions and do not overlap.

    A whole-file edit is bounded where it is parsed, by the per-path output
    allowance on its content, because its content *is* the result. Charging it
    twice would make one ceiling do two unrelated jobs.
    """
    content = "// " + "y" * 500 + "\n"
    assert len(content) < MAX_EDIT_BYTES
    application = apply_change_set(
        _change_set(FileEdit(path=NAV_TEST, operation=EditOperation.UPDATE, content=content)),
        root=worktree,
        policy=_policy(),
        result_max_bytes=10,
    )

    assert application.written == (NAV_TEST,)


def test_the_targeted_ceiling_is_deterministic_and_bounded(worktree: Path):
    """Same input, same output, every time."""
    start = (worktree / NAV_TEST).read_text(encoding="utf-8")
    answers = set()
    for _ in range(3):
        (worktree / NAV_TEST).write_text(start, encoding="utf-8")
        application = apply_change_set(
            _change_set(_replace(NAV_TEST, "renders an empty tree", "renders a bare tree")),
            root=worktree,
            policy=_policy(),
        )
        answers.add((application.written, (worktree / NAV_TEST).read_text(encoding="utf-8")))

    assert answers == {((NAV_TEST,), start.replace("renders an empty tree", "renders a bare tree"))}


# ===========================================================================
# 13. Unchanged surrounding content preserved byte for byte
# ===========================================================================


def test_everything_outside_the_matched_region_survives_byte_for_byte(worktree: Path):
    original = (worktree / NAV_TEST).read_text(encoding="utf-8")
    marker = "test('renders an empty tree', () => {"
    head, _, tail = original.partition(marker)

    application = apply_change_set(
        _change_set(_replace(NAV_TEST, marker, "test('renders a bare tree', () => {")),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    rewritten = (worktree / NAV_TEST).read_text(encoding="utf-8")
    assert rewritten == original.replace(marker, "test('renders a bare tree', () => {")
    # The parts either side of the match are untouched, not merely similar.
    assert rewritten.startswith(head)
    assert rewritten.endswith(tail)


def test_a_replacement_that_ends_the_file_keeps_the_bytes_before_it(worktree: Path):
    original = (worktree / NAV_TEST).read_text(encoding="utf-8")
    head, _, _ = original.partition("test('renders an empty tree'")

    application = apply_change_set(
        _change_set(_replace(NAV_TEST, "test('renders an empty tree'", "// dropped")),
        root=worktree,
        policy=_policy(),
    )

    rewritten = (worktree / NAV_TEST).read_text(encoding="utf-8")
    assert application.written == (NAV_TEST,)
    assert rewritten.startswith(head)
    assert "renders an empty tree" not in rewritten


# ===========================================================================
# 14. Existing content cannot disappear by being omitted
# ===========================================================================


def test_existing_tests_cannot_disappear_because_the_payload_did_not_mention_them(worktree: Path):
    """The RUN-000007 attempt-2 failure, as a property rather than a story.

    Attempt 2 fitted the output allowance by deleting 33 existing tests. Under
    a targeted edit the payload has no room to express that: a replacement
    changes the region it names and nothing else, so content the model did not
    mention is not a deletion, it is an absence of instruction.
    """
    original = (worktree / NAV_TEST).read_text(encoding="utf-8")
    existing = [f"test('renders an empty tree {{ index: {i}}}'" for i in range(33)]
    grown = original + "".join(f"{line}\n" for line in existing) + "\n"
    (worktree / NAV_TEST).write_text(grown, encoding="utf-8")
    before = (worktree / NAV_TEST).read_text(encoding="utf-8")

    application = apply_change_set(
        _change_set(
            _replace(NAV_TEST, "renders an empty tree', () => {", "renders a bare tree', () => {")
        ),
        root=worktree,
        policy=_policy(),
    )

    after = (worktree / NAV_TEST).read_text(encoding="utf-8")
    assert application.written == (NAV_TEST,)
    for line in existing[1:]:
        assert line in after
    # The whole file is the starting file with exactly one identifier renamed.
    # Nothing was dropped to make room, and nothing else moved.
    assert after == before.replace(
        "renders an empty tree', () => {", "renders a bare tree', () => {"
    )
    assert len(after) < len(before), "renaming 'empty' to 'bare' shortens the file"


@pytest.mark.asyncio
async def test_a_targeted_payload_over_the_emission_ceiling_fails_closed(
    session: Session,
    workspace_factory,
    git_settings: Settings,
):
    """The new operation does not become an unbounded channel.

    The payload ceiling is enforced where every other one is, at parse time,
    and a response refused there never reaches the worktree -- so the file is
    byte-identical to the starting commit afterwards.
    """
    _, _, workspace = workspace_factory()
    before = (workspace.path / NAV_TEST).read_text(encoding="utf-8")

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "a very large region",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "replace",
                        "content": "",
                        "oldText": "renders an empty tree",
                        "newText": "x" * 8_001,
                    }
                ],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert "8000" in attempt.feedback
    # No diff is even captured: the response is refused before it can be
    # applied, and the worktree is byte-identical to the starting commit.
    assert attempt.diff is None
    assert attempt.application is None
    assert (workspace.path / NAV_TEST).read_text(encoding="utf-8") == before


# ===========================================================================
# 15. Existing whole-file replacement still supported
# ===========================================================================


def test_a_whole_file_replacement_still_works_exactly_as_before(worktree: Path):
    application = apply_change_set(
        _change_set(
            FileEdit(
                path=NAV_TEST,
                operation=EditOperation.UPDATE,
                content="// whole file, written out in full\n",
            )
        ),
        root=worktree,
        policy=_policy(),
    )

    assert application.written == (NAV_TEST,)
    assert application.targeted == ()
    assert (worktree / NAV_TEST).read_text(encoding="utf-8") == (
        "// whole file, written out in full\n"
    )


def test_an_ordinary_update_payload_still_parses_with_no_target_fields():
    change_set = CodeChangeSet.from_payload(
        {
            "summary": "unchanged shape",
            "edits": [
                {"path": "a.ts", "operation": "update", "content": "one\n"},
                {"path": "b.ts", "operation": "create", "content": "two\n"},
                {"path": "c.ts", "operation": "delete", "content": ""},
            ],
        }
    )

    assert change_set.paths == ("a.ts", "b.ts", "c.ts")
    assert change_set.warnings == ()
    assert change_set.rejected_parse_edits == ()


def test_the_first_of_two_whole_file_edits_to_one_path_still_wins(worktree: Path):
    change_set = CodeChangeSet.from_payload(
        {
            "summary": "s",
            "edits": [
                {"path": "a.ts", "operation": "update", "content": "first\n"},
                {"path": "a.ts", "operation": "update", "content": "second\n"},
            ],
        }
    )

    assert len(change_set.edits) == 1
    assert change_set.edits[0].content == "first\n"
    assert "edited more than once" in change_set.warnings[0]


def test_the_schema_advertises_the_targeted_operation():
    item = EDIT_SCHEMA["properties"]["edits"]["items"]  # type: ignore[index]

    assert "replace" in item["properties"]["operation"]["enum"]  # type: ignore[index]
    assert "oldText" in item["properties"]  # type: ignore[operator]
    assert "newText" in item["properties"]  # type: ignore[operator]
    # Still one uniformly shaped object: every operation carries every key, and
    # the two that one operation does not use are simply empty.
    assert item["required"] == ["path", "operation", "content"]  # type: ignore[index]


def test_the_edit_schema_version_records_the_contract_change():
    """The schema version is concern 70's; the prompt version is not.

    The edit schema has not changed since concern 70 introduced ``replace``,
    so ``code-edits/3`` is still the contract this concern added. The coder
    prompt has moved on twice since: ``coder-prompt/4`` preferred whole-file
    updates for complete writable files, and ``coder-prompt/5`` bounded the
    coder's own verification. Both are real contract changes, so what this
    test pins is that the two versions move independently -- the schema stays
    where concern 70 left it while the prompt is free to advance.
    """
    assert EDIT_SCHEMA_VERSION == "code-edits/3"
    assert CODER_PROMPT_VERSION == "coder-prompt/5"


# ===========================================================================
# 16 / 17. Prompt and FIX feedback
# ===========================================================================


def _instructions(**overrides) -> str:
    return render_coding_instructions(
        Task(
            id=1,
            project_id=1,
            external_task_id="TS-070",
            title="Extend the navigation stack tests",
            instructions="Cover the stack layout.",
            complexity=Complexity.LOW,
        ),
        **overrides,
    )


def test_the_prompt_advertises_the_targeted_operation():
    prompt = _instructions()

    assert "'replace'" in prompt
    assert "oldText" in prompt
    assert "newText" in prompt
    assert "occur exactly once" in prompt


def test_the_system_prompt_states_supported_operations_without_small_replace_preference():
    assert 'operation "replace"' in CODER_SYSTEM_PROMPT
    assert 'operation "create" or "update"' in CODER_SYSTEM_PROMPT
    assert 'prefer "update"' in CODER_SYSTEM_PROMPT
    assert 'Use "create" for a new file' in CODER_SYSTEM_PROMPT
    assert "small change" not in CODER_SYSTEM_PROMPT
    assert 'Prefer "replace"' not in CODER_SYSTEM_PROMPT
    assert "if it occurs nowhere the edit is refused" in CODER_SYSTEM_PROMPT


def test_complete_writable_existing_files_are_steered_to_update():
    prompt = _instructions(path_output_limits={NAV_TEST: 14_766})

    assert "were supplied complete, so prefer 'update' for them" in prompt
    assert "prefer operation 'update'" in prompt
    assert "COMPLETE resulting file contents" in prompt
    assert "Preserve all existing content not intentionally changed" in prompt


def test_replace_remains_supported_with_exact_old_text_semantics():
    prompt = _instructions()

    assert "Use operation 'replace' as a supported targeted operation" in prompt
    assert "not appropriate or permitted" in prompt
    assert "'oldText' must be copied exactly" in prompt
    assert "occur exactly once" in prompt


def test_create_remains_the_operation_for_new_files():
    prompt = _instructions()

    assert "For a new file, use operation 'create'" in prompt
    assert "return the complete new contents" in prompt


def test_the_prompt_tells_the_model_to_preserve_unrelated_tests_and_content():
    """Both halves of the prompt carry the warning, in the same words.

    Not redundancy for its own sake: the instructions are rebuilt per task and
    the system prompt is not, so a rule that lives in only one of them is a rule
    that a fix attempt may not be told.
    """
    for prompt in (_instructions(), CODER_SYSTEM_PROMPT):
        # Compared case-insensitively because the same rule starts a sentence
        # in one half of the prompt and a bullet in the other.
        lowered = prompt.lower()
        assert "an omitted test is a deleted test" in lowered
        assert "the task did not ask you to remove" in lowered
        assert "keep the tests a file already has" in lowered


def test_the_prompt_says_the_whole_file_allowance_does_not_apply_to_a_replace():
    prompt = _instructions(path_output_limits={NAV_TEST: 14_766})

    assert "14766 bytes" in prompt
    assert "applies only to a 'create' or an 'update'" in prompt
    assert "a 'replace' of it is not measured against it" in prompt


@pytest.mark.asyncio
async def test_the_fix_feedback_advertises_the_same_capability(
    session: Session,
    workspace_factory,
    git_settings: Settings,
):
    """A zero-match refusal must teach the operation that would have worked.

    The concern's own failure was a coder told only to fit the limit, and told
    nothing about an alternative that fits. Every FIX path a targeted edit can
    reach has to name it.
    """
    _, _, workspace = workspace_factory()

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "small addition",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "replace",
                        "content": "",
                        "oldText": "text that is not in the file",
                        "newText": "replacement\n",
                    }
                ],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert "nothing was written" in attempt.feedback
    assert "'replace'" in attempt.feedback
    assert "oldText" in attempt.feedback
    assert "existing tests" in attempt.feedback


@pytest.mark.asyncio
async def test_the_parse_rejection_feedback_advertises_the_same_capability(
    session: Session,
    workspace_factory,
    git_settings: Settings,
):
    _, _, workspace = workspace_factory()

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "targeted fields on a whole-file operation",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "update",
                        "content": "x\n",
                        "oldText": "y",
                    }
                ],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert "belong to operation 'replace'" in attempt.feedback
    assert "'replace'" in attempt.feedback
    assert "oldText" in attempt.feedback
    assert "deleted test" in attempt.feedback


# ===========================================================================
# 11. Resulting max_diff_lines enforcement (through the whole agent)
# ===========================================================================


@pytest.mark.asyncio
async def test_a_small_targeted_payload_over_max_diff_lines_is_blocked_by_the_scope_guard(
    session: Session,
    repository: Path,
    workspace_factory,
    git_settings: Settings,
):
    """Scope is measured on the resulting Git diff, never on the payload.

    The payload here is under 5KB, well inside the targeted-edit ceiling. The
    diff it produces is 301 lines, because the matched region is one comment
    line replaced by 300 of them. A guard that counted the payload would wave
    this through; the scope guard reads the repository and blocks it.
    """
    filler = "".join(f"// baseline line {index}\n" for index in range(400))
    (repository / NAV_TEST).write_text(filler, encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-069: grow the navigation test file")

    _, _, workspace = workspace_factory(
        limits=TaskLimits(max_files_changed=2, max_diff_lines=150)
    )

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "rewrites one comment block into many lines",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "replace",
                        "content": "",
                        "oldText": "// baseline line 0\n",
                        "newText": "".join(
                            f"// replacement line {index}\n" for index in range(300)
                        ),
                    }
                ],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is FailureReason.SCOPE_VIOLATION
    assert attempt.scope is not None
    assert attempt.scope.decision is ScopePolicyDecision.BLOCK
    assert any("the task allows 150" in finding.detail for finding in attempt.scope.findings)


@pytest.mark.asyncio
async def test_a_targeted_change_wider_than_max_files_changed_is_blocked(
    session: Session,
    repository: Path,
    workspace_factory,
    git_settings: Settings,
):
    (repository / "src" / "second.ts").write_text("const second = 1;\n", encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-069: add a second source file")

    _, _, workspace = workspace_factory(
        files_to_modify=[NAV_TEST, "src/navigation.ts", "src/second.ts"],
        files_to_create=[],
        limits=TaskLimits(max_files_changed=2, max_diff_lines=150),
    )

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "one region in each of three files",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "replace",
                        "content": "",
                        "oldText": "renders an empty tree",
                        "newText": "renders a bare tree",
                    },
                    {
                        "path": "src/navigation.ts",
                        "operation": "replace",
                        "content": "",
                        "oldText": "nodes.length",
                        "newText": "nodes.length + 1",
                    },
                    {
                        "path": "src/second.ts",
                        "operation": "replace",
                        "content": "",
                        "oldText": "const second = 1;",
                        "newText": "const second = 2;",
                    },
                ],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is FailureReason.SCOPE_VIOLATION
    assert attempt.application is not None
    assert attempt.application.written == ()
    assert attempt.application.refused_whole


@pytest.mark.asyncio
async def test_a_legitimate_targeted_change_passes_verification_and_the_scope_guard(
    session: Session,
    workspace_factory,
    git_settings: Settings,
):
    _, _, workspace = workspace_factory()

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "adds a second test beside the first",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "replace",
                        "content": "",
                        "oldText": "test('renders an empty tree', () => {\n"
                        "  expect(navigation([])).toBe(0);\n"
                        "});",
                        "newText": "test('renders an empty tree', () => {\n"
                        "  expect(navigation([])).toBe(0);\n"
                        "});\n"
                        "test('counts a nested tree', () => {\n"
                        "  expect(navigation([{ id: 'a' }, { id: 'b' }])).toBe(2);\n"
                        "});",
                    }
                ],
                "testsAdded": [NAV_TEST],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is None, attempt.feedback
    assert attempt.scope is not None
    assert attempt.scope.decision is ScopePolicyDecision.ALLOW
    assert attempt.scope.files_changed == 1
    assert attempt.scope.diff_lines < 150
    assert attempt.application is not None
    assert attempt.application.targeted == (NAV_TEST,)
    text = (workspace.path / NAV_TEST).read_text(encoding="utf-8")
    assert "renders an empty tree" in text
    assert "counts a nested tree" in text


# ===========================================================================
# S. The synthetic RUN-000007 structural case
# ===========================================================================

#: RUN-000007, measured from the run's own records rather than estimated.
#: Attempt 1 was refused with "content is 15106 bytes, over the 14766-byte
#: limit for one file"; 14,766 is ``per_path_edit_allowance(11_813)``, so the
#: legitimate result was 340 bytes over the allowance for a change of a few
#: lines. Attempt 2 came back at 413 diff lines against a 150-line limit, 315
#: of them deletions, having removed all 39 of the baseline's tests.
RUN7_BASELINE_BYTES = 11_813
RUN7_RESULT_BYTES = 15_106
RUN7_DIFF_LINES = 150
RUN7_BASELINE_TESTS = 39

#: A line guaranteed to appear exactly once in the fixture, so a targeted edit
#: can anchor on it. Chosen over a snippet of test code so that "the model
#: picked an anchor" and "the model picked real code" are not conflated.
RUN7_MARKER = "// -- end of generated navigation stack fixture --"

#: How many bytes the legitimate addition has to be. The marker is replaced
#: by ``addition + marker``, so the file grows by exactly the addition's
#: length and the target is exact rather than approximate.
RUN7_ADDITION_BYTES = RUN7_RESULT_BYTES - RUN7_BASELINE_BYTES


def _run7_baseline() -> str:
    """An 11,813-byte test file holding the 39 tests the real baseline held.

    Padded to the exact byte count with a filler comment so the arithmetic in
    the regression below is a real measurement and not an approximation.
    """
    header = (
        "import { navigation } from '../navigation';\n"
        "\n"
        "describe('navigation stack', () => {\n"
    )
    body = "".join(
        f"  test('renders case {index:02d}', () => {{\n"
        f"    expect(navigation([{{ id: '{index}' }}])).toBe(1);\n"
        f"  }});\n"
        for index in range(RUN7_BASELINE_TESTS)
    )
    so_far = f"{header}{body});\n{RUN7_MARKER}\n"
    padding = RUN7_BASELINE_BYTES - len(so_far.encode()) - len("// \n")
    assert padding > 0, "the fixture body is already longer than the target size"
    return f"{header}{body});\n// {'x' * padding}\n{RUN7_MARKER}\n"


def _run7_addition() -> str:
    """The legitimate addition: one data-driven ordering test, ~61 lines.

    Sized to exactly ``RUN7_ADDITION_BYTES`` so the resulting file lands on the
    size RUN-000007 legitimately produced. The last row's label absorbs the
    remainder, which keeps the fixture a real-looking test rather than a
    comment padded to a target.
    """
    head = "test('orders a nested stack by depth', () => {\n  const rows = [\n"
    tail = (
        "  ];\n"
        "  expect(rows.map((row) => row.depth)).toEqual([1, 1, 2, 2, 2, 3]);\n"
        "});\n"
    )
    template = "    {{ id: '{id:02d}', label: '{label}', depth: {depth} }},\n"
    rows = [
        template.format(id=index, label=f"node-{index}", depth=1 + (index % 3))
        for index in range(55)
    ]
    built = f"{head}{''.join(rows)}{tail}"
    shortfall = RUN7_ADDITION_BYTES - len(built.encode())
    assert shortfall > 0, "55 rows already exceed the addition budget"
    rows[-1] = template.format(id=54, label=f"node-54{'x' * shortfall}", depth=1)
    addition = f"{head}{''.join(rows)}{tail}"
    assert len(addition.encode()) == RUN7_ADDITION_BYTES
    return addition



def test_the_synthetic_baseline_is_the_size_run_000007_saw():
    assert len(_run7_baseline().encode()) == RUN7_BASELINE_BYTES


def test_the_synthetic_addition_reaches_the_size_run_000007_produced():
    combined = _run7_baseline().replace(
        RUN7_MARKER + "\n", _run7_addition() + RUN7_MARKER + "\n"
    )
    assert len(combined.encode()) == RUN7_RESULT_BYTES


def test_a_whole_file_replacement_could_not_have_fitted_run_000007():
    """The structural premise, stated as an assertion, with the fix beside it.

    The per-path output allowance for an 11,813-byte complete source is
    14,766 bytes and the legitimate result is 15,106, so a faithful whole-file
    replacement had 340 bytes of nowhere to go. That is the arithmetic; the run
    records show what the model did with it, which is deletion.

    The same addition as a targeted edit is charged 3,293 bytes rather than
    15,106, so it fits with room to spare. Both halves are asserted here so the
    comparison cannot be quietly broken from one side: raising
    ``per_path_edit_allowance`` would fail the first assertion, and raising
    ``MAX_TARGETED_EDIT_PAYLOAD_BYTES`` past 3,293 would fail the second.
    """
    allowance = per_path_edit_allowance(RUN7_BASELINE_BYTES)
    assert allowance == 14_766
    assert allowance < RUN7_RESULT_BYTES
    assert RUN7_ADDITION_BYTES == 3_293
    assert RUN7_ADDITION_BYTES <= MAX_TARGETED_EDIT_PAYLOAD_BYTES
    assert MAX_TARGETED_EDIT_PAYLOAD_BYTES <= RUN7_RESULT_BYTES


@pytest.mark.asyncio
async def test_the_synthetic_run_000007_case_succeeds_without_returning_the_whole_file(
    session: Session,
    repository: Path,
    workspace_factory,
    git_settings: Settings,
):
    """The structural case, end to end through the coding agent.

    A 11,813-byte test file, one small targeted addition, a resulting diff well
    under 150 lines, and a model that never has to spend output allowance
    reproducing eleven kilobytes of unchanged source.
    """
    # Committed before the run, so the run's starting commit *is* the 11,813
    # bytes and the diff the scope guard measures is the coder's change rather
    # than this fixture.
    baseline = _run7_baseline()
    (repository / NAV_TEST).write_text(baseline, encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-069: grow the navigation test file")

    _, _, workspace = workspace_factory(
        limits=TaskLimits(max_files_changed=2, max_diff_lines=RUN7_DIFF_LINES)
    )

    addition = _run7_addition()
    answer = json.dumps(
        {
            "summary": "adds one data-driven ordering test beside the 33 existing ones",
            "edits": [
                {
                    "path": NAV_TEST,
                    "operation": "replace",
                    "content": "",
                    "oldText": RUN7_MARKER + "\n",
                    "newText": addition + RUN7_MARKER + "\n",
                }
            ],
            "testsAdded": [NAV_TEST],
        }
    )

    coder = ScriptedCoder(answer)
    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is None, attempt.feedback

    # The result is the file RUN-000007 legitimately needed.
    result = (workspace.path / NAV_TEST).read_text(encoding="utf-8")
    assert len(result.encode()) == RUN7_RESULT_BYTES
    assert "orders a nested stack by depth" in result

    # And every one of the 33 existing tests is still there.
    for index in range(33):
        assert f"renders case {index:02d}" in result

    # The diff is a legitimate small change, not 413 lines of churn.
    assert attempt.scope is not None
    assert attempt.scope.decision is ScopePolicyDecision.ALLOW
    assert attempt.scope.files_changed == 1
    assert attempt.scope.diff_lines < RUN7_DIFF_LINES
    assert attempt.scope.diff_lines < 100

    # The model did not return the file. Its whole answer is a small fraction
    # of the file it edited, and smaller than the ceiling for one targeted
    # edit, while the result is larger than the ceiling for a whole file.
    assert len(answer.encode()) < MAX_TARGETED_EDIT_PAYLOAD_BYTES
    assert len(answer.encode()) < len(result.encode()) // 4
    assert attempt.change_set is not None
    assert attempt.change_set.edits[0].size_bytes == len(
        (RUN7_MARKER + "\n").encode()
    ) + len((addition + RUN7_MARKER + "\n").encode())
    assert per_path_edit_allowance(RUN7_BASELINE_BYTES) < RUN7_RESULT_BYTES


@pytest.mark.asyncio
async def test_the_synthetic_run_000007_case_still_fails_closed_on_a_bad_match(
    session: Session,
    repository: Path,
    workspace_factory,
    git_settings: Settings,
):
    """The operation does not become a way to write anything anywhere.

    A ``replace`` whose ``oldText`` is not in the file is refused, the attempt
    fails closed, and the worktree is byte-identical to the starting commit.
    """
    (repository / NAV_TEST).write_text(_run7_baseline(), encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "TS-069: grow the navigation test file")

    _, _, workspace = workspace_factory()

    coder = ScriptedCoder(
        json.dumps(
            {
                "summary": "guesses at text that is not there",
                "edits": [
                    {
                        "path": NAV_TEST,
                        "operation": "replace",
                        "content": "",
                        "oldText": "test('renders a case that does not exist'",
                        "newText": "anything",
                    }
                ],
            }
        )
    )

    attempt = await run_coding_attempt(session, workspace, provider=coder, settings=git_settings)

    assert attempt.failure_reason is FailureReason.INVALID_MODEL_RESPONSE
    assert attempt.application is not None
    assert attempt.application.written == ()
    assert attempt.application.refused_whole
    assert (workspace.path / NAV_TEST).read_text(encoding="utf-8") == _run7_baseline()
    assert attempt.diff is not None
    assert attempt.diff.summary.files_changed == 0
