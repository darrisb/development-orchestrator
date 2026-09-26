"""Running a project's commands and keeping the evidence (build.md sections 9, 12).

``worker_service`` runs a command and knows nothing about the database;
``domain.commands`` decides whether it may run at all. This module is the join:
it starts a worker for a task run, runs the commands the *project* configured,
writes each one's output into the run directory, and records the artifact.

The rule from section 17 is what all of it is for: *never accept a model's
statement that a command passed -- the orchestrator must execute it.* So the
return value of everything here is what the command actually did, and the log
on disk is the proof. Classifying those exit codes into verification outcomes,
and the order the categories run in, belong to the verification pipeline
(phase H); this module executes and records, and judges nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.commands import CommandPolicy, unique_labels
from ..repositories import TaskRunRepository
from . import artifact_store
from .worker_service import CommandResult, Worker, worker_session
from .workspace import TaskWorkspace, load_run_context

logger = get_logger(__name__)

#: Log directory per category, following section 9's suggested run layout
#: (``build/``, ``tests/``, ``lint/``, ``security/``). A category the pipeline
#: has not named yet simply gets its own directory.
LOG_SUFFIX = ".log"
DEFAULT_CATEGORY = "verify"


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """One command's result and where its log was stored."""

    result: CommandResult
    #: Path relative to ``ARTIFACT_ROOT``, or ``None`` when recording was off.
    log_artifact: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result.succeeded

    def describe(self) -> dict[str, object]:
        return {**self.result.describe(), "log_artifact": self.log_artifact}


def store_command_log(
    session: Session,
    task_run_id: UUID,
    result: CommandResult,
    *,
    category: str = DEFAULT_CATEGORY,
    label: str | None = None,
    prefix: str = "",
    settings: Settings | None = None,
) -> str:
    """Write one command's output into the run directory and record it.

    The log is stored already redacted: ``Worker`` masks secrets as it captures,
    so a credential printed by a build script never reaches the disk (sections
    12 and 36).

    Args:
        prefix: a subdirectory for this attempt, so a retry's logs do not
            overwrite the ones that explain why it is retrying.

    Returns:
        The artifact path, relative to ``ARTIFACT_ROOT``.
    """
    name = f"{prefix}{_safe_segment(category)}/{label or result.command.label}{LOG_SUFFIX}"
    stored = artifact_store.write_text(
        session,
        task_run_id,
        name,
        _render_log(result),
        kind=f"{category}-log",
        settings=settings,
    )
    return stored.relative_path


def execute_commands(
    session: Session,
    worker: Worker,
    task_run_id: UUID,
    commands: Sequence[str],
    *,
    category: str = DEFAULT_CATEGORY,
    prefix: str = "",
    settings: Settings | None = None,
    stop_on_failure: bool = True,
    record_logs: bool = True,
    deadline_monotonic: float | None = None,
) -> tuple[CommandExecution, ...]:
    """Run ``commands`` in ``worker``, storing each one's log.

    Every command is approved before the first one runs, so a command that was
    never permitted cannot be discovered halfway through a profile and leave
    earlier passes looking like a complete result.

    Raises:
        CommandRejected: a command is not permitted by the worker's policy.
        WorkerNotRunning: the worker was closed, or a command timed out and
            took the worker with it.
    """
    approved = worker.policy.approve_all(commands)
    labels = unique_labels(approved)
    executions: list[CommandExecution] = []

    for entry, label in zip(approved, labels, strict=True):
        remaining = (
            min(
                worker.spec.command_timeout_seconds,
                max(1, int(deadline_monotonic - monotonic())),
            )
            if deadline_monotonic is not None
            else None
        )
        result = worker.run(entry, timeout_seconds=remaining)
        artifact = (
            store_command_log(
                session,
                task_run_id,
                result,
                category=category,
                label=label,
                prefix=prefix,
                settings=settings,
            )
            if record_logs
            else None
        )
        executions.append(CommandExecution(result=result, log_artifact=artifact))
        if stop_on_failure and not result.succeeded:
            logger.info(
                "command_sequence_stopped",
                run_id=str(task_run_id),
                category=category,
                command=entry.display,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                remaining=len(approved) - len(executions),
            )
            break
    return tuple(executions)


def run_task_commands(
    session: Session,
    workspace: TaskWorkspace,
    commands: Sequence[str] | None = None,
    *,
    category: str = DEFAULT_CATEGORY,
    settings: Settings | None = None,
    secrets: Mapping[str, str] | None = None,
    stop_on_failure: bool = True,
) -> tuple[CommandExecution, ...]:
    """Run a task's configured commands in a fresh worker for its worktree.

    This is phase D's whole claim in one call: the orchestrator executes the
    commands the project declared, inside an isolated worker that can only see
    this run's worktree, and keeps the output. The worker is created here and
    destroyed before this returns, whatever happens (section 11).

    Args:
        commands: defaults to the task's ``verify_commands`` -- the commands
            the *manifest* declared. A model never supplies these.
        secrets: injected into the worker individually, and redacted out of
            every log the worker captures.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
        WorkspaceMountRejected: the worktree is not one a worker may be given.
        WorkerBackendUnavailable: the container runtime is not usable.
        CommandRejected: a configured command is not permitted.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    to_run = list(commands if commands is not None else task.verify_commands)
    if not to_run:
        logger.info(
            "no_commands_configured",
            run_id=str(run.id),
            task=task.external_task_id,
            detail="the task declares no verification commands",
        )
        return ()

    with worker_session(
        workspace.path, profile=project.worker_profile, settings=config, secrets=secrets
    ) as worker:
        TaskRunRepository(session).update_fields(
            run.id, worker_image=worker.spec.runtime_description
        )
        logger.info(
            "task_commands_started",
            run_id=str(run.id),
            task=task.external_task_id,
            attempt=run.attempt_number,
            worker_id=worker.spec.worker_id,
            category=category,
            commands=len(to_run),
        )
        executions = execute_commands(
            session,
            worker,
            run.id,
            to_run,
            category=category,
            settings=config,
            stop_on_failure=stop_on_failure,
        )

    logger.info(
        "task_commands_finished",
        run_id=str(run.id),
        task=task.external_task_id,
        category=category,
        ran=len(executions),
        failed=sum(1 for execution in executions if not execution.succeeded),
    )
    return executions


def approve_task_commands(
    commands: Sequence[str], policy: CommandPolicy
) -> tuple[str, ...]:
    """Validate a task's commands without running them.

    Meant for import time: a manifest naming a command no worker will ever run
    should fail when it is imported, not on the first attempt at the task.

    Raises:
        CommandRejected: the first command that is not permitted.
    """
    return tuple(command.display for command in policy.approve_all(commands))


def _render_log(result: CommandResult) -> str:
    """The log artifact: the command, its outcome, then its output.

    The header exists so a log read on its own -- months later, by a human or a
    reviewer model -- says what was run and what it returned, rather than being
    a wall of output whose provenance is in another file.
    """
    outcome = (
        f"timed out after {result.duration_ms}ms"
        if result.timed_out
        else f"exit code {result.exit_code}"
    )
    header = [
        f"$ {result.command.display}",
        f"# {outcome} in {result.duration_ms}ms",
    ]
    if result.truncated:
        header.append(
            f"# output exceeded the capture limit; {result.output_bytes} bytes were produced"
        )
    return "\n".join([*header, "", result.combined_output, ""])


def _safe_segment(category: str) -> str:
    """A category as a directory name the artifact store will accept."""
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in category.strip().casefold()
    ).strip("-")
    return cleaned or DEFAULT_CATEGORY


__all__ = [
    "DEFAULT_CATEGORY",
    "CommandExecution",
    "approve_task_commands",
    "execute_commands",
    "run_task_commands",
    "store_command_log",
]
