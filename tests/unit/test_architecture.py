"""Architecture decision records and project memory (build.md section 16)."""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.architecture import (
    DecisionStatus,
    parse_decision,
    select_decisions,
)

_ADR = """# ADR-002: Store run artifacts on disk

Status: Accepted
Date: 2026-02-14

## Context
Run logs and diffs are large.

## Decision
Write artifacts to ARTIFACT_ROOT and keep only a path and a hash in the database.

## Consequences
The database stays small; the artifact root must be backed up separately.
"""


def test_a_well_formed_record_parses_into_its_documented_parts():
    decision = parse_decision(_ADR, path=".ai/decisions/ADR-002.md")

    assert decision.identifier == "ADR-002"
    assert decision.title == "Store run artifacts on disk"
    assert decision.status is DecisionStatus.ACCEPTED
    assert decision.date == "2026-02-14"
    assert decision.decision.startswith("Write artifacts to ARTIFACT_ROOT")
    assert decision.consequences.startswith("The database stays small")
    assert decision.warnings == ()
    assert decision.is_binding


@pytest.mark.parametrize(
    ("status", "expected", "binding"),
    [
        ("Accepted", DecisionStatus.ACCEPTED, True),
        ("proposed", DecisionStatus.PROPOSED, True),
        ("Superseded by ADR-007", DecisionStatus.SUPERSEDED, False),
        ("Deprecated", DecisionStatus.DEPRECATED, False),
        ("Rejected", DecisionStatus.REJECTED, False),
    ],
)
def test_status_decides_whether_a_decision_still_binds(status, expected, binding):
    decision = parse_decision(f"# ADR-001: T\n\nStatus: {status}\n", path="a.md")

    assert decision.status is expected
    assert decision.is_binding is binding


def test_an_unparseable_record_is_reported_not_raised():
    """A malformed ADR must not stop a task: it is a human note, not a contract."""
    decision = parse_decision("Some notes nobody formatted.", path=".ai/decisions/notes.md")

    assert decision.identifier == "notes"
    assert decision.status is DecisionStatus.UNKNOWN
    assert decision.warnings
    # Unknown status still binds: a record nobody retired is probably live.
    assert decision.is_binding


def test_a_retired_decision_is_never_selected():
    live = parse_decision("# ADR-001: Use a tree\n\nStatus: Accepted\n", path="a.md")
    retired = parse_decision("# ADR-002: Use a list\n\nStatus: Superseded\n", path="b.md")

    assert select_decisions([live, retired], {"tree", "list"}) == [live]


def test_selection_prefers_relevance_and_breaks_ties_stably():
    tree = parse_decision("# ADR-001: Navigation tree layout\n\nStatus: Accepted\n", path="a.md")
    other = parse_decision("# ADR-002: Payment retries\n\nStatus: Accepted\n", path="b.md")

    assert select_decisions([other, tree], {"navigation", "tree"}) == [tree]
    assert select_decisions([tree, other], {"unrelated"}) == []
    assert select_decisions([other, tree], {"unrelated"}, include_unmatched=True) == [tree, other]


def test_a_small_decision_set_can_be_sent_whole():
    decisions = [
        parse_decision(f"# ADR-00{index}: Choice {index}\n\nStatus: Accepted\n", path=f"{index}.md")
        for index in (1, 2)
    ]

    assert select_decisions(decisions, {"nothing"}, limit=5, include_unmatched=True) == decisions
    assert len(select_decisions(decisions, {"nothing"}, limit=1, include_unmatched=True)) == 1


def test_the_summary_form_keeps_the_decision_and_its_consequences():
    summary = parse_decision(_ADR, path="a.md").summary()

    assert "Status: ACCEPTED" in summary
    assert "ARTIFACT_ROOT" in summary
    assert "backed up separately" in summary
