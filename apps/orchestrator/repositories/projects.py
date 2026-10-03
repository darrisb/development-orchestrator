from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ..db.models import ProjectRow
from ..domain.enums import ProjectStatus
from ..domain.model_policy import ModelPolicy
from ..domain.models import Project
from ..domain.verification import VerificationProfile
from .base import Repository

_IMMUTABLE_FIELDS = frozenset({"id", "status", "created_at", "updated_at"})


class ProjectRepository(Repository[ProjectRow, Project]):
    row_type = ProjectRow

    def _to_domain(self, row: ProjectRow) -> Project:
        return Project(
            id=row.id,
            name=row.name,
            external_project_id=row.external_project_id,
            repository_path=row.repository_path,
            default_branch=row.default_branch,
            worker_profile=row.worker_profile,
            status=row.status,
            protected_paths=list(row.protected_paths or []),
            sensitive_path_exceptions=list(row.sensitive_path_exceptions or []),
            generated_path_exceptions=list(row.generated_path_exceptions or []),
            dependency_paths=list(row.dependency_paths or []),
            dependency_bootstrap_commands=list(row.dependency_bootstrap_commands or []),
            approval_gated_categories=(
                list(row.approval_gated_categories)
                if row.approval_gated_categories is not None
                else None
            ),
            verification=VerificationProfile.from_mapping(row.verification_profile),
            model_policy=ModelPolicy.from_mapping(row.model_policy),
            milestone_interval=row.milestone_interval,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def add(self, project: Project) -> Project:
        row = ProjectRow(
            id=project.id,
            name=project.name,
            external_project_id=project.external_project_id,
            repository_path=project.repository_path,
            default_branch=project.default_branch,
            worker_profile=project.worker_profile,
            status=project.status,
            protected_paths=list(project.protected_paths),
            sensitive_path_exceptions=list(project.sensitive_path_exceptions),
            generated_path_exceptions=list(project.generated_path_exceptions),
            dependency_paths=list(project.dependency_paths),
            dependency_bootstrap_commands=list(project.dependency_bootstrap_commands),
            approval_gated_categories=project.approval_gated_categories,
            verification_profile=project.verification.describe(),
            model_policy=project.model_policy.describe(),
            milestone_interval=project.milestone_interval,
        )
        self.session.add(row)
        self.session.flush()
        return self._to_domain(row)

    def get(self, project_id: UUID) -> Project | None:
        row = self._get_row(project_id)
        return self._to_domain(row) if row else None

    def lock(self, project_id: UUID) -> Project:
        """Read a project under a row lock, so what follows cannot race it.

        The same barrier ``TaskRepository.lock`` is for a task, at the one
        granularity the project owns something shared: the supervisor's
        per-project integration worktree
        (``integration_worktree_path(project.id)``). Baseline certification
        resets and runs commands in that directory, and the database's unique
        constraint protects only the row it ends with -- two workers measuring
        the same tree at once would be resetting the same checkout under each
        other, and whichever one wrote last would record evidence gathered from
        a tree the other one was mutating.

        Certification is not the only writer of that directory, and this lock is
        therefore not only certification's. ``integrate_candidate`` and
        ``integrate_human_commit`` reset the same checkout, merge into it and
        run the cumulative gate in it, so all three take this lock: whichever
        one gets the row owns the shared worktree, and the others wait. A lock
        held by only one of several writers of one resource excludes nothing.

        ``SELECT ... FOR UPDATE``: the database holds the lock, so a concurrent
        certifier blocks here rather than in the filesystem, and the wait is
        bounded by ``lock_timeout`` and surfaced as ``LockWaitTimeout`` like
        every other lock wait in the application (concern 57). The lock lasts
        for the caller's transaction, which is the convention the rest of the
        repositories follow and the reason this is a read and not a flag.

        Per *project*, not per project-and-commit: the worktree is the resource
        being protected and there is one of it per project, so a narrower
        identity would not actually exclude the two writers. Unrelated projects
        lock different rows and never wait for each other.

        Raises:
            LookupError: no such project.
        """
        row = self.session.scalar(
            select(ProjectRow).where(ProjectRow.id == project_id).with_for_update()
        )
        if row is None:
            raise LookupError(f"Project {project_id} not found")
        return self._to_domain(row)

    def get_by_external_id(self, external_id: str) -> Project | None:
        row = self.session.scalar(
            select(ProjectRow).where(ProjectRow.external_project_id == external_id)
        )
        return self._to_domain(row) if row else None

    def list(self) -> list[Project]:
        rows = self.session.scalars(select(ProjectRow).order_by(ProjectRow.created_at)).all()
        return [self._to_domain(row) for row in rows]

    def update_fields(self, project_id: UUID, **fields: object) -> Project:
        """Update declarative project fields.

        ``status`` is excluded on purpose: runtime status changes go through
        ``set_status`` so a manifest re-sync can never restart a paused project.
        """
        row = self._get_row(project_id)
        if row is None:
            raise LookupError(f"Project {project_id} not found")
        for key, value in fields.items():
            if key in _IMMUTABLE_FIELDS or not hasattr(row, key):
                raise AttributeError(f"Project field {key!r} cannot be updated here")
            setattr(row, key, value)
        self.session.flush()
        return self._to_domain(row)

    def set_status(self, project_id: UUID, status: ProjectStatus) -> Project:
        row = self._get_row(project_id)
        if row is None:
            raise LookupError(f"Project {project_id} not found")
        row.status = status
        self.session.flush()
        return self._to_domain(row)
