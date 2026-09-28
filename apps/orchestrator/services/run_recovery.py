"""Operator-initiated recovery of a stranded in-flight run (concern 67).

Concern 66 fixed the defect that stranded ``RUN-20260928-000005``: a fix-loop
turn no longer holds a database transaction across model inference, and a run
interrupted mid-turn reconstructs correctly from its committed rows and its
artifacts. What concern 66 could not do was *ask the orchestrator to do it*.
Its proposed operator action, ``POST /tasks/{task_id}/resume``, was executed
against the real run and refused:

    HTTP 409
    {"error":"EntityConflict","detail":"Task TS-109 is not paused"}

which is the correct answer. ``services.pauses.resume_task`` continues work a
person deliberately paused, and TS-109 was never paused -- it was ``VERIFYING``,
with a live ``RUNNING`` run whose executor had died. Widening resume to accept
active task states would have made one operation mean two things, and the more
dangerous of the two silently: "continue paused work" cannot acquire a second
executor, and "continue work somebody else may still be executing" absolutely
can.

**Four operations, kept apart.**

* **resume** -- continue intentionally ``PAUSED`` work. Unchanged.
* **recover** (this module) -- reconstruct execution for an *existing* durable
  in-flight ``TaskRun`` whose execution owner may no longer continue.
* **retry** (concern 65) -- authorize a *new* run after a terminal ``FAILED``
  task.
* **abandon** (concern 64) -- terminate an existing in-flight run.

Recovery is the only one of the four that continues a run rather than starting
or ending one, and it is therefore the only one whose central problem is
*ownership* rather than eligibility.

**The safety invariant, and why a status is not enough.** At most one execution
owner may continue a run. Nothing durable in the pre-concern-67 system could
express that: ``RUNNING`` means "somebody started this", not "somebody is still
doing it", and every weak proxy for the difference -- elapsed wall clock, an
absent PID, a missing container, a quiet event log -- is a guess that is wrong
exactly when it matters. So recovery does not try to prove the old executor is
gone. It makes the question irrelevant:

    old owner holds generation N and is off doing external work
    recovery atomically takes generation N+1
    old owner returns and tries to persist -> refused, it holds N
    recovered owner holds N+1 and continues

The token is ``task_runs.execution_generation``; the acquisition is one guarded
``UPDATE`` (``TaskRunRepository.acquire_execution``); the refusal is in
``TaskRunRepository.require_in_flight``, which every durable checkpoint in the
fix loop already passes through. Concern 64's terminal fencing and concern 66's
transaction boundary are unchanged and still do their own jobs -- this is a
third predicate on the same lock, not a replacement for either.

**``execution_owner`` is a held/not-held marker, never a liveness claim.** A
dispatch stamps it on acquisition and clears it on the way out, so a run nobody
is inside has no owner. A process killed mid-dispatch leaves one behind that
nothing will clear, and recovery then refuses rather than guessing -- which is
the fail-closed behaviour, and why ``override_active_owner`` exists as an
explicit, recorded operator decision rather than as a timeout nobody chose.
The override is safe for the same reason everything else here is: the older
executor is fenced whether it is alive or not.

**Recoverability is decided from durable evidence, and it fails closed.** Every
check below is a statement about what is on the record or on disk; none of them
is a statement about how long something has taken.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.loop_recovery import recover_loop_state
from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..db.models import WorkflowCheckpointRow
from ..domain.enums import (
    IN_FLIGHT_RUN_STATUSES,
    RunEventType,
    RunStatus,
    TaskStatus,
)
from ..domain.git import INTEGRATION_BRANCH
from ..domain.models import RunEvent, TaskRun
from ..domain.state_machine import ACTIVE_STATES
from ..repositories import (
    PauseRequestRepository,
    RunEventRepository,
    TaskRunRepository,
)
from .errors import EntityConflict
from .git_errors import GitError
from .workspace import load_run_context, repository_service, workspace_path

logger = get_logger(__name__)

#: Task states whose in-flight run recovery will continue.
#:
#: Built from the state machine's own ``ACTIVE_STATES`` rather than restated,
#: because "the task is in flight" already has one definition and a second copy
#: of it would eventually disagree with the first. The three additions are the
#: boundaries a run legitimately sits at without the task being mid-step:
#: ``READY`` (a run opened but not yet started), ``CHANGES_REQUESTED`` (between
#: a review and the next attempt) and ``APPROVED`` (waiting on delivery).
#:
#: ``PAUSED`` is deliberately absent, and that absence is the architectural
#: point of this module: a paused task has its own operation, and routing it
#: here instead would silently replace "continue what a person stopped" with
#: "take execution away from whoever has it". ``COMPLETE``, ``FAILED`` and
#: ``HUMAN_REVIEW`` are absent because there is nothing left to continue --
#: the first two are resting places and the third is waiting on a person.
RECOVERABLE_TASK_STATES: frozenset[TaskStatus] = ACTIVE_STATES | frozenset(
    {TaskStatus.READY, TaskStatus.CHANGES_REQUESTED, TaskStatus.APPROVED}
)


@dataclass(frozen=True, slots=True)
class RecoverabilityCheck:
    """One durable question, its answer, and what the answer was read from."""

    name: str
    passed: bool
    detail: str

    def describe(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RecoverabilityReport:
    """What a read of the durable record says about recovering one run.

    Produced without writing anything, which is what makes it usable as the
    eligibility answer an operator asks for before deciding. The mutating
    operation re-derives it inside its own transaction rather than trusting a
    report handed to it, because a report is a snapshot and the acquisition has
    to be a guard.
    """

    run_id: UUID
    external_run_id: str | None
    recoverable: bool
    checks: tuple[RecoverabilityCheck, ...]
    #: The generation an acquisition would have to match. The token a recovery
    #: request is implicitly quoting back to the database.
    execution_generation: int = 0
    execution_owner: str | None = None
    #: What the reconstruction says the next durable coder execution would be.
    next_attempt: int | None = None
    attempts_started: int | None = None
    reviews_completed: int | None = None
    review_cycle: int | None = None
    max_attempts: int | None = None
    candidate_commit: str | None = None
    integration_advanced_since_start: bool | None = None

    @property
    def refusals(self) -> tuple[RecoverabilityCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def describe(self) -> dict[str, object]:
        return {
            "run_id": str(self.run_id),
            "external_run_id": self.external_run_id,
            "recoverable": self.recoverable,
            "execution_generation": self.execution_generation,
            "execution_owner": self.execution_owner,
            "next_attempt": self.next_attempt,
            "attempts_started": self.attempts_started,
            "reviews_completed": self.reviews_completed,
            "review_cycle": self.review_cycle,
            "max_attempts": self.max_attempts,
            "candidate_commit": self.candidate_commit,
            "integration_advanced_since_start": self.integration_advanced_since_start,
            "checks": [check.describe() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class RecoveryAuthorization:
    """A recovery that happened: the same run, under a new generation."""

    run: TaskRun
    previous_generation: int
    generation: int
    owner: str
    report: RecoverabilityReport
    event_id: UUID | None = None
    #: Present so a caller can state the reconstruction it is about to execute
    #: without recomputing it.
    next_attempt: int | None = field(default=None)


def assess_recoverability(
    session: Session,
    run_id: UUID,
    *,
    settings: Settings | None = None,
    override_active_owner: bool = False,
) -> RecoverabilityReport:
    """Decide, read-only, whether this run may be recovered.

    Writes nothing and takes no lock, so it is safe as a dry run. Every check
    is recorded whether it passed or failed: an operator deciding what to do
    about a stranded run is better served by the whole picture than by the
    first refusal, and a later reader of the event payload gets the same.

    Raises:
        EntityNotFound: no such run, or its task or project is missing.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, run_id)
    checks: list[RecoverabilityCheck] = []

    def record(name: str, passed: bool, detail: str) -> bool:
        checks.append(RecoverabilityCheck(name=name, passed=passed, detail=detail))
        return passed

    # ---- the run itself ----------------------------------------------------
    if run.status is RunStatus.ABANDONED:
        record(
            "run_not_abandoned",
            False,
            "an operator abandoned this run; abandonment is terminal and a "
            "retry of the task is the supported next step",
        )
    else:
        record("run_not_abandoned", True, "the run was not abandoned")
    record(
        "run_in_flight",
        run.status in IN_FLIGHT_RUN_STATUSES,
        f"run status is {run.status}"
        + (
            ""
            if run.status in IN_FLIGHT_RUN_STATUSES
            else "; a terminal run has no execution to reconstruct"
        ),
    )

    # ---- the run is the task's expected in-flight run ----------------------
    in_flight = [
        other
        for other in TaskRunRepository(session).list_for_task(task.id)
        if other.status in IN_FLIGHT_RUN_STATUSES
    ]
    competing = [other for other in in_flight if other.id != run.id]
    record(
        "not_superseded",
        not competing,
        "no competing in-flight run of this task"
        if not competing
        else "run "
        + ", ".join(str(other.run_number) for other in competing)
        + " of this task is also in flight; this run has been superseded",
    )

    # ---- the task ----------------------------------------------------------
    if task.status is TaskStatus.PAUSED:
        record(
            "task_not_paused",
            False,
            "the task is PAUSED; paused work is continued with "
            "POST /tasks/{task_id}/resume, which is a different operation",
        )
    else:
        record("task_not_paused", True, "the task is not paused")
    record(
        "task_state_recoverable",
        task.status in RECOVERABLE_TASK_STATES,
        f"task status is {task.status}",
    )
    pause = PauseRequestRepository(session).in_force_for_task(project.id, task.id)
    record(
        "no_pause_in_force",
        pause is None,
        "no pause request is in force"
        if pause is None
        else "a pause request is in force; release it before recovering",
    )

    # ---- durable workflow state -------------------------------------------
    checkpoints = session.scalar(
        select(WorkflowCheckpointRow.checkpoint_id)
        .where(WorkflowCheckpointRow.thread_id == str(run.id))
        .order_by(WorkflowCheckpointRow.checkpoint_id.desc())
        .limit(1)
    )
    record(
        "checkpoint_exists",
        checkpoints is not None,
        f"latest workflow checkpoint {checkpoints}"
        if checkpoints is not None
        else "no durable workflow checkpoint exists for this run; there is no "
        "execution to reconstruct",
    )

    # ---- Git ---------------------------------------------------------------
    integration_advanced: bool | None = None
    if run.starting_commit is None:
        record("starting_commit_recorded", False, "the run records no starting commit")
    else:
        record(
            "starting_commit_recorded", True, f"starting commit {run.starting_commit}"
        )
        try:
            git = repository_service(project, settings=config)
            resolved = git.resolve_sha(run.starting_commit)
            record(
                "starting_commit_exists",
                True,
                f"{resolved} resolves in {project.repository_path}",
            )
            if not git.branch_exists(INTEGRATION_BRANCH):
                record(
                    "integration_baseline_compatible",
                    False,
                    f"{INTEGRATION_BRANCH} does not exist; there is no accepted "
                    "baseline to judge this run's starting commit against",
                )
            else:
                contained = git.contains_commit(
                    run.starting_commit, ref=INTEGRATION_BRANCH
                )
                integration_head = git.resolve_sha(INTEGRATION_BRANCH)
                integration_advanced = contained and integration_head != resolved
                record(
                    "integration_baseline_compatible",
                    contained,
                    (
                        f"{INTEGRATION_BRANCH} at {integration_head} contains the "
                        f"run's starting commit"
                        + (
                            " and has advanced since the run started"
                            if integration_advanced
                            else ""
                        )
                    )
                    if contained
                    else (
                        f"{INTEGRATION_BRANCH} at {integration_head} does not "
                        f"contain the run's starting commit {resolved}; the "
                        "accepted baseline diverged from what this run started "
                        "from, and continuing would build on a tree the "
                        "orchestrator never accepted"
                    ),
                )
            if run.candidate_commit is not None:
                try:
                    git.resolve_sha(run.candidate_commit)
                    record(
                        "candidate_state_understood",
                        True,
                        f"candidate commit {run.candidate_commit} resolves and is "
                        "left exactly as it is",
                    )
                except GitError as error:
                    record(
                        "candidate_state_understood",
                        False,
                        f"the run records candidate commit {run.candidate_commit}, "
                        f"which does not resolve: {error}",
                    )
            else:
                record(
                    "candidate_state_understood", True, "the run has no candidate commit"
                )
        except GitError as error:
            record(
                "starting_commit_exists",
                False,
                f"the run's starting commit is not readable in "
                f"{project.repository_path}: {error}",
            )

    # ---- worktree ----------------------------------------------------------
    if run.branch_name is None:
        # Nothing was prepared, so there is nothing to be wrong. The workflow's
        # prepare step will create it exactly as it would for a fresh dispatch.
        record(
            "worktree_usable",
            True,
            "no worktree was prepared; the workflow will create one",
        )
    else:
        path = workspace_path(
            project.id, task.external_task_id, run.run_number, settings=config
        )
        record(
            "worktree_usable",
            path.exists(),
            f"worktree {path} is present"
            if path.exists()
            else f"the run records branch {run.branch_name} but its worktree "
            f"{path} is missing; recovery does not silently rebuild Git state",
        )

    # ---- attempt / review accounting --------------------------------------
    next_attempt: int | None = None
    attempts_started: int | None = None
    reviews_completed: int | None = None
    try:
        loop_state = recover_loop_state(session, run, settings=config)
        next_attempt = loop_state.next_attempt
        attempts_started = loop_state.attempts_started
        reviews_completed = loop_state.reviews_completed
        within = next_attempt <= task.limits.max_attempts
        record(
            "attempt_accounting_reconstructable",
            within,
            f"the next durable coder execution would be attempt {next_attempt} "
            f"of at most {task.limits.max_attempts} "
            f"(attempts started: {attempts_started}, reviews completed: "
            f"{reviews_completed})"
            + (
                ""
                if within
                else "; there is no attempt left to execute, so recovering would "
                "only exhaust the budget again"
            ),
        )
    except Exception as error:  # pragma: no cover - defensive; fails closed
        record(
            "attempt_accounting_reconstructable",
            False,
            f"the run's attempt accounting could not be reconstructed: {error}",
        )

    # ---- ownership ---------------------------------------------------------
    if run.execution_owner is None:
        record("execution_ownership_available", True, "no dispatch holds this run")
    elif override_active_owner:
        record(
            "execution_ownership_available",
            True,
            f"dispatch {run.execution_owner} holds this run since "
            f"{run.execution_started_at}; the operator asked to override it, and "
            f"it is fenced at generation {run.execution_generation}",
        )
    else:
        record(
            "execution_ownership_available",
            False,
            f"dispatch {run.execution_owner} has held this run since "
            f"{run.execution_started_at}; recovery will not take a run an "
            "executor is inside. If that executor is known to be gone, repeat "
            "the request with override_active_owner",
        )

    report = RecoverabilityReport(
        run_id=run.id,
        external_run_id=run.external_run_id,
        recoverable=all(check.passed for check in checks),
        checks=tuple(checks),
        execution_generation=run.execution_generation,
        execution_owner=run.execution_owner,
        next_attempt=next_attempt,
        attempts_started=attempts_started,
        reviews_completed=reviews_completed,
        review_cycle=run.review_cycle,
        max_attempts=task.limits.max_attempts,
        candidate_commit=run.candidate_commit,
        integration_advanced_since_start=integration_advanced,
    )
    return report


def recover_run(
    session: Session,
    run_id: UUID,
    *,
    reason: str,
    requested_by: str | None = None,
    override_active_owner: bool = False,
    settings: Settings | None = None,
    owner: str | None = None,
) -> RecoveryAuthorization:
    """Take execution ownership of a stranded in-flight run.

    The run is not created, not renumbered, not re-identified and not rewritten:
    the only durable change is the execution generation, the owner stamp, and
    one append-only event saying a person asked for this and why.

    Args:
        session: the caller's transaction. The acquisition and its event are
            written in it, so the caller's commit decides whether the recovery
            happened.
        run_id: the durable run id -- ``task_runs.id``, never the external
            ``RUN-...`` identity. See the endpoint's contract.
        reason: required operator explanation, recorded in the event payload.
        requested_by: optional operator identifier, recorded in the payload.
        override_active_owner: take the run even though a dispatch is recorded
            as holding it. An explicit decision, recorded as one.
        owner: the dispatch identifier to stamp. Defaults to a fresh one; the
            caller passes its own when it is about to execute the run itself,
            so the token it fences with is the token in the row.

    Returns:
        The authorization, carrying the run at its new generation.

    Raises:
        ValueError: ``reason`` is empty or whitespace.
        EntityNotFound: no such run.
        EntityConflict: the run is not recoverable, or another transaction
            acquired it first.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required")

    report = assess_recoverability(
        session,
        run_id,
        settings=settings,
        override_active_owner=override_active_owner,
    )
    if not report.recoverable:
        raise EntityConflict(_refusal_text(report))

    runs = TaskRunRepository(session)
    dispatch = owner or uuid.uuid4().hex
    acquired = runs.acquire_execution(
        run_id,
        owner=dispatch,
        # The report's generation, quoted back. This is what makes two
        # simultaneous recoveries produce one owner: the loser's expected
        # generation is the one the winner already replaced.
        expected_generation=report.execution_generation,
        require_unowned=not override_active_owner,
    )
    if acquired is None:
        raise _explain_lost_acquisition(session, runs, run_id, report)

    run, task, _ = load_run_context(session, run_id)
    event = RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=task.project_id,
            task_id=task.id,
            event_type=RunEventType.RUN_RECOVERY_AUTHORIZED,
            attempt=run.attempt_number,
            payload={
                "reason": reason,
                "requested_by": requested_by,
                "external_run_id": run.external_run_id,
                "run_number": run.run_number,
                "previous_generation": report.execution_generation,
                "generation": acquired.execution_generation,
                "execution_owner": dispatch,
                "override_active_owner": override_active_owner,
                "previous_execution_owner": report.execution_owner,
                "task_status": task.status.value,
                "run_status": run.status.value,
                "next_attempt": report.next_attempt,
                "attempts_started": report.attempts_started,
                "reviews_completed": report.reviews_completed,
                "max_attempts": report.max_attempts,
                "checks": [check.describe() for check in report.checks],
            },
        )
    )
    logger.info(
        "run_recovery_authorized",
        run_id=str(run.id),
        external_run_id=run.external_run_id,
        previous_generation=report.execution_generation,
        generation=acquired.execution_generation,
        execution_owner=dispatch,
        next_attempt=report.next_attempt,
        reason=reason,
        requested_by=requested_by,
    )
    return RecoveryAuthorization(
        run=acquired,
        previous_generation=report.execution_generation,
        generation=acquired.execution_generation,
        owner=dispatch,
        report=report,
        event_id=event.id,
        next_attempt=report.next_attempt,
    )


def _refusal_text(report: RecoverabilityReport) -> str:
    refusals = report.refusals
    head = (
        f"Run {report.external_run_id or report.run_id} is not recoverable: "
        + "; ".join(check.detail for check in refusals)
    )
    return head


def _explain_lost_acquisition(
    session: Session,
    runs: TaskRunRepository,
    run_id: UUID,
    report: RecoverabilityReport,
) -> EntityConflict:
    """Why the guarded acquisition did not match, read back as it is now.

    A read, not a second enforcement: the refusal already happened in the
    ``UPDATE``. Both of its predicates can only have been falsified by another
    transaction committing, so the honest thing to report is what that
    transaction left behind.
    """
    session.expire_all()
    current = runs.get(run_id)
    if current is None:
        return EntityConflict(f"Run {run_id} disappeared while it was being recovered")
    if current.execution_generation != report.execution_generation:
        return EntityConflict(
            f"Run {current.external_run_id or current.id} moved to execution "
            f"generation {current.execution_generation} while this request was "
            f"in flight; it was assessed at {report.execution_generation}. "
            "Another recovery won, and a second executor is exactly what that "
            "guard exists to prevent"
        )
    if current.status not in IN_FLIGHT_RUN_STATUSES:
        return EntityConflict(
            f"Run {current.external_run_id or current.id} is {current.status} "
            "now; a run that finished while the request was in flight has no "
            "execution to recover"
        )
    return EntityConflict(
        f"Run {current.external_run_id or current.id} is held by dispatch "
        f"{current.execution_owner}; execution ownership was not acquired"
    )


__all__ = [
    "RECOVERABLE_TASK_STATES",
    "RecoverabilityCheck",
    "RecoverabilityReport",
    "RecoveryAuthorization",
    "assess_recoverability",
    "recover_run",
]
