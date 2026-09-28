"""Restart reconciliation for incomplete runs (build.md section 28).

**Abandoned runs are excluded by the query, not by a branch here (concern
64).** The first cut of this module carried an explicit ``ABANDONED``
disposition, reached from a check like ``if run.status is RunStatus.ABANDONED``,
which could never be true: the loop iterates ``list_incomplete()``, and that
repository method selects only ``PENDING`` and ``RUNNING``. The branch was
unreachable code standing in for a real guarantee, and the real guarantee is
one level down in the query itself. Terminality for *discovery* is therefore a
property of the repository filter, and a test asserts it directly -- an
abandoned run is absent from ``list_incomplete()`` after the session and
process are rebuilt, not merely skipped by a conditional that never runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.settings import Settings, get_settings
from ..domain.enums import TaskStatus
from ..repositories import TaskRunRepository
from ..services.workspace import load_run_context, workspace_path


class RecoveryDisposition(StrEnum):
    RESUMABLE = "RESUMABLE"
    PAUSED = "PAUSED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    WORKSPACE_MISSING = "WORKSPACE_MISSING"


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    run_id: UUID
    task_id: UUID
    disposition: RecoveryDisposition
    detail: str


def inspect_incomplete_runs(
    session: Session, *, settings: Settings | None = None
) -> tuple[RecoveryCandidate, ...]:
    """Classify old runs without assuming their worker containers survived."""
    config = settings or get_settings()
    candidates: list[RecoveryCandidate] = []
    for run in TaskRunRepository(session).list_incomplete():
        _, task, project = load_run_context(session, run.id)
        if task.status is TaskStatus.PAUSED:
            disposition = RecoveryDisposition.PAUSED
            detail = "operator pause is still in force"
        elif task.status is TaskStatus.HUMAN_REVIEW:
            disposition = RecoveryDisposition.WAITING_FOR_HUMAN
            detail = "an escalation must be answered"
        elif run.branch_name and not workspace_path(
            project.id,
            task.external_task_id,
            run.run_number,
            settings=config,
        ).exists():
            disposition = RecoveryDisposition.WORKSPACE_MISSING
            detail = "database records a worktree but its directory is missing"
        else:
            disposition = RecoveryDisposition.RESUMABLE
            detail = "resume from persisted database state and graph checkpoint"
        candidates.append(
            RecoveryCandidate(
                run_id=run.id,
                task_id=task.id,
                disposition=disposition,
                detail=detail,
            )
        )
    return tuple(candidates)


__all__ = ["RecoveryCandidate", "RecoveryDisposition", "inspect_incomplete_runs"]
