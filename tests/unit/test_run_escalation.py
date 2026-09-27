"""Escalations for a run no reviewer saw (build.md sections 24 and 25, phase J).

Section 24's requirement is a design brief, not a format: *the human should not
have to reconstruct the history manually.* These tests hold the renderer to the
parts of that sentence that can be checked -- every attempt is listed, the
blocker is named, the options are choices -- and to the one thing an escalation
must never get wrong, which is what it says about the repository.
"""

from __future__ import annotations

from apps.orchestrator.domain.enums import FailureReason
from apps.orchestrator.domain.escalation import (
    EscalationIntent,
    integration_escalation_options,
    render_integration_escalation,
    render_run_escalation,
    run_escalation_options,
)


def render(**overrides) -> str:
    fields: dict[str, object] = {
        "external_task_id": "TS-017",
        "reason": "3 of the task's 3 permitted attempts were made.",
        "requirement": "Capture native Go-to-Definition navigation.",
        "blocker": "tests failed: python tools/test.py (exit 1)",
        "attempts": (
            "attempt 1 (verification): tests failed",
            "attempt 2 (verification): tests failed",
            "attempt 3 (verification): tests failed",
        ),
        "options": run_escalation_options(FailureReason.RETRY_EXHAUSTED),
        "starting_commit": "abc123",
    }
    fields.update(overrides)
    return render_run_escalation(**fields)  # type: ignore[arg-type]


def test_the_escalation_carries_the_whole_history():
    text = render()

    assert text.startswith("TASK TS-017 — HUMAN REVIEW REQUIRED")
    for section in ("Reason:", "Requirement:", "Current blocker:", "Attempts:", "Options:"):
        assert section in text
    # Every attempt, in the order they happened.
    assert text.index("attempt 1") < text.index("attempt 2") < text.index("attempt 3")
    assert "tests failed: python tools/test.py (exit 1)" in text


def test_a_preserved_candidate_is_not_described_as_restored():
    """The section 24 example says "restored to", but a candidate held for a
    human decision has deliberately not been rolled back. Saying otherwise
    would tell a person their work is gone when it is not."""
    text = render(rolled_back=False)

    assert "abc123" in text
    assert "is uncommitted in the run's worktree" in text
    assert "no longer on disk" not in text


def test_a_rolled_back_candidate_says_so():
    text = render(rolled_back=True)

    assert "has been reset back to it" in text
    assert "no longer on disk" in text


def test_the_options_are_choices_not_a_question():
    for reason in (
        FailureReason.RETRY_EXHAUSTED,
        FailureReason.RUNTIME_EXHAUSTED,
        FailureReason.SCOPE_VIOLATION,
        FailureReason.HUMAN_DECISION_REQUIRED,
    ):
        options = run_escalation_options(reason)
        assert len(options) == 3
        assert [option[:2] for option in options] == ["A.", "B.", "C."]
        assert not any(option.endswith("?") for option in options)


def test_accepting_a_rejected_candidate_is_not_offered():
    """A change that broke its scope or failed the security scan is not one a
    person should be invited to wave through from an escalation."""
    for reason in (FailureReason.SCOPE_VIOLATION, FailureReason.SECURITY_FAILED):
        options = " ".join(run_escalation_options(reason)).casefold()
        assert "accept the candidate" not in options


def test_a_task_with_no_instructions_still_renders():
    text = render(requirement="")

    assert "(the task carries no instructions)" in text


def test_a_run_with_no_attempts_is_still_a_readable_escalation():
    text = render(attempts=(), starting_commit=None)

    assert "Attempts: none" in text
    assert "Current repository:" not in text


def test_a_blocked_integration_offers_only_what_can_be_done_deterministically():
    """Concern 51: one option, and it is a gate rather than a judgement.

    Retrying the task would discard a reviewed candidate and failing it would
    call delivered work failed, so neither is offered. What is offered is the
    same merge and the same cumulative verification, run again over whatever a
    person resolved.
    """
    options = integration_escalation_options()
    assert [option.intent for option in options] == [EscalationIntent.RETRY_INTEGRATION]
    assert options[0].key == "A"


def test_the_integration_escalation_page_names_the_tasks_it_is_holding_up():
    """Section 24: nobody should have to reconstruct the consequence either.

    The commit, the baseline it would not join, what the verifier said and which
    tasks are now waiting -- the last one is what an operator would otherwise
    discover by wondering why nothing is being picked up.
    """
    page = render_integration_escalation(
        external_task_id="PIPE-01",
        branch="agent/PIPE-01-add-a-step",
        candidate_commit="cafe1234",
        baseline_sha="beef5678",
        integration_branch="agent/integration",
        blocker="the merged tree failed the project's own verification",
        failed_commands=("pytest -q",),
        dependents=("PIPE-02", "PIPE-07"),
        options=integration_escalation_options(),
    )
    assert "TASK PIPE-01 — INTEGRATION BLOCKED" in page
    assert "cafe1234" in page and "beef5678" in page
    assert "pytest -q" in page
    assert "PIPE-02" in page and "PIPE-07" in page
    assert "re-attempt the integration" in page
    # And it says what is true of the repository: nothing was rolled back.
    assert "nothing has been rolled back" in page
