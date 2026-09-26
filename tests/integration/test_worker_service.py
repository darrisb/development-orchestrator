"""The worker runtime and the command runner (build.md sections 11, 12, phase D).

The phase D exit condition: *the orchestrator can execute configured
verification commands inside an isolated worker.* The first group of tests is
that sentence, against a real task run's worktree.

These run on the subprocess backend. That is the honest thing for a test suite
that must pass on a machine without Docker, and it does exercise everything that
is not the container flags: the policy, the argument vector, the capture, the
ceilings, the timeout kill, the redaction, the log artifacts and the cleanup.
The Docker backend's own argument vector is asserted separately, without a
daemon, because the flags in it *are* the isolation and a typo in one is not
something to discover in production.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.commands import CommandPolicy, CommandRejected
from apps.orchestrator.domain.enums import TaskStatus, WorkerProfile
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.domain.redaction import PLACEHOLDER
from apps.orchestrator.repositories import (
    ArtifactRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services.command_execution import (
    execute_commands,
    run_task_commands,
    store_command_log,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.worker_errors import (
    WorkerNotRunning,
    WorkspaceMountRejected,
)
from apps.orchestrator.services.worker_service import (
    CONTAINER_WORKDIR,
    OUTPUT_TRUNCATION_MARKER,
    Worker,
    build_spec,
    start_worker,
    worker_session,
)
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration

#: The subprocess backend runs whatever the policy allows, so these tests need
#: an executable that is both on the Python profile's allow-list and present on
#: the machine. ``python3`` stands in for ``npm test``.
PYTHON = "python3"

pytest.importorskip("sqlalchemy")
if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)


@pytest.fixture
def worker_settings(tmp_path: Path) -> Settings:
    """Settings for a worker that runs on the host, isolated from any `.env`."""
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    """A managed repository with a script standing in for `npm test`."""
    repo = tmp_path / "tracestack"
    repo.mkdir()
    (repo / "verify.py").write_text(
        "import sys\nprint('42 tests passed')\nsys.exit(0)\n", encoding="utf-8"
    )
    (repo / "fail.py").write_text(
        "import sys\nprint('1 test failed', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


@pytest.fixture
def project(session: Session, project_repo: Path) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            repository_path=str(project_repo),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
        )
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="TS-004",
            title="Implement navigation tree",
            verify_commands=[f"{PYTHON} verify.py"],
        )
    )
    return tasks.transition(task.id, TaskStatus.READY)


@pytest.fixture
def run(session: Session, task: Task) -> TaskRun:
    return create_run(session, task.id)


@pytest.fixture
def workspace(session: Session, run: TaskRun, worker_settings: Settings) -> TaskWorkspace:
    return prepare_workspace(session, run.id, settings=worker_settings)


# --- the exit condition ------------------------------------------------------


def test_the_orchestrator_runs_a_task_configured_command_in_an_isolated_worker(
    session: Session, workspace: TaskWorkspace, run: TaskRun, worker_settings: Settings
):
    """Phase D's exit condition, in one test."""
    executions = run_task_commands(session, workspace, settings=worker_settings)

    assert len(executions) == 1
    execution = executions[0]
    assert execution.succeeded
    assert execution.result.exit_code == 0
    assert "42 tests passed" in execution.result.stdout
    # The command ran in the run's own worktree, not the managed repository.
    assert execution.result.command.argv == (PYTHON, "verify.py")
    # Its output is on disk, recorded against the run.
    assert execution.log_artifact
    log = (worker_settings.artifact_root / execution.log_artifact).read_text()
    assert "$ " + execution.result.command.display in log
    assert "42 tests passed" in log


def test_a_failing_command_is_a_result_and_not_an_exception(
    session: Session, workspace: TaskWorkspace, worker_settings: Settings
):
    """A failing test is the pipeline working. It comes back as data so the
    caller can classify it, rather than as an exception that says less."""
    executions = run_task_commands(
        session, workspace, [f"{PYTHON} fail.py"], settings=worker_settings
    )

    result = executions[0].result
    assert not result.succeeded
    assert result.exit_code == 3
    assert "1 test failed" in result.stderr
    assert "--- stderr ---" in result.combined_output


def test_the_run_records_which_worker_ran_it(
    session: Session, workspace: TaskWorkspace, run: TaskRun, worker_settings: Settings
):
    run_task_commands(session, workspace, settings=worker_settings)

    assert TaskRunRepository(session).get(run.id).worker_image == "subprocess"


def test_a_task_with_no_configured_commands_runs_nothing(
    session: Session, workspace: TaskWorkspace, task: Task, worker_settings: Settings
):
    TaskRepository(session).update_fields(task.id, verify_commands=[])

    assert run_task_commands(session, workspace, settings=worker_settings) == ()


def test_each_command_gets_its_own_log_under_its_category(
    session: Session, workspace: TaskWorkspace, worker_settings: Settings
):
    """Section 9's run layout: build/, lint/, tests/ each hold their own logs."""
    executions = run_task_commands(
        session,
        workspace,
        [f"{PYTHON} verify.py", f"{PYTHON} verify.py"],
        category="tests",
        settings=worker_settings,
    )

    paths = [execution.log_artifact for execution in executions]
    assert [Path(path).name for path in paths] == [
        f"01-{PYTHON}-verify-py.log",
        f"02-{PYTHON}-verify-py.log",
    ]
    assert all("/tests/" in path for path in paths)
    kinds = {artifact.kind for artifact in ArtifactRepository(session).list_for_run(
        workspace.task_run_id
    )}
    assert "tests-log" in kinds


def test_a_sequence_stops_at_the_first_failure(
    session: Session, workspace: TaskWorkspace, worker_settings: Settings
):
    """Section 17: a deterministic failure goes back to the coder rather than
    on to the next check."""
    executions = run_task_commands(
        session,
        workspace,
        [f"{PYTHON} fail.py", f"{PYTHON} verify.py"],
        settings=worker_settings,
    )

    assert len(executions) == 1
    assert not executions[0].succeeded


def test_a_command_that_was_never_permitted_stops_the_sequence_before_it_starts(
    session: Session, workspace: TaskWorkspace, worker_settings: Settings
):
    """Discovering the third command was not permitted after the first two
    passed would report a result for a check that never happened."""
    with pytest.raises(CommandRejected):
        run_task_commands(
            session,
            workspace,
            [f"{PYTHON} verify.py", "git push"],
            settings=worker_settings,
        )

    assert ArtifactRepository(session).list_for_run(workspace.task_run_id) == []


# --- isolation ---------------------------------------------------------------


def test_a_worker_is_only_ever_given_a_task_run_worktree(
    project_repo: Path, worker_settings: Settings
):
    """The mount is the whole of a worker's write access. A worker handed the
    managed repository could change work the orchestrator has not branched."""
    with pytest.raises(WorkspaceMountRejected, match="WORKTREE_ROOT"):
        build_spec(project_repo, profile=WorkerProfile.PYTHON, settings=worker_settings)


def test_a_missing_directory_is_refused(tmp_path: Path, worker_settings: Settings):
    with pytest.raises(WorkspaceMountRejected, match="not a directory"):
        build_spec(
            worker_settings.worktree_root / "nothing-here",
            profile=WorkerProfile.PYTHON,
            settings=worker_settings,
        )


def test_a_containerized_orchestrator_translates_only_the_validated_mount_suffix(
    workspace: TaskWorkspace, tmp_path: Path
):
    host_root = tmp_path / "host-worktrees"
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=workspace.path.parents[1],
        host_worktree_root=host_root,
        worker_backend=WorkerBackend.DOCKER,
    )

    spec = build_spec(workspace.path, profile=WorkerProfile.PYTHON, settings=settings)

    assert spec.mount_source == host_root / workspace.path.relative_to(
        settings.worktree_root
    )


def test_the_worker_environment_is_built_not_inherited(
    workspace: TaskWorkspace, worker_settings: Settings, monkeypatch
):
    """Section 11: secrets are injected individually, never as the host
    environment. A variable this process holds must not simply appear."""
    monkeypatch.setenv("MY_HOST_VARIABLE", "leaked")

    spec = build_spec(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    )

    assert "MY_HOST_VARIABLE" not in spec.environment
    assert spec.environment["CI"] == "true"
    assert spec.environment["HOME"] == "/tmp"


def test_a_passthrough_variable_is_copied_and_a_credential_shaped_one_is_not(
    workspace: TaskWorkspace, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("TZ", "Europe/Dublin")
    monkeypatch.setenv("NPM_TOKEN", "must-not-travel")
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_env_passthrough="TZ,NPM_TOKEN",
    )

    spec = build_spec(workspace.path, profile=WorkerProfile.PYTHON, settings=settings)

    assert spec.environment["TZ"] == "Europe/Dublin"
    assert "NPM_TOKEN" not in spec.environment


def test_a_spec_describes_itself_without_printing_a_secret(
    workspace: TaskWorkspace, worker_settings: Settings
):
    spec = build_spec(
        workspace.path,
        profile=WorkerProfile.PYTHON,
        settings=worker_settings,
        secrets={"NPM_TOKEN": "must-not-be-logged"},
    )

    described = spec.describe()

    assert "must-not-be-logged" not in repr(described)
    assert described["secrets_injected"] == ["NPM_TOKEN"]


# --- capture, ceilings and cleanup -------------------------------------------


def test_a_secret_given_to_a_worker_is_redacted_out_of_what_it_prints(
    session: Session, workspace: TaskWorkspace, worker_settings: Settings
):
    """Sections 12 and 36: output becomes an artifact and part of a reviewer's
    package, so it is masked as it is captured -- before it reaches the disk."""
    (workspace.path / "leak.py").write_text(
        "import os\nprint('using', os.environ['NPM_TOKEN'])\n", encoding="utf-8"
    )

    with worker_session(
        workspace.path,
        profile=WorkerProfile.PYTHON,
        settings=worker_settings,
        secrets={"NPM_TOKEN": "a-real-looking-secret-value"},
    ) as worker:
        result = worker.run(f"{PYTHON} leak.py")
        artifact = store_command_log(
            session, workspace.task_run_id, result, settings=worker_settings
        )

    assert "a-real-looking-secret-value" not in result.stdout
    assert PLACEHOLDER in result.stdout
    stored = (worker_settings.artifact_root / artifact).read_text()
    assert "a-real-looking-secret-value" not in stored


def test_output_past_the_ceiling_is_clipped_with_a_visible_marker(
    workspace: TaskWorkspace, tmp_path: Path
):
    """A reader must never mistake a clipped log for a complete one."""
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_max_output_bytes=2048,
    )
    (workspace.path / "loud.py").write_text(
        "for index in range(20000): print('line', index)\n", encoding="utf-8"
    )

    with worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        result = worker.run(f"{PYTHON} loud.py")

    assert result.stdout_truncated
    assert result.stdout.endswith(OUTPUT_TRUNCATION_MARKER)
    # The command still ran to completion: draining past the ceiling is what
    # keeps a full pipe from turning an output limit into a hang.
    assert result.exit_code == 0
    assert result.output_bytes > 2048


def test_a_hanging_command_is_killed_and_reported_as_a_timeout(
    workspace: TaskWorkspace, tmp_path: Path
):
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=1,
    )
    (workspace.path / "hang.py").write_text(
        "import time\nprint('starting', flush=True)\ntime.sleep(120)\n", encoding="utf-8"
    )

    with worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        result = worker.run(f"{PYTHON} hang.py")

        assert result.timed_out
        assert not result.succeeded
        # No exit code: a timeout produced no verdict, and recording 0 would
        # let a caller that checks only the exit code read a kill as a pass.
        assert result.exit_code is None
        # The output captured before the kill is kept: a hanging suite's last
        # lines are usually the whole diagnosis.
        assert "starting" in result.stdout
        # A timed-out worker is not reused -- the process may have survived the
        # kill of whatever was watching it.
        with pytest.raises(WorkerNotRunning, match="timed out"):
            worker.run(f"{PYTHON} verify.py")


def test_a_command_runs_in_the_worktree_and_not_in_the_orchestrator_directory(
    workspace: TaskWorkspace, worker_settings: Settings
):
    (workspace.path / "where.py").write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")

    with worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    ) as worker:
        result = worker.run(f"{PYTHON} where.py")

    assert result.stdout.strip() == str(workspace.path)
    assert os.getcwd() not in result.stdout


def test_a_worker_is_destroyed_when_its_block_ends_even_on_an_exception(
    workspace: TaskWorkspace, worker_settings: Settings
):
    held: list[Worker] = []
    with pytest.raises(RuntimeError), worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    ) as worker:
        held.append(worker)
        raise RuntimeError("the attempt failed")

    with pytest.raises(WorkerNotRunning):
        held[0].run(f"{PYTHON} verify.py")


def test_a_closed_worker_refuses_further_commands(
    workspace: TaskWorkspace, worker_settings: Settings
):
    worker = start_worker(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    )
    worker.close()
    # Cleanup is idempotent: it runs on failure paths where a second error
    # would bury the first.
    worker.close()

    with pytest.raises(WorkerNotRunning):
        worker.run(f"{PYTHON} verify.py")


def test_policy_is_applied_even_to_a_caller_that_already_holds_a_worker(
    workspace: TaskWorkspace, worker_settings: Settings
):
    """There is no path into execution that skips policy."""
    with worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    ) as worker:
        with pytest.raises(CommandRejected):
            worker.run("git push")
        with pytest.raises(CommandRejected):
            worker.run(f"{PYTHON} verify.py && rm -rf /")


def test_the_tail_of_a_log_is_what_goes_back_to_a_coder(
    workspace: TaskWorkspace, worker_settings: Settings
):
    """The end of a failing build is where the reason is; the whole log would
    spend the coder's context window on a successful prelude."""
    (workspace.path / "long_fail.py").write_text(
        "import sys\n"
        "for index in range(200): print('step', index)\n"
        "print('ERROR: the last line is the reason')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    with worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    ) as worker:
        result = worker.run(f"{PYTHON} long_fail.py")

    tail = result.tail(lines=10)
    assert "ERROR: the last line is the reason" in tail
    assert "step 1\n" not in tail
    assert tail.startswith("[... earlier output omitted ...]")


# --- the Docker backend's isolation flags ------------------------------------


def test_the_container_is_created_with_every_restriction_section_11_requires(
    workspace: TaskWorkspace, tmp_path: Path
):
    """These flags *are* the isolation, so they are asserted rather than
    trusted. No daemon is involved: the argv is captured before it is run."""
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.DOCKER,
        worker_node_image="orchestrator-worker-node:test",
    )
    spec = build_spec(workspace.path, profile=WorkerProfile.NODE, settings=settings)
    worker = Worker(spec, CommandPolicy(profile=WorkerProfile.NODE))
    captured: list[tuple[str, ...]] = []

    def fake_docker(*args: str, timeout: int) -> str:
        captured.append(args)
        return "container123\n"

    worker._docker = fake_docker  # type: ignore[method-assign]
    worker.start()

    argv = " ".join(captured[0])
    assert worker.container_id == "container123"
    assert f"--volume {workspace.path}:{CONTAINER_WORKDIR}:rw" in argv
    assert "--network none" in argv
    assert "--read-only" in argv
    assert "--cap-drop ALL" in argv
    assert "--security-opt no-new-privileges" in argv
    assert "--pids-limit 512" in argv
    assert "--memory 4g" in argv
    assert "--tmpfs /tmp:rw,exec,nosuid,size=512m" in argv
    assert "orchestrator-worker-node:test" in argv
    # No Docker socket, and nothing of the host but the worktree.
    assert "docker.sock" not in argv
    assert "--privileged" not in argv


def test_a_command_in_a_container_is_an_exec_with_a_fixed_workdir(
    workspace: TaskWorkspace, tmp_path: Path
):
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.DOCKER,
    )
    spec = build_spec(workspace.path, profile=WorkerProfile.NODE, settings=settings)
    worker = Worker(spec, CommandPolicy(profile=WorkerProfile.NODE))
    worker.container_id = "container123"

    argv = worker._argv_for(worker.policy.approve("npm test"))

    assert argv == (
        "docker",
        "exec",
        "--workdir",
        CONTAINER_WORKDIR,
        "container123",
        "timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "900s",
        "npm",
        "test",
    )


def test_a_closed_container_worker_reports_its_cleanup(
    workspace: TaskWorkspace, tmp_path: Path
):
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.DOCKER,
    )
    spec = build_spec(workspace.path, profile=WorkerProfile.NODE, settings=settings)
    worker = Worker(spec, CommandPolicy(profile=WorkerProfile.NODE))
    worker.container_id = "container123"
    calls: list[tuple[str, ...]] = []
    worker._docker = lambda *args, timeout: calls.append(args) or ""  # type: ignore[method-assign]

    worker.close()
    worker.close()  # idempotent

    assert calls == [("rm", "--force", "--volumes", "container123")]


def test_the_subprocess_backend_runs_commands_directly(
    workspace: TaskWorkspace, worker_settings: Settings
):
    spec = build_spec(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    )
    worker = Worker(spec, CommandPolicy(profile=WorkerProfile.PYTHON))

    assert worker._argv_for(worker.policy.approve(f"{PYTHON} verify.py")) == (
        PYTHON,
        "verify.py",
    )
    assert spec.workdir == str(workspace.path)


def test_execute_commands_records_a_log_per_command_against_the_run(
    session: Session, workspace: TaskWorkspace, worker_settings: Settings
):
    with worker_session(
        workspace.path, profile=WorkerProfile.PYTHON, settings=worker_settings
    ) as worker:
        executions = execute_commands(
            session,
            worker,
            workspace.task_run_id,
            [f"{PYTHON} verify.py"],
            category="build",
            settings=worker_settings,
        )

    artifacts = ArtifactRepository(session).list_for_run(workspace.task_run_id)
    assert [artifact.kind for artifact in artifacts] == ["build-log"]
    assert artifacts[0].sha256
    assert artifacts[0].size_bytes > 0
    assert executions[0].log_artifact == artifacts[0].path
