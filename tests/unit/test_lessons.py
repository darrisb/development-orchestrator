"""The pure lesson logic (build.md sections 32 and 33).

One test per rule, because the six rules are the specification and a test that
does not name which one it is protecting stops saying anything when it fails.
"""

from __future__ import annotations

from uuid import uuid4

from apps.orchestrator.domain.enums import (
    IssueCategory,
    IssueSeverity,
    LessonConfidence,
    LessonStatus,
)
from apps.orchestrator.domain.lessons import (
    DEFAULT_RETRIEVAL_LIMIT,
    LessonCandidate,
    check_approval,
    extract_candidates,
    is_lesson_candidate,
    lesson_confidence,
    lesson_keywords,
    rank_lessons,
    retrieval_prompt_lines,
)
from apps.orchestrator.domain.models import Lesson, ReviewIssue

# --- fixtures ---------------------------------------------------------------


def _issue(
    *,
    severity: IssueSeverity = IssueSeverity.HIGH,
    category: IssueCategory = IssueCategory.TESTING,
    problem: str = "The navigation test asserts on internals, not rendered output.",
    required_fix: str = "Assert on the rendered output of the navigation component.",
    file: str | None = "src/components/navigation.tsx",
    line: int | None = 42,
    requirement_id: str | None = "REQ-3",
) -> ReviewIssue:
    return ReviewIssue(
        severity=severity,
        category=category,
        problem=problem,
        required_fix=required_fix,
        file=file,
        line=line,
        requirement_id=requirement_id,
        resolved=True,
    )


def _lesson(**overrides) -> Lesson:
    defaults = {
        "project_id": uuid4(),
        "language": "typescript",
        "framework": "react",
        "category": "testing",
        "title": "Assert on rendered output",
        "lesson": "Assert on the rendered output of a component, not its internals.",
        "status": LessonStatus.APPROVED,
        "confidence": LessonConfidence.MEDIUM,
        "occurrences": 1,
        "tags": ["category:testing"],
    }
    defaults.update(overrides)
    return Lesson(**defaults)


# --- rule 1: not every review comment becomes a lesson ----------------------


def test_an_info_finding_is_never_a_lesson():
    """INFO does not force a retry, so it never demonstrated a fix."""
    eligible, reason = is_lesson_candidate(_issue(severity=IssueSeverity.INFO))

    assert not eligible
    assert "do not force a retry" in reason


def test_a_low_severity_finding_is_never_a_lesson():
    eligible, reason = is_lesson_candidate(_issue(severity=IssueSeverity.LOW))

    assert not eligible
    assert reason


def test_a_style_finding_is_never_a_lesson():
    """Rule 1's named failure: a lesson list full of "prefer named exports"."""
    eligible, reason = is_lesson_candidate(_issue(category=IssueCategory.STYLE))

    assert not eligible
    assert "not engineering instructions" in reason


def test_a_finding_with_no_required_fix_is_never_a_lesson():
    """Nothing to generalise from: there is no instruction in it."""
    eligible, reason = is_lesson_candidate(_issue(required_fix="   "))

    assert not eligible
    assert "no required fix" in reason


def test_a_verified_finding_does_become_a_lesson():
    eligible, reason = is_lesson_candidate(_issue())

    assert eligible
    assert reason == ""


def test_a_cycle_with_nothing_eligible_proposes_nothing():
    assert extract_candidates([_issue(severity=IssueSeverity.INFO)]) == ()


def test_what_was_considered_is_reported_even_when_nothing_was_proposed():
    """A silent filter is indistinguishable from one that dropped the wrong things."""
    candidates = extract_candidates([_issue(category=IssueCategory.STYLE)])

    assert candidates == ()


def test_several_findings_about_one_thing_become_one_lesson():
    """Three instructions where one says it is less noise than three near-duplicates."""
    issues = [
        _issue(required_fix="Assert on the rendered output."),
        _issue(required_fix="Give the route a name."),
        _issue(required_fix="Handle the empty state."),
    ]

    candidates = extract_candidates(issues)

    assert len(candidates) == 1
    assert len(candidates[0].source_issue_ids) == 3


def test_findings_about_different_categories_stay_separate():
    issues = [_issue(), _issue(category=IssueCategory.SECURITY)]

    candidates = extract_candidates(issues)

    assert len(candidates) == 2
    assert {candidate.category for candidate in candidates} == {"testing", "security"}


def test_the_lesson_text_is_the_reviewers_own_instruction():
    """A paraphrase would be a second model's claim about what the reviewer meant."""
    candidates = extract_candidates(
        [_issue(required_fix="Assert on the rendered output of the component.")]
    )

    assert candidates[0].lesson.startswith("Assert on the rendered output")


def test_a_defect_description_becomes_an_instruction():
    """Reviewers write "the guard is missing"; that is a description, not a rule."""
    candidates = extract_candidates(
        [_issue(required_fix="The null check in the loader is missing.")]
    )

    # Not "Add the The null check..." -- the article is carried across, not
    # re-prepended.
    assert candidates[0].lesson.startswith("Add the null check in the loader.")


def test_a_lesson_cites_where_it_was_raised():
    """Rule 3: the rendered text names its source, not just the row."""
    lesson = extract_candidates([_issue()])[0].lesson

    assert "Raised against src/components/navigation.tsx:42." in lesson


# --- rule 2: prefer recurring and generalizable -----------------------------


def test_confidence_comes_from_how_often_a_finding_has_been_raised():
    assert lesson_confidence(1) is LessonConfidence.LOW
    assert lesson_confidence(2) is LessonConfidence.MEDIUM
    assert lesson_confidence(3) is LessonConfidence.HIGH
    assert lesson_confidence(9) is LessonConfidence.HIGH


def test_a_recurring_finding_is_graded_higher_than_a_one_off():
    once = extract_candidates([_issue()], occurrences=1)
    thrice = extract_candidates([_issue()], occurrences=3)

    assert once[0].confidence is LessonConfidence.LOW
    assert thrice[0].confidence is LessonConfidence.HIGH


def test_recurrence_ranks_a_lesson_above_a_more_similar_one_off():
    """Rule 2 as a ranking input, not only as a label."""
    relevant = _lesson(title="Navigation test assertions", occurrences=1)
    recurring = _lesson(
        title="Unrelated database advice",
        lesson="Migrations must be reversible.",
        occurrences=4,
        confidence=LessonConfidence.HIGH,
    )

    ranked = rank_lessons([relevant, recurring], keywords=["navigation", "assertions"])

    assert ranked[0].lesson.id == relevant.id


def test_an_unlocated_finding_is_flagged_as_generalising_weakly():
    candidates = extract_candidates([_issue(file=None, line=None)])

    assert "generalises weakly" in candidates[0].rationale


def test_a_located_finding_is_not_flagged():
    assert "generalises weakly" not in extract_candidates([_issue()])[0].rationale


# --- rule 3: traceability ----------------------------------------------------


def test_a_candidate_names_the_findings_it_came_from():
    issue = _issue()

    candidate = extract_candidates([issue])[0]

    assert candidate.source_issue_ids == (str(issue.id),)


def test_a_candidate_names_the_run_and_task_it_came_from():
    task_id, run_id = str(uuid4()), str(uuid4())

    candidate = extract_candidates([_issue()], source_task_id=task_id, source_run_id=run_id)[0]

    assert candidate.source_task_id == task_id
    assert candidate.source_run_id == run_id


def test_the_rationale_says_what_happened_to_the_findings():
    rationale = extract_candidates([_issue()])[0].rationale

    assert "confirmed addressed" in rationale
    assert "src/components/navigation.tsx:42" in rationale


def test_an_untraceable_candidate_cannot_be_approved():
    check = check_approval(
        LessonCandidate(
            title="A title",
            lesson="A long enough instruction to pass the length check.",
            category="testing",
            source_issue_ids=(),
        )
    )

    assert not check.approved
    assert any("cannot be traced" in reason for reason in check.reasons)


def test_an_empty_lesson_cannot_be_approved():
    check = check_approval(
        LessonCandidate(
            title="A title",
            lesson="   ",
            category="testing",
            source_issue_ids=(str(uuid4()),),
        )
    )

    assert not check.approved
    assert any("empty" in reason for reason in check.reasons)


def test_a_title_that_is_not_an_instruction_cannot_be_approved():
    check = check_approval(
        LessonCandidate(
            title="Testing",
            lesson="Assert on output.",
            category="testing",
            source_issue_ids=(str(uuid4()),),
        )
    )

    assert not check.approved
    assert any("too short" in reason for reason in check.reasons)


def test_a_traceable_candidate_can_be_approved():
    candidate = extract_candidates([_issue()])[0]

    assert check_approval(candidate).approved


# --- rule 4: project lessons do not become global ---------------------------


def test_a_lesson_with_no_project_is_the_only_global_one():
    assert _lesson(project_id=None).is_global
    assert not _lesson(project_id=uuid4()).is_global


# --- rule 5: retrieve only a small relevant set -----------------------------


def test_the_default_retrieval_is_a_small_number():
    assert DEFAULT_RETRIEVAL_LIMIT <= 5


def test_retrieval_is_capped():
    many = [_lesson(title=f"Lesson {index}") for index in range(20)]
    keywords = lesson_keywords("lesson")

    assert len(rank_lessons(many, keywords=keywords, limit=3)) == 3


def test_a_lesson_matching_nothing_is_not_returned():
    """An unapproved lesson reaching a prompt is the failure this prevents."""
    unrelated = _lesson(
        title="Database migrations must be reversible",
        lesson="Every migration must have a tested down path.",
        status=LessonStatus.PROPOSED,
    )

    assert rank_lessons([unrelated], keywords=["navigation", "component"]) == ()


def test_an_approved_lesson_shares_the_frame():
    lesson = _lesson()

    ranked = rank_lessons([lesson], keywords=("migration",))

    assert len(ranked) == 1
    assert "approved for this project" in ranked[0].reasons


def test_a_real_match_outranks_a_merely_approved_one():
    """Otherwise the floor would quietly become the ranking."""
    approved_only = _lesson(
        title="Database migrations must be reversible",
        lesson="Every migration must have a tested down path.",
    )
    matching = _lesson(
        title="Assert on the navigation output",
        lesson="Assert on the rendered output of the navigation component.",
    )

    ranked = rank_lessons(
        [approved_only, matching], keywords=["navigation", "rendered", "output"]
    )

    assert ranked[0].lesson.id == matching.id
    assert ranked[1].lesson.id == approved_only.id


def test_the_same_input_always_produces_the_same_package():
    """A coder whose prompt changes between two identical runs is unexplainable."""
    lessons = [_lesson(title=f"Lesson {index}", occurrences=index % 3) for index in range(8)]
    keywords = lesson_keywords("lesson")

    first = [entry.lesson.id for entry in rank_lessons(lessons, keywords=keywords)]
    second = [entry.lesson.id for entry in rank_lessons(list(reversed(lessons)), keywords=keywords)]

    assert first == second


def test_every_retrieval_says_why_it_was_chosen():
    lesson = _lesson()

    entry = rank_lessons([lesson], language="typescript")[0]

    assert entry.reasons
    assert "language typescript" in entry.reasons


def test_a_coder_can_see_why_a_lesson_is_in_the_prompt():
    """Rule 5: advice with no visible reason is indistinguishable from boilerplate."""
    lesson = _lesson()

    line = retrieval_prompt_lines(rank_lessons([lesson], language="typescript"))[0]

    assert line.startswith("[testing] Assert on rendered output")
    assert "selected because" in line


# --- rule 6: track retrieval and usefulness ---------------------------------


def test_a_recurring_lesson_outranks_a_one_off_with_the_same_text():
    once = _lesson(occurrences=1, confidence=LessonConfidence.LOW)
    often = _lesson(occurrences=5, confidence=LessonConfidence.HIGH)

    ranked = rank_lessons([once, often], keywords=("rendered",))

    assert ranked[0].lesson.id == often.id
    assert "raised in 5 runs" in ranked[0].reasons


def test_a_lesson_that_has_been_applied_is_told_so():
    lesson = _lesson(times_applied=3)

    entry = rank_lessons([lesson], keywords=("rendered",))[0]

    assert "applied 3 time(s)" in entry.reasons


# --- keyword extraction -----------------------------------------------------


def test_keywords_ignore_the_words_every_task_shares():
    """Otherwise a lesson matches on grammar and the ranking is noise."""
    keywords = lesson_keywords("The test should assert on the rendered output of the component")

    assert "the" not in keywords
    assert "should" not in keywords
    assert "rendered" in keywords
    assert "component" in keywords


def test_keywords_split_identifiers_into_words():
    assert "render" in lesson_keywords("renderComponent")
    assert "task" in lesson_keywords("task_run_id")


def test_keywords_are_bounded():
    assert len(lesson_keywords(" ".join(f"word{index}" for index in range(100)))) == 12
