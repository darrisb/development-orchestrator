"""Worker and command failures, classified (build.md sections 11, 12, 49).

A command that exits non-zero is *not* an error here: a failing test is the
verification pipeline working, and it comes back as a ``CommandResult`` with an
exit code. These exceptions are for the cases where the orchestrator could not
find out what the command would have done -- no backend, no container, a
timeout -- which is a different thing and has a different policy.
"""

from __future__ import annotations

from ..domain.enums import FailureReason
from .errors import ServiceError


class WorkerError(ServiceError):
    """Base class for worker-runtime failures.

    ``reason`` is what the workflow acts on (``domain.failure_policy``), so
    every subclass sets one rather than leaving the caller to infer a policy
    from the message.
    """

    reason: FailureReason = FailureReason.WORKER_FAILURE


class WorkerBackendUnavailable(WorkerError):
    """The configured backend cannot be used at all.

    Docker is not installed, not running, or refuses to talk to us. Classified
    ``RESOURCE_UNAVAILABLE`` rather than ``WORKER_FAILURE``: the policy is to
    pause, because retrying a run on a host with no container runtime would
    burn the attempt ceiling on an operator problem.
    """

    reason = FailureReason.RESOURCE_UNAVAILABLE


class WorkerStartFailed(WorkerError):
    """The worker could not be created or did not come up."""


class WorkerNotRunning(WorkerError):
    """A command was submitted to a worker that has been closed."""


class WorkspaceMountRejected(WorkerError):
    """The path offered as the worker's project mount is not acceptable.

    The mount is the whole of the worker's write access, so this is checked
    rather than assumed: a worker handed the managed repository instead of a
    run's worktree would be able to change work the orchestrator has not
    branched.
    """


class BackgroundProcessAlreadyRunning(WorkerError):
    """A second managed background process was requested.

    A worker owns at most one (``worker_service``, V1). Overlapping servers
    would need supervision this layer deliberately does not have, so the
    second request is refused rather than quietly replacing the first.
    """


class BackgroundProcessStartFailed(WorkerStartFailed):
    """A managed background process could not be started at all."""


class BackgroundProcessCleanupFailed(WorkerError):
    """A managed background process could not be proven dead.

    Raised rather than swallowed: the invariant is that nothing orchestrator-
    managed survives a worker lifecycle boundary, and reporting success while
    a process may still hold the worktree would make that invariant a guess.
    The worker is tainted so its container is destroyed, which is the
    backstop.
    """


class CommandTimedOut(WorkerError):
    """A command exceeded its timeout and was killed.

    Distinct from a failing command: a timeout produced no verdict. The output
    captured before the kill is kept, because a hanging test suite's last lines
    are usually the whole diagnosis.
    """

    def __init__(self, command: str, timeout_seconds: float) -> None:
        super().__init__(f"Command {command!r} timed out after {timeout_seconds}s")
        self.command = command
        self.timeout_seconds = timeout_seconds


__all__ = [
    "BackgroundProcessAlreadyRunning",
    "BackgroundProcessCleanupFailed",
    "BackgroundProcessStartFailed",
    "CommandTimedOut",
    "WorkerBackendUnavailable",
    "WorkerError",
    "WorkerNotRunning",
    "WorkerStartFailed",
    "WorkspaceMountRejected",
]
