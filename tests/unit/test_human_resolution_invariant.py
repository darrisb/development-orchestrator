"""The human conflict-resolution content invariant, as a rule rather than a run.

``check_resolution_content`` is the part of the concern 73 follow-up that has
to be right for its own sake: it is what stands between "an operator produced a
plausible tree" and "an operator produced the human commit's work, carried onto
the current baseline". It takes four tree reads and no operator input, so it can
be pinned here without a repository, and these tests are the specification of
exactly what equivalence it enforces -- and, in the last group, exactly what it
does not claim.
"""

from __future__ import annotations

from collections import Counter

from apps.orchestrator.services.human_resolution import (
    FULL_COMMIT_SHA,
    check_resolution_content,
    significant_lines,
)


def _facts(
    *,
    human_added: dict[str, list[str]],
    baseline: dict[str, str | None],
    resolution: dict[str, str | None],
    human_removed: dict[str, Counter[str]] | None = None,
) -> dict:
    return {
        "allowed_paths": tuple(human_added),
        "human_added": human_added,
        "baseline_texts": baseline,
        "resolution_texts": resolution,
        "human_removed": human_removed or {},
    }


# ------------------------------------------------------------- normalisation


def test_significant_lines_drops_whitespace_and_blanks():
    assert significant_lines("  a  \n\n\tb\t\n   \n") == Counter({"a": 1, "b": 1})


def test_significant_lines_counts_repeats():
    assert significant_lines("x\nx\ny") == Counter({"x": 2, "y": 1})


def test_absent_file_is_an_empty_multiset_not_an_error():
    """``None`` means "this file is not in that tree", which is an answer.

    A resolution that deletes a file the human edited and a resolution that
    never had it look identical to a function that crashed on ``None``, so the
    distinction has to survive all the way to a verdict.
    """
    assert significant_lines(None) == Counter()


# ------------------------------------------------------------------ accepted


def test_exact_carry_over_is_accepted():
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["added();"]},
            baseline={"f.ts": "kept();\n"},
            resolution={"f.ts": "kept();\nadded();\n"},
        )
    )
    assert verdict.accepted
    assert verdict.violations == ()
    assert verdict.carried_paths == ("f.ts",)


def test_relocated_carry_over_is_accepted():
    """The real case: conflict resolution legitimately moves code.

    The human appended after ``clear()``; the baseline had appended its own
    methods there, so the correct resolution puts the human's method elsewhere
    in the class. Placement is exactly what a resolution is allowed to change.
    """
    baseline = "class C {\n  clear() {}\n  find() {}\n}"
    resolution = "class C {\n  clear() {}\n  find() {}\n  added() {}\n}"
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["  added() {}", "  }"]},
            baseline={"f.ts": baseline},
            resolution={"f.ts": resolution},
        )
    )
    assert verdict.accepted


def test_indentation_changes_are_accepted():
    """Re-indentation is formatting, not authorship.

    Resolution rules and generators routinely reindent the code around a
    change, so the human's line surviving with different leading whitespace is
    the same claim being made in a different style.
    """
    baseline = "function f() {\n  body();\n}"
    human_added = ["  return this.entries;"]
    resolution = "function f() {\n  body();\n        return this.entries;\n}"
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": human_added},
            baseline={"f.ts": baseline},
            resolution={"f.ts": resolution},
        )
    )
    assert verdict.accepted


def test_human_removing_a_line_permits_the_resolution_to_remove_it():
    """The human replaced an import; the resolution must be allowed to as well."""
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["import { A, B } from 'm';"]},
            baseline={"f.ts": "import { A } from 'm';\nother();\n"},
            resolution={"f.ts": "import { A, B } from 'm';\nother();\n"},
            human_removed={"f.ts": Counter({"import { A } from 'm';": 1})},
        )
    )
    assert verdict.accepted


# ----------------------------------------------------------------- rejected


def test_omitting_part_of_the_human_implementation_is_rejected():
    """Phase 7 case 3, and the single most important negative.

    A resolution that carries most of the human's work is not the human's
    work. The check is per line and counted, so losing one line of a method
    body is enough to fail -- the tree would not typecheck, but the invariant
    has to be the thing that says so rather than the compiler.
    """
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["added();", "second();"]},
            baseline={"f.ts": "kept();\n"},
            resolution={"f.ts": "kept();\nadded();\n"},
        )
    )
    assert not verdict.accepted
    assert "omits 1 line(s) the human commit added" in verdict.violations[0]
    assert "second();" in verdict.violations[0]


def test_omitting_the_whole_human_change_is_rejected():
    """The Phase-2 fake resolution: keep the baseline, declare it merged.

    Ancestry is satisfied by a merge commit, so without the content invariant
    this passes every other gate in the contract and produces a baseline that
    claims to contain TS-109 and does not.
    """
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["filterBySource(source) { return entries; }"]},
            baseline={"f.ts": "class C { find() {} }"},
            resolution={"f.ts": "class C { find() {} }"},
        )
    )
    assert not verdict.accepted
    assert "omits 1 line(s)" in verdict.violations[0]


def test_dropping_a_test_the_human_wrote_is_rejected():
    """Phase 3 item 4 asks for the tests, not only the implementation."""
    verdict = check_resolution_content(
        **_facts(
            human_added={"t.ts": ["it('filters by source', () => {});"]},
            baseline={"t.ts": "describe('stack', () => {});"},
            resolution={"t.ts": "describe('stack', () => {});"},
        )
    )
    assert not verdict.accepted


def test_injecting_an_unrelated_line_is_rejected():
    """Phase 7 case 4: smuggling. The file is in scope, the line is not."""
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["added();"]},
            baseline={"f.ts": "kept();\n"},
            resolution={"f.ts": "kept();\nadded();\nbackdoor();\n"},
        )
    )
    assert not verdict.accepted
    assert "adds 1 line(s) the human commit did not add" in verdict.violations[0]
    assert "backdoor();" in verdict.violations[0]


def test_deleting_accepted_baseline_work_is_rejected():
    """Accepted TS-101..TS-108 work cannot be removed under cover of a merge."""
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["added();"]},
            baseline={"f.ts": "kept();\nacceptedTs108Work();\n"},
            resolution={"f.ts": "kept();\nadded();\n"},
        )
    )
    assert not verdict.accepted
    assert "removes 1 baseline line(s) the human commit kept" in verdict.violations[0]


def test_deleting_a_file_the_human_edited_is_rejected():
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["added();"]},
            baseline={"f.ts": "kept();\n"},
            resolution={"f.ts": None},
        )
    )
    assert not verdict.accepted


def test_every_violation_is_reported_not_just_the_first():
    """An operator fixing a resolution needs the whole list, not one line of it.

    Three violations across two files: ``a.ts`` dropped a line, ``b.ts`` dropped
    one and injected another. Reporting only the first would send an operator
    round the loop once per mistake.
    """
    verdict = check_resolution_content(
        **_facts(
            human_added={"a.ts": ["a1();"], "b.ts": ["b1();"]},
            baseline={"a.ts": "x();\n", "b.ts": "y();\n"},
            resolution={"a.ts": "x();\n", "b.ts": "y();\nB1();\n"},
        )
    )
    assert not verdict.accepted
    assert len(verdict.violations) == 3
    assert {v.split(":")[0] for v in verdict.violations} == {"a.ts", "b.ts"}
    assert any("omits" in v for v in verdict.violations)
    assert any("did not add" in v for v in verdict.violations)


def test_describe_is_serialisable_for_the_event_payload():
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["added();"]},
            baseline={"f.ts": "kept();\n"},
            resolution={"f.ts": "kept();\nadded();\n"},
        )
    )
    described = verdict.describe()
    assert described["accepted"] is True
    assert described["allowed_paths"] == ["f.ts"]
    assert described["violations"] == []


# --------------------------------------------------------------- sha shape


def test_full_sha_accepts_only_unabbreviated_lowercase_hex():
    assert FULL_COMMIT_SHA.match("cbff2c4bd919b860c73e3cb061bccff11789c37a")
    assert not FULL_COMMIT_SHA.match("cbff2c4")
    assert not FULL_COMMIT_SHA.match("CBFF2C4BD919B860C73E3CB061BCCFF11789C37A")
    assert not FULL_COMMIT_SHA.match("master")
    assert not FULL_COMMIT_SHA.match("master~1")
    assert not FULL_COMMIT_SHA.match("")
    assert not FULL_COMMIT_SHA.match("cbff2c4bd919b860c73e3cb061bccff11789c37a\n")
    assert not FULL_COMMIT_SHA.match("cbff2c4bd919b860c73e3cb061bccff11789c37a ")


# ------------------------------------------------- what it does not claim


def test_a_semantically_equivalent_rewrite_is_still_rejected():
    """The stated limit of the invariant, pinned so it cannot drift silently.

    An operator who retyped the human's method into different text but the same
    behaviour is refused. That is a false negative, and it is accepted
    deliberately: the alternative is trusting an assertion about equivalence,
    which is exactly what this contract exists to stop doing. Behaviour is
    covered by the project's own tests, which run over the resolved tree before
    anything is recorded.
    """
    verdict = check_resolution_content(
        **_facts(
            human_added={"f.ts": ["filterBySource(s) { return e; }"]},
            baseline={"f.ts": "class C {}"},
            resolution={"f.ts": "class C { filterBySource(x) { return all; } }"},
        )
    )
    assert not verdict.accepted
    assert "omits 1 line(s)" in verdict.violations[0]
