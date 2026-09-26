from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.escalation import EscalationIntent
from apps.orchestrator.domain.models import TaskLimits
from apps.orchestrator.domain.workflow import deadline_exceeded, effect_of, run_deadline


def test_runtime_deadline_uses_the_persisted_start_and_accepts_sqlite_datetimes():
    started = datetime(2026, 9, 26, 12, 0)  # SQLite commonly restores this naive.
    deadline = run_deadline(started, TaskLimits(max_runtime_minutes=7))

    assert deadline == datetime(2026, 9, 26, 12, 7, tzinfo=UTC)
    assert not deadline_exceeded(deadline, now=deadline - timedelta(seconds=1))
    assert deadline_exceeded(deadline, now=deadline)


def test_every_human_intent_has_an_explicit_effect():
    assert {effect_of(intent).intent for intent in EscalationIntent} == set(
        EscalationIntent
    )
    assert effect_of(EscalationIntent.ACCEPT_CANDIDATE).commit_candidate
    assert effect_of(EscalationIntent.REQUEST_CHANGES).feedback_to_coder
    assert effect_of(EscalationIntent.RETRY_TASK).task_status is TaskStatus.READY
    assert effect_of(EscalationIntent.COMPLETED_BY_HAND).completes_task
    assert effect_of(EscalationIntent.ABANDON_TASK).task_status is TaskStatus.FAILED
