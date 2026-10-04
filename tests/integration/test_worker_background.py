"""The worker's one managed background process (build.md sections 11, 12).

The primitive these tests pin down: a worker can start a long-running program,
keep it alive while it does other work, see whether it is still there, read a
bounded amount of what it printed, and stop it -- and nothing it started ever
outlives a worker lifecycle boundary.

They run on the subprocess backend, which is a real process lifecycle (its own
session, real signals, real exit codes) and needs neither Docker nor a network.
The Docker backend's sibling-container flags and its cleanup sequence are
asserted separately, without a daemon, the same way the worker's own container
flags are: those flags *are* the behaviour.

Nothing here knows what the background program is. There is no readiness poll,
no port, no HTTP: this concern owns the process, and whatever asks for a
process owns the question of when it is useful.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.commands import CommandPolicy, CommandRejected
from apps.orchestrator.domain.enums import WorkerProfile
from apps.orchestrator.domain.redaction import PLACEHOLDER
from apps.orchestrator.services.worker_errors import (
    BackgroundProcessAlreadyRunning,
    BackgroundProcessCleanupFailed,
    BackgroundProcessStartFailed,
    WorkerBackendUnavailable,
    WorkerNotRunning,
)
from apps.orchestrator.services.worker_service import (
    BACKGROUND_LOG_OPTIONS,
    CONTAINER_WORKDIR,
    OUTPUT_TRUNCATION_MARKER,
    BackgroundState,
    Worker,
    build_spec,
    start_worker,
    worker_session,
)

pytestmark = pytest.mark.integration

PYTHON = "python3"

if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)

#: Stands in for `npm start`: prints a line, then stays up until it is told to
#: go. Deliberately tiny, and on the Python profile's allow-list.
SERVER = """
import sys, time
print("serving", flush=True)
print("warning on stderr", file=sys.stderr, flush=True)
time.sleep(300)
"""

#: Stands in for a server that dies on its own -- a port already taken, a
#: missing config -- which is a state the orchestrator must be able to read.
DIES = """
import sys
print("boom", file=sys.stderr, flush=True)
sys.exit(7)
"""

#: Ignores SIGTERM, so only the forced stage of termination can end it.
STUBBORN = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(300)
"""

CHATTY = """
for index in range(5000):
    print(f"line {index} with enough text to pass any sensible ceiling")
import time
time.sleep(300)
"""

LEAKS = """
import os, time
print("token is", os.environ.get("API_TOKEN"), flush=True)
time.sleep(300)
"""


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A directory shaped like a task run's worktree, with the fixtures in it."""
    tree = tmp_path / "worktrees" / "run-1"
    tree.mkdir(parents=True)
    for name, source in (
        ("server.py", SERVER),
        ("dies.py", DIES),
        ("stubborn.py", STUBBORN),
        ("chatty.py", CHATTY),
        ("leaks.py", LEAKS),
    ):
        (tree / name).write_text(source, encoding="utf-8")
    (tree / "verify.py").write_text("print('42 tests passed')\n", encoding="utf-8")
    return tree


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    fields: dict[str, object] = {
        "artifact_root": tmp_path / "data",
        "worktree_root": tmp_path / "worktrees",
        "worker_backend": WorkerBackend.SUBPROCESS,
        "worker_command_timeout_seconds": 30,
        "worker_background_stop_grace_seconds": 2,
        **overrides,
    }
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _settings(tmp_path)


def _await(predicate, timeout: float = 10.0) -> bool:
    """Poll ``predicate`` until it holds; process state is not instantaneous."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    """Ask the operating system, not the handle -- the point is no orphans."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# --- the lifecycle -----------------------------------------------------------


def test_a_long_running_process_starts_and_reports_itself_running(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} server.py")

        assert process.is_running()
        assert process.state is BackgroundState.RUNNING
        assert process.exit_code is None
        assert worker.background is process
        # Running is answered by the process, not by the worker being alive.
        assert _alive(process._process.pid)
        assert _await(lambda: "serving" in process.stdout)


def test_a_process_that_exits_on_its_own_reports_its_exit_code(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} dies.py")

        assert _await(lambda: not process.is_running())
        assert process.state is BackgroundState.EXITED
        assert process.exit_code == 7
        assert not process.terminated


def test_explicit_termination_stops_the_process_and_is_idempotent(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} server.py")
        pid = process._process.pid

        process.terminate()
        process.terminate()  # cleanup runs on paths already unwinding a failure

        assert not process.is_running()
        assert process.state is BackgroundState.TERMINATED
        assert process.terminated
        assert not _alive(pid)
        # Terminating the process does not cost the worker.
        assert worker.run(f"{PYTHON} verify.py").succeeded


def test_termination_through_the_worker_clears_the_slot(worktree: Path, settings: Settings):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        worker.start_background(f"{PYTHON} server.py")

        worker.terminate_background()
        worker.terminate_background()  # idempotent

        assert worker.background is None
        # The slot is free, so a new one may be started.
        assert worker.start_background(f"{PYTHON} server.py").is_running()


def test_a_second_background_process_is_refused_while_one_is_alive(
    worktree: Path, settings: Settings
):
    """V1 limit: one managed process per worker, refused rather than supervised."""
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        first = worker.start_background(f"{PYTHON} server.py")

        with pytest.raises(BackgroundProcessAlreadyRunning, match="at most one"):
            worker.start_background(f"{PYTHON} server.py")

        assert first.is_running()
        assert worker.background is first


def test_a_finished_process_does_not_block_the_next_one(worktree: Path, settings: Settings):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        first = worker.start_background(f"{PYTHON} dies.py")
        assert _await(lambda: not first.is_running())

        second = worker.start_background(f"{PYTHON} server.py")

        assert second.is_running()
        # The finished handle keeps its own evidence.
        assert first.exit_code == 7


def test_forced_termination_ends_a_process_that_ignores_the_graceful_signal(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} stubborn.py")
        assert _await(lambda: "ready" in process.stdout)
        pid = process._process.pid

        started = time.monotonic()
        process.terminate(grace_seconds=1)
        elapsed = time.monotonic() - started

        assert not _alive(pid)
        assert process.state is BackgroundState.TERMINATED
        # Killed, not exited: the grace is bounded, so cleanup cannot hang.
        assert process.exit_code == -9
        assert elapsed < 20


# --- the cleanup guarantee ---------------------------------------------------


def test_worker_session_kills_the_background_process_when_its_block_ends(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} server.py")
        pid = process._process.pid

    assert not _alive(pid)
    assert process.state is BackgroundState.TERMINATED


def test_an_exception_in_the_block_still_kills_the_background_process(
    worktree: Path, settings: Settings
):
    held: list[int] = []
    with pytest.raises(RuntimeError), worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        held.append(worker.start_background(f"{PYTHON} server.py")._process.pid)
        raise RuntimeError("the attempt failed")

    assert not _alive(held[0])


def test_a_retained_worker_still_does_not_leave_the_process_running(
    tmp_path: Path, worktree: Path
):
    """``WORKER_RETAIN_ON_FAILURE`` keeps a container for inspection. It is not
    a licence to leave an orchestrator-managed process holding the worktree."""
    settings = _settings(tmp_path, worker_retain_on_failure=True)
    held: list[int] = []

    with pytest.raises(RuntimeError), worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        held.append(worker.start_background(f"{PYTHON} server.py")._process.pid)
        raise RuntimeError("the attempt failed")

    assert not _alive(held[0])


def test_closing_a_worker_kills_the_background_process(worktree: Path, settings: Settings):
    worker = start_worker(worktree, profile=WorkerProfile.PYTHON, settings=settings)
    process = worker.start_background(f"{PYTHON} server.py")
    pid = process._process.pid

    worker.close()
    worker.close()  # idempotent

    assert not _alive(pid)
    with pytest.raises(WorkerNotRunning):
        worker.run(f"{PYTHON} verify.py")


def test_a_closed_or_tainted_worker_refuses_to_start_a_background_process(
    tmp_path: Path, worktree: Path
):
    settings = _settings(tmp_path)
    worker = start_worker(worktree, profile=WorkerProfile.PYTHON, settings=settings)
    worker.close()

    with pytest.raises(WorkerNotRunning):
        worker.start_background(f"{PYTHON} server.py")

    # And the same after a timeout has tainted a worker.
    hanging = _settings(tmp_path, worker_command_timeout_seconds=1)
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=hanging
    ) as tainted:
        assert tainted.run(f"{PYTHON} server.py").timed_out
        with pytest.raises(WorkerNotRunning, match="timed out"):
            tainted.start_background(f"{PYTHON} server.py")


def test_cleanup_that_cannot_be_proven_taints_the_worker(worktree: Path, settings: Settings):
    """Fail closed: a process that may still be alive is not a clean worker."""
    worker = start_worker(worktree, profile=WorkerProfile.PYTHON, settings=settings)
    process = worker.start_background(f"{PYTHON} server.py")

    def refuse(**_: object) -> None:
        raise BackgroundProcessCleanupFailed("could not be proven dead")

    process.terminate = refuse  # type: ignore[method-assign]

    with pytest.raises(BackgroundProcessCleanupFailed):
        worker.terminate_background()

    assert worker._tainted
    with pytest.raises(WorkerNotRunning):
        worker.run(f"{PYTHON} verify.py")
    worker._background = None
    worker.close()


# --- policy ------------------------------------------------------------------


def test_the_command_policy_applies_to_a_background_command_too(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        # Shell backgrounding is not how this works, and it is still refused.
        with pytest.raises(CommandRejected, match="shell syntax"):
            worker.start_background(f"{PYTHON} server.py &")
        with pytest.raises(CommandRejected, match="shell syntax"):
            worker.start_background(f"nohup {PYTHON} server.py & echo started")
        # `sh -c` is not a way round it either.
        with pytest.raises(CommandRejected, match="never permitted"):
            worker.start_background("sh -c 'python3 server.py'")
        # Nor is an unapproved executable.
        with pytest.raises(CommandRejected, match="not allowed"):
            worker.start_background("npm start")

        assert worker.background is None


def test_a_background_process_that_cannot_start_is_an_explicit_failure(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        spec = replace(worker.spec, mount_source=worktree / "missing")

        with pytest.raises(BackgroundProcessStartFailed):
            Worker(spec, worker.policy).start_background(f"{PYTHON} server.py")


def test_an_unsupported_backend_refuses_rather_than_simulating_a_process(
    worktree: Path, settings: Settings
):
    """A backend nobody has decided about gets no process, not a fake one."""
    spec = replace(
        build_spec(worktree, profile=WorkerProfile.PYTHON, settings=settings),
        backend="imaginary",  # type: ignore[arg-type]
    )
    worker = Worker(spec, CommandPolicy(profile=WorkerProfile.PYTHON))

    with pytest.raises(WorkerBackendUnavailable, match="no process was started"):
        worker.start_background(f"{PYTHON} server.py")

    assert worker.background is None


# --- foreground work alongside a background process --------------------------


def test_a_foreground_command_runs_while_the_background_process_is_alive(
    worktree: Path, settings: Settings
):
    """The whole point of the primitive: start something, then do work."""
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} server.py")
        assert _await(lambda: "serving" in process.stdout)

        first = worker.run(f"{PYTHON} verify.py")
        second = worker.run(f"{PYTHON} verify.py")

        assert first.succeeded and second.succeeded
        assert "42 tests passed" in first.stdout
        # The foreground commands did not disturb the background process.
        assert process.is_running()


def test_a_worker_with_no_background_process_behaves_exactly_as_before(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        assert worker.background is None
        worker.terminate_background()  # a no-op, not an error

        assert worker.run(f"{PYTHON} verify.py").succeeded


# --- diagnostics -------------------------------------------------------------


def test_diagnostic_output_is_bounded(tmp_path: Path, worktree: Path):
    settings = _settings(tmp_path, worker_background_max_output_bytes=2048)

    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} chatty.py")
        assert _await(lambda: process.stdout.endswith(OUTPUT_TRUNCATION_MARKER))

        # Bounded in memory however much the process goes on printing, and
        # clipped visibly: a reader must not mistake this for the whole log.
        assert len(process.stdout) <= 2048 + len(OUTPUT_TRUNCATION_MARKER) + 16
        assert process.is_running()


def test_diagnostic_output_survives_the_process(worktree: Path, settings: Settings):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} server.py")
        assert _await(lambda: "serving" in process.stdout)
        process.terminate()

    # Still readable after termination and after the worker is gone: this is
    # the evidence a later caller reports with.
    assert "serving" in process.stdout
    assert "warning on stderr" in process.stderr
    assert "--- stderr ---" in process.combined_output
    assert "serving" in process.output_tail(5)
    assert process.describe()["state"] == "terminated"


def test_exit_evidence_is_readable_after_a_process_dies_on_its_own(
    worktree: Path, settings: Settings
):
    with worker_session(
        worktree, profile=WorkerProfile.PYTHON, settings=settings
    ) as worker:
        process = worker.start_background(f"{PYTHON} dies.py")
        assert _await(lambda: not process.is_running())

    assert "boom" in process.stderr
    assert process.exit_code == 7


def test_secrets_are_redacted_from_background_output(worktree: Path, settings: Settings):
    with worker_session(
        worktree,
        profile=WorkerProfile.PYTHON,
        settings=settings,
        secrets={"API_TOKEN": "s3cr3t-value"},
    ) as worker:
        process = worker.start_background(f"{PYTHON} leaks.py")

        assert _await(lambda: PLACEHOLDER in process.stdout)
        assert "s3cr3t-value" not in process.stdout
        assert "s3cr3t-value" not in process.combined_output


# --- the Docker backend, without a daemon ------------------------------------


def _docker_worker(worktree: Path, tmp_path: Path) -> Worker:
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.DOCKER,
        worker_python_image="orchestrator-worker-python:test",
    )
    spec = build_spec(worktree, profile=WorkerProfile.PYTHON, settings=settings)
    worker = Worker(spec, CommandPolicy(profile=WorkerProfile.PYTHON))
    worker.container_id = "worker123"
    return worker


def test_a_background_process_in_a_container_is_a_restricted_sibling(
    worktree: Path, tmp_path: Path
):
    """A sibling container, not a `docker exec`: `docker exec` does not forward
    signals, so stopping it would leave the program running -- the orphan this
    primitive exists to prevent. The flags are asserted because they are the
    isolation."""
    worker = _docker_worker(worktree, tmp_path)
    calls: list[tuple[str, ...]] = []
    worker._docker = lambda *args, timeout: calls.append(args) or "bg123\n"  # type: ignore[method-assign]

    process = worker.start_background(f"{PYTHON} server.py")

    argv = " ".join(calls[0])
    assert argv.startswith("run --detach --init")
    assert f"--name {worker.spec.worker_id}-bg" in argv
    assert f"--volume {worktree}:{CONTAINER_WORKDIR}:rw" in argv
    # The worker's own network namespace: not a wider one, and reachable from
    # a command the worker runs.
    assert "--network container:worker123" in argv
    assert "--read-only" in argv
    assert "--cap-drop ALL" in argv
    assert "--security-opt no-new-privileges" in argv
    assert "--pids-limit 512" in argv
    assert "orchestrator-worker-python:test python3 server.py" in argv
    # Bounded on the host too: the handle bounds memory, this bounds the file
    # the daemon keeps behind it.
    for option in BACKGROUND_LOG_OPTIONS:
        assert f"--log-opt {option}" in argv
    # Not `--rm`: the exit code and the log of a process that died on its own
    # are the evidence for why it died.
    assert "--rm" not in argv.split()
    assert process._container == "bg123"  # type: ignore[attr-defined]


def test_a_container_background_process_reads_its_state_from_the_container(
    worktree: Path, tmp_path: Path
):
    worker = _docker_worker(worktree, tmp_path)
    worker._docker = lambda *args, timeout: "bg123\n"  # type: ignore[method-assign]
    answers = {"inspect": "true 0"}
    calls: list[tuple[str, ...]] = []

    def fake_capture(*args: str, timeout: int, max_output_bytes: int = 64_000):
        calls.append(args)
        from apps.orchestrator.services.worker_service import _Capture

        return _Capture(
            exit_code=0,
            stdout=answers.get(args[0], ""),
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            output_bytes=0,
        )

    worker._docker_capture = fake_capture  # type: ignore[method-assign]
    process = worker.start_background(f"{PYTHON} server.py")

    assert process.is_running()
    # Not "the worker is up, so it must be running".
    assert calls[0][0] == "inspect"

    answers["inspect"] = "false 7"
    assert not process.is_running()
    assert process.state is BackgroundState.EXITED
    assert process.exit_code == 7


def test_a_container_background_process_is_stopped_gracefully_then_removed(
    worktree: Path, tmp_path: Path
):
    worker = _docker_worker(worktree, tmp_path)
    worker._docker = lambda *args, timeout: "bg123\n"  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []

    def fake_capture(*args: str, timeout: int, max_output_bytes: int = 64_000):
        calls.append(args)
        from apps.orchestrator.services.worker_service import _Capture

        return _Capture(
            exit_code=0,
            stdout="true 0" if args[0] == "inspect" else "serving",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            output_bytes=0,
        )

    worker._docker_capture = fake_capture  # type: ignore[method-assign]
    process = worker.start_background(f"{PYTHON} server.py")

    worker.terminate_background()
    process.terminate()  # idempotent

    verbs = [call[0] for call in calls]
    # The log is read out before the container is removed; `docker stop` is the
    # graceful-then-forced sequence; `rm --force` is what makes it certain.
    assert "logs" in verbs
    assert verbs.index("logs") < verbs.index("rm")
    assert ("stop", "--time", "10", "bg123") in calls
    assert ("rm", "--force", "--volumes", "bg123") in calls
    assert process.state is BackgroundState.TERMINATED
    # The output read before removal is still there afterwards.
    assert "serving" in process.stdout
    assert worker.background is None


def test_a_container_that_cannot_be_removed_fails_closed(worktree: Path, tmp_path: Path):
    worker = _docker_worker(worktree, tmp_path)
    worker._docker = lambda *args, timeout: "bg123\n"  # type: ignore[method-assign]

    def fake_capture(*args: str, timeout: int, max_output_bytes: int = 64_000):
        from apps.orchestrator.services.worker_service import _Capture

        failed = args[0] == "rm"
        return _Capture(
            exit_code=1 if failed else 0,
            stdout="" if failed else "true 0",
            stderr="daemon is unreachable" if failed else "",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            output_bytes=0,
        )

    worker._docker_capture = fake_capture  # type: ignore[method-assign]
    worker.start_background(f"{PYTHON} server.py")

    with pytest.raises(BackgroundProcessCleanupFailed, match="could not be removed"):
        worker.terminate_background()

    # Fail closed: the worker's integrity is now unknown, so it is tainted and
    # its own container will be destroyed -- the ultimate cleanup boundary.
    assert worker._tainted


def test_destroying_the_worker_sweeps_up_the_sibling_container(
    worktree: Path, tmp_path: Path
):
    """The background container is a sibling, so the worker's own destruction
    would not take it with it. Close sweeps it up even when the handle's
    cleanup could not."""
    worker = _docker_worker(worktree, tmp_path)
    worker._docker = lambda *args, timeout: "bg123\n"  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []

    def fake_capture(*args: str, timeout: int, max_output_bytes: int = 64_000):
        from apps.orchestrator.services.worker_service import _Capture

        calls.append(args)
        # Every removal fails, so the handle cannot prove its own cleanup.
        failed = args[0] == "rm"
        return _Capture(
            exit_code=1 if failed else 0,
            stdout="" if failed else "true 0",
            stderr="daemon is unreachable" if failed else "",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            output_bytes=0,
        )

    worker._docker_capture = fake_capture  # type: ignore[method-assign]
    worker.start_background(f"{PYTHON} server.py")

    worker.close()

    # The failed handle cleanup did not stop the sweep, and the worker's own
    # container is destroyed after it: container destruction is the boundary.
    assert ("rm", "--force", "--volumes", f"{worker.spec.worker_id}-bg") in calls
