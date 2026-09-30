"""Concern 76: host/container worktree path portability and stranded delivery recovery.

Two defects, one incident.

**Portability.** A linked Git worktree stores an *absolute* administrative path in
its ``.git`` file and a matching pointer back in the main repository's
``.git/worktrees/<name>/gitdir``. When the orchestrator creates the integration
worktree through one namespace -- the container spelling ``/workspace/...`` -- and
later runs a supported operation through another -- the host spelling
``/home/...`` of the *same bind-mounted repository* -- the recorded absolute path
does not resolve, and the first real Git command fails with ``not a git
repository``. Every Python path check had already passed, so the crash surfaced
as an opaque HTTP 500 during delivery, after the candidate was committed.

**Recovery.** The crash left a run ``RUNNING`` with its task ``APPROVED``, a
committed candidate on disk, ``candidate_commit`` still ``NULL`` and no execution
owner -- exactly the state a delivery transaction that never committed produces.
Recovery must finish that owed delivery from durable evidence and Git state,
*without* a second coder call, reviewer call, attempt or ``TaskRun``.

These tests reproduce the namespace defect with **real Git on real metadata**
rather than mocking it away, and drive the recovery through the same
``assess_recoverability`` -> ``recover_run`` -> ``WorkflowRunner.run`` path the
operator endpoint uses.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db import models  # noqa: F401  (registers every table)
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import WorkflowCheckpointRow
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import (
    Complexity,
    ModelPurpose,
    ModelRole,
    ReviewDecision,
    RunEventType,
    RunStatus,
    TaskStatus,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.git import INTEGRATION_BRANCH, run_branch_name
from apps.orchestrator.domain.models import (
    Model,
    ModelRun,
    Project,
    Review,
    Task,
    TaskLimits,
    VerificationRun,
)
from apps.orchestrator.repositories import (
    ModelRepository,
    ModelRunRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services import delivery as delivery_module
from apps.orchestrator.services import integration as integration_service
from apps.orchestrator.services.errors import EntityConflict
from apps.orchestrator.services.git_errors import GitError, MergeConflict
from apps.orchestrator.services.git_service import GitService
from apps.orchestrator.services.integration import integrate_candidate
from apps.orchestrator.services.run_recovery import (
    RecoveryMode,
    assess_recoverability,
    recover_run,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.workspace import workspace_path
from apps.orchestrator.workflow import WorkflowRunner
from tests.conftest import run_git
from tests.integration.test_fix_loop import ScriptedModel
from tests.integration.test_fix_loop import reviewer as make_reviewer

# A container-namespace spelling that is deliberately NOT present on the host, so
# Git refuses to resolve the worktree the way a host-run process refuses a
# container-written ``.git`` file.
FOREIGN_NAMESPACE = "/workspace/concern76-foreign"


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@localhost",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@localhost",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _commit_in(worktree: Path, message: str) -> str:
    subprocess.run(("git", "add", "-A"), cwd=worktree, env=_git_env(), check=True)
    subprocess.run(
        ("git", "commit", "--quiet", "-m", message), cwd=worktree, env=_git_env(), check=True
    )
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=worktree,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# =============================================================================
# 1. Namespace portability of the supervisor-owned integration worktree.
# =============================================================================


def _corrupt_worktree_namespace(worktree: Path, repository: Path, name: str) -> None:
    """Rewrite a linked worktree's metadata to a path foreign to this namespace.

    Not a mock: it produces byte-for-byte the on-disk state a container run would
    leave behind for a host run to find -- a ``.git`` file pointing at a
    ``/workspace/...`` administrative directory that does not exist here, and the
    main repository's back-pointer recorded at the foreign worktree location.
    """
    (worktree / ".git").write_text(
        f"gitdir: {FOREIGN_NAMESPACE}/.git/worktrees/{name}\n", encoding="utf-8"
    )
    (repository / ".git" / "worktrees" / name / "gitdir").write_text(
        f"{FOREIGN_NAMESPACE}/_wt/{name}/.git\n", encoding="utf-8"
    )


def test_foreign_namespace_integration_worktree_is_unusable_without_repair(
    git_settings: Settings, fixture_repo: Path
) -> None:
    """Establish the defect with real Git: foreign-namespace metadata makes a
    present worktree directory unreadable."""
    baseline = run_git(fixture_repo, "rev-parse", "HEAD").strip()
    repository = GitService(fixture_repo, default_branch="main", settings=git_settings)
    path = git_settings.worktree_root / "proj" / "_integration"
    path.parent.mkdir(parents=True, exist_ok=True)
    repository.create_detached_worktree(path, baseline)
    _corrupt_worktree_namespace(path, fixture_repo, "_integration")

    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=path,
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )
    assert completed.returncode != 0
    assert "not a git repository" in completed.stderr
    assert FOREIGN_NAMESPACE in completed.stderr
    assert (path / ".git").exists()  # present on disk, unusable to Git


def test_usable_or_recreated_repairs_stale_integration_metadata(
    git_settings: Settings, fixture_repo: Path
) -> None:
    """The supervisor-owned integration worktree is recreated in this namespace."""
    baseline = run_git(fixture_repo, "rev-parse", "HEAD").strip()
    repository = GitService(fixture_repo, default_branch="main", settings=git_settings)
    path = integration_service.integration_worktree_path(uuid.uuid4(), settings=git_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    repository.create_detached_worktree(path, baseline)
    _corrupt_worktree_namespace(path, fixture_repo, path.name)

    repaired = integration_service._usable_or_recreated_integration_worktree(
        repository, path, baseline
    )

    assert repaired.get_head_sha() == baseline
    gitdir = (path / ".git").read_text(encoding="utf-8")
    assert FOREIGN_NAMESPACE not in gitdir
    assert str(fixture_repo.resolve()) in gitdir


def test_recreation_is_idempotent_when_worktree_is_healthy(
    git_settings: Settings, fixture_repo: Path
) -> None:
    """A usable integration worktree is reused (reset), not destroyed and rebuilt."""
    baseline = run_git(fixture_repo, "rev-parse", "HEAD").strip()
    repository = GitService(fixture_repo, default_branch="main", settings=git_settings)
    path = integration_service.integration_worktree_path(uuid.uuid4(), settings=git_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    repository.create_detached_worktree(path, baseline)
    marker = path / "scratch.txt"
    marker.write_text("leftover\n", encoding="utf-8")

    reused = integration_service._usable_or_recreated_integration_worktree(
        repository, path, baseline
    )
    assert reused.get_head_sha() == baseline
    assert not marker.exists()  # reset clears disposable content
    assert FOREIGN_NAMESPACE not in (path / ".git").read_text(encoding="utf-8")


def test_usable_integration_worktree_with_unresolvable_baseline_raises_without_destroy(
    git_settings: Settings, fixture_repo: Path
) -> None:
    """A *usable* tree plus a bad baseline is a loud error, never a silent rebuild.

    Recreating on every GitError would turn an operator's problem into a deletion
    of the worktree; only a worktree that is itself *unusable* is replaced.
    """
    baseline = run_git(fixture_repo, "rev-parse", "HEAD").strip()
    repository = GitService(fixture_repo, default_branch="main", settings=git_settings)
    path = integration_service.integration_worktree_path(uuid.uuid4(), settings=git_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    repository.create_detached_worktree(path, baseline)
    with pytest.raises(GitError):
        integration_service._usable_or_recreated_integration_worktree(
            repository, path, "0" * 40
        )
    assert (path / ".git").exists()  # it was usable, so nothing was destroyed


def test_integration_repair_never_touches_an_unrelated_worktree(
    git_settings: Settings, fixture_repo: Path
) -> None:
    """A foreign/user worktree is never removed or repointed by integration repair.

    The unrelated tree is placed as a *sibling* of the integration worktree under
    the same project directory, so a repair that over-reaches -- removing the whole
    project directory instead of exactly the integration worktree -- is detected.
    """
    baseline = run_git(fixture_repo, "rev-parse", "HEAD").strip()
    repository = GitService(fixture_repo, default_branch="main", settings=git_settings)

    project_id = uuid.uuid4()
    integration_path = integration_service.integration_worktree_path(
        project_id, settings=git_settings
    )
    integration_path.parent.mkdir(parents=True, exist_ok=True)
    repository.create_detached_worktree(integration_path, baseline)
    _corrupt_worktree_namespace(integration_path, fixture_repo, integration_path.name)

    foreign = integration_path.parent / "someone-elses-tree"
    repository.create_detached_worktree(foreign, baseline)
    foreign_file = foreign / "keep-me.txt"
    foreign_file.write_text("precious\n", encoding="utf-8")

    integration_service._usable_or_recreated_integration_worktree(
        repository, integration_path, baseline
    )

    assert foreign.exists()
    assert foreign_file.read_text(encoding="utf-8") == "precious\n"
    # And the integration worktree itself was genuinely repaired in this namespace.
    assert FOREIGN_NAMESPACE not in (integration_path / ".git").read_text(encoding="utf-8")


# =============================================================================
# 2. A stranded, post-approval delivery: the durable + Git state TS-110 was left in.
# =============================================================================


@dataclass
class Stranded:
    """A private database, a real repository, and an approved-but-undelivered run."""

    factory: sessionmaker
    settings: Settings
    repository: Path
    project_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    starting: str
    candidate: str
    worktree: Path


@contextlib.contextmanager
def _build_stranded(
    tmp_path: Path,
    *,
    approved: bool = True,
    make_commit: bool = True,
    touch_files: tuple[str, ...] = ("src/nav.py",),
    max_files: int = 12,
    corrupt_worktree: bool = False,
    make_checkpoint: bool = True,
) -> Iterator[Stranded]:
    """Seed a run whose reviewer approved a committed candidate but whose delivery
    transaction never committed: task APPROVED, run RUNNING, no owner, and
    ``candidate_commit`` NULL while the worktree HEAD holds the real commit."""
    repository = tmp_path / "project"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "nav.py").write_text(
        "def navigate():\n    return None\n", "utf-8"
    )
    (repository / "src" / "extra.py").write_text("EXTRA = 1\n", "utf-8")
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")
    run_git(repository, "branch", INTEGRATION_BRANCH)

    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
    )
    engine = create_db_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    starting = run_git(repository, "rev-parse", INTEGRATION_BRANCH).strip()

    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="fixture",
                repository_path=str(repository),
                default_branch="main",
                worker_profile=WorkerProfile.PYTHON,
            )
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-110",
                title="implement navigation",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
                limits=TaskLimits(
                    max_attempts=3, max_review_cycles=3, max_files_changed=max_files
                ),
            )
        )
        TaskRepository(session).transition(task.id, TaskStatus.READY)
        branch = run_branch_name(task.external_task_id, task.title, 1)
        run = create_run(session, task.id, branch_name=branch, starting_commit=starting)
        model = ModelRepository(session).add(
            Model(
                provider="openai_compatible",
                model_name="qwen3-coder-30b",
                role=ModelRole.CODER,
                endpoint="http://localhost:11434/v1",
            )
        )
        project_id, task_id, run_id = project.id, task.id, run.id

    # The committed candidate lives only on the run's worktree, exactly as after a
    # coder turn that reached an approved result and then died before delivery.
    worktree = workspace_path(project_id, "TS-110", run.run_number, settings=settings)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_git(repository, "worktree", "add", "-b", branch, str(worktree), starting)
    candidate = starting
    if make_commit:
        for rel in touch_files:
            target = worktree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def navigate():\n    return target\n", encoding="utf-8")
        candidate = _commit_in(worktree, "TS-110: implement navigation")

    if corrupt_worktree:
        _corrupt_worktree_namespace(worktree, repository, worktree.name)

    with factory.begin() as session:
        TaskRunRepository(session).update_fields(
            run_id,
            status=RunStatus.RUNNING,
            attempt_number=1,
            review_cycle=1,
            candidate_commit=None,
            starting_commit=starting,
            branch_name=branch,
        )
        # The durable CODE call that charged attempt 1.
        ModelRunRepository(session).add(
            ModelRun(
                task_run_id=run_id,
                model_id=model.id,
                purpose=ModelPurpose.CODE,
                status=RunStatus.SUCCEEDED,
                attempt=1,
                review_cycle=1,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
        # The candidate verification gates the reviewer relied on.
        for gate in (
            VerificationType.SCOPE,
            VerificationType.BUILD,
            VerificationType.LINT,
            VerificationType.TESTS,
            VerificationType.SECURITY,
            VerificationType.DIFF_POLICY,
        ):
            VerificationRunRepository(session).add(
                VerificationRun(
                    task_run_id=run_id,
                    verification_type=gate,
                    command="pytest",
                    status=VerificationStatus.PASSED,
                    exit_code=0,
                )
            )
        ReviewRepository(session).add(
            Review(
                task_run_id=run_id,
                reviewer_provider="openai_compatible",
                reviewer_model="reviewer",
                decision=(
                    ReviewDecision.APPROVED if approved else ReviewDecision.CHANGES_REQUESTED
                ),
                summary="approved" if approved else "needs work",
                cycle=1,
            )
        )
        # A durable workflow checkpoint, so ``checkpoint_exists`` is satisfied.
        # The runner-based tests build a real checkpoint by driving the graph, so
        # they opt out of this synthetic row (LangGraph cannot load a stub one).
        if make_checkpoint:
            session.add(
                WorkflowCheckpointRow(
                    thread_id=str(run_id),
                    checkpoint_ns="",
                    checkpoint_id="76-delivery-checkpoint",
                    parent_checkpoint_id=None,
                    checkpoint_type="msgpack",
                    checkpoint=b"\x80",
                    metadata_type="msgpack",
                    checkpoint_metadata=b"\x80",
                )
            )
        for status in (
            TaskStatus.CODING,
            TaskStatus.VERIFYING,
            TaskStatus.REVIEW_PENDING,
            TaskStatus.REVIEWING,
        ):
            TaskRepository(session).transition(task_id, status)
        if approved:
            TaskRepository(session).transition(task_id, TaskStatus.APPROVED)

    yield Stranded(
        factory=factory,
        settings=settings,
        repository=repository,
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        starting=starting,
        candidate=candidate,
        worktree=worktree,
    )
    engine.dispose()


@pytest.fixture
def stranded(tmp_path: Path) -> Iterator[Stranded]:
    with _build_stranded(tmp_path) as strand:
        yield strand


def _refusal_names(report) -> set[str]:  # noqa: ANN001
    return {check.name for check in report.refusals}


# --- recovery happy path ------------------------------------------------------


def test_stranded_approved_run_is_assessed_as_delivery_only(stranded: Stranded) -> None:
    with stranded.factory() as session:
        report = assess_recoverability(session, stranded.run_id, settings=stranded.settings)
    assert report.recoverable is True
    assert report.recovery_mode is RecoveryMode.DELIVERY_ONLY
    assert report.candidate_commit == stranded.candidate
    assert report.execution_owner is None


@pytest.mark.asyncio
async def test_delivery_only_recovery_completes_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the incident, then recover it, end to end through the graph.

    The stranded state is produced honestly: a real ``APPROVED`` run whose
    delivery crashes inside ``integrate_candidate`` -- a Git failure the delivery
    transaction cannot roll back, exactly the namespace crash of the incident --
    leaving ``candidate_commit`` NULL, a committed clean worktree, a live durable
    checkpoint, and no execution owner. Recovery then re-enters the ordinary
    delivery node and lands the run with no coder and no reviewer call.
    """
    with _build_stranded(tmp_path, make_checkpoint=False) as strand:
        with strand.factory() as session:
            before_calls = len(ModelRunRepository(session).list_for_run(strand.run_id))
            before_reviews = len(ReviewRepository(session).list_for_run(strand.run_id))

        git = GitService(strand.repository, default_branch="main", settings=strand.settings)

        # --- simulate the crash inside delivery's integrate step -------------
        real_integrate = delivery_module.integrate_candidate
        crash_state = {"raised": False}

        def crashing_integrate(*args, **kwargs):
            if not crash_state["raised"]:
                crash_state["raised"] = True
                raise MergeConflict(strand.repository, ("simulated namespace crash",))
            return real_integrate(*args, **kwargs)

        monkeypatch.setattr(delivery_module, "integrate_candidate", crashing_integrate)

        crasher = WorkflowRunner(
            strand.factory,
            coder=ScriptedModel(),
            reviewer=make_reviewer(),
            settings=strand.settings,
        )
        with pytest.raises(MergeConflict):
            await crasher.run(strand.run_id)
        await crasher.aclose()

        # The durable aftermath, exactly the incident: still in flight, approved,
        # committed on disk, no candidate recorded, no owner holding it.
        with strand.factory() as session:
            stranded_run = TaskRunRepository(session).get(strand.run_id)
            stranded_task = TaskRepository(session).get(strand.task_id)
        assert stranded_run.status is RunStatus.RUNNING
        assert stranded_run.candidate_commit is None
        assert stranded_run.execution_owner is None
        assert stranded_task.status is TaskStatus.APPROVED
        worktree_git = GitService(
            strand.worktree, default_branch="main", settings=strand.settings
        )
        assert worktree_git.get_head_sha() == strand.candidate

        # A real checkpoint now exists, so recovery is eligible.
        with strand.factory() as session:
            report = assess_recoverability(session, strand.run_id, settings=strand.settings)
        assert report.recoverable is True
        assert report.recovery_mode is RecoveryMode.DELIVERY_ONLY

        with strand.factory.begin() as session:
            authorization = recover_run(
                session,
                strand.run_id,
                reason="delivery crashed after the candidate was committed",
                requested_by="operator",
                settings=strand.settings,
            )
        assert authorization.recovery_mode is RecoveryMode.DELIVERY_ONLY

        coder = ScriptedModel()
        runner = WorkflowRunner(
            strand.factory,
            coder=coder,
            reviewer=make_reviewer(),
            settings=strand.settings,
        )
        try:
            state = await runner.run(
                strand.run_id, acquired=(authorization.owner, authorization.generation)
            )
        finally:
            await runner.aclose()

        with strand.factory() as session:
            run = TaskRunRepository(session).get(strand.run_id)
            runs = TaskRunRepository(session).list_for_task(strand.task_id)
            task = TaskRepository(session).get(strand.task_id)
            events = RunEventRepository(session).list_for_run(strand.run_id)
            after_calls = len(ModelRunRepository(session).list_for_run(strand.run_id))
            after_reviews = len(ReviewRepository(session).list_for_run(strand.run_id))

        assert state["outcome"] == "COMPLETED"
        assert task.status is TaskStatus.COMPLETE
        assert run.status is RunStatus.SUCCEEDED
        assert run.execution_owner is None
        # Exactly-once: the same run, one run of the task, attempt never moved.
        assert len(runs) == 1
        assert run.id == strand.run_id
        assert run.run_number == 1
        assert run.attempt_number == 1
        # The committed candidate became durable, derived from Git not a caller.
        assert run.candidate_commit == strand.candidate
        assert git.contains_commit(strand.candidate, ref=INTEGRATION_BRANCH)
        # No coder and no reviewer were called: no new model run, no new review.
        assert coder.requests == []
        assert after_calls == before_calls
        assert after_reviews == before_reviews
        assert (
            len([e for e in events if e.event_type == RunEventType.RUN_RECOVERY_AUTHORIZED])
            == 1
        )

        # A replay after success is refused: the run is terminal and no TaskRun
        # or second integration appears.
        with strand.factory() as session:
            replay = assess_recoverability(session, strand.run_id, settings=strand.settings)
        assert replay.recoverable is False
        with pytest.raises(EntityConflict), strand.factory.begin() as session:
            recover_run(
                session,
                strand.run_id,
                reason="replay after success",
                settings=strand.settings,
            )
        with strand.factory() as session:
            runs = TaskRunRepository(session).list_for_task(strand.task_id)
        assert len(runs) == 1


# --- refusals: partial or contradictory provenance fails closed ---------------


def test_recovery_refused_when_no_candidate_work(tmp_path: Path) -> None:
    with _build_stranded(tmp_path, make_commit=False) as strand, strand.factory() as session:
        report = assess_recoverability(session, strand.run_id, settings=strand.settings)
    assert report.recoverable is False
    assert "delivery_candidate_provenance" in _refusal_names(report)


def test_recovery_refused_on_wrong_ancestry(tmp_path: Path) -> None:
    with _build_stranded(tmp_path) as strand:
        # Move the accepted baseline onto an unrelated child of ``starting`` so it
        # still contains ``starting`` (passes the baseline-compatibility gate) but
        # the candidate HEAD no longer descends from the run's recorded start.
        run_git(strand.repository, "commit", "--quiet", "--allow-empty", "-m", "distractor")
        new_head = run_git(strand.repository, "rev-parse", "HEAD").strip()
        run_git(strand.repository, "branch", "--force", INTEGRATION_BRANCH, new_head)
        with strand.factory.begin() as session:
            TaskRunRepository(session).update_fields(
                strand.run_id, starting_commit=new_head
            )
        with strand.factory() as session:
            report = assess_recoverability(session, strand.run_id, settings=strand.settings)
    assert report.recoverable is False
    assert "delivery_candidate_ancestry" in _refusal_names(report)


def test_recovery_refused_on_scope_mismatch(tmp_path: Path) -> None:
    with _build_stranded(
        tmp_path, touch_files=("src/nav.py", "src/extra.py"), max_files=1
    ) as strand, strand.factory() as session:
        report = assess_recoverability(session, strand.run_id, settings=strand.settings)
    assert report.recoverable is False
    assert "delivery_scope_within_allowance" in _refusal_names(report)


def test_recovery_refused_when_review_not_approved(tmp_path: Path) -> None:
    # A task marked APPROVED whose only review did not approve: delivery would
    # proceed on a contradiction, so provenance refuses.
    with _build_stranded(tmp_path, approved=False) as strand:
        with strand.factory.begin() as session:
            TaskRepository(session).transition(strand.task_id, TaskStatus.APPROVED)
        with strand.factory() as session:
            report = assess_recoverability(session, strand.run_id, settings=strand.settings)
    assert report.recoverable is False
    assert "delivery_review_approved" in _refusal_names(report)


def test_recovery_refused_when_supplied_candidate_disagrees(tmp_path: Path) -> None:
    with _build_stranded(tmp_path) as strand:
        # The operator records a candidate_commit that resolves but is not the
        # worktree HEAD; recovery refuses rather than trusting the supplied SHA.
        with strand.factory.begin() as session:
            TaskRunRepository(session).update_fields(
                strand.run_id, candidate_commit=strand.starting
            )
        with strand.factory() as session:
            report = assess_recoverability(session, strand.run_id, settings=strand.settings)
    assert report.recoverable is False
    assert "delivery_candidate_provenance" in _refusal_names(report)


def test_recovery_refused_when_worktree_unusable_namespace(tmp_path: Path) -> None:
    # A task worktree whose metadata was written in another namespace is NOT
    # silently rebuilt -- it may be the only copy of the approved candidate.
    with (
        _build_stranded(tmp_path, corrupt_worktree=True) as strand,
        strand.factory() as session,
    ):
        report = assess_recoverability(session, strand.run_id, settings=strand.settings)
    assert report.recoverable is False
    assert "delivery_candidate_derivable" in _refusal_names(report)


# --- concurrency, fencing and replay -----------------------------------------


def test_recovery_refused_when_a_dispatch_holds_the_run(stranded: Stranded) -> None:
    with stranded.factory.begin() as session:
        TaskRunRepository(session).acquire_execution(stranded.run_id, owner="live-dispatch")
    with stranded.factory() as session:
        report = assess_recoverability(session, stranded.run_id, settings=stranded.settings)
    assert report.recoverable is False
    assert "execution_ownership_available" in _refusal_names(report)


def test_integrate_candidate_is_idempotent_when_already_in_baseline(
    stranded: Stranded,
) -> None:
    """A replayed integration of an already-landed candidate does not move the ref."""
    git = GitService(stranded.repository, default_branch="main", settings=stranded.settings)
    with stranded.factory.begin() as session:
        project = ProjectRepository(session).get(stranded.project_id)
        task = TaskRepository(session).get(stranded.task_id)
        run = TaskRunRepository(session).get(stranded.run_id)
        first = integrate_candidate(
            session, project, task, run, stranded.candidate, settings=stranded.settings
        )
        assert first.advanced is True
        baseline_after_first = git.resolve_sha(INTEGRATION_BRANCH)

    with stranded.factory.begin() as session:
        project = ProjectRepository(session).get(stranded.project_id)
        task = TaskRepository(session).get(stranded.task_id)
        run = TaskRunRepository(session).get(stranded.run_id)
        second = integrate_candidate(
            session, project, task, run, stranded.candidate, settings=stranded.settings
        )
        # Idempotent: reports integrated, but does not move the ref a second time.
        assert second.advanced is True
        assert second.merged_sha is None
        assert git.resolve_sha(INTEGRATION_BRANCH) == baseline_after_first


def test_generation_fencing_blocks_a_superseded_executor(stranded: Stranded) -> None:
    """Only the generation token, not ownership, discriminates here.

    Release first so the unowned-run predicate cannot be what rejects the stale
    acquisition: the generation is the fence under test, exactly the one recovery
    relies on to stop a superseded executor from persisting.
    """
    with stranded.factory.begin() as session:
        first = TaskRunRepository(session).acquire_execution(stranded.run_id, owner="dispatch-a")
        assert first is not None
        assert first.execution_generation == 1
        TaskRunRepository(session).release_execution(stranded.run_id, owner="dispatch-a")

    with stranded.factory.begin() as session:
        # The run is unowned again, but its generation advanced to 1; a stale
        # executor quoting 0 is refused, and one quoting the current 1 wins.
        stale = TaskRunRepository(session).acquire_execution(
            stranded.run_id, owner="dispatch-b", expected_generation=0
        )
        assert stale is None

    with stranded.factory.begin() as session:
        current = TaskRunRepository(session).acquire_execution(
            stranded.run_id, owner="dispatch-c", expected_generation=1
        )
        assert current is not None
        assert current.execution_generation == 2
