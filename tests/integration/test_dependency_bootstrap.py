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
from apps.orchestrator.services.git_service import GitService
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
    (repository / ".gitignore").write_text(
        "vendor/\nvendor.tar\nextra/\n", encoding="utf-8"
    )
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
    dependency_paths: list[str] | None = None,
) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Deps",
            repository_path=str(repository),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            dependency_paths=dependency_paths or ["vendor"],
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


def _repo_git(repository: Path, settings: Settings) -> GitService:
    return GitService(repository, default_branch="main", settings=settings)


def _published_marker(repository: Path, settings: Settings, declared: str = "vendor") -> Path:
    """The marker the repository keeps for a published dependency path.

    Markers live in Git's administrative directory, not in the working tree, so
    a test that wants to inspect or corrupt one has to ask for it the same way
    the implementation does.
    """
    store = deps._published_marker_store(_repo_git(repository, settings))
    return deps._marker_file(store, declared)


def _write_published_marker(
    repository: Path, settings: Settings, *, declared: str = "vendor", fingerprint: str
) -> None:
    deps._write_marker(
        deps._published_marker_store(_repo_git(repository, settings)),
        declared=declared,
        fingerprint=fingerprint,
        integration_sha="0" * 40,
    )


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
    # Preparing a workspace certifies the baseline before the candidate worktree
    # exists, and a baseline whose declared dependency path does not exist yet
    # has to be given one before it can be measured. That is the first `bridge`,
    # in the detached integration worktree -- a different tree from the
    # candidate's, not a second run of the candidate's bootstrap.
    assert networks == ["bridge"]
    networks.clear()

    _make_candidate_change(workspace)

    report = verify_candidate(session, workspace, settings=docker_settings)

    assert report.passed
    assert (workspace.path / "vendor" / "installed.txt").read_text(encoding="utf-8") == "bridge"
    # The candidate's own bootstrap, then its verification worker.
    assert networks == ["bridge", "none"]


def test_uncertified_published_tree_is_not_trusted(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """A dependency tree nobody certified is not evidence of anything."""
    (dependency_repo / "vendor").mkdir()
    (dependency_repo / "vendor" / "old.txt").write_text("old", encoding="utf-8")
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    assert not (workspace.path / "vendor" / "old.txt").exists()

    report = verify_candidate(session, workspace, settings=docker_settings)

    assert report.passed
    assert (workspace.path / "vendor" / "installed.txt").exists()


def test_worker_network_override_and_default_behavior(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """The override is bootstrap's alone; verification takes the default.

    ``docker_settings`` declares ``worker_network="none"``, so this test is
    about which of the three networked operations overrides it. Baseline
    certification and candidate bootstrap both install dependencies, so both
    are `bridge`; candidate verification runs the project's commands and is
    left on the default.
    """
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    assert networks == ["bridge"]
    networks.clear()

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
    assert _published_marker(dependency_repo, docker_settings).exists()


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


def test_published_marker_for_other_inputs_is_not_trusted(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    (dependency_repo / "vendor").mkdir()
    (dependency_repo / "vendor" / "future.txt").write_text("future", encoding="utf-8")
    _write_published_marker(dependency_repo, docker_settings, fingerprint="someone-elses")
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
    published = _published_marker(dependency_repo, docker_settings)
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


# ===========================================================================
# Audit repairs. One test per confirmed defect.
# ===========================================================================


def _bootstrap_creates(*relative: str):
    """A bootstrap callback that creates each given path (file or directory)."""

    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        if command.source == BOOTSTRAP_COMMAND:
            for item in relative:
                target = path / item
                if item.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(network, encoding="utf-8")
        return 0

    return callback


# --- defect 7A: a dependency path that is a single file --------------------


def test_file_valued_dependency_path_bootstraps_and_publishes(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """A marker beside a file dependency path used to be unignored source.

    It landed in the working tree between the two source-change snapshots, so
    bootstrap rejected itself every single time, blaming the project for a file
    the orchestrator had created. Markers now live under ``.git``, where Git
    never reports them.
    """
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates("vendor.tar"))
    project = _project(
        session, dependency_repo, bootstrap=True, dependency_paths=["vendor.tar"]
    )
    task, run, workspace = _workspace(session, project, docker_settings)
    # Baseline certification bootstraps the file into the integration worktree
    # before the candidate's worktree exists; the candidate bootstraps its own.
    assert networks == ["bridge"]
    networks.clear()

    (workspace.path / "README.md").write_text("# Changed\n", encoding="utf-8")

    report = verify_candidate(session, workspace, settings=docker_settings)

    step = report.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert report.passed, step.detail if step else "no bootstrap step"
    assert (workspace.path / "vendor.tar").read_text(encoding="utf-8") == "bridge"
    assert networks == ["bridge", "none"]

    candidate = commit_task_work(session, workspace, task)
    result = integrate_candidate(
        session, project, task, run, candidate, settings=docker_settings
    )

    assert result.advanced
    assert (dependency_repo / "vendor.tar").is_file()
    assert _published_marker(dependency_repo, docker_settings, "vendor.tar").exists()


def test_file_valued_dependency_path_leaves_no_untracked_source(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """The marker must not be visible in the working tree at all."""
    _install_fake_worker(monkeypatch, _bootstrap_creates("vendor.tar"))
    project = _project(
        session, dependency_repo, bootstrap=True, dependency_paths=["vendor.tar"]
    )
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    assert verify_candidate(session, workspace, settings=docker_settings).passed

    visible = run_git(workspace.path, "status", "--porcelain=v1", "--untracked-files=all")
    assert "orchestrator-dependencies" not in visible
    assert not list(workspace.path.glob("*orchestrator-dependency*"))


# --- defect 7B: declared paths nested inside one another ------------------


def test_nested_dependency_paths_publish_and_are_reused(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Writing the inner marker used to invalidate the outer one, forever.

    The outer tree's content hash covered the inner tree's marker file, so the
    outer path was re-bootstrapped from the network on every task. There is no
    content hash now, and no marker inside either tree.
    """
    networks = _install_fake_worker(
        monkeypatch, _bootstrap_creates("vendor/installed.txt", "vendor/sub/lib.txt")
    )
    project = _project(
        session,
        dependency_repo,
        bootstrap=True,
        dependency_paths=["vendor", "vendor/sub"],
    )
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced
    assert (dependency_repo / "vendor" / "sub" / "lib.txt").exists()
    assert _published_marker(dependency_repo, docker_settings, "vendor").exists()
    assert _published_marker(dependency_repo, docker_settings, "vendor/sub").exists()

    networks.clear()
    second = _integrate_one_task(
        session, project, docker_settings, external_task_id="T-2", content="# Two\n"
    )

    assert second.advanced
    assert "bridge" not in networks, "the nested set must be reusable, not rebuilt"


# --- defect 7C: a rejected bootstrap must stay rejected -------------------


def test_rejected_bootstrap_is_rejected_again_on_retry(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Markers used to be stamped before the source-change check ran.

    A bootstrap that mutated tracked source was rejected, but had already left
    valid-looking markers behind -- so a retry in the same worktree found the
    set 'certified', skipped bootstrap, and silently trusted the tree that had
    just been refused. The stamp now follows the proof.
    """

    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        if command.source == BOOTSTRAP_COMMAND:
            (path / "vendor").mkdir(exist_ok=True)
            (path / "vendor" / "installed.txt").write_text("ok", encoding="utf-8")
            (path / "smuggled.py").write_text("print('hi')\n", encoding="utf-8")
        return 0

    _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    _, run, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    first = verify_candidate(session, workspace, settings=docker_settings)
    step = first.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert not first.passed
    assert step is not None and "tracked or unignored source paths" in step.detail

    # Nothing was certified, even though the tree was built and is on disk.
    store = deps._marker_store(workspace.git)
    assert deps._marker_payload(store, "vendor") is None
    assert (workspace.path / "vendor" / "installed.txt").exists()

    # Undo the damage so the retry starts from the same place the first run did.
    (workspace.path / "smuggled.py").unlink()

    # The retry re-runs bootstrap rather than trusting the refused tree, and
    # reaches the same verdict.
    with pytest.raises(deps.DependencyBootstrapError, match="tracked or unignored"):
        deps.bootstrap_dependencies(
            session,
            project,
            run,
            worktree_path=workspace.path,
            worktree_git=workspace.git,
            integration_sha=workspace.starting_commit,
            settings=docker_settings,
            prefix="retry/",
        )


# --- defect 4: a failed restore must keep the last good backup -------------


def test_publish_one_keeps_the_backup_when_restoration_fails(tmp_path: Path):
    """The rollback used to be undone by its own cleanup.

    ``finally`` deleted the backup unconditionally, including after the restore
    in ``except`` had itself failed -- destroying the only remaining copy of the
    previously published tree.
    """
    source = tmp_path / "src" / "vendor"
    source.mkdir(parents=True)
    (source / "new.txt").write_text("NEW", encoding="utf-8")
    target = tmp_path / "repo" / "vendor"
    target.mkdir(parents=True)
    (target / "precious.txt").write_text("LAST GOOD COPY", encoding="utf-8")

    real_replace = deps.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:  # target -> backup succeeds
            return real_replace(src, dst)
        raise OSError(28, "No space left on device")  # staging -> target, restore

    monkeypatch_replace = deps.os.replace
    deps.os.replace = flaky_replace
    try:
        with pytest.raises(deps.DependencyBootstrapError, match="kept at"):
            deps._publish_one(source, target)
    finally:
        deps.os.replace = monkeypatch_replace

    backups = [p for p in (tmp_path / "repo").iterdir() if "orchestrator-replaced" in p.name]
    assert len(backups) == 1, f"the last good tree was not preserved: {backups}"
    assert (backups[0] / "precious.txt").read_text(encoding="utf-8") == "LAST GOOD COPY"
    staging = [p for p in (tmp_path / "repo").iterdir() if "orchestrator-staging" in p.name]
    assert staging == [], "staging must still be cleaned up"


def test_publish_one_removes_the_backup_on_success(tmp_path: Path):
    """The backup is cleanup, not an archive, when publication works."""
    source = tmp_path / "src" / "vendor"
    source.mkdir(parents=True)
    (source / "new.txt").write_text("NEW", encoding="utf-8")
    target = tmp_path / "repo" / "vendor"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    deps._publish_one(source, target)

    assert (target / "new.txt").read_text(encoding="utf-8") == "NEW"
    assert not (target / "old.txt").exists()
    assert sorted(p.name for p in (tmp_path / "repo").iterdir()) == ["vendor"]


# --- defect 5: validation and filesystem errors are outcomes --------------


def test_validation_error_preparing_dependencies_blocks_integration(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """A ValueError used to escape into a path documented as never raising."""
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    task, run, workspace = _workspace(session, project, docker_settings)
    (workspace.path / "README.md").write_text("# Changed\n", encoding="utf-8")
    candidate = commit_task_work(session, workspace, task)

    def refuse(*args, **kwargs):
        raise ValueError("Dependency path 'vendor' must be ignored by Git")

    monkeypatch.setattr(integration_service, "prepopulate_dependencies", refuse)
    before = run_git(
        dependency_repo, "rev-parse", integration_service.INTEGRATION_BRANCH
    ).strip()

    result = integrate_candidate(
        session, project, task, run, candidate, settings=docker_settings
    )

    assert not result.advanced
    assert any("dependency preparation" in c for c in result.failed_commands)
    assert result.escalation_id is not None
    after = run_git(
        dependency_repo, "rev-parse", integration_service.INTEGRATION_BRANCH
    ).strip()
    assert after == before


def test_filesystem_error_publishing_dependencies_blocks_integration(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """An OSError during publication is a blocked integration, not a crash."""
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    task, run, workspace = _workspace(session, project, docker_settings)
    (workspace.path / "README.md").write_text("# Changed\n", encoding="utf-8")
    candidate = commit_task_work(session, workspace, task)

    def full_disk(source: Path, target: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(deps, "_publish_one", full_disk)
    before = run_git(
        dependency_repo, "rev-parse", integration_service.INTEGRATION_BRANCH
    ).strip()

    result = integrate_candidate(
        session, project, task, run, candidate, settings=docker_settings
    )

    assert not result.advanced
    assert any("No space left on device" in c for c in result.failed_commands)
    after = run_git(
        dependency_repo, "rev-parse", integration_service.INTEGRATION_BRANCH
    ).strip()
    assert after == before


def test_filesystem_error_during_bootstrap_fails_verification_cleanly(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """And on the task path it is a failed step with a readable reason."""
    _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(deps, "_write_marker", full_disk)

    report = verify_candidate(session, workspace, settings=docker_settings)

    step = report.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert not report.passed
    assert step is not None
    assert "No space left on device" in step.detail


# --- item 8: the snapshot must not mutate the index -----------------------


def test_visible_snapshot_is_read_only(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
):
    """It used to stage the whole worktree intent-to-add in order to audit it."""
    project = _project(session, dependency_repo)
    (dependency_repo / "vendor").mkdir()
    (dependency_repo / "vendor" / "installed.txt").write_text("x", encoding="utf-8")
    _, _, workspace = _workspace(session, project, docker_settings)
    (workspace.path / "untracked.txt").write_text("scratch\n", encoding="utf-8")
    (workspace.path / "README.md").write_text("# edited\n", encoding="utf-8")

    # ``ls-files`` is the probe that works here: an intent-to-add entry is in
    # the index but is deliberately invisible to ``diff --cached``, so the
    # obvious check would pass even while the index was being rewritten.
    index_before = run_git(workspace.path, "ls-files")
    snapshot = deps._visible_snapshot(workspace.git)
    index_after = run_git(workspace.path, "ls-files")

    assert index_after == index_before
    assert "untracked.txt" not in index_after
    # Purity must not cost detection: the untracked file is still in the status.
    assert any(entry[0] == "untracked.txt" for entry in snapshot[0])


def test_snapshot_still_detects_bootstrap_source_changes_without_staging(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """The rejection still fires, and the index is untouched afterwards."""

    def callback(path: Path, command: ApprovedCommand, network: str) -> int:
        if command.source == BOOTSTRAP_COMMAND:
            (path / "vendor").mkdir(exist_ok=True)
            (path / "vendor" / "installed.txt").write_text("ok", encoding="utf-8")
            (path / "smuggled.py").write_text("print('hi')\n", encoding="utf-8")
        return 0

    _install_fake_worker(monkeypatch, callback)
    project = _project(session, dependency_repo, bootstrap=True)
    _, _, workspace = _workspace(session, project, docker_settings)
    _make_candidate_change(workspace)

    report = verify_candidate(session, workspace, settings=docker_settings)

    step = report.step_for(VerificationType.DEPENDENCY_BOOTSTRAP)
    assert not report.passed
    assert step is not None and "smuggled.py" in step.detail
    assert "smuggled.py" not in run_git(workspace.path, "ls-files"), (
        "auditing the bootstrap must not stage what it found"
    )


# --- item 3: several declared paths, publication and recovery -------------


def test_multiple_dependency_paths_publish_together_and_are_reused(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    networks = _install_fake_worker(
        monkeypatch, _bootstrap_creates("vendor/installed.txt", "extra/payload.txt")
    )
    project = _project(
        session, dependency_repo, bootstrap=True, dependency_paths=["vendor", "extra"]
    )
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced
    assert (dependency_repo / "vendor" / "installed.txt").exists()
    assert (dependency_repo / "extra" / "payload.txt").exists()

    networks.clear()
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-2", content="# Two\n"
    ).advanced
    assert "bridge" not in networks


def test_half_published_dependency_set_is_rebuilt_not_half_trusted(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """A set is certified as a set, so half of one is worth nothing."""
    networks = _install_fake_worker(
        monkeypatch, _bootstrap_creates("vendor/installed.txt", "extra/payload.txt")
    )
    project = _project(
        session, dependency_repo, bootstrap=True, dependency_paths=["vendor", "extra"]
    )
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced

    # Simulate a publication that landed one path and lost the other.
    shutil.rmtree(dependency_repo / "extra")

    networks.clear()
    task = _task(session, project, "T-NEXT")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=docker_settings)

    assert not (workspace.path / "vendor").exists(), (
        "half a set must not be copied in piecemeal"
    )
    (workspace.path / "README.md").write_text("# Next\n", encoding="utf-8")
    assert verify_candidate(session, workspace, settings=docker_settings).passed
    assert "bridge" in networks
    assert (workspace.path / "extra" / "payload.txt").exists()


# --- item: human integration, inputs unchanged and changed ----------------


def test_human_integration_without_dependency_changes_keeps_the_set_valid(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """Advancing the baseline by hand is not a dependency-input change."""
    networks = _install_fake_worker(monkeypatch, _bootstrap_creates_vendor)
    project = _project(session, dependency_repo, bootstrap=True)
    assert _integrate_one_task(
        session, project, docker_settings, external_task_id="T-1", content="# One\n"
    ).advanced

    # A new file, so the operator's commit merges cleanly into the baseline the
    # first task already advanced. The point is the *absence* of a manifest
    # change, not the merge.
    (dependency_repo / "NOTES.md").write_text("# Operator notes\n", encoding="utf-8")
    run_git(dependency_repo, "add", "-A")
    run_git(dependency_repo, "commit", "--quiet", "-m", "Operator adds notes")
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

    networks.clear()
    task = _task(session, project, "T-NEXT")
    run = create_run(session, task.id)
    workspace = prepare_workspace(session, run.id, settings=docker_settings)
    (workspace.path / "README.md").write_text("# After the operator\n", encoding="utf-8")

    assert (workspace.path / "vendor" / "installed.txt").exists(), (
        "the published set is still what these inputs call for"
    )
    assert verify_candidate(session, workspace, settings=docker_settings).passed
    assert "bridge" not in networks


# --- item: containment of the copy target --------------------------------


def test_copy_target_containment_is_enforced_through_an_intermediate_symlink(
    tmp_path: Path,
):
    """The copy target was the one path this module did not containment-check.

    A tracked symlink at an intermediate component resolves out of the tree, and
    the target is now checked with the same rule as every source path. Tested
    directly because Git refuses such a path earlier in the real flow -- see
    ``test_dependency_path_beyond_a_symlink_is_refused`` -- which makes this the
    only place the second line of defence is observable.
    """
    worktree = tmp_path / "wt"
    (worktree / "inner").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "linked").symlink_to(outside, target_is_directory=True)

    # A declared path under a real directory is fine.
    assert deps._contained(worktree, Path("inner/dep"), label="inner/dep") == (
        worktree / "inner" / "dep"
    )

    # The same shape through a symlink is not.
    with pytest.raises(ValueError, match="escapes its root"):
        deps._contained(worktree, Path("linked/dep"), label="linked/dep")

    assert not (outside / "dep").exists()


def test_dependency_path_beyond_a_symlink_is_refused(
    session: Session,
    tmp_path: Path,
    docker_settings: Settings,
):
    """End to end, such a path never reaches the copy at all.

    ``git check-ignore`` fails with "beyond a symbolic link", so the path cannot
    satisfy the must-be-ignored rule and is rejected as a configuration fault.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    repository = tmp_path / "repo"
    repository.mkdir()
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    (repository / ".gitignore").write_text("linked/dep\ndep\n", encoding="utf-8")
    (repository / "README.md").write_text("# Project\n", encoding="utf-8")
    (repository / "linked").symlink_to(outside, target_is_directory=True)
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "Initial")

    project = _project(
        session, repository, bootstrap=True, dependency_paths=["linked/dep"]
    )
    task = _task(session, project)
    run = create_run(session, task.id)

    with pytest.raises(ValueError, match="must be ignored by Git|escapes its root"):
        prepare_workspace(session, run.id, settings=docker_settings)

    assert not (outside / "dep").exists(), "nothing may be written outside the tree"


# --- manifest-read failures and lock namespacing (review follow-up) ----------


def test_missing_tracked_manifest_fingerprints_as_absent(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
):
    """A tracked manifest deleted from the working tree is a real input state."""
    project = _project(session, dependency_repo, bootstrap=True)
    git = _repo_git(dependency_repo, docker_settings)

    assert deps._manifest_bytes(git, "requirements.txt") == b"example==1\n"
    (dependency_repo / "requirements.txt").unlink()

    assert deps._manifest_bytes(git, "requirements.txt") == b"\0absent\0"
    # And it is a *distinct* fingerprint, not the one an empty manifest gives.
    with_absent = deps.dependency_fingerprint(project, git, settings=docker_settings)
    (dependency_repo / "requirements.txt").write_text("", encoding="utf-8")
    with_empty = deps.dependency_fingerprint(project, git, settings=docker_settings)
    assert with_absent != with_empty


def test_unreadable_manifest_is_a_deterministic_dependency_failure(
    session: Session,
    dependency_repo: Path,
    docker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    """A non-missing-file OSError must not masquerade as an absent manifest."""
    project = _project(session, dependency_repo, bootstrap=True)
    git = _repo_git(dependency_repo, docker_settings)
    original = Path.read_bytes

    def refusing_read_bytes(self: Path) -> bytes:
        if self.name == "requirements.txt":
            raise PermissionError(13, "Permission denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", refusing_read_bytes)

    with pytest.raises(PermissionError):
        deps._manifest_bytes(git, "requirements.txt")

    with pytest.raises(deps.DependencyBootstrapError) as failure:
        deps.dependency_fingerprint(project, git, settings=docker_settings)

    assert "reading dependency manifests" in str(failure.value)
    assert isinstance(failure.value, deps.DEPENDENCY_FAILURES)


def test_publication_lock_is_shared_across_worktree_roots(
    session: Session,
    dependency_repo: Path,
    tmp_path: Path,
    docker_settings: Settings,
):
    """Two orchestrators over one repository must take the *same* lock file."""
    other = Settings(
        _env_file=None,
        artifact_root=tmp_path / "other-data",
        worktree_root=tmp_path / "other-worktrees",
        worker_backend=WorkerBackend.DOCKER,
        worker_network="none",
    )
    assert other.worktree_root != docker_settings.worktree_root
    project = _project(session, dependency_repo, bootstrap=True)

    opened: list[Path] = []
    original_open = Path.open

    def recording_open(self: Path, *args, **kwargs):
        if self.name.startswith("dependency-publication-"):
            opened.append(self)
        return original_open(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", recording_open)
        with deps._dependency_lock(project, settings=docker_settings):
            pass
        with deps._dependency_lock(project, settings=other):
            pass

    assert len(opened) == 2
    assert opened[0] == opened[1]
    assert opened[0].parent == dependency_repo / ".git" / "orchestrator-locks"
    assert not (docker_settings.worktree_root / "locks").exists()
    assert not (other.worktree_root / "locks").exists()
