from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update

from ..db.models import TaskRow
from ..domain.enums import TaskStatus
from ..domain.errors import InvalidStateTransition
from ..domain.models import Task, TaskLimits
from ..domain.state_machine import assert_transition
from .base import Repository

_IMMUTABLE_FIELDS = frozenset(
    {
        "id",
        "project_id",
        "external_task_id",
        "status",
        "created_at",
        "updated_at",
        # Runtime state, like `status`: whether an accepted candidate is in the
        # integration baseline is a fact about Git (concern 51), and a manifest
        # re-sync must not be able to assert it. `record_integration` writes it.
        "unintegrated_commit",
    }
)


class TaskRepository(Repository[TaskRow, Task]):
    row_type = TaskRow
    label = "Task"

    def _to_domain(self, row: TaskRow) -> Task:
        return Task(
            id=row.id,
            project_id=row.project_id,
            external_task_id=row.external_task_id,
            title=row.title,
            section=row.section,
            instructions=row.instructions,
            complexity=row.complexity,
            risk_level=row.risk_level,
            status=row.status,
            depends_on=list(row.depends_on or []),
            verify_commands=list(row.verify_commands or []),
            files_to_inspect=list(row.files_to_inspect or []),
            files_to_modify=list(row.files_to_modify or []),
            files_to_create=list(row.files_to_create or []),
            limits=TaskLimits(
                max_attempts=row.max_attempts,
                max_review_cycles=row.max_review_cycles,
                max_runtime_minutes=row.max_runtime_minutes,
                max_files_changed=row.max_files_changed,
                max_diff_lines=row.max_diff_lines,
            ),
            unintegrated_commit=row.unintegrated_commit,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def add(self, task: Task) -> Task:
        row = TaskRow(
            id=task.id,
            project_id=task.project_id,
            external_task_id=task.external_task_id,
            title=task.title,
            section=task.section,
            instructions=task.instructions,
            complexity=task.complexity,
            risk_level=task.risk_level,
            status=task.status,
            depends_on=list(task.depends_on),
            verify_commands=list(task.verify_commands),
            files_to_inspect=list(task.files_to_inspect),
            files_to_modify=list(task.files_to_modify),
            files_to_create=list(task.files_to_create),
            max_attempts=task.limits.max_attempts,
            max_review_cycles=task.limits.max_review_cycles,
            max_runtime_minutes=task.limits.max_runtime_minutes,
            max_files_changed=task.limits.max_files_changed,
            max_diff_lines=task.limits.max_diff_lines,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, task_id: UUID) -> Task | None:
        row = self._get_row(task_id)
        return self._to_domain(row) if row else None

    def get_by_external_id(self, project_id: UUID, external_task_id: str) -> Task | None:
        row = self.session.scalar(
            select(TaskRow).where(
                TaskRow.project_id == project_id,
                TaskRow.external_task_id == external_task_id,
            )
        )
        return self._to_domain(row) if row else None

    def list_for_project(
        self, project_id: UUID, status: TaskStatus | None = None
    ) -> list[Task]:
        stmt = select(TaskRow).where(TaskRow.project_id == project_id)
        if status is not None:
            stmt = stmt.where(TaskRow.status == status)
        rows = self.session.scalars(stmt.order_by(TaskRow.section, TaskRow.external_task_id)).all()
        return [self._to_domain(row) for row in rows]

    def update_fields(self, task_id: UUID, **fields: object) -> Task:
        """Update declarative task fields (title, limits, dependencies, ...).

        ``status`` is excluded on purpose: it is runtime state owned by the
        state machine, and the database stays authoritative for it after a
        manifest re-sync (build.md section 5).
        """
        row = self._get_row(task_id)
        if row is None:
            raise LookupError(f"Task {task_id} not found")
        for key, value in fields.items():
            if key in _IMMUTABLE_FIELDS or not hasattr(row, key):
                raise AttributeError(f"Task field {key!r} cannot be updated here")
            setattr(row, key, value)
        self.session.flush()
        return self._to_domain(row)

    def record_integration(self, task_id: UUID, *, unintegrated_commit: str | None) -> Task:
        """Record whether this task's accepted output is in the baseline.

        Written only by the integration path (concern 51): ``None`` when the
        baseline advanced to include the candidate, and the candidate's SHA when
        it did not. Separate from ``update_fields`` because this is runtime state
        derived from Git rather than anything the manifest may say, and separate
        from ``transition`` because the task's own status does not change -- a
        blocked integration leaves a delivered task ``COMPLETE``.

        Raises:
            LookupError: no such task.
        """
        row = self._get_row(task_id)
        if row is None:
            raise LookupError(f"Task {task_id} not found")
        row.unintegrated_commit = unintegrated_commit
        self.session.flush()
        return self._to_domain(row)

    def unintegrated_external_ids(self, project_id: UUID) -> frozenset[str]:
        """Ids of this project's tasks with output outside the baseline.

        The set readiness needs (concern 51), as one query rather than a scan of
        loaded rows: it is read on every scheduling pass.
        """
        rows = self.session.scalars(
            select(TaskRow.external_task_id).where(
                TaskRow.project_id == project_id,
                TaskRow.unintegrated_commit.is_not(None),
            )
        ).all()
        return frozenset(rows)

    def transition(self, task_id: UUID, new_status: TaskStatus) -> Task:
        """Move a task to ``new_status``, enforcing the state machine.

        Concern 64. A compare-and-swap, and not a read followed by a write, for
        the same reason ``TaskRunRepository.finish`` is one: the status the
        state machine checked is the status this session *believes* the task is
        in, and a long workflow transaction believes it for a long time. When
        the operator abandons a run, the same transaction moves the task to
        FAILED; a workflow still holding the older copy then writes its own idea
        of the task back over that, and the operator's decision silently
        disappears from the task row while it stands on the run row. A task
        resurrected from FAILED to APPROVED is worse than the run's own
        resurrection, because APPROVED is the state that makes a candidate
        deliverable.

        So the database evaluates the predicate, while it holds the row lock:

        * this call's commit lands first -- the move happens, and the operator's
          abandonment then finds a task it may still move;
        * the operator's commit landed first -- no row matches, nothing is
          written, and the caller is told the task is no longer where it left
          it rather than being allowed to overwrite a newer decision.

        Nothing else changes. Every transition the orchestrator makes on its own
        is unaffected, and ``InvalidStateTransition`` still answers "that move
        is not permitted" from the same helper it always used -- what is new is
        that a move is also refused when the row moved underneath the caller.

        Raises:
            LookupError: no such task.
            InvalidStateTransition: the move is not permitted from the task's
                current status, including when another transaction changed it
                after this session read it.
        """
        row = self._get_row(task_id)
        if row is None:
            raise LookupError(f"Task {task_id} not found")
        current = row.status
        target = assert_transition(current, new_status)
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.status == current)
            .values(status=target)
            # "fetch" so the identity map reflects the row that was matched
            # rather than the copy this session read earlier.
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount != 1:
            # The row is there -- the predicate is what failed. Its current
            # status is read with a statement rather than through the identity
            # map, because the identity map is the stale copy this whole guard
            # exists about, and quoting it would produce a confident and false
            # message. assert_transition then explains the refusal in the terms
            # the rest of the code already uses.
            now = self.session.scalar(
                select(TaskRow.status).where(TaskRow.id == task_id)
            )
            raise InvalidStateTransition(
                TaskStatus(now) if now is not None else current, new_status
            )
        return self._to_domain(self._require_row(task_id))
