"""Dependency bootstrap lifecycle."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.commands import ApprovedCommand, CommandPolicy
from apps.orchestrator.domain.enums import TaskStatus, VerificationType, WorkerProfile
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services import dependency_bootstrap as deps
from apps.orchestrator.services import integration as integration_service
from apps.orchestrator.services import verification as verification_service
from apps.orchestrator.services.integration import (
    integrate_candidate,
    integrate_human_commit,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.verification import verify_candidate
from apps.orchestrator.services.worker_service import CommandResult, WorkerSpec
from apps.orchestrator.services.workspace import commit_task_work, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration


BOOTSTRAP_COMMAND = "python -m pip install -r requirements.txt"
VERIFY_COMMAND = "python -m pytest"


@dataclass
class FakeWorker:
    path: Path
    network: str
    callback: Callable[[Path, ApprovedCommand, str], int]
    profile: WorkerProfile = WorkerProfile.PYTHON

    def __post_init__(self) -> None:
        self.policy = CommandPolicy(profile=self.profile)
        self.spec = WorkerSpec(
            worker_id=f"fake-{self.network}",
            profile=self.profile,
            mount_source=self.path,
            backend=WorkerBackend.DOCKER,
            image="fake-worker:latest",
            network=self.network,
            command_timeout_seconds=60,
        )

    def run(
        self, command: ApprovedCommand, *, timeout_seconds: int | None = None
    ) -> CommandResult:
        exit_code = self.callback(self.path, command, self.network)
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=f"{command.display} on {self.network}\n",
            worker_id=self.spec.worker_id,
        )


@pytest.fixture
def docker_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.DOCKER,
        worker_network="none",
    )


@pytest.fixture
def dependency_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    (repository / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    (repository / "README.md").write_text("# Project\n", encoding="utf-8")
    (repository / "requirements.txt").write_text("example==1\n", encoding="utf-8")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "Initial")
    return repository


def _project(
    session: Session,
    repository: Path,
    *,
    bootstrap: bool = False,
    verification: VerificationProfile | None = None,
) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Deps",
            repository_path=str(repository),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            dependency_paths=["vendor"],
            dependency_bootstrap_commands=[BOOTSTRAP_COMMAND] if bootstrap else [],
            verification=verification or VerificationProfile(build=(VERIFY_COMMAND,)),
        )
    )


def _task(session: Session, project: Project, external_task_id: str = "T-1") -> Task:
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id=external_task_id, title="Do work")
    )
    return TaskRepository(session).transition(task.id, TaskStatus.READY)


def _workspace(session: Session, project: Project, settings: Settings):
    task = _task(session, project)
    run = create_run(session, task.id)
    return task, run, prepare_workspace(session, run.id, settings=settings)


def _install_fake_worker(monkeypatch: pytest.MonkeyPatch, callback):
    networks: list[str] = []

    @contextmanager
    def fake_worker_session(
        mount_source: Path,
        *,
        profile: WorkerProfile,
        settings: Settings,
        secrets=None,
        worker_network: str | None = None,
    ) -> Iterator[FakeWorker]:
        network = worker_network if worker_network is not None else settings.worker_network
        networks.append(network)
        yield FakeWorker(Path(mount_source), network, callback, profile=profile)

    monkeypatch.setattr(deps, "worker_session", fake_worker_session)
    monkeypatch.setattr(verification_service, "worker_session", fake_worker_session)
    monkeypatch.setattr(integration_service, "worker_session", fake_worker_session)
    return networks


def _make_candidate_change(workspace) -> None:
    (workspace.path / "README.md").write_text("# Candidate\n", encoding="utf-8")


def _bootstrap_creates_vendor(path: Path, command: ApprovedCommand, network: str) -> int:
    if command.source == BOOTSTRAP_COMMAND:
        (path / "vendor").mkdir(exist_ok=True)
        (path / "vendor" / "installed.txt").write_text(network, encoding="utf-8")
    return 0


def test_existing_dependency_paths_behavior_without_bootstrap(
    session: Session, dependency_repo: Path, docker_settings: Settings
):
    (dependency_repo / "vendor").mkdir()
    (dependency_repo / "vendor" / "installed.txt").write_text("cached\n", encoding="utf-8")
    project = _project(session, dependency_repo)

    _, _, workspace = _workspace(session, project, docker_settings)

    assert (workspace.path / "vendor" / "installed.txt").read_text(encoding="utf-8") == (
        "cached\n"
    )


def test_missing_dependency_path_without_bootstrap_is_rejected(
    session: Session, dependency_repo: Path, docker_settings: Settings
):
    project = _project(session, dependency_repo)
    task = _task(session, project)
    run = create_run(session, task.id)

    with pytest.raises(ValueError, match="Declared dependency path does not exist"):
        prepare_workspace(session, run.id, settings=docker_settings)


def test_missing_dependency_path_with_bootstrap_is_built(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    report = verify_candidate(session, workspace, settings=docker_settings)

    assert report.passed
    assert (workspace.path / "vendor" / "installed.txt").read_text(encoding="utf-8") == "bridge"
    assert networks == ["bridge", "none"]


def test_stale_marker_detection_rebuilds_dependency_path(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    (dependency_repo / "vendor").mkdir()
    (dependency_repo / "vendor" / "old.txt").write_text("old", encoding="utf-8")
    (dependency_repo / "vendor" / deps.MARKER_FILENAME).write_text("{}", encoding="utf-8")
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    assert not (workspace.path / "vendor" / "old.txt").exists()

    report = verify_candidate(session, workspace, settings=docker_settings)

    assert report.passed
    assert (workspace.path / "vendor" / deps.MARKER_FILENAME).exists()


def test_worker_network_override_and_default_behavior(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    verify_candidate(session, workspace, settings=docker_settings)

    assert networks == ["bridge", "none"]


def test_subprocess_bootstrap_refusal(
    session: Session, dependency_repo: Path, tmp_path: Path
):
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
    )
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, settings)
    _make_candidate_change(workspace)

    report = verify_candidate(session, workspace, settings=settings)

    step = report.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert not report.passed
    assert step is not None
    assert "requires the Docker worker backend" in step.detail


def test_bootstrap_source_change_rejection(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        if command.source == BOOTSTRAP_COMMAND:
            (path / "vendor").mkdir(exist_ok=True)
            (path / "vendor" / "installed.txt").write_text("ok", encoding="utf-8")
            (path / "README.md").write_text("changed by bootstrap\n", encoding="utf-8")
        return 0

    _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    report = verify_candidate(session, workspace, settings=docker_settings)

    step = report.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert not report.passed
    assert step is not None
    assert "tracked or unignored source paths" in step.detail


def test_bootstrap_failure_propagation(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_worker(monkeypatch, lambda path, command, network: 1)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    report = verify_candidate(session, workspace, settings=docker_settings)

    step = report.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert not report.passed
    assert step is not None
    assert step.exit_code == 1


def test_ordinary_verification_remains_networkless(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    verify_candidate(session, workspace, settings=docker_settings)

    assert networks[-1] == "none"


def test_successful_integration_publication(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    task, run, workspace = _workspace(session, project, docker_settings)
    (workspace.path / "README.md").write_text("# Changed\n", encoding="utf-8")
    candidate = commit_task_work(session, workspace, task)

    result = integrate_candidate(
        session, project, task, run, candidate, settings=docker_settings
    )

    assert result.advanced
    assert (dependency_repo / "vendor" / "installed.txt").exists()
    assert (dependency_repo / "vendor" / deps.MARKER_FILENAME).exists()


def test_failed_integration_does_not_publish(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        _bootstrap_creates_vendor(path, command, network)
        return 1 if command.source == VERIFY_COMMAND else 0

    _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    task, run, workspace = _workspace(session, project, docker_settings)
    (workspace.path / "README.md").write_text("# Changed\n", encoding="utf-8")
    candidate = commit_task_work(session, workspace, task)

    result = integrate_candidate(
        session, project, task, run, candidate, settings=docker_settings
    )

    assert not result.advanced
    assert not (dependency_repo / "vendor").exists()


def test_marker_mismatch_after_interrupted_publication(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    (dependency_repo / "vendor").mkdir()
    (dependency_repo / "vendor" / "future.txt").write_text("future", encoding="utf-8")
    (dependency_repo / "vendor" / deps.MARKER_FILENAME).write_text(
        '{"version": 1, "integration_sha": "future"}\n',
        encoding="utf-8",
    )
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    assert not (workspace.path / "vendor" / "future.txt").exists()

    report = verify_candidate(session, workspace, settings=docker_settings)

    assert report.passed
    assert (workspace.path / "vendor" / "installed.txt").exists()


def test_publication_and_copy_use_project_lock(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    lock_calls: list[int] = []
    original = deps.fcntl.flock

    def recording_flock(fd: int, operation: int) -> None:
        lock_calls.append(operation)
        original(fd, operation)

    monkeypatch.setattr(deps.fcntl, "flock", recording_flock)
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    task, run, workspace = _workspace(session, project, docker_settings)
    (workspace.path / "README.md").write_text("# Changed\n", encoding="utf-8")
    candidate = commit_task_work(session, workspace, task)

    integrate_candidate(session, project, task, run, candidate, settings=docker_settings)

    assert deps.fcntl.LOCK_EX in lock_calls
    assert deps.fcntl.LOCK_UN in lock_calls


# --- fingerprint-keyed validity (defects 1, 2 and 4) ------------------------


def _integrate_one_task(
    session: Session,
    project: Project,
    settings: Settings,
    *,
    external_task_id: str,
    content: str,
):
    """Take one task all the way to an advanced baseline."""
    task = TaskRepository(session).transition(
        TaskRepository(session)
        .add(Task(project_id=project.id, external_task_id=external_task_id, title="Work"))
        .id,
        TaskStatus.READY,
    )
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=settings)
    (workspace.path / "README.md").write_text(content, encoding="utf-8")
    candidate = commit_task_work(session, workspace, task)
    return integrate_candidate(session, project, task, run, candidate, settings=settings)


def test_unchanged_dependency_inputs_reuse_published_tree_across_baselines(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 1/4a: advancing the baseline is not a dependency-input change.

    The first task publishes a dependency tree; the second starts from a
    different integration commit but identical manifests, so the published tree
    is still exactly what its inputs call for. Nothing networked may run.
    """
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)

    first = _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    )
    assert first.advanced
    assert (dependency_repo / "vendor" / "installed.txt").exists()

    networks.clear()
    second = _integrate_one_task(
        session, project, docker_settings, external_task_id="T-2", content="# Two\n"
    )

    assert second.advanced
    assert "bridge" not in networks
    assert networks == ["none"]


def test_bootstrap_is_skipped_when_worktree_tree_is_already_valid(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 2: a second bootstrap over a valid tree starts no container."""
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, run, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    assert verify_candidate(session, workspace, settings=docker_settings).passed
    networks.clear()

    # Ignored churn inside the dependency path -- exactly what real tools leave
    # behind. It is not a reason to reinstall, because it cannot change what the
    # declared inputs ask for.
    (workspace.path / "vendor" / "tool-state.log").write_text("noise\n", encoding="utf-8")

    again = deps.bootstrap_dependencies(
        session,
        project,
        run,
        worktree_path=workspace.path,
        worktree_git=workspace.git,
        integration_sha=workspace.starting_commit,
        settings=docker_settings,
        prefix="retry/",
    )

    assert again.skipped
    assert again.executions == ()
    assert networks == []


def test_changed_manifest_invalidates_the_dependency_tree(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 1: the manifest, not the commit, decides validity."""
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced

    networks.clear()
    task = _task(session, project, "T-NEXT")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=docker_settings)
    # The candidate's own work changes what would be installed.
    (workspace.path / "requirements.txt").write_text("example==2\n", encoding="utf-8")

    assert verify_candidate(session, workspace, settings=docker_settings).passed
    assert "bridge" in networks


def test_changed_worker_runtime_invalidates_the_dependency_tree(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 1: a tree installed by a different runtime is not reusable."""
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced

    requeued = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=docker_settings.worktree_root,
        worker_backend=WorkerBackend.DOCKER,
        worker_network="none",
        worker_python_image="python-worker:next",
    )
    assert requeued.worker_python_image != docker_settings.worker_python_image

    networks.clear()
    task = _task(session, project, "T-NEXT")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=requeued)
    (workspace.path / "README.md").write_text("# Requeued\n", encoding="utf-8")

    assert verify_candidate(session, workspace, settings=requeued).passed
    assert "bridge" in networks


def test_dependency_inputs_changed_by_a_human_integration_are_detected(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 4b: the human path advances the ref without republishing.

    So the next task must not trust the published tree -- it was installed from
    a manifest the baseline no longer carries.
    """
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced
    published = dependency_repo / "vendor" / deps.MARKER_FILENAME
    before = published.read_text(encoding="utf-8")

    # An operator commits a dependency change and integrates it by hand.
    (dependency_repo / "requirements.txt").write_text("example==3\n", encoding="utf-8")
    run_git(dependency_repo, "add", "-A")
    run_git(dependency_repo, "commit", "--quiet", "-m", "Operator bumps the dependency")
    human_sha = run_git(dependency_repo, "rev-parse", "HEAD").strip()
    human_task = _task(session, project, "T-HUMAN")
    human_run = create_run(session, human_task.id)
    integrate_human_commit(
        session,
        project,
        human_task,
        human_sha,
        task_run_id=human_run.id,
        settings=docker_settings,
    )

    assert published.read_text(encoding="utf-8") == before, (
        "the human path must not republish"
    )

    networks.clear()
    task = _task(session, project, "T-NEXT")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=docker_settings)

    assert not (workspace.path / "vendor").exists(), (
        "a tree built from the superseded manifest must not be copied in"
    )
    (workspace.path / "README.md").write_text("# After the operator\n", encoding="utf-8")
    assert verify_candidate(session, workspace, settings=docker_settings).passed
    assert "bridge" in networks


# --- verification writes inside dependency paths (defect 3) -----------------


def test_verification_writing_inside_dependency_paths_still_publishes(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 3: ignored churn inside a dependency path is not corruption.

    Real build and test commands write caches inside ``vendor``. The published
    tree must still be published, and the marker it carries must describe the
    bytes that were actually published -- otherwise the next task reads it as a
    torn publication and reinstalls from the network forever.
    """

    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        _bootstrap_creates_vendor(path, command, network)
        if command.source == VERIFY_COMMAND:
            cache = path / "vendor" / "__pycache__"
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "mod.pyc").write_bytes(b"compiled")
        return 0

    networks = _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced

    assert (dependency_repo / "vendor" / "__pycache__" / "mod.pyc").exists()

    # The published marker describes the published tree, so the next task
    # reuses it rather than treating the churn as a failed publication.
    networks.clear()
    task = _task(session, project, "T-NEXT")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=docker_settings)

    assert (workspace.path / "vendor" / "__pycache__" / "mod.pyc").exists()
    (workspace.path / "README.md").write_text("# Two\n", encoding="utf-8")
    assert verify_candidate(session, workspace, settings=docker_settings).passed
    assert "bridge" not in networks


def test_verification_cannot_launder_tracked_changes_into_source(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 3, the other half: marker refresh publishes nothing but markers."""

    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        _bootstrap_creates_vendor(path, command, network)
        if command.source == VERIFY_COMMAND:
            (path / "README.md").write_text("rewritten by a tool\n", encoding="utf-8")
            (path / "requirements.txt").write_text("smuggled==9\n", encoding="utf-8")
        return 0

    _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    result = _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    )

    assert result.advanced
    baseline = run_git(dependency_repo, "rev-parse", integration_service.INTEGRATION_BRANCH).strip()
    tracked = run_git(dependency_repo, "show", f"{baseline}:requirements.txt")
    assert tracked == "example==1\n"
    assert "smuggled" not in run_git(dependency_repo, "show", f"{baseline}:README.md")
    assert not (dependency_repo / "vendor" / "README.md").exists()


# --- deterministic failure instead of an escaping exception (defect 5) ------


def test_publication_failure_blocks_the_integration_without_raising(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defect 5: a DependencyBootstrapError after verification is a result.

    The merged tree verified, then its dependency tree vanished. The baseline
    must not advance past a tree it cannot publish, and the supervisor must not
    see an exception.
    """

    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        _bootstrap_creates_vendor(path, command, network)
        if command.source == VERIFY_COMMAND:
            shutil.rmtree(path / "vendor")
        return 0

    _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    before = run_git(dependency_repo, "rev-parse", "HEAD").strip()

    result = _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    )

    assert not result.advanced
    assert any(
        "dependency publication" in command for command in result.failed_commands
    )
    baseline = run_git(
        dependency_repo, "rev-parse", integration_service.INTEGRATION_BRANCH
    ).strip()
    assert baseline == before
    assert not (dependency_repo / "vendor").exists()
