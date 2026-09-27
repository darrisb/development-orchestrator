"""The code-edit contract and the completion report (phase G items 4 and 5).

Parsing is where a local model's answer meets the orchestrator, so these tests
are mostly about answers that are nearly right: a delete carrying content, one
path edited twice, an operation spelled with a capital letter. Each has one
unambiguous reading or none, and the difference decides whether the attempt is
salvaged or reported.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.completion import (
    RejectedEdit,
    build_completion_report,
)
from apps.orchestrator.domain.edits import (
    EDIT_SIZE_HEADROOM,
    MAX_EDIT_BYTES,
    CodeChangeSet,
    EditOperation,
    MalformedChangeSet,
    max_edit_bytes_for_context,
)
from apps.orchestrator.domain.enums import ScopePolicyDecision
from apps.orchestrator.domain.git import DiffSummary, FileChange
from apps.orchestrator.domain.scope import ScopePolicy, evaluate_scope
from apps.orchestrator.domain.tokens import characters_for_tokens


def _payload(**overrides) -> dict:
    payload: dict = {
        "summary": "Rendered the navigation tree.",
        "edits": [
            {
                "path": "src/navigation.ts",
                "operation": "update",
                "content": "export const navigation = () => 1;\n",
            }
        ],
        "testsAdded": ["tests/navigation.test.ts"],
    }
    payload.update(overrides)
    return payload


# --- parsing -----------------------------------------------------------------


def test_a_well_formed_answer_becomes_a_change_set():
    change_set = CodeChangeSet.from_payload(_payload())

    assert change_set.paths == ("src/navigation.ts",)
    assert change_set.edits[0].operation is EditOperation.UPDATE
    assert change_set.tests_added == ("tests/navigation.test.ts",)
    assert change_set.warnings == ()


def test_an_operation_name_is_read_case_insensitively():
    change_set = CodeChangeSet.from_payload(
        _payload(edits=[{"path": "a.ts", "operation": "CREATE", "content": "x\n"}])
    )

    assert change_set.edits[0].operation is EditOperation.CREATE


def test_a_delete_does_not_need_content_and_any_content_is_ignored():
    change_set = CodeChangeSet.from_payload(
        _payload(edits=[{"path": "a.ts", "operation": "delete", "content": "stale"}])
    )

    assert change_set.edits[0].content is None
    assert "content sent with a delete was ignored" in change_set.warnings[0]


def test_the_first_of_two_edits_to_one_path_wins_and_the_second_is_reported():
    """There is no safe merge of two answers about one file, and silently
    keeping the later one would hide that the model changed its mind."""
    change_set = CodeChangeSet.from_payload(
        _payload(
            edits=[
                {"path": "a.ts", "operation": "update", "content": "first\n"},
                {"path": "a.ts", "operation": "update", "content": "second\n"},
            ]
        )
    )

    assert change_set.edits[0].content == "first\n"
    assert len(change_set.edits) == 1
    assert "edited more than once" in change_set.warnings[0]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"operation": "update", "content": "x"}, "no path"),
        ({"path": "/etc/passwd", "operation": "update", "content": "x"}, "repository-relative"),
        ({"path": "a.ts", "content": "x"}, "no operation"),
        ({"path": "a.ts", "operation": "rewrite", "content": "x"}, "is not one of"),
        ({"path": "a.ts", "operation": "update"}, "needs content"),
    ],
)
def test_an_unusable_edit_is_dropped_with_the_reason(entry: dict, expected: str):
    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload({"summary": "s", "edits": [entry]})

    assert expected in str(error.value)


def test_an_oversized_file_is_refused_rather_than_written():
    huge = "x" * (MAX_EDIT_BYTES + 1)

    with pytest.raises(MalformedChangeSet) as error:
        CodeChangeSet.from_payload(
            {"summary": "s", "edits": [{"path": "a.ts", "operation": "create", "content": huge}]}
        )

    assert "over the" in str(error.value)


def test_an_answer_with_no_edits_at_all_is_malformed():
    with pytest.raises(MalformedChangeSet):
        CodeChangeSet.from_payload({"summary": "I decided not to change anything", "edits": []})


def test_edits_that_are_not_a_list_are_malformed():
    with pytest.raises(MalformedChangeSet):
        CodeChangeSet.from_payload({"summary": "s", "edits": "src/a.ts"})


def test_an_edit_is_described_without_its_content():
    """A file's worth of source has no business in an event payload."""
    described = CodeChangeSet.from_payload(_payload()).edits[0].describe()

    assert set(described) == {"path", "operation", "size_bytes"}


# --- completion report -------------------------------------------------------


def _report(*, applied: tuple[str, ...], claimed_tests: tuple[str, ...] = (), rejected=()):
    change_set = CodeChangeSet.from_payload(
        _payload(testsAdded=list(claimed_tests), requirementsMet=["renders each node"])
    )
    scope = evaluate_scope(
        DiffSummary(
            tuple(FileChange(path=path, insertions=5, deletions=1) for path in applied)
        ),
        ScopePolicy(max_files_changed=10, max_diff_lines=100),
    )
    return build_completion_report(
        external_task_id="TS-004",
        attempt=1,
        change_set=change_set,
        applied_paths=applied,
        deleted_paths=(),
        rejected_edits=rejected,
        scope=scope,
        planned=True,
        planned_paths=applied,
    )


def test_the_report_keeps_the_claim_and_the_measurement_apart():
    report = _report(applied=("src/navigation.ts",), claimed_tests=("tests/nav.test.ts",))
    described = report.describe()

    assert described["claimed"]["tests"] == ["tests/nav.test.ts"]
    assert described["measured"]["applied_paths"] == ["src/navigation.ts"]


def test_a_test_the_coder_claims_but_never_wrote_is_a_discrepancy():
    report = _report(applied=("src/navigation.ts",), claimed_tests=("tests/nav.test.ts",))

    assert any("tests/nav.test.ts" in entry for entry in report.discrepancies)
    # Still usable: whether the code works is the verifier's answer, not this
    # report's. The discrepancy travels with the candidate to the reviewer.
    assert report.usable


def test_a_claimed_test_that_was_actually_written_is_not_a_discrepancy():
    report = _report(
        applied=("src/navigation.ts", "tests/nav.test.ts"),
        claimed_tests=("tests/nav.test.ts",),
    )

    assert report.discrepancies == ()
    assert report.tests_written == ("tests/nav.test.ts",)


def test_a_file_outside_the_approved_plan_is_measured_as_a_discrepancy():
    report = build_completion_report(
        external_task_id="TS-004",
        attempt=1,
        change_set=CodeChangeSet.from_payload(_payload()),
        applied_paths=("src/navigation.ts", "src/extra.ts"),
        deleted_paths=(),
        rejected_edits=(),
        scope=evaluate_scope(
            DiffSummary(
                (
                    FileChange(path="src/navigation.ts", insertions=1),
                    FileChange(path="src/extra.ts", insertions=1),
                )
            ),
            ScopePolicy(max_files_changed=10, max_diff_lines=100),
        ),
        planned=True,
        planned_paths=("src/navigation.ts",),
    )

    assert report.unplanned_paths == ("src/extra.ts",)
    assert any("outside the approved plan" in entry for entry in report.discrepancies)


def test_a_refused_edit_is_a_discrepancy_because_it_is_not_in_the_candidate():
    report = _report(
        applied=("src/navigation.ts",),
        rejected=(RejectedEdit(path=".env", operation="update", reason="protected"),),
    )

    assert any("refused" in entry for entry in report.discrepancies)


def test_a_report_with_nothing_written_is_not_usable():
    report = _report(applied=())

    assert not report.usable
    assert report.scope_decision is ScopePolicyDecision.BLOCK


def test_the_report_renders_both_halves_for_the_reviewer():
    text = _report(applied=("src/navigation.ts",)).render()

    assert "Reported by the coder (unverified)" in text
    assert "Measured by the orchestrator" in text


# --- the two ceilings are one number (concern 11) ----------------------------


def test_the_edit_ceiling_is_derived_from_the_context_budget():
    """Concern 11: `CONTEXT_MAX_ITEM_TOKENS` bounds what a file looks like on
    the way in and the edit ceiling bounds what it may be on the way out. They
    describe the same file at two moments, so one is computed from the other
    through `domain.tokens`' single ratio rather than chosen separately.
    """
    budget = 2_000

    ceiling = max_edit_bytes_for_context(budget)

    assert ceiling == int(characters_for_tokens(budget) * EDIT_SIZE_HEADROOM)
    # Larger on the way out than in, because adding a guard clause makes a file
    # longer -- but by a stated factor, not by two orders of magnitude.
    assert characters_for_tokens(budget) < ceiling < characters_for_tokens(budget) * 2


def test_a_wider_context_budget_widens_the_edit_ceiling_with_it():
    assert max_edit_bytes_for_context(4_000) == 2 * max_edit_bytes_for_context(2_000)


def test_the_derived_ceiling_is_never_zero():
    """A budget too small to show anything must still parse an edit rather than
    reject every one of them for being over a ceiling of nothing."""
    assert max_edit_bytes_for_context(0) >= 1
