"""The reviewer's bounded package (build.md section 21).

Section 21's list of what goes in is easy; the sentence after it is the hard
part -- *reviewer should not receive unrelated repository contents* -- and so
is what happens when all of it does not fit. These tests are about the second
question: what is shed, in what order, and whether the reviewer is ever
allowed to believe it saw a whole change when it did not.
"""

from __future__ import annotations

from apps.orchestrator.domain.enums import IssueCategory, IssueSeverity
from apps.orchestrator.domain.models import ReviewIssue
from apps.orchestrator.domain.review_package import (
    ReviewBudget,
    ReviewSource,
    assemble_review_package,
)

DIFF = "\n".join(
    f"+    line {index} of the candidate change" for index in range(1, 40)
)


def source(path: str, lines: int = 20) -> ReviewSource:
    return ReviewSource(
        path=path,
        content="\n".join(f"const value{index} = {index};" for index in range(lines)),
        reason="imported by the changed file",
        language="typescript",
    )


def package(**overrides):
    arguments = {
        "external_task_id": "TS-004",
        "attempt": 1,
        "cycle": 1,
        "task_specification": "Task: TS-004 — restore the saved selection",
        "starting_commit": "abc123",
        "diff_text": DIFF,
        "changed_paths": ("src/nav.ts",),
        "files_changed": 1,
        "diff_lines": 39,
    }
    arguments.update(overrides)
    return assemble_review_package(**arguments)


def test_a_whole_package_reports_itself_complete():
    built = package(
        verification="Outcome: verification passed",
        completion_report="# Completion report",
        decisions=(source(".ai/decisions/adr-001.md"),),
        sources=(source("src/types.ts"),),
        lessons=("[testing] always assert the error message",),
    )

    assert built.complete
    assert "TS-004" in built.render()
    assert "abc123" in built.render()
    assert "verification passed" in built.render()
    assert "adr-001.md" in built.render()
    assert "always assert the error message" in built.render()
    assert not built.omitted


def test_the_diff_is_the_subject_and_is_labelled_as_such():
    rendered = package().render()

    assert "## Candidate change" in rendered
    assert "```diff" in rendered
    assert "line 1 of the candidate change" in rendered


def test_verification_results_are_marked_as_facts_and_the_report_as_claims():
    """A reviewer that cannot tell them apart will either re-litigate passing
    tests or believe a claim the diff does not support."""
    rendered = package(
        verification="Outcome: verification passed",
        completion_report="I added tests/test_nav.ts",
    ).render()

    assert "executed by the orchestrator, not by the coder" in rendered
    assert "Read this against the diff" in rendered


def test_lessons_are_shed_before_source_and_source_before_decisions():
    built = package(
        decisions=(source(".ai/decisions/adr-001.md", lines=40),),
        sources=(source("src/types.ts", lines=40),),
        lessons=("a lesson",),
        budget=ReviewBudget(max_tokens=700, min_diff_tokens=100),
    )

    assert not built.lessons
    assert not built.sources
    assert any("lesson" in note for note in built.omitted)
    assert any("supporting file" in note for note in built.omitted)
    # The diff itself survived.
    assert "line 1 of the candidate change" in built.render()


def test_the_diff_is_clipped_only_after_everything_else_is_gone():
    built = package(
        decisions=(source(".ai/decisions/adr-001.md", lines=40),),
        sources=(source("src/types.ts", lines=40),),
        lessons=("a lesson",),
        budget=ReviewBudget(max_tokens=200, min_diff_tokens=30),
    )

    assert built.diff_truncated
    assert not built.complete
    assert not built.decisions
    assert any("clipped" in note for note in built.omitted)


def test_a_clipped_package_tells_the_reviewer_not_to_approve_blind():
    rendered = package(budget=ReviewBudget(max_tokens=120, min_diff_tokens=20)).render()

    assert "Completeness warning" in rendered
    assert "HUMAN_REVIEW_REQUIRED" in rendered


def test_a_clip_that_happened_upstream_still_counts_against_completeness():
    """``DiffCapture`` has its own byte ceiling. A package must not report
    itself whole just because this function did not have to cut anything."""
    built = package(diff_already_truncated=True)

    assert built.diff_truncated
    assert not built.complete


def test_dropping_a_lesson_does_not_make_a_package_incomplete():
    """The change is still whole, which is the sense that matters."""
    built = package(
        lessons=tuple(f"lesson {index}" for index in range(200)),
        budget=ReviewBudget(max_tokens=600, min_diff_tokens=100),
    )

    assert built.complete
    assert built.omitted


def test_decisions_beyond_the_budgets_count_are_named_not_silently_cut():
    built = package(
        decisions=tuple(source(f".ai/decisions/adr-{index}.md", lines=2) for index in range(9)),
        budget=ReviewBudget(max_decisions=3),
    )

    assert len(built.decisions) == 3
    assert any("6 architecture decision(s)" in note for note in built.omitted)


def test_open_findings_from_earlier_cycles_are_shown_on_a_re_review():
    built = package(
        cycle=2,
        unresolved_issues=(
            ReviewIssue(
                severity=IssueSeverity.HIGH,
                category=IssueCategory.REQUIREMENT,
                problem="the saved selection is still not restored",
                required_fix="restore it",
                file="src/nav.ts",
                line=84,
            ),
        ),
    )
    rendered = built.render()

    assert "earlier review cycles" in rendered
    assert "still not restored" in rendered
    assert "src/nav.ts:84" in rendered


def test_the_hash_covers_exactly_what_the_reviewer_sees():
    first = package(lessons=("a lesson",))
    same = package(lessons=("a lesson",))
    different = package(lessons=("another lesson",))

    assert first.content_hash == same.content_hash
    assert first.content_hash != different.content_hash


def test_an_empty_diff_is_said_in_words_rather_than_rendered_as_nothing():
    assert "empty diff" in package(diff_text="").render()
