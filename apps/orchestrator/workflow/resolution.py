"""Apply machine-readable human escalation answers (phase K, concern 32)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..config.settings import Settings, get_settings
from ..domain.enums import EscalationStatus
from ..domain.escalation import EscalationIntent, option_for_intent
from ..domain.workflow import effect_of
from ..repositories import EscalationRepository, TaskRepository
from ..services.delivery import complete_by_hand, deliver_escalated_candidate
from ..services.errors import EntityConflict, EntityNotFound
from ..services.integration import retry_integration
from ..services.workspace import attach_workspace
from ..services.worktrees import release_for_run


def apply_escalation_answer(
    session: Session,
    escalation_id: UUID,
    *,
    resolution: str,
    intent: EscalationIntent | None,
    status: EscalationStatus = EscalationStatus.RESOLVED,
    settings: Settings | None = None,
):
    """Record an answer and perform exactly the selected option's effect.

    Dismissing an escalation has no intent and therefore no side effect. A
    resolved answer must select an option that was actually offered; accepting
    a rolled-back candidate can never be smuggled in through the API.
    """
    config = settings or get_settings()
    escalations = EscalationRepository(session)
    existing = escalations.get(escalation_id)
    if existing is None:
        raise EntityNotFound("Escalation", escalation_id)
    if existing.status is not EscalationStatus.OPEN:
        raise EntityConflict(
            f"Escalation {escalation_id} was already "
            f"{existing.status.value.casefold()}"
        )
    has_actionable_options = any(hasattr(option, "intent") for option in existing.options)
    if status is EscalationStatus.RESOLVED and intent is None and has_actionable_options:
        raise EntityConflict("A resolved escalation must select an offered option")
    if intent is not None and option_for_intent(existing.options, intent) is None:
        raise EntityConflict(
            f"Escalation {escalation_id} did not offer {intent.value}"
        )

    answered = escalations.resolve(
        escalation_id,
        resolution=resolution,
        status=status,
        intent=intent,
    )
    if intent is None:
        return answered

    task = TaskRepository(session).get(existing.task_id)
    if task is None:
        raise EntityNotFound("Task", existing.task_id)
    effect = effect_of(intent)
    run_id = existing.task_run_id

    if effect.retry_integration:
        # The task is already COMPLETE and stays so, whether this succeeds or
        # not (concern 51). What the answer authorises is one more attempt at the
        # cumulative gate; if it fails again, `integrate_candidate` blocks again
        # and opens a new escalation, so the condition never disappears quietly.
        if run_id is None:
            raise EntityConflict(
                "This escalation has no run, so there is no candidate to integrate"
            )
        retry_integration(session, run_id, settings=config)
        return answered

    if effect.commit_candidate:
        if run_id is None:
            raise EntityConflict("This escalation has no candidate run to accept")
        deliver_escalated_candidate(
            session, attach_workspace(session, run_id, settings=config), settings=config
        )
    elif effect.completes_task:
        complete_by_hand(session, task, task_run_id=run_id)
    elif task.status is not effect.task_status:
        TaskRepository(session).transition(task.id, effect.task_status)

    if effect.release_worktree and run_id is not None and not effect.commit_candidate:
        release_for_run(session, run_id, settings=config)
    return answered


def intent_for_key(escalation, key: str) -> EscalationIntent:
    """Resolve a displayed key without interpreting the option's prose."""
    normalized = key.strip().rstrip(".").casefold()
    for option in escalation.options:
        if hasattr(option, "key") and option.key.casefold() == normalized:
            return option.intent
    raise EntityConflict(f"Escalation {escalation.id} did not offer option {key!r}")


__all__ = ["apply_escalation_answer", "intent_for_key"]
