from __future__ import annotations

import pytest

from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.errors import InvalidStateTransition
from apps.orchestrator.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    assert_transition,
    can_transition,
    is_active,
    is_terminal,
)


def test_happy_path_walks_from_pending_to_complete():
    path = [
        TaskStatus.PENDING,
        TaskStatus.READY,
        TaskStatus.PLANNING,
        TaskStatus.CODING,
        TaskStatus.VERIFYING,
        TaskStatus.REVIEW_PENDING,
        TaskStatus.REVIEWING,
        TaskStatus.APPROVED,
        TaskStatus.COMPLETE,
    ]
    for current, nxt in zip(path, path[1:], strict=False):
        assert can_transition(current, nxt), f"{current} -> {nxt} should be allowed"


def test_fix_loop_returns_changes_requested_to_coding():
    assert can_transition(TaskStatus.REVIEWING, TaskStatus.CHANGES_REQUESTED)
    assert can_transition(TaskStatus.CHANGES_REQUESTED, TaskStatus.CODING)


def test_failed_verification_routes_back_to_coder_not_to_review():
    assert can_transition(TaskStatus.VERIFYING, TaskStatus.CODING)
    assert not can_transition(TaskStatus.CODING, TaskStatus.REVIEW_PENDING)


def test_complete_is_terminal():
    assert is_terminal(TaskStatus.COMPLETE)
    assert ALLOWED_TRANSITIONS[TaskStatus.COMPLETE] == frozenset()
    for status in TaskStatus:
        assert not can_transition(TaskStatus.COMPLETE, status)


def test_coding_cannot_jump_straight_to_complete():
    with pytest.raises(InvalidStateTransition) as exc:
        assert_transition(TaskStatus.CODING, TaskStatus.COMPLETE)
    assert exc.value.current is TaskStatus.CODING
    assert exc.value.requested is TaskStatus.COMPLETE


def test_every_state_except_complete_can_be_left():
    for status, targets in ALLOWED_TRANSITIONS.items():
        if status is TaskStatus.COMPLETE:
            continue
        assert targets, f"{status} is a dead end"


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)


def test_active_states_are_the_in_flight_ones():
    assert is_active(TaskStatus.CODING)
    assert not is_active(TaskStatus.READY)
    assert not is_active(TaskStatus.COMPLETE)


def test_pause_resumes_only_at_safe_boundaries():
    resumable = ALLOWED_TRANSITIONS[TaskStatus.PAUSED]
    assert TaskStatus.READY in resumable
    # Resuming directly into an in-flight state would assume a worker survived.
    assert TaskStatus.CODING not in resumable
    assert TaskStatus.REVIEWING not in resumable
