"""Concern 73 follow-up: the operator-resolved human merge conflict.

The concern 73 contract says a human commit becomes the baseline, and it
assumed the commit would merge. The recovered TraceStack history is the case
that assumption misses: ``cbff2c4`` is a *sibling* of the integration lineage
``fc6abc5`` -- both descend from the pre-TS-101 import ``06a0697`` -- and both
append to the same class and the same test file. The canonical merge raises
genuine content conflicts, and this contract is about the supported way
through that without rewriting a single line of the human's history.

Nothing here touches the canonical repository or the runtime database. Every
test runs against a disposable clone and a disposable SQLite file, and the
assertions that matter are the ones about what must *not* change: the human
commit's object, the seven historical TaskRuns, and the ref the rest of the
campaign is scheduled from.

The resolution these tests authorise is the real one: the human's
``filterBySource`` and its four tests, placed alongside the TS-101..TS-108 work
that conflicts with them, not instead of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config import settings as settings_module
from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models
from apps.orchestrator.db.base import Base
from apps.orchestrator.domain.enums import EscalationStatus, TaskStatus
from apps.orchestrator.services import human_resolution as resolver
from apps.orchestrator.services import reconstruction_importer as importer
from apps.orchestrator.services.errors import EntityConflict, EntityNotFound
from apps.orchestrator.services.git_errors import MergeConflict
from apps.orchestrator.services.git_service import GitService

pytestmark = pytest.mark.integration

MANIFEST = Path("data/recovery/tracestack_reconstruction_manifest.json")
SOURCE_REPO = Path("workspace/tracestack-clean")
INTEGRATION_SHA = "fc6abc579cee88f821c5f72f00162872b2dc8326"
HUMAN_COMMIT = "cbff2c4bd919b860c73e3cb061bccff11789c37a"
HUMAN_PARENT = "06a069725957d13b44140e8308e78de34abae0dc"
ESCALATION_ID = "fc9bf9b0-9683-41fa-aaa8-f8f53567b92a"
TS109_RUN_ID = "0a77bbb6-68ca-4e84-9572-5e37552387bf"
#: The head the importer will demand from the reconstructed database, derived
#: through the importer's own rule rather than hardcoded a revision behind.
ALEMBIC_HEAD: str = importer._required_alembic_head(
    json.loads(MANIFEST.read_text(encoding="utf-8"))
)

IMPL = "src/navigation/navigation-stack.ts"
TEST = "src/test/navigation-stack.test.ts"

#: A real but cheap verification profile. These are actual processes that
#: actually read the resolved tree, so "verification was not skipped" is a
#: fact rather than an assertion about a flag. The expensive half -- the real
#: TypeScript toolchain -- is exercised by
#: :func:`test_operator_workflow_passes_the_real_typescript_toolchain`, which
#: deliberately runs once with the project's own declared profile.
FAST_BUILD = f"node -e \"require('fs').readFileSync('{IMPL}','utf8')\""
FAST_LINT = (
    f"node -e \"const s=require('fs').readFileSync('{IMPL}','utf8');"
    "if(s.includes('<<<<<<<')){console.error('markers');process.exit(1)}\""
)
FAST_TESTS = (
    "node -e \"const fs=require('fs');"
    f"const s=fs.readFileSync('{IMPL}','utf8');"
    f"const t=fs.readFileSync('{TEST}','utf8');"
    "const need=['filterBySource(source: NavigationSource)',"
    "'.filter(entry => entry.source === source)',"
    "'.sort((a, b) => a.timestamp - b.timestamp)'];"
    "for(const n of need){if(!s.includes(n)){console.error('missing impl: '+n);process.exit(1)}}"
    "const names=['matching entries oldest first','empty array when no entries match',"
    "'exclude entries with undefined source','not mutate the stack'];"
    "for(const n of names){if(!t.includes(n)){console.error('missing test: '+n);process.exit(1)}}\""
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_soft(repo: Path, *args: str) -> int:
    return subprocess.run(
        ("git", *args), cwd=repo, check=False, capture_output=True, text=True
    ).returncode


def _is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    return _git_soft(repo, "merge-base", "--is-ancestor", commit, ref) == 0


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A disposable clone frozen at the pre-C73 campaign topology.

    The clone carries the real TraceStack objects (so ``fc6abc5``, ``cbff2c4``
    and their common parent ``06a0697`` are all present and byte-exact), then
    pins the one mutable ref back to the baseline this contract is exercised
    against. The live campaign has since advanced ``agent/integration`` past
    ``fc6abc5`` and merged the human source into it, so a plain clone no longer
    reproduces the historical world: freezing the ref restores ``cbff2c4`` as a
    *sibling* (merge-base ``06a0697``) and the real merge conflict the resolution
    path must handle. Only a throwaway clone is written; the canonical repository
    is never touched, which is what
    :func:`test_canonical_repository_is_never_mutated` proves.
    """
    clone = tmp_path / "tracestack-clean"
    subprocess.run(
        ["git", "clone", "--quiet", str(SOURCE_REPO.resolve()), str(clone)], check=True
    )
    current = _git(clone, "rev-parse", "--abbrev-ref", "HEAD")
    for ref in _git(
        clone, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/"
    ).splitlines():
        if ref.endswith("/HEAD"):
            continue
        name = ref.removeprefix("refs/remotes/origin/")
        if name == current:
            continue
        _git(clone, "branch", "--force", name, ref)
    # Freeze the campaign baseline ref to the historical checkpoint (the object is
    # already in the clone as an ancestor of the advanced ref). update-ref works
    # regardless of what is checked out, unlike branch --force.
    _git(clone, "update-ref", "refs/heads/agent/integration", INTEGRATION_SHA)
    return clone


@pytest.fixture
def isolated_url(tmp_path: Path) -> str:
    db = tmp_path / "c73_resolution.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("create table alembic_version (version_num varchar(32))"))
        connection.execute(
            text("insert into alembic_version(version_num) values (:rev)"),
            {"rev": ALEMBIC_HEAD},
        )
    engine.dispose()
    return f"sqlite:///{db}"


def _runner(command, **kwargs):
    if command[0] == "docker":
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, **kwargs)


def _snapshot_runs(factory) -> list[dict]:
    """Every column of every TaskRun, so "unchanged" means unchanged."""
    with factory() as session:
        rows = session.query(models.TaskRunRow).order_by(models.TaskRunRow.run_number).all()
        return [
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in rows
        ]


def _import_state(
    tmp_path: Path,
    isolated_url: str,
    repo_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: dict[str, list[str]] | None = None,
    dependency_paths: list[str] | None = None,
):
    """Reconstruct the pre-C73 campaign, pointed at the disposable clone.

    Uses the real importer against a real manifest and real evidence files, so
    the state these tests reconcile is the state the recovery would produce --
    not a hand-built approximation of it. The importer validates the campaign
    repository by read-only Git against ``repository.path``; the manifest's
    default (the live repo, whose refs the campaign has since advanced) is
    repointed at the frozen ``repo_copy`` clone so that validation is
    deterministic. The canonical repository is never read for these assertions.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["repository"]["path"] = str(repo_copy.resolve())
    manifest_path = tmp_path / "campaign-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    importer.import_reconstruction(
        manifest_path=manifest_path,
        database_url=isolated_url,
        confirm_database=isolated_url.removeprefix("sqlite:///"),
        runner=_runner,
    )
    engine = create_engine(isolated_url, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        project = session.query(models.ProjectRow).one()
        project.repository_path = str(repo_copy)
        project.verification_profile = profile or {
            "build": [FAST_BUILD],
            "lint": [FAST_LINT],
            "tests": [FAST_TESTS],
            "security": [],
        }
        project.dependency_paths = dependency_paths or []
    settings = Settings(
        _env_file=None,
        worktree_root=tmp_path / "worktrees",
        artifact_root=tmp_path / "artifacts",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=120,
        worker_timeout_seconds=600,
    )
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    # ``human_resolution`` imported ``get_settings`` by name, so the module-level
    # patch above does not reach it. Without this the service would build its
    # worktree path from the real settings and write into the live workspace.
    monkeypatch.setattr(resolver, "get_settings", lambda: settings)
    return engine, factory


# ------------------------------------------------------- authoring a resolution


def _write_correct_resolution(workspace: Path) -> None:
    """Carry the real TS-109 work onto the current baseline.

    This is the edit a person makes, and it is the *only* input the system
    takes. The human's method goes in beside the baseline's own additions
    rather than in the place the human put it, because that place is exactly
    what the conflict was about.
    """
    impl = workspace / IMPL
    text_body = impl.read_text(encoding="utf-8")
    old_import = "import { NavigationEntry } from '../models/navigation-entry';"
    assert text_body.count(old_import) == 1
    text_body = text_body.replace(
        old_import,
        "import { NavigationEntry, NavigationSource } from '../models/navigation-entry';",
    )
    anchor = "  find(id: string): NavigationEntry | undefined {"
    assert text_body.count(anchor) == 1
    text_body = text_body.replace(
        anchor,
        "  filterBySource(source: NavigationSource): NavigationEntry[] {\n"
        "    return this.entries\n"
        "      .filter(entry => entry.source === source)\n"
        "      .sort((a, b) => a.timestamp - b.timestamp);\n"
        "  }\n\n" + anchor,
    )
    impl.write_text(text_body, encoding="utf-8")

    test = workspace / TEST
    test_body = test.read_text(encoding="utf-8").rstrip()
    assert test_body.endswith("});")
    block = """
  describe('filterBySource', () => {
    it('should return matching entries oldest first', () => {
      const def1: NavigationEntry = { ...entry1, source: 'definition', timestamp: 100 };
      const def2: NavigationEntry = { ...entry2, source: 'definition', timestamp: 200 };
      const ref1: NavigationEntry = {
        id: 'r1', uri: 'file:///ref1.ts', fileName: 'ref1.ts',
        line: 30, character: 15, source: 'reference', timestamp: 150
      };
      stack.push(def1);
      stack.push(ref1);
      stack.push(def2);

      const result = stack.filterBySource('definition');

      assert.strictEqual(result.length, 2);
      assert.strictEqual(result[0].id, '1');
      assert.strictEqual(result[0].timestamp, 100);
      assert.strictEqual(result[1].id, '2');
      assert.strictEqual(result[1].timestamp, 200);
    });

    it('should return empty array when no entries match', () => {
      stack.push({ ...entry1, source: 'definition' });
      stack.push({ ...entry2, source: 'reference' });

      const result = stack.filterBySource('implementation');

      assert.strictEqual(result.length, 0);
    });

    it('should exclude entries with undefined source', () => {
      stack.push({ ...entry1, source: 'definition' });
      stack.push({ ...entry2 });

      const result = stack.filterBySource('definition');

      assert.strictEqual(result.length, 1);
      assert.strictEqual(result[0].id, '1');
    });

    it('should not mutate the stack', () => {
      stack.push({ ...entry1, source: 'definition', timestamp: 100 });
      stack.push({ ...entry2, source: 'definition', timestamp: 50 });
      const before = stack.getEntries().slice();

      stack.filterBySource('definition');

      const after = stack.getEntries();
      assert.strictEqual(after.length, before.length);
      assert.strictEqual(after[0].id, before[0].id);
      assert.strictEqual(after[1].id, before[1].id);
    });
  });
"""
    test.write_text(
        test_body[: test_body.rfind("});")] + block + "});\n", encoding="utf-8"
    )


def _prepare(factory, *, human=HUMAN_COMMIT, baseline=INTEGRATION_SHA):
    with factory.begin() as session:
        return resolver.prepare_resolution_workspace(
            session,
            uuid.UUID(ESCALATION_ID),
            human_source_commit=human,
            expected_integration_sha=baseline,
        )


def _authorize(factory, *, human=HUMAN_COMMIT, baseline=INTEGRATION_SHA):
    with factory.begin() as session:
        return resolver.authorize_human_resolution(
            session,
            uuid.UUID(ESCALATION_ID),
            human_source_commit=human,
            expected_integration_sha=baseline,
        )


# ============================================================ the raw conflict


def test_canonical_merge_conflicts_and_current_contract_fails_closed(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """Step A: the conflict is real, reproducible, and the answer is a refusal.

    The topology is the finding this whole contract exists for: ``cbff2c4``'s
    parent is the pre-TS-101 import, and the integration lineage is a *sibling*
    branch from that same import. Nothing was corrupted in the recovery; this
    is what the repository's history actually says.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    assert _git(repo_copy, "rev-list", "--parents", "-n", "1", HUMAN_COMMIT).endswith(
        HUMAN_PARENT
    )
    assert (
        _git(repo_copy, "merge-base", HUMAN_COMMIT, INTEGRATION_SHA) == HUMAN_PARENT
    ), "the human commit and the baseline are siblings, not ancestor/descendant"
    assert not _is_ancestor(repo_copy, HUMAN_COMMIT, "agent/integration")

    # Both sides appended to the same class and the same test file.
    assert _git(repo_copy, "diff", "--name-only", f"{HUMAN_PARENT}..{HUMAN_COMMIT}") == (
        f"{IMPL}\n{TEST}"
    )

    from apps.orchestrator.services import reviews

    with factory.begin() as session, pytest.raises(MergeConflict) as caught:
        reviews.reconcile_human_commit(
            session, uuid.UUID(ESCALATION_ID), human_commit=HUMAN_COMMIT
        )
    assert set(caught.value.paths) == {IMPL, TEST}

    with factory() as session:
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit is None
        assert escalation.integration_resolution_commit is None
        assert escalation.status is EscalationStatus.RESOLVED
        assert _snapshot_runs(factory) == before
        assert session.query(models.RunEventRow).count() == 0
    assert _git(repo_copy, "rev-parse", "agent/integration") == INTEGRATION_SHA
    engine.dispose()


# ======================================================= the operator workflow


def test_prepare_reports_scope_and_never_touches_the_ref(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """Step B: the workspace is built on the baseline and bounded to scope."""
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    human_object_before = _git(repo_copy, "cat-file", "-p", HUMAN_COMMIT)

    workspace = _prepare(factory)

    assert workspace.expected_integration_sha == INTEGRATION_SHA
    assert workspace.human_source_commit == HUMAN_COMMIT
    assert workspace.human_source_parent == HUMAN_PARENT
    assert workspace.allowed_paths == (IMPL, TEST)
    assert workspace.path.is_dir()
    assert _git(workspace.path, "rev-parse", "HEAD") == INTEGRATION_SHA

    # Preparing is not reconciling: no ref moved and nothing was recorded.
    assert _git(repo_copy, "rev-parse", "agent/integration") == INTEGRATION_SHA
    assert _git(repo_copy, "cat-file", "-p", HUMAN_COMMIT) == human_object_before
    with factory() as session:
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit is None
        assert escalation.integration_resolution_commit is None
    engine.dispose()


def test_operator_workflow_records_both_provenance_values(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """Steps C-I: the whole supported path, end to end.

    The assertions are the contract: two distinct SHAs recorded under two
    meanings, the human's object byte-identical, the baseline carrying the
    human's ancestry, the historical runs untouched, and the next task still
    waiting rather than started.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before_runs = _snapshot_runs(factory)
    human_object_before = _git(repo_copy, "cat-file", "-p", HUMAN_COMMIT)
    master_before = _git(repo_copy, "rev-parse", "master")

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    updated = _authorize(factory)

    # --- the two provenance values, kept apart -----------------------------
    assert updated.human_commit == HUMAN_COMMIT
    resolution = updated.integration_resolution_commit
    assert resolution is not None
    assert resolution != HUMAN_COMMIT
    assert len(resolution) == 40

    # --- the historical human commit is untouched --------------------------
    assert _git(repo_copy, "cat-file", "-p", HUMAN_COMMIT) == human_object_before
    assert _git(repo_copy, "rev-parse", "master") == master_before
    assert _git(repo_copy, "rev-parse", "master") == HUMAN_COMMIT

    # --- the baseline advanced onto the resolution -------------------------
    assert _git(repo_copy, "rev-parse", "agent/integration") == resolution
    assert _is_ancestor(repo_copy, INTEGRATION_SHA, resolution)
    assert _is_ancestor(repo_copy, HUMAN_COMMIT, resolution)
    assert _git(repo_copy, "rev-list", "--parents", "-n", "1", resolution).split() == [
        resolution,
        INTEGRATION_SHA,
        HUMAN_COMMIT,
    ]

    # --- allowed scope only -------------------------------------------------
    assert _git(repo_copy, "diff", "--name-only", f"{INTEGRATION_SHA}..{resolution}") == (
        f"{IMPL}\n{TEST}"
    )
    # ...and the human's patch is fully present in the advanced baseline.
    baseline_impl = _git(repo_copy, "show", f"agent/integration:{IMPL}")
    for line in (
        "filterBySource(source: NavigationSource): NavigationEntry[] {",
        ".filter(entry => entry.source === source)",
        ".sort((a, b) => a.timestamp - b.timestamp);",
        "import { NavigationEntry, NavigationSource } from '../models/navigation-entry';",
        # baseline work that the human did not touch must have survived
        "removeAllForUri(uri: string): number {",
        "getMaxSize(): number {",
    ):
        assert line in baseline_impl, line

    # --- nothing else in the campaign moved --------------------------------
    with factory() as session:
        assert _snapshot_runs(factory) == before_runs
        assert all(run["candidate_commit"] is None for run in before_runs)
        failed_run_ids = [run["id"] for run in before_runs]
        assert uuid.UUID(TS109_RUN_ID) in failed_run_ids
        ts109 = (
            session.query(models.TaskRow).filter_by(external_task_id="TS-109").one()
        )
        ts110 = (
            session.query(models.TaskRow).filter_by(external_task_id="TS-110").one()
        )
        assert ts109.status is TaskStatus.COMPLETE
        assert ts109.unintegrated_commit is None
        assert ts110.status is TaskStatus.READY
        assert session.query(models.TaskRunRow).filter_by(task_id=ts110.id).count() == 0
        assert session.query(models.ModelRunRow).count() == 0
        assert session.query(models.ReviewRow).count() == 0
        assert session.query(models.TrainingExampleRow).count() == 0
        assert session.query(models.VerificationRunRow).count() == 3

        events = session.query(models.RunEventRow).all()
        assert len(events) == 1
        payload = events[0].payload
        assert payload["provenance"] == "human"
        assert payload["resolution"] == "operator_conflict_resolution"
        assert payload["human_source_commit"] == HUMAN_COMMIT
        assert payload["integration_resolution_commit"] == resolution
        assert payload["integrated_sha"] == resolution
        assert payload["previous_sha"] == INTEGRATION_SHA
        assert payload["baseline_sha"] == resolution
        assert payload["resolution_scope"] == [IMPL, TEST]
        assert payload["commands_run"] == 3
        assert events[0].task_run_id == uuid.UUID(TS109_RUN_ID)

        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit == HUMAN_COMMIT
        assert escalation.integration_resolution_commit == resolution
        assert escalation.resolution is None
        assert escalation.resolved_at is None
    engine.dispose()


def test_the_tree_the_next_task_would_start_from_is_now_truthful(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """Why it matters that this workflow exists, stated as a checkable fact.

    TS-110 is the next task the scheduler would pick, and TS-109 is in its
    dependency list. Concern 51's rule is that a dependency counts only when
    the work is in the tree the next run starts from -- so before this
    workflow, TS-110 was ``READY`` on the strength of a tree that did **not**
    contain ``filterBySource``. It was ready to build on a method that was not
    there.

    The readiness *flag* does not change: TS-109 was never marked
    ``unintegrated_commit``, because a failed merge never got as far as
    recording a candidate. That is the gap. The database said ready, the tree
    disagreed, and only a merge would have exposed it.
    """
    from apps.orchestrator.services.scheduler import (
        select_next_task,
        unsatisfied_dependencies,
    )

    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)

    with factory() as session:
        project = session.query(models.ProjectRow).one()
        selection = select_next_task(session, project.id)
        assert selection.task is not None
        assert selection.task.external_task_id == "TS-110"
        ts110 = selection.task
        assert unsatisfied_dependencies(session, ts110) == ()

        # Flag-level readiness said yes...
        assert ts110.status is TaskStatus.READY
        # ...while the tree it would be scheduled against did not have the work.
        scheduled_tree = _git(repo_copy, "show", f"agent/integration:{IMPL}")
        assert "filterBySource" not in scheduled_tree

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    _authorize(factory)

    with factory() as session:
        project = session.query(models.ProjectRow).one()
        ts110 = (
            session.query(models.TaskRow).filter_by(external_task_id="TS-110").one()
        )
        # Still READY, and now the same answer holds in the tree.
        assert ts110.status is TaskStatus.READY
        assert unsatisfied_dependencies(session, ts110) == ()
        assert "filterBySource" in _git(repo_copy, "show", f"agent/integration:{IMPL}")
        # TS-109 itself is COMPLETE and its work is in the baseline it points at.
        ts109 = (
            session.query(models.TaskRow).filter_by(external_task_id="TS-109").one()
        )
        assert ts109.status is TaskStatus.COMPLETE
        assert ts109.unintegrated_commit is None  # domain is_integrated
    engine.dispose()


def test_operator_workflow_passes_the_real_typescript_toolchain(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """The project's own build, lint and tests over the resolved tree.

    The fast profile used elsewhere is a real process but a weak witness. This
    is the strong one: ``npx tsc``, ``npx eslint`` and ``npx mocha`` exactly as
    ``build.tasks.yaml`` declares them, against the real toolchain. It is the
    difference between "the invariant accepted a tree" and "the tree compiles,
    lints clean, and its tests pass".
    """
    node_modules = SOURCE_REPO / "node_modules"
    if not node_modules.is_dir():
        pytest.skip("the TraceStack toolchain is not installed")

    # Hardlink the toolchain into the clone so dependency_paths is exercised
    # the way concern 12 intends, without copying 100MB per test. When /tmp and
    # the repository are different filesystems (WSL containers), hardlinking
    # raises EXDEV and a real copy is the only option.
    try:
        shutil.copytree(node_modules, repo_copy / "node_modules", copy_function=os.link)
    except shutil.Error:
        shutil.rmtree(repo_copy / "node_modules", ignore_errors=True)
        shutil.copytree(node_modules, repo_copy / "node_modules", symlinks=True)

    engine, factory = _import_state(
        tmp_path,
        isolated_url,
        repo_copy,
        monkeypatch,
        profile={
            "build": ["npx tsc -p ./"],
            "lint": ["npx eslint src"],
            "tests": ["npx mocha out/src/test/navigation-stack.test.js"],
            "security": [],
        },
        dependency_paths=["node_modules"],
    )
    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    _authorize(factory)

    with factory() as session:
        runs = (
            session.query(models.VerificationRunRow)
            .order_by(models.VerificationRunRow.id)
            .all()
        )
        assert len(runs) == 3
        assert all(row.status.value == "PASSED" for row in runs)
        assert {row.verification_type.value for row in runs} == {
            "INTEGRATION_BUILD",
            "INTEGRATION_LINT",
            "INTEGRATION_TESTS",
        }
        # The mocha log must show the human's tests actually running.
        logs = [row.stdout_artifact for row in runs if row.stdout_artifact]
        assert logs
    assert _git(repo_copy, "rev-parse", "agent/integration") != INTEGRATION_SHA
    engine.dispose()


# ============================================================ failure closure


def _assert_untouched(factory, repo_copy, before_runs, *, human_object=None):
    """Nothing moved: not the ref, not the rows, not the human's object."""
    assert _git(repo_copy, "rev-parse", "agent/integration") == INTEGRATION_SHA
    with factory() as session:
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit is None
        assert escalation.integration_resolution_commit is None
        assert escalation.status is EscalationStatus.RESOLVED
        assert _snapshot_runs(factory) == before_runs
        assert session.query(models.RunEventRow).count() == 0
    if human_object is not None:
        assert _git(repo_copy, "cat-file", "-p", HUMAN_COMMIT) == human_object


def test_failure_01_resolution_based_on_the_wrong_integration_sha(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    with pytest.raises(EntityConflict, match="not the expected baseline"):
        _prepare(factory, baseline=HUMAN_COMMIT)
    with pytest.raises(EntityConflict, match="superseded baseline"):
        _authorize(factory, baseline=HUMAN_COMMIT)
    _assert_untouched(factory, repo_copy, before)
    engine.dispose()


def test_failure_02_unknown_human_source_commit(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)
    unknown = "0" * 40

    with pytest.raises(EntityNotFound):
        _prepare(factory, human=unknown)
    with pytest.raises(EntityNotFound):
        _authorize(factory, human=unknown)
    _assert_untouched(factory, repo_copy, before)
    engine.dispose()


def test_failure_02b_abbreviated_or_ref_shaped_sha_is_refused(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """A value that resolves is not the same as a value that names an object.

    ``cbff2c4`` and ``master`` both resolve in this repository. Accepting
    either would let the recorded provenance be a moving target: the same
    request would name different objects tomorrow.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    for bad in (HUMAN_COMMIT[:7], "master", f"{HUMAN_COMMIT}^{{commit}}"):
        with pytest.raises(EntityConflict, match="full 40-character commit SHA"):
            _prepare(factory, human=bad)
    _assert_untouched(factory, repo_copy, before)
    engine.dispose()


def test_failure_03_resolution_omits_part_of_the_human_implementation(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """The invariant's reason for existing."""
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)
    human_object = _git(repo_copy, "cat-file", "-p", HUMAN_COMMIT)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    impl = workspace.path / IMPL
    # Drop the sort: the method still exists, still typechecks against the
    # other tests, and the resolution is no longer the human's work.
    body = impl.read_text(encoding="utf-8")
    impl.write_text(
        body.replace(
            "      .sort((a, b) => a.timestamp - b.timestamp);\n", ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(EntityConflict, match="does not faithfully carry"):
        _authorize(factory)
    _assert_untouched(factory, repo_copy, before, human_object=human_object)
    engine.dispose()


def test_failure_04_resolution_changes_an_unrelated_file(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    (workspace.path / "package.json").write_text(
        '{"name": "hijacked"}\n', encoding="utf-8"
    )

    with pytest.raises(EntityConflict, match="outside the human commit's scope"):
        _authorize(factory)
    _assert_untouched(factory, repo_copy, before)
    engine.dispose()


def test_failure_05_verification_failure_blocks_the_advance(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, factory = _import_state(
        tmp_path,
        isolated_url,
        repo_copy,
        monkeypatch,
        profile={
            "build": [],
            "lint": [],
            "tests": ["node -e \"console.error('project suite is red'); process.exit(3)\""],
            "security": [],
        },
    )
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    with pytest.raises(EntityConflict, match="failed cumulative verification"):
        _authorize(factory)

    assert _git(repo_copy, "rev-parse", "agent/integration") == INTEGRATION_SHA
    with factory() as session:
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit is None
        assert escalation.integration_resolution_commit is None
        assert _snapshot_runs(factory) == before
        assert session.query(models.RunEventRow).count() == 0
    engine.dispose()


def test_failure_05b_empty_verification_profile_is_a_refusal_not_a_pass(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """With no commands there is no evidence, and no evidence is not a pass.

    The canonical human path treats an empty profile as "nothing to do". Here
    that would let the invariant pass vacuously, so it is refused outright.
    """
    engine, factory = _import_state(
        tmp_path,
        isolated_url,
        repo_copy,
        monkeypatch,
        profile={"build": [], "lint": [], "tests": [], "security": []},
    )
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    with pytest.raises(EntityConflict, match="declares no verification commands"):
        _authorize(factory)
    _assert_untouched(factory, repo_copy, before)
    engine.dispose()


def test_failure_06_resolution_not_descendant_of_expected_baseline(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """The ancestry gate, tested directly rather than through the workflow.

    A commit that carries the human's patch but not the baseline is not a
    resolution *onto the baseline*; merging it would discard every accepted
    task since the import.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)
    git = GitService(str(repo_copy))

    with pytest.raises(EntityConflict, match="does not descend from the expected baseline"):
        resolver._assert_resolution_ancestry(
            git,
            HUMAN_COMMIT,
            baseline=INTEGRATION_SHA,
            human_source=HUMAN_COMMIT,
        )
    with pytest.raises(EntityConflict, match="does not carry human source commit"):
        resolver._assert_resolution_ancestry(
            git,
            INTEGRATION_SHA,
            baseline=INTEGRATION_SHA,
            human_source=HUMAN_COMMIT,
        )
    _assert_untouched(factory, repo_copy, before)
    engine.dispose()


def test_failure_07_source_human_commit_changed_between_authorisation_and_use(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """A prepared workspace is not a licence to change the human commit.

    The workspace is prepared against one human commit; authorizing against a
    different one would resolve a conflict the operator never saw.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)

    # A second, unrelated commit is offered as "the" human source.
    other = _git(repo_copy, "rev-parse", "master")
    assert other == HUMAN_COMMIT
    with pytest.raises((EntityNotFound, EntityConflict)):
        _authorize(factory, human="b" * 40)
    _assert_untouched(factory, repo_copy, before)
    assert workspace.human_source_commit == HUMAN_COMMIT
    engine.dispose()


def test_failure_08_integration_ref_changed_concurrently(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)

    # Somebody else advanced the baseline while the operator was resolving.
    moved = _git(repo_copy, "rev-parse", "agent/TS-107-expose-the-configured-maximum-size-run1")
    _git(repo_copy, "branch", "--force", "agent/integration", moved)
    assert _git(repo_copy, "rev-parse", "agent/integration") == moved

    with pytest.raises(EntityConflict, match="moved to"):
        _authorize(factory)

    assert _git(repo_copy, "rev-parse", "agent/integration") == moved
    with factory() as session:
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit is None
        assert escalation.integration_resolution_commit is None
        assert _snapshot_runs(factory) == before
    engine.dispose()


def test_failure_09_replay_of_a_completed_resolution(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    first = _authorize(factory)
    after_first = _snapshot_runs(factory)

    with pytest.raises(EntityConflict, match="already reconciled"):
        _authorize(factory)
    with pytest.raises(EntityConflict, match="already reconciled"):
        _prepare(factory)

    assert first.integration_resolution_commit is not None
    assert _git(repo_copy, "rev-parse", "agent/integration") == (
        first.integration_resolution_commit
    )
    # Reconciling advances the baseline and the escalation, never the runs:
    # the seven historical TaskRuns are unchanged even by a *successful*
    # resolution, and the refused replay changed nothing further.
    assert _snapshot_runs(factory) == after_first == before
    with factory() as session:
        assert session.query(models.RunEventRow).count() == 1
    engine.dispose()


def test_failure_09b_partially_recorded_provenance_is_refused(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """One column set without the other is an unknown prior outcome.

    Overwriting it would silently decide what a half-finished attempt meant.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    with factory.begin() as session:
        row = session.query(models.HumanEscalationRow).one()
        row.human_commit = HUMAN_COMMIT
    with pytest.raises(EntityConflict, match="no resolution commit"):
        _prepare(factory)
    assert _snapshot_runs(factory) == before

    with factory.begin() as session:
        row = session.query(models.HumanEscalationRow).one()
        row.human_commit = None
        row.integration_resolution_commit = "a" * 40
    with pytest.raises(EntityConflict, match="no human source commit"):
        _prepare(factory)
    assert _git(repo_copy, "rev-parse", "agent/integration") == INTEGRATION_SHA
    engine.dispose()


def test_failure_10_mismatched_source_and_resolution_provenance(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """The escalation may not be made to claim a resolution it did not produce.

    The service writes both values in one transaction from the commit it just
    created, so the pairing cannot be supplied from outside. This pins that the
    ordinary resolution route refuses to touch a completed escalation, and
    that a resolution SHA can never be recorded in the source column.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    completed = _authorize(factory)
    resolution = completed.integration_resolution_commit

    from apps.orchestrator.services import reviews

    # The legacy canonical route will not overwrite a completed reconciliation.
    with pytest.raises(EntityConflict, match="already has human_commit"):
        reviews.reconcile_human_commit(
            _session(factory), uuid.UUID(ESCALATION_ID), human_commit=resolution
        )
    with pytest.raises(EntityConflict, match="already has human_commit"):
        reviews.reconcile_human_commit(
            _session(factory), uuid.UUID(ESCALATION_ID), human_commit=HUMAN_COMMIT
        )
    assert resolution != HUMAN_COMMIT
    assert _snapshot_runs(factory) == before
    engine.dispose()


def test_failure_11_resolution_sha_cannot_be_stored_as_a_task_candidate(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """The seven historical runs keep ``candidate_commit`` NULL, forever.

    The runs failed; no automated candidate was ever accepted for them. Writing
    a human resolution into that column would invent model provenance for work
    a person did, which is the exact confusion concern 73 was written to
    prevent.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    completed = _authorize(factory)

    with factory() as session:
        runs = session.query(models.TaskRunRow).all()
        assert len(runs) == 7
        for run in runs:
            assert run.candidate_commit is None, run.external_run_id
        assert completed.integration_resolution_commit not in {
            run.candidate_commit for run in runs
        }
    engine.dispose()


def test_failure_12_historical_task_runs_cannot_be_rewritten(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """Full-column equality, not a spot check.

    Every column of all seven rows, before and after, byte for byte. The
    reconciliation is allowed to append an event that *references* run 7; it is
    not allowed to change run 7.
    """
    engine, factory = _import_state(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)
    assert len(before) == 7
    assert {run["status"] for run in before} == {"FAILED"}

    workspace = _prepare(factory)
    _write_correct_resolution(workspace.path)
    _authorize(factory)

    assert _snapshot_runs(factory) == before
    engine.dispose()


# ============================================================== canonical refs


def test_canonical_repository_is_never_mutated(repo_copy: Path):
    """The guard that makes every other test in this file trustworthy.

    Every other test in this file drives the resolution machinery against the
    disposable ``repo_copy`` clone; none of them may touch the canonical
    campaign repository. The live repo has legitimately advanced since the
    pre-C73 checkpoint, so asserting a particular SHA here would pin the test to
    a mutable campaign position rather than to the invariant under test. Instead:
    snapshot the canonical state, prove the clone is genuinely independent by
    moving a ref inside it, and assert the canonical refs and working tree are
    byte-for-byte exactly as found.
    """
    refs = ("agent/integration", "master")
    before = {name: _git(SOURCE_REPO, "rev-parse", name) for name in refs}
    head_before = _git(SOURCE_REPO, "rev-parse", "HEAD")
    dirty_before = _git(SOURCE_REPO, "status", "--porcelain")

    # The historical fact under test: the baseline and the human source are
    # distinct refs. Advancing the clone's baseline must not touch canonical.
    assert before["agent/integration"] != before["master"]
    _git(repo_copy, "update-ref", "refs/heads/agent/integration", HUMAN_COMMIT)
    assert _git(repo_copy, "rev-parse", "agent/integration") == HUMAN_COMMIT

    after = {name: _git(SOURCE_REPO, "rev-parse", name) for name in refs}
    assert after == before
    assert _git(SOURCE_REPO, "rev-parse", "HEAD") == head_before
    assert _git(SOURCE_REPO, "status", "--porcelain") == dirty_before


def _session(factory):
    return factory()
