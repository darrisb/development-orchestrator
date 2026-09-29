"""Concern 73 compatibility of reconstructed campaign state.

The reconstruction importer claims the pre-C73 state it creates is accepted
by the historical human-commit reconciliation contract. This test proves it
against disposable state only:

* a fresh isolated SQLite database (never the runtime `orchestrator` DB),
* a disposable clone of the TraceStack repository (never the canonical refs).

Two facts hold the contract to account:

A. state acceptance: every concern 73 pre-integration gate (escalation
   RESOLVED, intent COMPLETED_BY_HAND, human_commit unset, task COMPLETE,
   commit exists, not yet contained) accepts the reconstructed rows. The
   raw evidenced human commit cbff2c4 is a *sibling* of the agent baseline
   (it is built on the pre-TS-101 imported branch), so the canonical merge
   raises a genuine content conflict -- identically to what the original
   pre-loss state would have done, because the Git repository was never
   lost. Fail-closed: the reconstructed rows survive untouched.

B. the documented operator path: after an operator resolves that conflict
   in Git by merging cbff2c4 into the baseline (the modeled resolution here
   keeps the baseline file contents -- only the merge's ancestry matters to
   the state contract; content correctness and profile execution are covered
   by test_concern73.py), reconcile_human_commit accepts cbff2c4, records it
   on the escalation, and the baseline contains it.

The verification profile and dependency paths are emptied inside the
disposable database only so the test stays hermetic (no node toolchain, no
gigabyte dependency copy).
"""

from __future__ import annotations

import json
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
from apps.orchestrator.services import reconstruction_importer as importer
from apps.orchestrator.services.git_errors import MergeConflict

pytestmark = pytest.mark.integration

MANIFEST = Path("data/recovery/tracestack_reconstruction_manifest.json")
SOURCE_REPO = Path("workspace/tracestack-clean")
INTEGRATION_SHA = "fc6abc579cee88f821c5f72f00162872b2dc8326"
HUMAN_COMMIT = "cbff2c4bd919b860c73e3cb061bccff11789c37a"
ESCALATION_ID = "fc9bf9b0-9683-41fa-aaa8-f8f53567b92a"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    completed = subprocess.run(
        ("git", "merge-base", "--is-ancestor", commit, ref),
        cwd=repo,
        check=False,
    )
    return completed.returncode == 0


def _git_soft(repo: Path, *args: str) -> int:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    ).returncode


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """Disposable clone carrying the real TraceStack refs and commits."""
    clone = tmp_path / "tracestack-clean"
    subprocess.run(
        ["git", "clone", "--quiet", str(SOURCE_REPO.resolve()), str(clone)],
        check=True,
    )
    refs = _git(clone, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").splitlines()
    current = _git(clone, "rev-parse", "--abbrev-ref", "HEAD")
    for ref in refs:
        if ref.endswith("/HEAD"):
            continue
        name = ref.removeprefix("refs/remotes/origin/")
        if name == current:
            continue
        _git(clone, "branch", "--force", name, ref)
    return clone


@pytest.fixture
def isolated_url(tmp_path: Path) -> str:
    db = tmp_path / "c73_contract.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("create table alembic_version (version_num varchar(32))"))
        connection.execute(
            text("insert into alembic_version(version_num) values ('e8a3c7f21d49')")
        )
    engine.dispose()
    return f"sqlite:///{db}"


def _runner(command, **kwargs):
    if command[0] == "docker":
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, **kwargs)


def _snapshot_runs(factory) -> list[dict]:
    with factory() as session:
        rows = session.query(models.TaskRunRow).order_by(models.TaskRunRow.run_number).all()
        return [
            {
                "id": str(row.id),
                "run_number": row.run_number,
                "attempt_number": row.attempt_number,
                "review_cycle": row.review_cycle,
                "status": row.status.value,
                "failure_reason": row.failure_reason,
                "starting_commit": row.starting_commit,
                "candidate_commit": row.candidate_commit,
                "external_run_id": row.external_run_id,
            }
            for row in rows
        ]


def _import_and_point_at_copy(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    importer.import_reconstruction(
        manifest_path=MANIFEST,
        database_url=isolated_url,
        confirm_database=isolated_url.removeprefix("sqlite:///"),
        runner=_runner,
    )
    engine = create_engine(isolated_url, future=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        project = session.query(models.ProjectRow).one()
        project.repository_path = str(repo_copy)
        project.verification_profile = {"build": [], "lint": [], "tests": [], "security": []}
        project.dependency_paths = []
    disposable_settings = Settings(
        _env_file=None,
        worktree_root=tmp_path / "worktrees",
        artifact_root=tmp_path / "artifacts",
        worker_backend=WorkerBackend.SUBPROCESS,
    )
    monkeypatch.setattr(settings_module, "get_settings", lambda: disposable_settings)
    return engine, factory


def test_raw_human_commit_fails_only_at_git_content_conflict(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """A: every C73 state gate accepts the reconstruction.

    The reconciliation must progress past all escalation/task/project checks
    and fail only at the canonical Git merge, because cbff2c4 conflicts with
    the TS-101..108 baseline work (true in the original world too). Then
    fail-closed: the reconstructed state is untouched.
    """
    from apps.orchestrator.services import reviews

    engine, factory = _import_and_point_at_copy(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)
    assert not _is_ancestor(repo_copy, HUMAN_COMMIT, "agent/integration")

    with factory.begin() as session, pytest.raises(MergeConflict):
        reviews.reconcile_human_commit(
            session,
            uuid.UUID(ESCALATION_ID),
            human_commit=HUMAN_COMMIT,
        )

    with factory() as session:
        escalation = session.query(models.HumanEscalationRow).one()
        assert escalation.human_commit is None
        assert escalation.status is EscalationStatus.RESOLVED
        assert _snapshot_runs(factory) == before
        assert session.query(models.RunEventRow).count() == 0
    assert _git(repo_copy, "rev-parse", "agent/integration") == INTEGRATION_SHA
    engine.dispose()


def test_operator_resolved_merge_completes_the_contract(
    tmp_path: Path, isolated_url: str, repo_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    """B: the documented concern 73 end state is reachable from the copy.

    An operator resolves the conflict with a merge commit onto the baseline
    (the modeled resolution keeps the baseline contents: only the merge's
    ancestry is relevant to this state contract), then reconcile_human_commit
    accepts the evidenced cbff2c4, records it, and the baseline contains it.
    """
    from apps.orchestrator.services import reviews

    # Model the operator's conflict-resolved merge inside the disposable copy.
    _git(repo_copy, "checkout", "--quiet", "-B", "operator-merge-base", "agent/integration")
    assert _git_soft(repo_copy, "merge", "--no-commit", "--no-ff", HUMAN_COMMIT) != 0
    # Resolve both conflicted paths by keeping the baseline (stage-2) side;
    # the merge's *ancestry*, not its content, is what this state test pins.
    _git(repo_copy, "checkout", "--ours", "--", "src/navigation/navigation-stack.ts")
    _git(repo_copy, "checkout", "--ours", "--", "src/test/navigation-stack.test.ts")
    _git(repo_copy, "add", "-A")
    _git(repo_copy, "-c", "user.name=Operator", "-c", "user.email=operator@example.invalid",
         "commit", "--no-verify", "-m", "Merge cbff2c4 into agent/integration (resolved)")
    resolved = _git(repo_copy, "rev-parse", "HEAD")
    _git(repo_copy, "branch", "--force", "agent/integration", resolved)
    _git(repo_copy, "checkout", "--quiet", "master")
    assert _is_ancestor(repo_copy, HUMAN_COMMIT, "agent/integration")

    engine, factory = _import_and_point_at_copy(tmp_path, isolated_url, repo_copy, monkeypatch)
    before = _snapshot_runs(factory)

    with factory.begin() as session:
        updated = reviews.reconcile_human_commit(
            session,
            uuid.UUID(ESCALATION_ID),
            human_commit=HUMAN_COMMIT,
        )

    assert str(updated.id) == ESCALATION_ID
    assert updated.status is EscalationStatus.RESOLVED
    assert updated.resolution_intent == "COMPLETED_BY_HAND"
    assert updated.human_commit == HUMAN_COMMIT
    assert updated.resolved_at is None
    assert updated.resolution is None

    assert _is_ancestor(repo_copy, HUMAN_COMMIT, "agent/integration")
    assert _git(repo_copy, "rev-parse", "agent/integration") == resolved

    with factory() as session:
        # The historical seven runs are byte-for-byte unchanged.
        assert _snapshot_runs(factory) == before
        ts109 = session.query(models.TaskRow).filter_by(external_task_id="TS-109").one()
        ts110 = session.query(models.TaskRow).filter_by(external_task_id="TS-110").one()
        assert ts109.status is TaskStatus.COMPLETE
        assert ts109.unintegrated_commit is None
        assert ts110.status is TaskStatus.READY
        assert session.query(models.TaskRunRow).filter_by(task_id=ts110.id).count() == 0
        assert session.query(models.ModelRunRow).count() == 0
        assert session.query(models.ReviewRow).count() == 0
        assert session.query(models.VerificationRunRow).count() == 0
        events = session.query(models.RunEventRow).all()
        assert len(events) == 1
        assert events[0].payload["integrated_sha"] == HUMAN_COMMIT
        assert events[0].payload["provenance"] == "human"
        assert events[0].payload["baseline_sha"] == resolved
        assert events[0].task_run_id == uuid.UUID("0a77bbb6-68ca-4e84-9572-5e37552387bf")
    engine.dispose()


def test_canonical_repository_never_mutated(
    isolated_url: str,
):
    """The clone fixtures must never move the canonical refs."""
    assert _git(SOURCE_REPO, "rev-parse", "agent/integration") == INTEGRATION_SHA
    assert not _is_ancestor(SOURCE_REPO, HUMAN_COMMIT, "agent/integration")


def test_manifest_escalation_and_human_commit_are_evidenced():
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["escalation"]["id"] == ESCALATION_ID
    assert payload["escalation"]["identity_provenance"] == "EVIDENCED"
    assert payload["repository"]["human_commit"] == HUMAN_COMMIT
    fix_loop = json.loads(Path("data/runs/RUN-20260929-000002/fix-loop.json").read_text())
    assert fix_loop["escalation_id"] == ESCALATION_ID
    assert fix_loop["task_run_id"] == payload["escalation"]["task_run_id"]
