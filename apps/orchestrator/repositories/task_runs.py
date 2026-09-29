from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.sql.elements import ColumnElement

from ..db.models import TaskRow, TaskRunRow
from ..domain.enums import ABANDONABLE_RUN_STATUSES, IN_FLIGHT_RUN_STATUSES, RunStatus
from ..domain.errors import (
    AbandonedRunError,
    RunNotInFlightError,
    RunOwnershipLostError,
)
from ..domain.models import TaskRun
from .base import Repository


class TaskRunRepository(Repository[TaskRunRow, TaskRun]):
    row_type = TaskRunRow
    label = "Task run"

    def _to_domain(self, row: TaskRunRow) -> TaskRun:
        return TaskRun(
            id=row.id,
            task_id=row.task_id,
            run_number=row.run_number,
            attempt_number=row.attempt_number,
            review_cycle=row.review_cycle,
            status=row.status,
            external_run_id=row.external_run_id,
            coder_model_id=row.coder_model_id,
            worker_image=row.worker_image,
            starting_commit=row.starting_commit,
            candidate_commit=row.candidate_commit,
            branch_name=row.branch_name,
            context_hash=row.context_hash,
            prompt_version=row.prompt_version,
            failure_reason=row.failure_reason,
            artifact_path=row.artifact_path,
            started_at=row.started_at,
            completed_at=row.completed_at,
            active_runtime_ms=row.active_runtime_ms,
            active_started_at=row.active_started_at,
            execution_generation=row.execution_generation,
            execution_owner=row.execution_owner,
            execution_started_at=row.execution_started_at,
        )

    def next_run_number(self, task_id: UUID) -> int:
        current = self.session.scalar(
            select(func.max(TaskRunRow.run_number)).where(TaskRunRow.task_id == task_id)
        )
        return (current or 0) + 1

    def add(self, run: TaskRun) -> TaskRun:
        row = TaskRunRow(
            id=run.id,
            task_id=run.task_id,
            run_number=run.run_number,
            attempt_number=run.attempt_number,
            review_cycle=run.review_cycle,
            status=run.status,
            external_run_id=run.external_run_id,
            coder_model_id=run.coder_model_id,
            worker_image=run.worker_image,
            starting_commit=run.starting_commit,
            candidate_commit=run.candidate_commit,
            branch_name=run.branch_name,
            context_hash=run.context_hash,
            prompt_version=run.prompt_version,
            failure_reason=run.failure_reason,
            artifact_path=run.artifact_path,
            started_at=run.started_at,
            completed_at=run.completed_at,
            active_runtime_ms=run.active_runtime_ms,
            active_started_at=run.active_started_at,
            execution_generation=run.execution_generation,
            execution_owner=run.execution_owner,
            execution_started_at=run.execution_started_at,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, run_id: UUID) -> TaskRun | None:
        row = self._get_row(run_id)
        return self._to_domain(row) if row else None

    def list_for_task(self, task_id: UUID) -> list[TaskRun]:
        rows = self.session.scalars(
            select(TaskRunRow)
            .where(TaskRunRow.task_id == task_id)
            .order_by(TaskRunRow.run_number)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_for_project(self, project_id: UUID) -> list[TaskRun]:
        """Every run of a project, oldest first.

        Phase L is arithmetic over this set: section 35's metrics, the review
        history and the training index all need "all the runs of this project"
        in one query rather than a lookup per task.
        """
        rows = self.session.scalars(
            select(TaskRunRow)
            .join(TaskRow, TaskRunRow.task_id == TaskRow.id)
            .where(TaskRow.project_id == project_id)
            .order_by(TaskRunRow.started_at, TaskRunRow.id)
        ).all()
        return [self._to_domain(row) for row in rows]

    def list_terminal(self, project_id: UUID | None = None) -> list[TaskRun]:
        """Runs that have finished, whatever the outcome (section 35).

        Both terminal states, deliberately: a failed run is the half of the
        evidence that makes a success rate a measurement rather than a count
        of what went right.
        """
        stmt = select(TaskRunRow).where(
            TaskRunRow.status.in_(
                [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABANDONED]
            )
        )
        if project_id is not None:
            stmt = stmt.join(TaskRow, TaskRunRow.task_id == TaskRow.id).where(
                TaskRow.project_id == project_id
            )
        rows = self.session.scalars(stmt.order_by(TaskRunRow.started_at, TaskRunRow.id)).all()
        return [self._to_domain(row) for row in rows]

    def list_incomplete(self) -> list[TaskRun]:
        """Runs that were in flight when the orchestrator stopped (section 28).

        The ``status IN (PENDING, RUNNING)`` filter is the mechanism by which a
        terminal run stays undiscoverable, and it is deliberately the only
        mechanism. ``SUCCEEDED``, ``FAILED`` and ``ABANDONED`` (concern 64) are
        all absent, so recovery never has to decide whether a terminal run is
        worth resuming: it is never handed one. A caller cannot accidentally
        resume an abandoned run by forgetting to check, because the query it
        iterates does not return abandoned runs in the first place.

        The filter is ``IN_FLIGHT_RUN_STATUSES`` rather than a second literal,
        and concern 65's operator retry refuses on exactly that set: a task may
        be retried only when recovery would say no run of its work is going.
        One definition, so the two answers cannot disagree.
        """
        rows = self.session.scalars(
            select(TaskRunRow).where(TaskRunRow.status.in_(IN_FLIGHT_RUN_STATUSES))
        ).all()
        return [self._to_domain(row) for row in rows]

    def in_flight_for_task(self, task_id: UUID) -> TaskRun | None:
        """The run of this task that is still in flight, if there is one.

        The read that explains a refusal: concern 65's operator retry is
        guarded on the absence of an in-flight run, and when the guard refuses
        the operator is told *which* run is in the way rather than a bare
        conflict. A statement, not the identity map, because the answer has to
        be the committed one -- the identity map is precisely the stale copy
        the guard exists about.
        """
        row = self.session.scalar(
            select(TaskRunRow)
            .where(
                TaskRunRow.task_id == task_id,
                TaskRunRow.status.in_(IN_FLIGHT_RUN_STATUSES),
            )
            .order_by(TaskRunRow.run_number)
            .limit(1)
        )
        return self._to_domain(row) if row is not None else None

    def finish(
        self,
        run_id: UUID,
        status: RunStatus,
        failure_reason: str | None = None,
        *,
        expected_generation: int | None = None,
    ) -> TaskRun:
        """Close a run, durably refusing to move one an operator abandoned.

        Concern 64. Abandonment is the only terminal status a running workflow
        can still race with: the operator writes ``ABANDONED`` from a different
        transaction than the one that finishes the run. An unconditional
        assignment here loses that race -- the workflow's ``SUCCEEDED``
        overwrites the operator's decision and the run looks like it completed
        normally, which is exactly the silent resurrection concern 64 is about.

        So this is a compare-and-swap, not a read followed by a write. The
        ``status <> 'ABANDONED'`` predicate is evaluated by the database while
        the row lock is held, which is what makes the outcome a function of
        commit order rather than of whichever transaction happened to read the
        row last:

        * abandonment committed first -- no row matches, and the caller is told
          the run is abandoned;
        * this call committed first -- the run reaches its intended terminal
          status, and a later :meth:`abandon` finds nothing active to abandon.

        No other transition is affected. ``PENDING``, ``RUNNING``,
        ``SUCCEEDED`` and ``FAILED`` runs finish exactly as they did before;
        only ``ABANDONED`` is protected, because it is the only status whose
        loss is silent.

        Args:
            expected_generation: the execution generation this executor was
                dispatched at. Given, it is part of the same guarded statement,
                so an executor that has been superseded finds no row to write --
                the same fence :meth:`require_in_flight` applies to every other
                durable checkpoint. Omitted, the write is unfenced as before.

        Raises:
            LookupError: no such run.
            AbandonedRunError: the run was abandoned by an operator. The write
                did not happen.
            RunOwnershipLostError: the run moved to a newer execution
                generation, so this executor no longer owns it. The write did
                not happen.
        """
        if status is RunStatus.ABANDONED:
            # The operator path has the mirror-image predicate, and it is the
            # one that can legitimately lose; see abandon().
            abandoned = self.abandon(run_id, failure_reason=failure_reason)
            if abandoned is not None:
                return abandoned
            row = self._require_row(run_id)
            raise AbandonedRunError(row.id)
        conditions: list[ColumnElement[bool]] = [
            TaskRunRow.status != RunStatus.ABANDONED
        ]
        if expected_generation is not None:
            conditions.append(
                TaskRunRow.execution_generation == expected_generation
            )
        finished = self._try_finish(run_id, status, failure_reason, *conditions)
        if finished is not None:
            return finished
        row = self._require_row(run_id)
        if expected_generation is not None and (
            row.execution_generation != expected_generation
        ):
            raise RunOwnershipLostError(
                row.id,
                held=expected_generation,
                current=row.execution_generation,
            )
        raise AbandonedRunError(row.id)

    def abandon(self, run_id: UUID, *, failure_reason: str | None = None) -> TaskRun | None:
        """Compare-and-swap a run to ``ABANDONED`` from an in-flight status.

        The mirror image of :meth:`finish`: this write is refused unless the
        run is ``PENDING`` or ``RUNNING``, so an operator cannot rewrite a run
        that already completed, and two operators racing cannot both win.

        Returns ``None`` -- rather than raising -- when the predicate did not
        match, because "someone else already finished or abandoned this run" is
        a *result* the caller has to branch on, not an error. The caller
        re-reads the run and reports idempotency or a conflict truthfully.
        """
        return self._try_finish(
            run_id,
            RunStatus.ABANDONED,
            failure_reason,
            TaskRunRow.status.in_(ABANDONABLE_RUN_STATUSES),
        )

    def _try_finish(
        self,
        run_id: UUID,
        status: RunStatus,
        failure_reason: str | None,
        *conditions: ColumnElement[bool],
    ) -> TaskRun | None:
        """Write a terminal status if the row still matches ``conditions``.

        Returns the finished run, or ``None`` if the row did not match -- which
        is the same answer whether it was missing, already terminal, or lost
        the race. The caller re-reads to tell those apart.
        """
        result = self.session.execute(
            update(TaskRunRow)
            .where(TaskRunRow.id == run_id, *conditions)
            .values(
                status=status,
                failure_reason=failure_reason,
                completed_at=datetime.now(UTC),
            )
            # "fetch" refreshes the identity map from the rows the update
            # matched, so the TaskRun returned below reflects this write rather
            # than a stale copy read earlier in the transaction.
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            return None
        return self._to_domain(self._require_row(run_id))

    def acquire_execution(
        self,
        run_id: UUID,
        *,
        owner: str,
        expected_generation: int | None = None,
        require_unowned: bool = True,
    ) -> TaskRun | None:
        """Take exclusive execution ownership of an in-flight run (concern 67).

        One guarded ``UPDATE``, because the acquisition *is* the guard. It
        increments ``execution_generation`` and stamps ``execution_owner`` in a
        single statement whose predicate the database evaluates while it holds
        the row lock, so two dispatches racing for the same run produce one
        winner decided by commit order and not by whichever read the row last.
        A read-then-write here would be the classic check-then-act: both
        callers would see an unowned run at generation N and both would write
        generation N+1, which is precisely the second executor this exists to
        make impossible.

        The increment is the fencing token. Whoever loses the race -- or whoever
        was executing before it -- still holds the older number, and
        :meth:`require_in_flight` refuses every durable checkpoint that presents
        it. That is what makes an older executor's late answer harmless rather
        than merely unlikely.

        Args:
            owner: an opaque identifier for this dispatch, written to
                ``execution_owner`` and required back to release it.
            expected_generation: when given, the generation the caller assessed
                the run at. The update matches only if the row is still there,
                which is how an operator recovery refuses a run that moved
                between the recoverability assessment and the acquisition.
            require_unowned: when true (the default), refuse a run another
                dispatch is already holding. An operator may override this
                deliberately -- a process killed mid-dispatch leaves an owner
                behind that nothing will ever clear -- and the generation fence
                is what keeps that override safe.

        Returns:
            The run, now owned at the new generation, or ``None`` when the
            predicate did not match. ``None`` is a result to branch on and not
            an error: the caller re-reads the row to say *why* truthfully.
        """
        conditions: list[ColumnElement[bool]] = [
            TaskRunRow.status.in_(IN_FLIGHT_RUN_STATUSES)
        ]
        if expected_generation is not None:
            conditions.append(TaskRunRow.execution_generation == expected_generation)
        if require_unowned:
            conditions.append(TaskRunRow.execution_owner.is_(None))
        result = self.session.execute(
            update(TaskRunRow)
            .where(TaskRunRow.id == run_id, *conditions)
            .values(
                execution_generation=TaskRunRow.execution_generation + 1,
                execution_owner=owner,
                execution_started_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            return None
        return self._to_domain(self._require_row(run_id))

    def release_execution(self, run_id: UUID, *, owner: str) -> bool:
        """Give up ownership, but only if this dispatch still holds it.

        The ``execution_owner = :owner`` predicate is why a fenced executor's
        cleanup cannot strip ownership from the executor that superseded it: an
        old dispatch unwinding through its ``finally`` matches no row and clears
        nothing. The generation is deliberately *not* decremented -- it only
        ever goes forwards, so a token that was fenced stays fenced.

        Returns whether this call released anything.
        """
        result = self.session.execute(
            update(TaskRunRow)
            .where(TaskRunRow.id == run_id, TaskRunRow.execution_owner == owner)
            .values(execution_owner=None, execution_started_at=None)
            .execution_options(synchronize_session="fetch")
        )
        return result.rowcount == 1

    def require_in_flight(
        self, run_id: UUID, *, expected_generation: int | None = None
    ) -> None:
        """Barrier: refuse unless this run is still PENDING or RUNNING.

        Concern 64, and the companion to :meth:`finish`. ``finish`` guards one
        write -- the terminal status -- and that is not the same question as
        "may this workflow keep going". Concern 66 commits before every model
        call, so an operator can commit while the external operation is in
        flight. The graph's own fence is a status read in an earlier node, so
        everything after that read needs this fresh, locking validation.

        This is where that window is closed. ``SELECT ... FOR UPDATE`` with the
        in-flight predicate, so the database evaluates the predicate while it
        holds the row lock, and the ordering is the one PostgreSQL gives:

        * the operator's commit landed first -- the lock is free, the predicate
          is re-evaluated against the committed ``ABANDONED``, no row comes
          back, and the turn is refused;
        * this call got there first -- the operator's write blocks behind the
          lock, this turn commits, and the operator's ``abandon`` finds the run
          it is about to take. Whoever holds the lock first is the one whose
          decision the other has to live with, which is the only ordering rule
          that can be true for both.

        A read would not do. ``SELECT`` without a lock takes no lock, so the
        operator could commit immediately afterwards and the check would be
        true and wrong -- a guard that reports the answer it read a moment
        before the answer changed.

        **Concern 67 adds the second half of the question.** "Is this run in
        flight" and "am I still the one executing it" are different questions,
        and until an operator could recover a stranded run only the first one
        could be wrong. Now a run can be in flight, unabandoned, and owned by
        somebody else -- so ``expected_generation`` is folded into the same
        locked predicate rather than checked separately afterwards. One
        statement, one lock, one answer: a caller cannot pass the in-flight test
        and then lose the ownership test to a commit that landed in between.

        Callers that pass no ``expected_generation`` get exactly the concern 64
        behaviour. That is for the call sites whose question really is only
        about terminality; every durable checkpoint in the fix loop passes its
        generation, because that is where progress becomes persistent.

        Raises:
            LookupError: no such run.
            AbandonedRunError: an operator abandoned the run.
            RunOwnershipLostError: the run is in flight but a newer execution
                generation owns it (concern 67).
            RunNotInFlightError: the run is no longer in flight for some other
                reason -- it finished on its own. The workflow is later than its
                own run, which is a different fault and is not reported as an
                abandonment.
        """
        conditions: list[ColumnElement[bool]] = [
            TaskRunRow.status.in_(ABANDONABLE_RUN_STATUSES)
        ]
        if expected_generation is not None:
            conditions.append(TaskRunRow.execution_generation == expected_generation)
        found = self.session.scalar(
            select(TaskRunRow.id)
            .where(TaskRunRow.id == run_id, *conditions)
            .with_for_update()
        )
        if found is not None:
            return
        row = self._require_row(run_id)
        if row.status is RunStatus.ABANDONED:
            raise AbandonedRunError(row.id)
        if (
            expected_generation is not None
            and row.execution_generation != expected_generation
            and row.status in ABANDONABLE_RUN_STATUSES
        ):
            # Concern 67. The run is in flight and nobody stopped it; somebody
            # else was authorized to continue it. Reported as its own fault so
            # an operator is not told a run they recovered was abandoned.
            raise RunOwnershipLostError(
                row.id,
                held=expected_generation,
                current=row.execution_generation,
            )
        raise RunNotInFlightError(
            f"Run {row.id} is {row.status}; a workflow still holding it is not in flight"
        )

    def update_fields(self, run_id: UUID, **fields: object) -> TaskRun:
        row = self._require_row(run_id)
        # Concern 64: the same invariant as finish(), on the other write path
        # into a run's status. _prepare_workspace uses this to move PENDING to
        # RUNNING, which would resurrect an abandoned run just as surely as a
        # late SUCCEEDED would. A read-then-write is enough here because this
        # method is bookkeeping on a run the caller already holds, and the
        # authoritative guard is finish()'s compare-and-swap.
        if "status" in fields:
            requested = fields["status"]
            if requested is not RunStatus.ABANDONED and row.status is RunStatus.ABANDONED:
                raise AbandonedRunError(row.id)
        for key, value in fields.items():
            if not hasattr(row, key):
                raise AttributeError(f"TaskRun has no field {key!r}")
            setattr(row, key, value)
        self.session.flush()
        return self._to_domain(row)
