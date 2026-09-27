from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.escalation import EscalationIntent
from apps.orchestrator.domain.models import TaskLimits
from apps.orchestrator.domain.workflow import deadline_exceeded, effect_of, run_deadline


def test_runtime_deadline_uses_active_start_remaining_budget_and_accepts_sqlite_datetimes():
    started = datetime(2026, 9, 26, 12, 0)  # SQLite commonly restores this naive.
    deadline = run_deadline(
        started, TaskLimits(max_runtime_minutes=7), consumed_runtime_ms=120_000
    )

    assert deadline == datetime(2026, 9, 26, 12, 5, tzinfo=UTC)
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


def test_retrying_an_integration_does_not_move_the_task():
    """Concern 51's resolution authorises one thing and nothing else.

    The task is already COMPLETE with a reviewed candidate, so the answer must
    not commit anything, must not reopen the task, and must not release a
    worktree that delivery already released. What it changes is whether the
    baseline contains the work.
    """
    effect = effect_of(EscalationIntent.RETRY_INTEGRATION)
    assert effect.retry_integration
    assert effect.task_status is TaskStatus.COMPLETE
    assert not effect.commit_candidate
    assert not effect.reopens_task
    assert not effect.feedback_to_coder
    assert not effect.release_worktree
