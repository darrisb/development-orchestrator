"""Closing review findings across cycles (build.md section 23, phase J).

Concern 27: ``mark_issue_resolved`` existed and nothing called it, so the open
list only grew and by the third cycle the reviewer was reading every finding
ever raised against the run. The rule these tests pin down is who gets to close
one: a reviewer that was shown a finding and did not raise it again -- never the
coder's claim to have fixed it.
"""

from __future__ import annotations

from apps.orchestrator.domain.enums import IssueCategory, IssueSeverity
from apps.orchestrator.domain.models import ReviewIssue
from apps.orchestrator.domain.review import (
    issue_fingerprint,
    reraised_unresolved_blocking_issues,
    unreraised_issues,
)


def issue(**overrides) -> ReviewIssue:
    fields: dict[str, object] = {
        "severity": IssueSeverity.HIGH,
        "category": IssueCategory.REQUIREMENT,
        "problem": "Saved selection is not restored.",
        "required_fix": "Restore the stored selection after opening the document.",
        "file": "src/navigation.ts",
        "line": 4,
        "requirement_id": "TS-004-R7",
    }
    fields.update(overrides)
    return ReviewIssue(**fields)  # type: ignore[arg-type]


def test_a_finding_the_next_review_did_not_raise_is_resolved():
    earlier = [issue()]

    assert unreraised_issues(earlier, []) == tuple(earlier)


def test_a_finding_the_next_review_raised_again_stays_open():
    earlier = [issue()]
    # Same defect, described differently: a reviewer does not repeat its own
    # wording, which is why the prose is not part of the identity.
    again = [issue(problem="The selection is still dropped on open.", line=9)]

    assert unreraised_issues(earlier, again) == ()


def test_only_the_findings_that_were_dropped_are_resolved():
    fixed = issue(requirement_id="TS-004-R7")
    outstanding = issue(
        requirement_id="TS-004-R8",
        file="src/tree.ts",
        problem="The tree is not refreshed.",
    )

    resolved = unreraised_issues([fixed, outstanding], [outstanding])

    assert resolved == (fixed,)


def test_a_requirement_pursued_in_another_file_is_not_resolved():
    """The reviewer is still on the same requirement, having traced it
    somewhere else. Closing the original would lose the thread."""
    earlier = [issue(requirement_id="TS-004-R7", file="src/navigation.ts")]
    moved = [issue(requirement_id="TS-004-R7", file="src/provider.ts")]

    assert unreraised_issues(earlier, moved) == ()


def test_an_approval_resolves_every_open_finding():
    """An approving review raises nothing, so nothing is re-raised: the
    reviewer read the diff with the open list in front of it and accepted."""
    earlier = [issue(), issue(requirement_id="TS-004-R8", file="src/tree.ts")]

    assert unreraised_issues(earlier, []) == tuple(earlier)


def test_an_unrelated_new_finding_does_not_keep_an_old_one_open():
    earlier = [issue()]
    unrelated = [
        issue(
            requirement_id="",
            file="src/other.ts",
            category=IssueCategory.STYLE,
            problem="This helper is duplicated.",
        )
    ]

    assert unreraised_issues(earlier, unrelated) == tuple(earlier)


def test_the_fingerprint_ignores_wording_severity_and_line():
    """What a reviewer restates consistently is the requirement, the file and
    the category; everything else it recomposes each cycle."""
    first = issue()
    second = issue(
        severity=IssueSeverity.CRITICAL,
        line=91,
        problem="different words",
        required_fix="different fix",
    )

    assert issue_fingerprint(first) == issue_fingerprint(second)


def test_the_fingerprint_is_case_insensitive_about_paths_and_requirements():
    assert issue_fingerprint(issue(file="SRC/Navigation.ts")) == issue_fingerprint(
        issue(file="src/navigation.ts")
    )
    assert issue_fingerprint(issue(requirement_id=" ts-004-r7 ")) == issue_fingerprint(
        issue(requirement_id="TS-004-R7")
    )


def test_a_reraised_unresolved_blocking_finding_is_identified():
    earlier = [issue()]
    again = [issue(problem="The selection is still dropped on open.", line=9)]

    assert reraised_unresolved_blocking_issues(earlier, again) == tuple(earlier)


def test_a_genuinely_different_blocking_finding_is_not_a_reraised_dispute():
    earlier = [issue()]
    different = [
        issue(
            requirement_id="TS-004-R8",
            file="src/tree.ts",
            problem="The tree is not refreshed.",
        )
    ]

    assert reraised_unresolved_blocking_issues(earlier, different) == ()


def test_a_shared_requirement_with_a_different_fingerprint_is_not_a_dispute():
    earlier = [issue(requirement_id="TS-004-R7", file="src/navigation.ts")]
    moved = [issue(requirement_id="TS-004-R7", file="src/provider.ts")]

    assert reraised_unresolved_blocking_issues(earlier, moved) == ()


def test_a_disappeared_finding_is_not_a_reraised_dispute():
    earlier = [issue()]

    assert reraised_unresolved_blocking_issues(earlier, []) == ()


def test_reraised_low_or_info_findings_do_not_trigger_blocking_dispute():
    low = issue(severity=IssueSeverity.LOW)
    info = issue(severity=IssueSeverity.INFO, requirement_id="TS-004-R8")

    assert reraised_unresolved_blocking_issues([low], [low]) == ()
    assert reraised_unresolved_blocking_issues([info], [info]) == ()
