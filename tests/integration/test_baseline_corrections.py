"""Project-level operator baseline corrections (the UI-001 lifecycle gap).

The gap these are about: an operator fixes ``build.tasks.yaml`` on ``main``,
``import-tasks`` syncs the specification into the database and ``retry`` makes
the task READY, but ``agent/integration`` still holds the old manifest -- so the
next workspace would be cut from a tree that disagrees with the database about
to drive it.

What is deliberately *not* asserted anywhere below: that any task became
COMPLETE, that a run was created, or that a task's ``unintegrated_commit`` was
cleared. Several tests assert the opposite, because recording a specification
fix as task output is the failure mode this operation exists to avoid.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.db.models import RunEventRow, TaskRow, TaskRunRow
from apps.orchestrator.domain.enums import RunEventType, TaskStatus
from apps.orchestrator.domain.errors import ManifestError
from apps.orchestrator.domain.git import INTEGRATION_BRANCH
from apps.orchestrator.domain.models import Project
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services.baseline_correction import (
    ALLOWED_CORRECTION_PATHS,
    apply_baseline_correction,
)
from apps.orchestrator.services.errors import (
    EntityConflict,
    EntityNotFound,
    LockWaitTimeout,
)
from apps.orchestrator.services.git_service import GitService, MergeConflict
from apps.orchestrator.services.manifest_loader import MANIFEST_FILENAME
from apps.orchestrator.services.task_importer import import_manifest
from tests.conftest import run_git

pytestmark = pytest.mark.integration


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def correction_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
    )


@pytest.fixture
def spec_repo(tmp_path: Path, manifest_document: dict) -> Path:
    """A managed repository whose baseline is behind ``main``.

    ``agent/integration`` is pinned at the first commit on purpose: that is the
    state the gap occurs in, and leaving the branch absent would let
    ``ensure_integration_branch`` create it at whatever ``main`` points to,
    which would silently include the correction being tested.
    """
    repository = tmp_path / "project"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    document = copy.deepcopy(manifest_document)
    document["project"]["repository"] = str(repository)
    (repository / MANIFEST_FILENAME).write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")
    run_git(repository, "branch", INTEGRATION_BRANCH)
    return repository


@pytest.fixture
def project(session: Session, spec_repo: Path, correction_settings: Settings) -> Project:
    """The project and its tasks, imported as ``import-tasks`` would.

    TS-001 is then put into FAILED, which is the runtime state the UI-001
    sequence had and the one a correction must not disturb.
    """
    manifest = _manifest_of(spec_repo)
    report = import_manifest(session, manifest)
    project = ProjectRepository(session).get(report.project_id)
    assert project is not None
    first = next(
        task
        for task in TaskRepository(session).list_for_project(project.id)
        if task.external_task_id == "TS-001"
    )
    # Written directly: the point is the durable runtime state a correction
    # must preserve, not the lifecycle path that produced it.
    session.execute(update(TaskRow).where(TaskRow.id == first.id).values(status=TaskStatus.FAILED))
    return project


def _manifest_of(repository: Path):
    from apps.orchestrator.services.manifest_loader import load_repository_manifest

    return load_repository_manifest(repository)


def _git(repository: Path, settings: Settings) -> GitService:
    return GitService(repository, default_branch="main", settings=settings)


def _baseline(repository: Path, settings: Settings) -> str:
    return _git(repository, settings).resolve_sha(INTEGRATION_BRANCH)


def _write_manifest(repository: Path, mutate) -> None:
    document = yaml.safe_load((repository / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    mutate(document)
    (repository / MANIFEST_FILENAME).write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def _commit_correction(repository: Path, mutate, *, message: str = "Correct TS-002 spec") -> str:
    """The operator's correction: edit the manifest on ``main`` and commit."""
    _write_manifest(repository, mutate)
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", message)
    return run_git(repository, "rev-parse", "HEAD").strip()


def _retitle(title: str):
    def mutate(document: dict) -> None:
        for task in document["tasks"]:
            if task["id"] == "TS-002":
                task["title"] = title

    return mutate


def _task(session: Session, project: Project, external_id: str):
    return next(
        task
        for task in TaskRepository(session).list_for_project(project.id)
        if task.external_task_id == external_id
    )


def _counts(session: Session) -> tuple[int, int]:
    """Task runs, and baseline-correction events. Both are provenance claims."""
    runs = session.scalar(select(func.count()).select_from(TaskRunRow)) or 0
    events = (
        session.scalar(
            select(func.count())
            .select_from(RunEventRow)
            .where(RunEventRow.event_type == RunEventType.BASELINE_CORRECTION_APPLIED.value)
        )
        or 0
    )
    return runs, events


# --- the successful correction ------------------------------------------------


def test_a_manifest_only_correction_advances_the_baseline_and_resyncs_specs(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """The whole lifecycle the gap was blocking, in one call."""
    before = _baseline(spec_repo, correction_settings)
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    assert _task(session, project, "TS-002").title == "Navigation core"

    result = apply_baseline_correction(
        session,
        project.id,
        commit_sha=correction,
        reason="UI-001 declared .gitignore writable",
        requested_by="operator@example.com",
        settings=correction_settings,
    )

    assert result.applied is True and result.already_applied is False
    assert result.previous_sha == before
    assert result.changed_paths == (MANIFEST_FILENAME,)
    # The baseline moved, and it now contains the correction.
    after = _baseline(spec_repo, correction_settings)
    assert after != before
    assert after == result.baseline_sha == result.merged_sha
    assert _git(spec_repo, correction_settings).contains_commit(
        correction, ref=INTEGRATION_BRANCH
    )
    # The specification the next run would be driven by was re-synchronised.
    assert _task(session, project, "TS-002").title == "Navigation core, corrected"
    assert result.import_report is not None
    assert "TS-002" in result.import_report.updated
    # And the corrected manifest is what a workspace cut from the new baseline
    # would actually read.
    merged_manifest = _git(spec_repo, correction_settings).read_file_at(
        INTEGRATION_BRANCH, MANIFEST_FILENAME
    )
    assert "Navigation core, corrected" in merged_manifest


def test_the_correction_does_not_complete_a_task_or_create_a_run(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 3. A specification fix is not task output.

    TS-001 is FAILED and TS-002 is pending. Neither may move, no run may
    appear, and nothing may claim a task was finished.
    """
    runs_before, _ = _counts(session)
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))

    apply_baseline_correction(
        session,
        project.id,
        commit_sha=correction,
        reason="manifest fix",
        requested_by="operator",
        settings=correction_settings,
    )

    # The FAILED runtime status is preserved: task_importer deliberately keeps
    # ``status`` out of the fields a manifest owns.
    assert _task(session, project, "TS-001").status is TaskStatus.FAILED
    # TS-002 is demoted to BLOCKED -- not by anything this operation does, but
    # by ``refresh_readiness`` inside ``import_manifest``, because its
    # dependency TS-001 is FAILED. That is the existing rule, and the identical
    # thing POST /import-tasks would have done; asserted here so the demotion
    # is not later mistaken for correction-specific status handling.
    assert _task(session, project, "TS-002").status is TaskStatus.BLOCKED
    # No task was completed and no integration state was invented for one.
    tasks = TaskRepository(session).list_for_project(project.id)
    assert all(task.status is not TaskStatus.COMPLETE for task in tasks)
    assert all(task.unintegrated_commit is None for task in tasks)
    # No fake run, and no task-scoped provenance.
    runs_after, _ = _counts(session)
    assert runs_after == runs_before == 0
    task_scoped = session.scalar(
        select(func.count())
        .select_from(RunEventRow)
        .where(RunEventRow.event_type == RunEventType.INTEGRATION_ADVANCED.value)
    )
    assert task_scoped == 0


def test_the_correction_records_project_scoped_provenance(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 13, and that the provenance is about the project only."""
    before = _baseline(spec_repo, correction_settings)
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))

    result = apply_baseline_correction(
        session,
        project.id,
        commit_sha=correction,
        reason="UI-001 manifest declared .gitignore writable",
        requested_by="operator@example.com",
        settings=correction_settings,
    )

    row = session.get(RunEventRow, result.event_id)
    assert row is not None
    assert row.event_type == RunEventType.BASELINE_CORRECTION_APPLIED.value
    # Project-scoped: no run and no task, so this cannot be read as either
    # having produced the correction.
    assert row.project_id == project.id
    assert row.task_run_id is None
    assert row.task_id is None
    payload = row.payload
    assert payload["previous_sha"] == before
    assert payload["correction_sha"] == result.correction_sha
    assert payload["merged_sha"] == result.baseline_sha
    assert payload["reason"] == "UI-001 manifest declared .gitignore writable"
    assert payload["requested_by"] == "operator@example.com"
    assert payload["changed_paths"] == [MANIFEST_FILENAME]
    assert payload["provenance"] == "operator_specification_correction"


# --- replay -------------------------------------------------------------------


def test_replaying_an_applied_correction_is_truthful_and_changes_nothing(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 15. The second call reports the truth and writes nothing."""
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    first = apply_baseline_correction(
        session,
        project.id,
        commit_sha=correction,
        reason="manifest fix",
        requested_by="operator",
        settings=correction_settings,
    )
    runs_before, events_before = _counts(session)

    second = apply_baseline_correction(
        session,
        project.id,
        commit_sha=correction,
        reason="manifest fix",
        requested_by="operator",
        settings=correction_settings,
    )

    assert second.already_applied is True and second.applied is False
    # Truthful: the baseline is where the first call left it, and did not move.
    assert second.previous_sha == second.baseline_sha == first.baseline_sha
    assert second.merged_sha is None
    assert second.event_id is None and second.import_report is None
    # No duplicate provenance, no new run, no task state change.
    assert _counts(session) == (runs_before, events_before)
    assert events_before == 1
    assert _task(session, project, "TS-001").status is TaskStatus.FAILED


# --- fail-closed refusals -----------------------------------------------------


def _assert_nothing_moved(
    session: Session,
    project: Project,
    spec_repo: Path,
    settings: Settings,
    *,
    baseline: str,
    title: str = "Navigation core",
) -> None:
    """The baseline, the specifications and the runtime state are all intact."""
    assert _baseline(spec_repo, settings) == baseline
    assert _task(session, project, "TS-002").title == title
    assert _task(session, project, "TS-001").status is TaskStatus.FAILED
    assert _counts(session) == (0, 0)


def test_a_missing_commit_is_refused_and_nothing_moves(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(EntityNotFound):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha="0" * 40,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    _assert_nothing_moved(
        session, project, spec_repo, correction_settings, baseline=baseline
    )


def test_a_correction_not_based_on_the_baseline_is_refused(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 7. The baseline must already contain the correction's parent.

    An intervening commit on ``main`` that the baseline never accepted is
    exactly the stale-base case: merging the correction as it stands would drag
    that unaccepted commit in with it.
    """
    (spec_repo / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    run_git(spec_repo, "add", "-A")
    run_git(spec_repo, "commit", "--quiet", "-m", "unaccepted source change")
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(EntityConflict, match="does not contain"):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    _assert_nothing_moved(
        session, project, spec_repo, correction_settings, baseline=baseline
    )


def test_a_correction_touching_source_code_is_refused(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 8. The allowed set is the manifest and nothing else."""
    _write_manifest(spec_repo, _retitle("Navigation core, corrected"))
    (spec_repo / "src" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    run_git(spec_repo, "add", "-A")
    run_git(spec_repo, "commit", "--quiet", "-m", "manifest fix plus code")
    correction = run_git(spec_repo, "rev-parse", "HEAD").strip()
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(EntityConflict, match="src/app.py"):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    _assert_nothing_moved(
        session, project, spec_repo, correction_settings, baseline=baseline
    )
    assert {MANIFEST_FILENAME} == ALLOWED_CORRECTION_PATHS


def test_an_invalid_corrected_manifest_is_refused_before_the_baseline_moves(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirements 9 and 14. Validation happens on the merged tree."""

    def break_it(document: dict) -> None:
        document["tasks"][1]["depends_on"] = ["TS-DOES-NOT-EXIST"]

    correction = _commit_correction(spec_repo, break_it)
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(ManifestError):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    _assert_nothing_moved(
        session, project, spec_repo, correction_settings, baseline=baseline
    )


def test_a_merge_conflict_leaves_the_baseline_unchanged(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 14. A conflict is a refusal, not a partial advance.

    The baseline is given its own edit to the same manifest lines first, so the
    conflict is a real Git conflict rather than a simulated one.
    """
    run_git(spec_repo, "checkout", "--quiet", INTEGRATION_BRANCH)
    _write_manifest(spec_repo, _retitle("Navigation core, baseline edit"))
    run_git(spec_repo, "add", "-A")
    run_git(spec_repo, "commit", "--quiet", "-m", "baseline manifest edit")
    run_git(spec_repo, "checkout", "--quiet", "main")
    correction = _commit_correction(spec_repo, _retitle("Navigation core, operator edit"))
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(MergeConflict):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    assert _baseline(spec_repo, correction_settings) == baseline
    assert _task(session, project, "TS-002").title == "Navigation core"
    assert _task(session, project, "TS-001").status is TaskStatus.FAILED
    assert _counts(session) == (0, 0)


def test_a_merge_commit_is_refused(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """A merge commit has no single diff to scope, so it cannot be a correction."""
    run_git(spec_repo, "checkout", "--quiet", "-b", "side")
    _write_manifest(spec_repo, _retitle("Navigation core, side"))
    run_git(spec_repo, "add", "-A")
    run_git(spec_repo, "commit", "--quiet", "-m", "side edit")
    run_git(spec_repo, "checkout", "--quiet", "main")
    (spec_repo / "README.md").write_text("# project\n", encoding="utf-8")
    run_git(spec_repo, "add", "-A")
    run_git(spec_repo, "commit", "--quiet", "-m", "main edit")
    run_git(spec_repo, "merge", "--quiet", "--no-ff", "-m", "merge side", "side")
    merge_commit = run_git(spec_repo, "rev-parse", "HEAD").strip()
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(EntityConflict, match="merge commit"):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=merge_commit,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    assert _baseline(spec_repo, correction_settings) == baseline
    assert _counts(session) == (0, 0)


def test_an_empty_reason_or_operator_identity_is_refused(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings
):
    """Requirement 2. Both provenance fields are mandatory."""
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    baseline = _baseline(spec_repo, correction_settings)

    with pytest.raises(ValueError, match="reason"):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="   ",
            requested_by="operator",
            settings=correction_settings,
        )
    with pytest.raises(ValueError, match="requested_by"):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="manifest fix",
            requested_by="",
            settings=correction_settings,
        )

    _assert_nothing_moved(
        session, project, spec_repo, correction_settings, baseline=baseline
    )


def test_an_unknown_project_is_refused(
    session: Session, spec_repo: Path, correction_settings: Settings
):
    import uuid

    with pytest.raises(EntityNotFound):
        apply_baseline_correction(
            session,
            uuid.uuid4(),
            commit_sha="HEAD",
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )


# --- the project lock ---------------------------------------------------------


def test_the_project_lock_is_taken_before_the_shared_worktree_is_touched(
    session: Session,
    project: Project,
    spec_repo: Path,
    correction_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Requirement 4, and what a lost lock must leave behind.

    Deterministic rather than threaded, following the concern 78 certification
    locking tests: the default test engine is SQLite, where
    ``with_for_update()`` is a no-op, so real threads would prove nothing about
    the lock. What is worth pinning is the logic the lock protects -- that it is
    taken at all, that it is taken before the shared integration worktree is
    reached, and that failing to get it advances nothing.
    """
    from apps.orchestrator.services import baseline_correction as module

    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    baseline = _baseline(spec_repo, correction_settings)
    worktree_root = correction_settings.worktree_root
    calls: list[str] = []

    def refuse(self, project_id):  # noqa: ANN001 - monkeypatched repository method
        calls.append("lock")
        raise LockWaitTimeout("another integration holds the project")

    monkeypatch.setattr(ProjectRepository, "lock", refuse)
    original = module._usable_or_recreated_integration_worktree

    def record_worktree(*args, **kwargs):
        calls.append("worktree")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_usable_or_recreated_integration_worktree", record_worktree)

    with pytest.raises(LockWaitTimeout):
        apply_baseline_correction(
            session,
            project.id,
            commit_sha=correction,
            reason="manifest fix",
            requested_by="operator",
            settings=correction_settings,
        )

    # The lock was attempted, and the shared worktree was never reached.
    assert calls == ["lock"]
    assert not worktree_root.exists() or not any(worktree_root.iterdir())
    _assert_nothing_moved(
        session, project, spec_repo, correction_settings, baseline=baseline
    )


def test_the_lock_is_taken_on_the_project_row(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """The lock identity is this project's row, so unrelated projects never wait."""
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    locked: list = []
    original = ProjectRepository.lock

    def record(self, project_id):  # noqa: ANN001 - monkeypatched repository method
        locked.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectRepository, "lock", record)

    apply_baseline_correction(
        session,
        project.id,
        commit_sha=correction,
        reason="manifest fix",
        requested_by="operator",
        settings=correction_settings,
    )

    assert locked == [project.id]


# --- the API contract ---------------------------------------------------------


@pytest.fixture
def client(session: Session, correction_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    from collections.abc import Iterator  # noqa: F401 - documents the generator

    from fastapi.testclient import TestClient

    from apps.orchestrator.config import get_settings
    from apps.orchestrator.db.session import get_db
    from apps.orchestrator.main import create_app

    monkeypatch.setenv("ARTIFACT_ROOT", str(correction_settings.artifact_root))
    monkeypatch.setenv("WORKTREE_ROOT", str(correction_settings.worktree_root))
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_the_endpoint_applies_a_correction_and_then_reports_the_replay(
    session: Session, project: Project, spec_repo: Path, correction_settings: Settings, client
):
    """The contract: 200 with the full provenance, then a truthful replay."""
    correction = _commit_correction(spec_repo, _retitle("Navigation core, corrected"))
    body = {
        "commit_sha": correction,
        "reason": "UI-001 declared .gitignore writable",
        "requested_by": "operator@example.com",
    }

    first = client.post(f"/projects/{project.id}/baseline-corrections", json=body)

    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["applied"] is True and payload["already_applied"] is False
    assert payload["changed_paths"] == [MANIFEST_FILENAME]
    assert payload["baseline_sha"] == _baseline(spec_repo, correction_settings)
    assert payload["event_id"]
    assert "TS-002" in payload["tasks"]["updated"]

    second = client.post(f"/projects/{project.id}/baseline-corrections", json=body)

    assert second.status_code == 200, second.text
    replay = second.json()
    assert replay["applied"] is False and replay["already_applied"] is True
    assert replay["event_id"] is None and replay["tasks"] is None


def test_the_endpoint_maps_refusals_to_their_status_codes(
    session: Session, project: Project, spec_repo: Path, client
):
    """Each fail-closed reason reaches the caller as the right status."""
    missing = client.post(
        f"/projects/{project.id}/baseline-corrections",
        json={"commit_sha": "0" * 40, "reason": "r", "requested_by": "o"},
    )
    assert missing.status_code == 404, missing.text

    _write_manifest(spec_repo, _retitle("Navigation core, corrected"))
    (spec_repo / "src" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
    run_git(spec_repo, "add", "-A")
    run_git(spec_repo, "commit", "--quiet", "-m", "manifest plus code")
    disallowed = client.post(
        f"/projects/{project.id}/baseline-corrections",
        json={
            "commit_sha": run_git(spec_repo, "rev-parse", "HEAD").strip(),
            "reason": "r",
            "requested_by": "o",
        },
    )
    assert disallowed.status_code == 409, disallowed.text

    # Both provenance fields are required by the schema, so an empty one is a
    # request-validation failure rather than a service refusal.
    empty = client.post(
        f"/projects/{project.id}/baseline-corrections",
        json={"commit_sha": "HEAD", "reason": "", "requested_by": "o"},
    )
    assert empty.status_code == 422, empty.text
