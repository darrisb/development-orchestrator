"""Review and escalation queries for the API (build.md section 39).

Thin on purpose. Reviewing is the agent's job and routing is the domain's;
this module only reads what they left behind and answers the one write a
human makes -- resolving an escalation.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import EscalationStatus
from ..domain.models import HumanEscalation, Review
from ..repositories import EscalationRepository, ReviewRepository
from .errors import EntityConflict, EntityNotFound
from .runs import get_run

logger = get_logger(__name__)


def list_reviews_for_run(session: Session, run_id: UUID) -> list[Review]:
    """Every review of a run, oldest cycle first.

    Raises:
        EntityNotFound: no such run. Distinguished from a run with no reviews,
            which is an empty list and an ordinary state.
    """
    get_run(session, run_id)
    return ReviewRepository(session).list_for_run(run_id)


def list_open_escalations(
    session: Session, *, task_id: UUID | None = None
) -> list[HumanEscalation]:
    return EscalationRepository(session).list_open(task_id=task_id)


def resolve_escalation(
    session: Session,
    escalation_id: UUID,
    *,
    resolution: str,
    status: EscalationStatus = EscalationStatus.RESOLVED,
    option_key: str | None = None,
    human_commit: str | None = None,
) -> HumanEscalation:
    """Record a human's answer to an escalation (section 24).

    The task is deliberately not moved here. What an answer *means* for the
    run -- retry from the known-good SHA, accept the candidate, abandon the
    task -- is a workflow decision (phase K), and an endpoint that guessed it
    would be the system deciding what the human decided.

    Raises:
        EntityNotFound: no such escalation.
        EntityConflict: it has already been answered.
    """
    escalations = EscalationRepository(session)
    existing = escalations.get(escalation_id)
    if existing is None:
        raise EntityNotFound("Escalation", escalation_id)
    if existing.status is not EscalationStatus.OPEN:
        raise EntityConflict(
            f"Escalation {escalation_id} was already {existing.status.value.casefold()}"
        )
    # Imported here to keep the resource service free of graph construction
    # while still making the answer take effect in this transaction.
    from ..workflow.resolution import apply_escalation_answer, intent_for_key

    intent = intent_for_key(existing, option_key) if option_key else None
    resolved = apply_escalation_answer(
        session,
        escalation_id,
        resolution=resolution,
        status=status,
        intent=intent,
        human_commit=human_commit,
    )
    logger.info(
        "escalation_resolved",
        escalation_id=str(escalation_id),
        task_id=str(resolved.task_id),
        status=resolved.status.value,
    )
    return resolved


def reconcile_human_commit(
    session: Session,
    escalation_id: UUID,
    *,
    human_commit: str,
) -> HumanEscalation:
    """Reconcile a historical COMPLETED_BY_HAND escalation with a human commit.

    For escalations resolved before concern 73, where the human commit was not
    recorded or integrated. This function:
    - Validates the escalation is RESOLVED with COMPLETED_BY_HAND intent
    - Validates the task is COMPLETE
    - Validates the human_commit exists
    - Integrates the commit through the canonical mechanism
    - Updates the escalation record with the human_commit
    - Records provenance in events

    Raises:
        EntityNotFound: no such escalation or commit.
        EntityConflict: escalation not in correct state, or integration failed.
    """
    from ..config.settings import get_settings
    from ..domain.enums import EscalationStatus, TaskStatus
    from ..domain.escalation import EscalationIntent
    from ..repositories import EscalationRepository, TaskRepository
    from ..services.errors import EntityConflict, EntityNotFound
    from ..services.integration import integrate_human_commit

    escalations = EscalationRepository(session)
    existing = escalations.get(escalation_id)
    if existing is None:
        raise EntityNotFound("Escalation", escalation_id)

    if existing.status is not EscalationStatus.RESOLVED:
        raise EntityConflict(
            f"Escalation {escalation_id} is {existing.status.value}, not RESOLVED"
        )

    if existing.resolution_intent is not EscalationIntent.COMPLETED_BY_HAND:
        raise EntityConflict(
            f"Escalation {escalation_id} was resolved with {existing.resolution_intent}, "
            "not COMPLETED_BY_HAND"
        )

    if existing.human_commit is not None:
        raise EntityConflict(
            f"Escalation {escalation_id} already has human_commit {existing.human_commit}"
        )

    task = TaskRepository(session).get(existing.task_id)
    if task is None:
        raise EntityNotFound("Task", existing.task_id)

    if task.status is not TaskStatus.COMPLETE:
        raise EntityConflict(
            f"Task {task.external_task_id} is {task.status.value}, not COMPLETE"
        )

    config = get_settings()
    project = _load_project_for_task(session, task.project_id)

    integrate_human_commit(
        session,
        project,
        task,
        human_commit,
        task_run_id=existing.task_run_id,
        settings=config,
    )

    updated = escalations.reconcile_human_commit(escalation_id, human_commit)

    logger.info(
        "human_commit_reconciled",
        escalation_id=str(escalation_id),
        task_id=str(existing.task_id),
        human_commit=human_commit,
    )

    return updated


def _load_project_for_task(session: Session, project_id: UUID):
    from ..repositories import ProjectRepository

    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise EntityNotFound("Project", project_id)
    return project


__all__ = [
    "list_open_escalations",
    "list_reviews_for_run",
    "reconcile_human_commit",
    "resolve_escalation",
]
