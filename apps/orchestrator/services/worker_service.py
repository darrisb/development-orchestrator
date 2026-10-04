"""Disposable coding workers and the command runner (build.md sections 11, 12).

One worker per active task run, holding exactly one writable mount: that run's
worktree. It executes approved argument vectors, captures what they printed,
enforces a timeout and an output ceiling, redacts secrets from what it captured,
and is destroyed afterwards.

The shape of the module follows section 12's separation. ``CommandPolicy``
(``domain.commands``) decides whether a command may run; ``Worker`` decides
nothing and runs what it is given, in the place it is allowed to run it. There
is no ``run_shell``, no way to pass a command as a string to a shell, and no way
to reach a path outside the mount.

Two backends:

* **Docker** (the default, and the only one to use against code you did not
  write): one container per run, non-root, no capabilities, no new privileges,
  a read-only root filesystem with a tmpfs for scratch, CPU/memory/PID limits,
  no Docker socket, and no network unless an operator turns one on.
* **Subprocess** (``WORKER_BACKEND=subprocess``): the same policy and the same
  capture, in a process on the host. It has *weaker isolation than a container*
  -- a permitted command can still read the developer's home directory -- and
  exists so the loop can be developed on a machine without Docker.

Beside the foreground runner there is one narrow extra capability: a single
orchestrator-managed *background* process per worker (``start_background``).
It exists so a later concern can start a long-running program, do other work
while it runs, and stop it again. It owns the process lifecycle only -- start,
liveness, exit state, bounded output, termination. Readiness (is the thing it
started actually serving?) belongs to whatever asks for it, not here.

Deliberately free of the database: like ``GitService``, this service does the
work and something else records it (``services.command_logs``).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..config.logging import get_logger
from ..config.settings import Settings, WorkerBackend, get_settings
from ..domain.commands import ApprovedCommand, CommandPolicy
from ..domain.enums import WorkerProfile
from ..domain.redaction import Redactor, is_secret_name
from .worker_errors import (
    BackgroundProcessAlreadyRunning,
    BackgroundProcessCleanupFailed,
    BackgroundProcessStartFailed,
    WorkerBackendUnavailable,
    WorkerNotRunning,
    WorkerStartFailed,
    WorkspaceMountRejected,
)

logger = get_logger(__name__)

#: Where the worktree is mounted inside a container. Fixed, because it appears
#: in captured output: a log that says ``/workspace/src/a.ts`` means the same
#: thing whichever project produced it.
CONTAINER_WORKDIR = "/workspace"

#: Appended when captured output hits the ceiling. A reader must never mistake
#: a clipped log for a complete one -- the same rule as a clipped diff.
OUTPUT_TRUNCATION_MARKER = "\n[output truncated by orchestrator]\n"

#: The container's scratch space. The root filesystem is read-only, so anything
#: that needs to write outside the worktree writes here and is discarded with
#: the worker.
_TMPFS_MOUNTS: tuple[tuple[str, str], ...] = (
    ("/tmp", "rw,exec,nosuid,size=512m"),
    ("/home/worker", "rw,nosuid,size=64m"),
)

#: The last lines of a background process' container log that are fetched for
#: diagnostics. A ceiling in lines as well as bytes: ``docker logs`` without
#: one would stream a whole dev server's history through a pipe to be thrown
#: away.
BACKGROUND_LOG_LINES = 2_000

#: Docker's own log-file ceiling for a managed background process. The handle
#: keeps bounded output in memory; this keeps the daemon from accumulating an
#: unbounded file on the host behind it.
BACKGROUND_LOG_OPTIONS: tuple[str, ...] = ("max-size=4m", "max-file=1")

#: Environment every worker gets. No host values: a worker is given what it
#: needs, never what this process happens to hold (section 11).
BASE_ENVIRONMENT: Mapping[str, str] = {
    "HOME": "/tmp",
    "CI": "true",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    # Keep package managers' caches inside the discarded tmpfs rather than in
    # a home directory that does not exist in a read-only container.
    "npm_config_cache": "/tmp/.npm",
    "npm_config_update_notifier": "false",
    "YARN_CACHE_FOLDER": "/tmp/.yarn",
    "PIP_CACHE_DIR": "/tmp/.pip",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "UV_CACHE_DIR": "/tmp/.uv",
    "MAVEN_OPTS": "-Dmaven.repo.local=/tmp/.m2",
    "GRADLE_USER_HOME": "/tmp/.gradle",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one command did.

    A non-zero ``exit_code`` is not an error: a failing test is the pipeline
    working. The failure is reported as data so the caller can classify it
    (section 49) instead of catching an exception that says less.
    """

    command: ApprovedCommand
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    #: Bytes the command actually produced, before the ceiling was applied.
    output_bytes: int = 0
    worker_id: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated

    @property
    def combined_output(self) -> str:
        """Both streams, labelled, as one log artifact."""
        sections = []
        if self.stdout.strip():
            sections.append(f"--- stdout ---\n{self.stdout}")
        if self.stderr.strip():
            sections.append(f"--- stderr ---\n{self.stderr}")
        return "\n".join(sections) if sections else "(no output)"

    def tail(self, lines: int = 40) -> str:
        """The last ``lines`` of output, for feeding a failure back to a coder.

        The end of a failing build is where the reason is; sending the whole
        log would spend the coder's context window on a successful prelude.
        """
        text = self.combined_output.rstrip("\n")
        split = text.splitlines()
        if len(split) <= lines:
            return text
        return "\n".join(["[... earlier output omitted ...]", *split[-lines:]])

    def describe(self) -> dict[str, object]:
        """Metadata only -- the output itself is an artifact on disk."""
        return {
            **self.command.describe(),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "output_bytes": self.output_bytes,
            "worker_id": self.worker_id,
        }


class BackgroundState(StrEnum):
    """What the orchestrator knows about its managed background process.

    ``RUNNING`` is answered by asking the process itself, never by observing
    that the worker is alive: a container that is up says nothing about
    whether the program inside it is still there.
    """

    RUNNING = "running"
    #: Exited on its own, for whatever reason. ``exit_code`` says which.
    EXITED = "exited"
    #: Stopped by the orchestrator (``terminate``, worker close, taint path).
    TERMINATED = "terminated"


class BackgroundProcess:
    """A handle on the one background process a worker manages.

    The worker owns the process; this object is how a caller sees and ends it.
    Use it as a context manager to tie the process to a narrower scope than
    the worker's own -- but the worker will clean it up regardless, and so
    will the container's destruction behind that.

    It answers process questions only: running, exited, exit code, recent
    output. Whether the program has finished starting up, is listening, or
    answers a request is the caller's business, not this handle's.
    """

    def __init__(
        self,
        *,
        command: ApprovedCommand,
        worker_id: str,
        max_output_bytes: int,
        stop_grace_seconds: float,
        redactor: Redactor,
    ) -> None:
        self.command = command
        self.worker_id = worker_id
        self.max_output_bytes = max_output_bytes
        self.stop_grace_seconds = stop_grace_seconds
        self._redactor = redactor
        self._state = BackgroundState.RUNNING
        self._exit_code: int | None = None
        self._cleaned = False

    # --------------------------------------------------------------- state

    @property
    def state(self) -> BackgroundState:
        """The state as of the last observation. ``is_running`` refreshes it."""
        self._refresh()
        return self._state

    @property
    def exit_code(self) -> int | None:
        """The exit code, or ``None`` while running or when it is unknowable."""
        self._refresh()
        return self._exit_code

    def is_running(self) -> bool:
        self._refresh()
        return self._state is BackgroundState.RUNNING

    @property
    def terminated(self) -> bool:
        """True when the orchestrator stopped it, rather than it exiting."""
        return self._state is BackgroundState.TERMINATED

    # ----------------------------------------------------------- diagnostics

    @property
    def stdout(self) -> str:
        """Bounded, redacted stdout. Still available after the process ends."""
        return self._redactor.redact(self._read_stream(error=False))

    @property
    def stderr(self) -> str:
        return self._redactor.redact(self._read_stream(error=True))

    @property
    def combined_output(self) -> str:
        sections = []
        if self.stdout.strip():
            sections.append(f"--- stdout ---\n{self.stdout}")
        if self.stderr.strip():
            sections.append(f"--- stderr ---\n{self.stderr}")
        return "\n".join(sections) if sections else "(no output)"

    def output_tail(self, lines: int = 40) -> str:
        """The last ``lines`` of output -- where a server says why it died."""
        text = self.combined_output.rstrip("\n")
        split = text.splitlines()
        if len(split) <= lines:
            return text
        return "\n".join(["[... earlier output omitted ...]", *split[-lines:]])

    def describe(self) -> dict[str, object]:
        """Metadata only; the output is fetched explicitly."""
        return {
            **self.command.describe(),
            "worker_id": self.worker_id,
            "state": self.state.value,
            "exit_code": self._exit_code,
        }

    # ----------------------------------------------------------- termination

    def terminate(self, *, grace_seconds: float | None = None) -> None:
        """Stop the process: graceful signal, bounded wait, then force.

        Idempotent -- it is called on every cleanup path, including ones that
        are already unwinding another failure.

        Raises:
            BackgroundProcessCleanupFailed: the process could not be proven
                dead. The caller must treat the worker as compromised.
        """
        if self._cleaned:
            return
        grace = self.stop_grace_seconds if grace_seconds is None else grace_seconds
        was_running = self.is_running()
        try:
            self._stop(grace)
        finally:
            self._cleaned = True
        if was_running:
            self._state = BackgroundState.TERMINATED
        logger.info(
            "background_process_terminated",
            worker_id=self.worker_id,
            command=self.command.display,
            was_running=was_running,
            exit_code=self._exit_code,
        )

    def __enter__(self) -> BackgroundProcess:
        return self

    def __exit__(self, *_: object) -> None:
        self.terminate()

    # ------------------------------------------------------------ subclasses

    def _refresh(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _read_stream(self, *, error: bool) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _stop(self, grace_seconds: float) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class _HostBackgroundProcess(BackgroundProcess):
    """A background process on the host (``WorkerBackend.SUBPROCESS``).

    A real process in its own session, with its output drained into bounded
    buffers, terminated by signalling the whole process group -- the same
    mechanism the foreground runner uses on a timeout. Isolation is the
    backend's usual weak isolation; the lifecycle is genuine.
    """

    def __init__(self, *, process: subprocess.Popen, drains: tuple[_Drain, _Drain], **kwargs
                 ) -> None:
        super().__init__(**kwargs)
        self._process = process
        self._out, self._err = drains

    def _refresh(self) -> None:
        if self._state is not BackgroundState.RUNNING:
            return
        code = self._process.poll()
        if code is not None:
            self._exit_code = code
            self._state = BackgroundState.EXITED
            # Let the drains finish so output is complete for whoever reads it
            # after the exit; they are daemon threads on a closed pipe.
            self._out.join(timeout=5)
            self._err.join(timeout=5)

    def _read_stream(self, *, error: bool) -> str:
        return (self._err if error else self._out).text()

    def _stop(self, grace_seconds: float) -> None:
        if self._process.poll() is None:
            _kill_group(self._process, grace_seconds=grace_seconds)
        self._out.join(timeout=5)
        self._err.join(timeout=5)
        code = self._process.poll()
        if code is None:
            raise BackgroundProcessCleanupFailed(
                f"background process {self.command.display!r} survived SIGKILL"
            )
        self._exit_code = code


class _ContainerBackgroundProcess(BackgroundProcess):
    """A background process in its own container, beside the worker's.

    A sibling container rather than a ``docker exec``, for one reason:
    ``docker exec`` does not forward signals, so killing the client leaves the
    program running inside the container -- exactly the orphan this primitive
    exists to prevent. A container can be asked whether it is running, asked
    for its exit code, stopped gracefully then forcibly (``docker stop``), and
    removed with certainty (``docker rm --force``).

    It shares the worker's network namespace, so a program it starts is
    reachable from a command the worker runs. The worker's container remains
    free to run foreground commands the whole time.
    """

    def __init__(
        self,
        *,
        container: str,
        capture: Callable[..., _Capture],
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._container = container
        self._capture = capture
        self._removed = False
        self._frozen_output: dict[bool, str] = {}

    def _refresh(self) -> None:
        if self._state is not BackgroundState.RUNNING:
            return
        result = self._capture("inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}",
                               self._container, timeout=30)
        fields = result.stdout.split()
        if result.exit_code != 0 or len(fields) != 2:
            # The container is gone (or unreadable): it is certainly not
            # running, and its exit code is no longer knowable.
            self._state = BackgroundState.EXITED
            return
        running, code = fields
        if running == "true":
            return
        self._state = BackgroundState.EXITED
        with contextlib.suppress(ValueError):
            self._exit_code = int(code)

    def _read_stream(self, *, error: bool) -> str:
        if self._removed:
            return self._frozen_output.get(error, "")
        result = self._capture(
            "logs", "--tail", str(BACKGROUND_LOG_LINES), self._container,
            timeout=60, max_output_bytes=self.max_output_bytes,
        )
        text = result.stderr if error else result.stdout
        return text

    def _stop(self, grace_seconds: float) -> None:
        # Read the log out before the container is removed: after that there is
        # nothing left to ask, and the output is the diagnosis.
        for error in (False, True):
            with contextlib.suppress(Exception):
                self._frozen_output[error] = self._read_stream(error=error)
        self._refresh()
        # `docker stop` is the graceful-then-forced sequence: SIGTERM, wait
        # --time, SIGKILL. A failure here is not fatal; `rm --force` is.
        stop = self._capture("stop", "--time", str(int(max(0, grace_seconds))),
                             self._container, timeout=int(grace_seconds) + 30)
        if stop.exit_code != 0:
            logger.warning(
                "background_process_stop_failed",
                worker_id=self.worker_id,
                container=self._container,
                error=stop.stderr.strip()[-300:],
            )
        removal = self._capture("rm", "--force", "--volumes", self._container, timeout=60)
        self._removed = True
        if removal.exit_code != 0 and "no such container" not in removal.stderr.lower():
            raise BackgroundProcessCleanupFailed(
                f"the container for {self.command.display!r} could not be removed: "
                f"{removal.stderr.strip()[-300:]}"
            )


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """How one worker is created. Every field is a policy from section 11."""

    worker_id: str
    profile: WorkerProfile
    #: Host directory mounted as the worker's project tree. The only writable
    #: project mount there is.
    mount_source: Path
    image: str | None = None
    backend: WorkerBackend = WorkerBackend.DOCKER
    network: str = "none"
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    command_timeout_seconds: int = 900
    max_output_bytes: int = 1_000_000
    #: Per-stream ceiling for the managed background process' diagnostics.
    background_max_output_bytes: int = 256_000
    #: Grace given to the managed background process before it is killed.
    background_stop_grace_seconds: int = 10
    environment: Mapping[str, str] = field(default_factory=dict)
    user: str | None = None

    @property
    def background_container_name(self) -> str:
        """The sibling container's name. Derived, so it is findable by hand."""
        return f"{self.worker_id}-bg"

    def background_container_name_for(self, slot: str) -> str:
        """The sibling container name for one managed background slot."""
        if slot == "default":
            return self.background_container_name
        return f"{self.worker_id}-bg-{slot}"

    @property
    def runtime_description(self) -> str:
        """What actually ran the commands, for ``task_runs.worker_image``.

        The image name only when there was a container: recording an image for
        a subprocess run would be a false answer to "what ran this?", and that
        column is one of the questions section 9 says a run must be able to
        answer.
        """
        if self.backend is WorkerBackend.DOCKER:
            return self.image or "docker"
        return self.backend.value

    @property
    def workdir(self) -> str:
        return (
            CONTAINER_WORKDIR
            if self.backend is WorkerBackend.DOCKER
            else str(self.mount_source)
        )

    def describe(self) -> dict[str, object]:
        """Safe to log: the environment is masked by variable name."""
        return {
            "worker_id": self.worker_id,
            "profile": self.profile.value,
            "backend": self.backend.value,
            "image": self.image,
            "mount_source": str(self.mount_source),
            "network": self.network,
            "cpus": self.cpus,
            "memory": self.memory,
            "environment": sorted(
                key for key in self.environment if not is_secret_name(key)
            ),
            "secrets_injected": sorted(
                key for key in self.environment if is_secret_name(key)
            ),
        }


class Worker:
    """A live worker. Use it as a context manager; it is destroyed on exit.

    ``run`` takes a command *string* and approves it first, so there is no path
    into execution that skips policy even for a caller holding a worker.
    """

    def __init__(
        self,
        spec: WorkerSpec,
        policy: CommandPolicy,
        *,
        redactor: Redactor | None = None,
        docker_binary: str = "docker",
    ) -> None:
        self.spec = spec
        self.policy = policy
        self.redactor = redactor or Redactor()
        self.docker_binary = docker_binary
        self.container_id: str | None = None
        self._closed = False
        #: Orchestrator-managed background processes, keyed by caller-owned
        #: slot. The legacy singular API uses the ``default`` slot.
        self._backgrounds: dict[str, BackgroundProcess] = {}
        #: Whether this worker has ever had one. Only then is there a sibling
        #: container to sweep up on close.
        self._background_started = False
        self._background_container_names: set[str] = set()
        #: Set when a command had to be killed. The process inside a container
        #: may survive a killed ``docker exec``, so a timed-out worker is
        #: destroyed rather than reused -- see ``run``.
        self._tainted = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> Worker:
        if self.spec.backend is WorkerBackend.DOCKER:
            self.container_id = self._start_container()
        logger.info("worker_started", **self.spec.describe())
        return self

    def close(self, *, retain: bool = False) -> None:
        """Destroy the worker (section 11: destroy after completion or failure).

        Safe to call twice, and never raises: cleanup runs on failure paths,
        where a second error would bury the first.
        """
        if self._closed:
            return
        self._closed = True
        # The managed background process goes first: the container's removal
        # would take it with it, but the subprocess backend has no container
        # behind it, and an explicit kill is what makes the guarantee the same
        # on both backends.
        self._cleanup_background()
        if self.container_id is None:
            return
        # The background process runs in a *sibling* container, so destroying
        # this one would not take it with it. The handle's own cleanup has
        # already run; this is the backstop for the case where it could not.
        if self._background_started:
            names = self._background_container_names or {self.spec.background_container_name}
            for name in names:
                with contextlib.suppress(Exception):
                    self._docker_capture("rm", "--force", "--volumes", name, timeout=60)
        if retain and not self._tainted:
            logger.warning(
                "worker_retained", worker_id=self.spec.worker_id, container=self.container_id
            )
            return
        try:
            self._docker("rm", "--force", "--volumes", self.container_id, timeout=60)
        except Exception as error:  # noqa: BLE001 - cleanup must not mask a failure
            logger.warning(
                "worker_cleanup_failed",
                worker_id=self.spec.worker_id,
                container=self.container_id,
                error=str(error),
            )
        else:
            logger.info("worker_destroyed", worker_id=self.spec.worker_id)

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------- execution

    def run(
        self, command: str | ApprovedCommand, *, timeout_seconds: int | None = None
    ) -> CommandResult:
        """Approve and run one command.

        Raises:
            CommandRejected: the command is not permitted by this policy.
            WorkerNotRunning: the worker has been closed, or a previous command
                timed out and the worker was destroyed with it.
        """
        if self._closed or self._tainted:
            raise WorkerNotRunning(
                f"Worker {self.spec.worker_id} is no longer running"
                + (" (a previous command timed out)" if self._tainted else "")
            )
        approved = (
            command if isinstance(command, ApprovedCommand) else self.policy.approve(command)
        )
        timeout = timeout_seconds or self.spec.command_timeout_seconds

        logger.info(
            "command_started",
            worker_id=self.spec.worker_id,
            command=approved.display,
            timeout_seconds=timeout,
        )
        started = time.monotonic()
        capture = _execute(
            self._argv_for(approved, timeout_seconds=timeout),
            cwd=None if self.spec.backend is WorkerBackend.DOCKER else self.spec.mount_source,
            env=self._process_environment(),
            timeout_seconds=(
                timeout + 7 if self.spec.backend is WorkerBackend.DOCKER else timeout
            ),
            max_output_bytes=self.spec.max_output_bytes,
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        container_timeout = (
            self.spec.backend is WorkerBackend.DOCKER
            and capture.exit_code in {124, 137}
            and not capture.timed_out
        )
        result = CommandResult(
            command=approved,
            exit_code=capture.exit_code,
            stdout=self.redactor.redact(capture.stdout),
            stderr=self.redactor.redact(capture.stderr),
            duration_ms=duration_ms,
            timed_out=capture.timed_out or container_timeout,
            stdout_truncated=capture.stdout_truncated,
            stderr_truncated=capture.stderr_truncated,
            output_bytes=capture.output_bytes,
            worker_id=self.spec.worker_id,
        )
        if capture.timed_out:
            # Killing `docker exec` does not stop the process it started, so the
            # container is destroyed now rather than left running a test suite
            # nobody is waiting for.
            self._tainted = True
            logger.warning(
                "command_timed_out",
                worker_id=self.spec.worker_id,
                command=approved.display,
                timeout_seconds=timeout,
            )
            self.close()
        elif result.timed_out:
            logger.warning(
                "command_timed_out",
                worker_id=self.spec.worker_id,
                command=approved.display,
                timeout_seconds=timeout,
                worker_reusable=True,
            )
        else:
            logger.info(
                "command_finished",
                worker_id=self.spec.worker_id,
                command=approved.display,
                exit_code=result.exit_code,
                duration_ms=duration_ms,
                truncated=result.truncated,
            )
        return result

    def run_all(
        self, commands: Sequence[str], *, stop_on_failure: bool = True
    ) -> tuple[CommandResult, ...]:
        """Run commands in order, stopping at the first failure by default.

        Every command is approved *before* the first one runs: discovering the
        third command was never permitted after the first two passed would
        report a result for a check that never happened.
        """
        approved = self.policy.approve_all(commands)
        results: list[CommandResult] = []
        for entry in approved:
            result = self.run(entry)
            results.append(result)
            if stop_on_failure and not result.succeeded:
                break
        return tuple(results)

    # ------------------------------------------------- background lifecycle

    @property
    def background(self) -> BackgroundProcess | None:
        """The managed background process, running or finished, if any."""
        return self._backgrounds.get("default")

    def start_background(
        self,
        command: str | ApprovedCommand,
        *,
        environment: Mapping[str, str] | None = None,
        name: str = "default",
    ) -> BackgroundProcess:
        """Start the worker's one managed long-running process.

        The command goes through the *same* ``CommandPolicy`` as ``run``:
        there is no shell here either, nothing is backgrounded with ``&``, and
        an executable this profile does not allow is refused exactly as it
        would be in the foreground. What makes the command a background one is
        that the orchestrator keeps the process instead of waiting for it.

        Ownership stays with the worker. The process is stopped by
        ``terminate``, by ``close``, by ``worker_session`` leaving its block
        however it leaves it, and ultimately by the container's destruction.

        This starts a process; it does not wait for the program to become
        useful. Readiness -- a port open, an HTTP answer, a page rendered --
        belongs to the caller that knows what it asked to be started.

        Args:
            environment: extra variables for *this process only*, on top of the
                worker's own. A long-running program often needs one (a port, a
                mode) that the foreground commands must not see, and widening
                the worker's environment to carry it would change the
                environment the build and the tests ran in. Credential-shaped
                names are refused: a secret reaches a worker by being injected
                at its creation (section 11), not through a caller's keyword.

        Raises:
            CommandRejected: the command is not permitted by this policy.
            WorkerNotRunning: the worker is closed or tainted.
            BackgroundProcessAlreadyRunning: one is already alive (V1 limit).
            BackgroundProcessStartFailed: the process could not be started.
            WorkerBackendUnavailable: this backend has no background support.
            ValueError: ``environment`` names something credential-shaped.
        """
        if self._closed or self._tainted:
            raise WorkerNotRunning(
                f"Worker {self.spec.worker_id} is no longer running"
                + (" (a previous command timed out)" if self._tainted else "")
            )
        extra = dict(environment or {})
        refused = sorted(name for name in extra if is_secret_name(name))
        if refused:
            raise ValueError(
                f"a managed background process may not be given credential-shaped "
                f"variable(s) {', '.join(refused)}; inject secrets when the worker "
                f"is created"
            )
        approved = (
            command if isinstance(command, ApprovedCommand) else self.policy.approve(command)
        )
        if not name or any(character.isspace() for character in name):
            raise ValueError("background process name must be a non-empty token")
        existing = self._backgrounds.get(name)
        if existing is not None:
            if existing.is_running():
                limit = (
                    "; a worker manages at most one background process in the default slot"
                    if name == "default"
                    else ""
                )
                raise BackgroundProcessAlreadyRunning(
                    f"Worker {self.spec.worker_id} already manages "
                    f"{existing.command.display!r} as background process {name!r}"
                    f"{limit}"
                )
            # A finished one still holds resources (a stopped container, drain
            # threads). Clear them before taking on another.
            self.terminate_background(name=name)

        common = {
            "command": approved,
            "worker_id": self.spec.worker_id,
            "max_output_bytes": self.spec.background_max_output_bytes,
            "stop_grace_seconds": float(self.spec.background_stop_grace_seconds),
            "redactor": self.redactor,
        }
        if self.spec.backend is WorkerBackend.DOCKER:
            process: BackgroundProcess = self._start_background_container(
                approved, common, extra, name=name
            )
        elif self.spec.backend is WorkerBackend.SUBPROCESS:
            process = self._start_background_host(approved, common, extra)
        else:  # pragma: no cover - a backend added without deciding this
            raise WorkerBackendUnavailable(
                f"the {self.spec.backend} backend does not support a managed background "
                f"process; no process was started"
            )
        self._backgrounds[name] = process
        self._background_started = True
        logger.info(
            "background_process_started",
            worker_id=self.spec.worker_id,
            command=approved.display,
            backend=self.spec.backend.value,
        )
        return process

    def terminate_background(
        self, *, grace_seconds: float | None = None, name: str = "default"
    ) -> None:
        """Stop the managed background process, if there is one.

        Idempotent. Leaves the worker usable: nothing about the worker's own
        container is touched.

        Raises:
            BackgroundProcessCleanupFailed: cleanup could not be proven. The
                worker is tainted, so its container will be destroyed.
        """
        process = self._backgrounds.get(name)
        if process is None:
            return
        try:
            process.terminate(grace_seconds=grace_seconds)
        except BackgroundProcessCleanupFailed:
            # Fail closed: a process that may still be alive means this
            # worker's integrity is unknown, which is what taint means.
            self._tainted = True
            raise
        finally:
            self._backgrounds.pop(name, None)

    def terminate_backgrounds(self, *, grace_seconds: float | None = None) -> None:
        """Stop every managed background process, newest first."""
        first_error: BackgroundProcessCleanupFailed | None = None
        for name in reversed(tuple(self._backgrounds)):
            try:
                self.terminate_background(grace_seconds=grace_seconds, name=name)
            except BackgroundProcessCleanupFailed as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _cleanup_background(self) -> None:
        """Terminate the background process on a path that must not raise."""
        try:
            self.terminate_backgrounds()
        except Exception as error:  # noqa: BLE001 - close() must not mask a failure
            logger.warning(
                "background_process_cleanup_failed",
                worker_id=self.spec.worker_id,
                error=str(error),
            )

    def _start_background_host(
        self,
        approved: ApprovedCommand,
        common: dict[str, object],
        extra_environment: Mapping[str, str],
    ) -> BackgroundProcess:
        try:
            process = subprocess.Popen(  # noqa: S603 - approved argv, never a shell
                list(approved.argv),
                cwd=str(self.spec.mount_source),
                env={**(self._process_environment() or {}), **extra_environment},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise BackgroundProcessStartFailed(
                f"{approved.display!r} could not be started: {error}"
            ) from error
        limit = self.spec.background_max_output_bytes
        drains = (_Drain(process.stdout, limit), _Drain(process.stderr, limit))
        for drain in drains:
            drain.start()
        return _HostBackgroundProcess(process=process, drains=drains, **common)  # type: ignore[arg-type]

    def _start_background_container(
        self,
        approved: ApprovedCommand,
        common: dict[str, object],
        extra_environment: Mapping[str, str] | None = None,
        *,
        name: str = "default",
    ) -> BackgroundProcess:
        if self.container_id is None:
            raise WorkerNotRunning(f"Worker {self.spec.worker_id} has no container")
        container_name = self.spec.background_container_name_for(name)
        # Removed explicitly rather than with `--rm`, because the exit code and
        # the log of a process that died on its own are the evidence for why.
        argv: list[str] = [
            "run",
            "--detach",
            "--init",
            "--name",
            container_name,
            "--workdir",
            CONTAINER_WORKDIR,
            "--volume",
            f"{self.spec.mount_source}:{CONTAINER_WORKDIR}:rw",
            # The worker's own network namespace: whatever this starts is
            # reachable from a command the worker runs, and it inherits the
            # worker's network restriction rather than widening it.
            "--network",
            f"container:{self.container_id}",
            "--cpus",
            str(self.spec.cpus),
            "--memory",
            self.spec.memory,
            "--pids-limit",
            str(self.spec.pids_limit),
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]
        for option in BACKGROUND_LOG_OPTIONS:
            argv += ["--log-opt", option]
        for target, options in _TMPFS_MOUNTS:
            argv += ["--tmpfs", f"{target}:{options}"]
        if self.spec.user:
            argv += ["--user", self.spec.user]
        for key, value in {**self.spec.environment, **(extra_environment or {})}.items():
            argv += ["--env", f"{key}={value}"]
        argv += [self.spec.image or "", *approved.argv]

        try:
            output = self._docker(*argv, timeout=120)
        except WorkerStartFailed as error:
            # Leave nothing half-created behind a failed start.
            with contextlib.suppress(Exception):
                self._docker_capture("rm", "--force", "--volumes", container_name, timeout=60)
            raise BackgroundProcessStartFailed(
                f"{approved.display!r} could not be started: {error}"
            ) from error
        container = output.strip().splitlines()[-1] if output.strip() else container_name
        self._background_container_names.add(container_name)
        return _ContainerBackgroundProcess(
            container=container, capture=self._docker_capture, **common
        )  # type: ignore[arg-type]

    # --------------------------------------------------------------- backends

    def _argv_for(
        self, command: ApprovedCommand, *, timeout_seconds: float | None = None
    ) -> tuple[str, ...]:
        if self.spec.backend is not WorkerBackend.DOCKER:
            return command.argv
        if self.container_id is None:
            raise WorkerNotRunning(f"Worker {self.spec.worker_id} has no container")
        timeout = timeout_seconds or self.spec.command_timeout_seconds
        return (
            self.docker_binary,
            "exec",
            "--workdir",
            CONTAINER_WORKDIR,
            self.container_id,
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{max(1, int(timeout))}s",
            *command.argv,
        )

    def _process_environment(self) -> dict[str, str] | None:
        """The environment for the *local* process we are about to start.

        For Docker that is the ``docker`` client, which needs the host's own
        environment to find the daemon and must not be handed the worker's
        variables. For the subprocess backend it is the worker's environment,
        plus ``PATH`` -- the one host value passed through, because without it
        nothing resolves at all.
        """
        if self.spec.backend is WorkerBackend.DOCKER:
            return None
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            **{key: value for key, value in self.spec.environment.items()},
        }

    def _start_container(self) -> str:
        image = self.spec.image or ""
        if not image:
            raise WorkerStartFailed(
                f"No image configured for the {self.spec.profile} worker profile"
            )
        argv: list[str] = [
            "run",
            "--detach",
            "--rm",
            "--init",
            "--name",
            self.spec.worker_id,
            "--workdir",
            CONTAINER_WORKDIR,
            "--volume",
            f"{self.spec.mount_source}:{CONTAINER_WORKDIR}:rw",
            "--network",
            self.spec.network,
            "--cpus",
            str(self.spec.cpus),
            "--memory",
            self.spec.memory,
            "--pids-limit",
            str(self.spec.pids_limit),
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]
        for target, options in _TMPFS_MOUNTS:
            argv += ["--tmpfs", f"{target}:{options}"]
        if self.spec.user:
            argv += ["--user", self.spec.user]
        for key, value in self.spec.environment.items():
            argv += ["--env", f"{key}={value}"]
        argv += [image, "sleep", "infinity"]

        result = self._docker(*argv, timeout=120)
        container = result.strip().splitlines()[-1] if result.strip() else ""
        if not container:
            raise WorkerStartFailed(f"Docker did not return a container id for {image}")
        return container

    def _docker_capture(
        self, *args: str, timeout: int, max_output_bytes: int = 64_000
    ) -> _Capture:
        """Run a ``docker`` subcommand and return what it did, without judging.

        ``_docker`` raises on failure, which is right when a worker cannot be
        created. Inspecting and stopping a background process needs the
        opposite: a failure there is information (the container is gone), not
        an exception.
        """
        return _execute(
            (self.docker_binary, *args),
            cwd=None,
            env=None,
            timeout_seconds=timeout,
            max_output_bytes=max_output_bytes,
        )

    def _docker(self, *args: str, timeout: int) -> str:
        capture = _execute(
            (self.docker_binary, *args),
            cwd=None,
            env=None,
            timeout_seconds=timeout,
            max_output_bytes=64_000,
        )
        if capture.timed_out:
            raise WorkerStartFailed(f"docker {args[0]} timed out after {timeout}s")
        if capture.exit_code != 0:
            raise WorkerStartFailed(
                f"docker {args[0]} failed ({capture.exit_code}): {capture.stderr.strip()}"
            )
        return capture.stdout


# ------------------------------------------------------------------ factories


def worker_image_for(profile: WorkerProfile, settings: Settings) -> str:
    return settings.worker_images().get(profile, "")


def policy_for_project(
    profile: WorkerProfile, settings: Settings | None = None
) -> CommandPolicy:
    """The command policy for a worker profile."""
    config = settings or get_settings()
    return CommandPolicy(
        profile=profile,
        extra_allowed=frozenset(config.worker_extra_executables_list()),
        allow_relative_scripts=config.worker_allow_relative_scripts,
    )


def build_spec(
    mount_source: Path,
    *,
    profile: WorkerProfile,
    settings: Settings | None = None,
    secrets: Mapping[str, str] | None = None,
    worker_id: str | None = None,
    worker_network: str | None = None,
) -> WorkerSpec:
    """Assemble a spec, enforcing the mount rule before anything is created.

    Raises:
        WorkspaceMountRejected: the path is missing, is not a directory, or is
            not inside ``WORKTREE_ROOT``.
    """
    config = settings or get_settings()
    mount = mount_source.expanduser().resolve()
    _assert_mountable(mount, config)
    daemon_mount = _daemon_mount(mount, config)

    environment = {
        **BASE_ENVIRONMENT,
        **_passthrough_environment(config),
        **(secrets or {}),
    }
    return WorkerSpec(
        worker_id=worker_id or f"orchestrator-worker-{uuid.uuid4().hex[:12]}",
        profile=profile,
        mount_source=daemon_mount,
        image=worker_image_for(profile, config),
        backend=config.worker_backend,
        network=worker_network if worker_network is not None else config.worker_network,
        cpus=config.worker_cpus,
        memory=config.worker_memory,
        pids_limit=config.worker_pids_limit,
        command_timeout_seconds=config.worker_command_timeout_seconds,
        max_output_bytes=config.worker_max_output_bytes,
        background_max_output_bytes=config.worker_background_max_output_bytes,
        background_stop_grace_seconds=config.worker_background_stop_grace_seconds,
        environment=environment,
        user=_container_user(config),
    )


def start_worker(
    mount_source: Path,
    *,
    profile: WorkerProfile,
    settings: Settings | None = None,
    secrets: Mapping[str, str] | None = None,
    worker_id: str | None = None,
    worker_network: str | None = None,
) -> Worker:
    """Create and start a worker for one worktree.

    Raises:
        WorkspaceMountRejected: the mount is not an acceptable worktree.
        WorkerBackendUnavailable: the configured backend cannot be used.
        WorkerStartFailed: the container could not be created.
    """
    config = settings or get_settings()
    spec = build_spec(
        mount_source,
        profile=profile,
        settings=config,
        secrets=secrets,
        worker_id=worker_id,
        worker_network=worker_network,
    )
    if spec.backend is WorkerBackend.DOCKER:
        assert_backend_available(config)
    else:
        logger.warning(
            "worker_backend_subprocess",
            worker_id=spec.worker_id,
            detail="isolation is weaker than a container; WORKER_BACKEND=docker is the default",
        )
    worker = Worker(
        spec,
        policy_for_project(profile, config),
        redactor=Redactor.for_values(
            [*(secrets or {}).values(), config.review_api_key or None]
        ),
        docker_binary=config.docker_binary,
    )
    return worker.start()


@contextmanager
def worker_session(
    mount_source: Path,
    *,
    profile: WorkerProfile,
    settings: Settings | None = None,
    secrets: Mapping[str, str] | None = None,
    worker_network: str | None = None,
) -> Iterator[Worker]:
    """A worker that is destroyed when the block ends, however it ends.

    ``WORKER_RETAIN_ON_FAILURE=true`` keeps the container after an exception so
    an operator can look inside it. It leaks containers by design and is never
    the default.
    """
    config = settings or get_settings()
    worker = start_worker(
        mount_source,
        profile=profile,
        settings=config,
        secrets=secrets,
        worker_network=worker_network,
    )
    failed = False
    try:
        yield worker
    except BaseException:
        failed = True
        raise
    finally:
        worker.close(retain=failed and config.worker_retain_on_failure)


def assert_backend_available(settings: Settings | None = None) -> None:
    """Check the container runtime before a run depends on it.

    Raises:
        WorkerBackendUnavailable: Docker is missing or its daemon is not
            answering.
    """
    config = settings or get_settings()
    if config.worker_backend is not WorkerBackend.DOCKER:
        return
    if shutil.which(config.docker_binary) is None:
        raise WorkerBackendUnavailable(
            f"{config.docker_binary!r} is not on PATH; install Docker or set "
            f"WORKER_BACKEND=subprocess"
        )
    capture = _execute(
        (config.docker_binary, "version", "--format", "{{.Server.Version}}"),
        cwd=None,
        env=None,
        timeout_seconds=30,
        max_output_bytes=8_000,
    )
    if capture.timed_out or capture.exit_code != 0:
        raise WorkerBackendUnavailable(
            "the Docker daemon is not answering: "
            + (capture.stderr.strip() or "no response").splitlines()[-1]
        )


def _assert_mountable(mount: Path, settings: Settings) -> None:
    root = settings.worktree_root.expanduser().resolve()
    if not mount.is_dir():
        raise WorkspaceMountRejected(f"{mount} is not a directory")
    if mount == root or root not in mount.parents:
        raise WorkspaceMountRejected(
            f"{mount} is not inside WORKTREE_ROOT ({root}); a worker is only ever "
            f"given a task run's own worktree, never a managed repository"
        )


def _daemon_mount(mount: Path, settings: Settings) -> Path:
    """Translate a container path to the Docker daemon host's path.

    The untrusted input is validated against ``WORKTREE_ROOT`` first. Only its
    relative suffix is then joined to the operator-configured host root, so a
    task can never use this translation to widen its mount allowance.
    """
    if settings.worker_backend is not WorkerBackend.DOCKER:
        return mount
    if settings.host_worktree_root is None:
        return mount
    relative = mount.relative_to(settings.worktree_root)
    return (settings.host_worktree_root / relative).resolve()


def _passthrough_environment(settings: Settings) -> dict[str, str]:
    """Host variables an operator listed, minus anything credential-shaped.

    A secret reaches a worker only by being passed to ``start_worker``
    explicitly (section 11: injected individually). A passthrough list is a
    convenience for things like ``TZ``, and letting it carry a secret would
    make that convenience the leak.
    """
    passed: dict[str, str] = {}
    for name in settings.worker_env_passthrough_list():
        if is_secret_name(name):
            logger.warning("worker_env_passthrough_refused", variable=name)
            continue
        value = os.environ.get(name)
        if value is not None:
            passed[name] = value
    return passed


def _container_user(settings: Settings) -> str | None:
    """``uid:gid`` for the container, so files land owned by the host user.

    The worker images run as uid 1000. When the orchestrator's own uid differs,
    everything the worker writes into the worktree would be owned by a user the
    host cannot then read or clean up, and Git would see a tree it cannot
    touch. Matching the host uid is the pragmatic fix; it is still not root.
    """
    if settings.worker_user:
        return settings.worker_user
    if not hasattr(os, "getuid"):  # pragma: no cover - Windows
        return None
    return f"{os.getuid()}:{os.getgid()}"


# --------------------------------------------------------------- the runner


@dataclass(frozen=True, slots=True)
class _Capture:
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    output_bytes: int


class _Drain(threading.Thread):
    """Reads a pipe to the end, keeping at most ``limit`` bytes.

    Draining past the limit matters: a child whose pipe fills up blocks
    forever, so a runner that simply stopped reading would turn an output
    ceiling into a hang.
    """

    def __init__(self, stream, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = max(0, limit)
        self.chunks: list[bytes] = []
        self.kept = 0
        self.total = 0

    def run(self) -> None:
        # ``read1`` rather than ``read``: a buffered ``read(n)`` blocks until it
        # has n bytes or the pipe closes, so a managed background process'
        # output would be invisible until it died. ``read1`` returns what has
        # arrived, which is what makes a live server's log readable.
        read = getattr(self.stream, "read1", None) or self.stream.read
        try:
            while True:
                chunk = read(65_536)
                if not chunk:
                    break
                self.total += len(chunk)
                if self.kept < self.limit:
                    room = self.limit - self.kept
                    self.chunks.append(chunk[:room])
                    self.kept += min(room, len(chunk))
        except (ValueError, OSError):  # pragma: no cover - pipe closed under us
            pass
        finally:
            with contextlib.suppress(Exception):
                self.stream.close()

    @property
    def truncated(self) -> bool:
        return self.total > self.kept

    def text(self) -> str:
        # Snapshot the list: a background process' output is read while the
        # drain thread is still appending to it.
        decoded = b"".join(list(self.chunks)).decode("utf-8", errors="replace")
        return decoded + OUTPUT_TRUNCATION_MARKER if self.truncated else decoded


def _execute(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    timeout_seconds: float,
    max_output_bytes: int,
) -> _Capture:
    """Run ``argv`` directly -- never through a shell -- and capture it.

    On timeout the whole process group is killed, not just the leader: a test
    runner that spawned children would otherwise leave them holding the
    worktree.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise WorkerBackendUnavailable(f"{argv[0]!r} could not be executed: {error}") from error
    except PermissionError as error:
        raise WorkerBackendUnavailable(f"{argv[0]!r} is not executable: {error}") from error

    out = _Drain(process.stdout, max_output_bytes)
    err = _Drain(process.stderr, max_output_bytes)
    out.start()
    err.start()

    timed_out = False
    try:
        exit_code: int | None = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        # Deliberately not `process.poll()`: whatever the process reports now is
        # the result of being killed, not the command's verdict. A timeout
        # produced no verdict, and recording 0 here would let a caller that
        # checks only the exit code read a killed test suite as a pass.
        exit_code = None

    out.join(timeout=10)
    err.join(timeout=10)
    return _Capture(
        exit_code=exit_code,
        stdout=out.text(),
        stderr=err.text(),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        timed_out=timed_out,
        output_bytes=out.total + err.total,
    )


def _kill_group(process: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    """Terminate, then kill, the process group started by ``_execute``.

    ``grace_seconds`` is how long the group is given to exit after SIGTERM; a
    background process gets the operator-configured grace, a timed-out
    foreground command the default.
    """
    try:
        group = os.getpgid(process.pid)
    except (ProcessLookupError, AttributeError):  # pragma: no cover - already gone
        process.kill()
        return
    for sig, grace in ((signal.SIGTERM, max(0.1, grace_seconds)), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


__all__ = [
    "BACKGROUND_LOG_LINES",
    "BACKGROUND_LOG_OPTIONS",
    "BASE_ENVIRONMENT",
    "CONTAINER_WORKDIR",
    "OUTPUT_TRUNCATION_MARKER",
    "BackgroundProcess",
    "BackgroundState",
    "CommandResult",
    "Worker",
    "WorkerSpec",
    "assert_backend_available",
    "build_spec",
    "policy_for_project",
    "start_worker",
    "worker_image_for",
    "worker_session",
]
