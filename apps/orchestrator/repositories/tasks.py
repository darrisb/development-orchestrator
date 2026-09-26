from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ..db.models import TaskRow
from ..domain.enums import TaskStatus
from ..domain.models import Task, TaskLimits
from ..domain.state_machine import assert_transition
from .base import Repository

_IMMUTABLE_FIELDS = frozenset(
    {"id", "project_id", "external_task_id", "status", "created_at", "updated_at"}
)


class TaskRepository(Repository[TaskRow, Task]):
    row_type = TaskRow

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

    def transition(self, task_id: UUID, new_status: TaskStatus) -> Task:
        """Move a task to ``new_status``, enforcing the state machine.

        Raises:
            InvalidStateTransition: if the move is not permitted.
        """
        row = self._get_row(task_id)
        if row is None:
            raise LookupError(f"Task {task_id} not found")
        row.status = assert_transition(row.status, new_status)
        self.session.flush()
        return self._to_domain(row)
