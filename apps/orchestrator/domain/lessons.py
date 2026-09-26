"""The Lessons System and its retrieval (build.md sections 32 and 33).

A lesson is a reusable engineering instruction distilled from a *verified*
review/fix cycle -- a finding a reviewer raised, the coder then addressed, and
a later reviewer confirmed was addressed. Section 32 then gives six rules, and
every design decision in this module is one of them being taken seriously:

1.  **Do not create a lesson from every review comment.** So eligibility is
    filtered on what kind of finding it is and on what happened to it, and a
    run that passed first time contributes nothing (there was no fix cycle).
2.  **Prefer recurring/generalizable issues.** So recurrence is the primary
    signal for both promotion (``occurrences``) and confidence, and a
    file-and-category-scoped finding is preferred over an unlocated one because
    it generalises to a rule rather than to a patch.
3.  **A lesson must be traceable to its source.** So every candidate carries
    the review issue it came from and the run that raised it, and the rendered
    text names where it came from.
4.  **Project-specific lessons do not become global.** Enforced structurally:
    a lesson is stored with its project or with none, and nothing promotes a
    project lesson to a global one.
5.  **Retrieve only a small relevant set.** Ranking returns a bounded list and
    says why each entry was chosen.
6.  **Track retrieval and usefulness.** Retrieval and application are counted
    separately, which is the whole signal: a lesson that is retrieved often and
    never applied is advice being ignored.

**No model is asked to write a lesson.** ``ModelPurpose.LESSON_EXTRACTION``
exists for a later phase, and a model would be the obvious way to phrase rule 3
more fluently -- but a lesson is only as good as the finding it cites, and
deriving the text mechanically from the reviewer's own ``required_fix`` keeps
the claim attributable to the reviewer rather than to a second summariser that
cannot be checked against anything. See concerns.md for the cost of that
choice.

Pure: no I/O, no model, no repository, no database.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .enums import IssueCategory, IssueSeverity, LessonConfidence, LessonStatus
from .models import Lesson, ReviewIssue

#: Categories that become lessons at all. A ``style`` or ``observation``
#: finding is real and worth recording as a review issue -- it is just not an
#: engineering instruction, and turning one into guidance is how a lesson list
#: fills up with "prefer named exports" (section 32 rule 1).
LESSON_CATEGORIES: frozenset[IssueCategory] = frozenset(
    {
        IssueCategory.REQUIREMENT,
        IssueCategory.ARCHITECTURE,
        IssueCategory.CORRECTNESS,
        IssueCategory.SECURITY,
        IssueCategory.TESTING,
    }
)

#: Severities worth a lesson. ``INFO`` never forces a retry (section 22), so it
#: never demonstrates a defect the coder had to fix.
LESSON_SEVERITIES: frozenset[IssueSeverity] = frozenset(
    {IssueSeverity.CRITICAL, IssueSeverity.HIGH, IssueSeverity.MEDIUM}
)

#: The most issues one candidate may summarise. A candidate built from a dozen
#: findings is a summary of the reviewer, not an instruction, and the source
#: traceability rule 3 asks for gets weaker with every issue folded in.
MAX_CANDIDATE_ISSUES = 3

#: Occurrences at which a candidate's confidence reaches ``high``, and at which
#: it reaches ``medium``. Below the first it stays ``low``: it has been seen
#: once, and once is not a pattern.
HIGH_CONFIDENCE_OCCURRENCES = 3
MEDIUM_CONFIDENCE_OCCURRENCES = 2

#: Ceiling on what one task is given (section 32 rule 5). The context builder
#: has its own budget for this, and this is the number it defaults to.
DEFAULT_RETRIEVAL_LIMIT = 5

#: Words carrying no retrieval signal. Stop-words would be a long list to keep
#: honest; what actually matters is not letting a lesson match a task on
#: grammar, so a small set of the words that dominate every task in a codebase
#: is enough to keep ranking from being noise.
_STOP_WORDS = frozenset(
    {
        "about", "after", "again", "against", "also", "and", "any", "are",
        "because", "been", "before", "being", "between", "both", "but", "can",
        "cannot", "could", "did", "does", "doing", "done", "during", "each",
        "few", "for", "from", "had", "has", "have", "here", "how", "into",
        "its", "itself", "just", "more", "most", "must", "not", "now", "off",
        "once", "only", "other", "our", "out", "over", "own", "same", "should",
        "since", "some", "such", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "too", "under",
        "until", "use", "used", "using", "very", "was", "were", "what", "when",
        "where", "which", "while", "who", "why", "will", "with", "would", "you",
        "your",
    }
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
#: ``renderComponent`` -> ``render Component``. An identifier and the English
#: sentence describing it share no characters, so without this a lesson written
#: in prose never matches a task that names the function -- which in a codebase
#: is most of them.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass(frozen=True, slots=True)
class LessonCandidate:
    """A proposed lesson, before anybody has decided it is reusable.

    This is the whole of phases L's extraction output: enough to render a
    lesson from, enough to show a person why it was proposed, and nothing that
    presumes the answer.
    """

    title: str
    lesson: str
    category: str
    language: str | None = None
    framework: str | None = None
    tags: tuple[str, ...] = ()
    #: The findings it summarises. Never empty -- rule 3.
    source_issue_ids: tuple[str, ...] = ()
    source_task_id: str | None = None
    source_run_id: str | None = None
    #: How many distinct runs have now raised this. ``1`` for a first sighting.
    occurrences: int = 1
    confidence: LessonConfidence = LessonConfidence.LOW
    #: Why it was proposed, in words a person can disagree with.
    rationale: str = ""
    #: Findings that were considered and are *not* part of this candidate,
    #: with the reason. Kept so a rejection can be explained.
    skipped: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {
            "title": self.title,
            "lesson": self.lesson,
            "category": self.category,
            "language": self.language,
            "framework": self.framework,
            "tags": list(self.tags),
            "source_issue_ids": list(self.source_issue_ids),
            "source_task_id": self.source_task_id,
            "source_run_id": self.source_run_id,
            "occurrences": self.occurrences,
            "confidence": self.confidence.value,
            "rationale": self.rationale,
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True, slots=True)
class RetrievedLesson:
    """A lesson chosen for a task, with the reason it was chosen (rule 5).

    The reason is not decoration. A coder handed a lesson cannot tell whether
    it is guidance about this task or boilerplate that happens to be in the
    database, and the context builder prints this next to the text.
    """

    lesson: Lesson
    score: int
    reasons: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {
            "lesson_id": str(self.lesson.id),
            "category": self.lesson.category,
            "title": self.lesson.title,
            "score": self.score,
            "reasons": list(self.reasons),
        }


# ----------------------------------------------------------------- eligibility


def is_lesson_candidate(issue: ReviewIssue) -> tuple[bool, str]:
    """Whether one finding may become a lesson, and why not when it may not.

    Returns the reason as well as the verdict so the candidate list can show a
    person what was considered. A silent filter is indistinguishable from a
    filter that dropped the wrong things.
    """
    if issue.severity not in LESSON_SEVERITIES:
        return False, (
            f"{issue.severity.value} findings do not force a retry, so they "
            "never demonstrate a defect the coder had to fix"
        )
    if issue.category not in LESSON_CATEGORIES:
        return False, f"{issue.category.value} findings are not engineering instructions"
    if not issue.required_fix.strip():
        return False, "the reviewer stated no required fix to generalise"
    return True, ""


def lesson_confidence(occurrences: int) -> LessonConfidence:
    """Confidence from recurrence (section 32 rule 2).

    Not a measure of how right the lesson is -- nothing here can be that. It is
    a measure of how often the same thing has been found, which is the only
    evidence available before phase M has produced any.
    """
    if occurrences >= HIGH_CONFIDENCE_OCCURRENCES:
        return LessonConfidence.HIGH
    if occurrences >= MEDIUM_CONFIDENCE_OCCURRENCES:
        return LessonConfidence.MEDIUM
    return LessonConfidence.LOW


# ------------------------------------------------------------------- extraction


def extract_candidates(
    issues: Sequence[ReviewIssue],
    *,
    language: str | None = None,
    framework: str | None = None,
    source_task_id: str | None = None,
    source_run_id: str | None = None,
    occurrences: int = 1,
) -> tuple[LessonCandidate, ...]:
    """Propose lessons from the findings of a verified review/fix cycle.

    Args:
        issues: the findings of the cycle. Only issues a later reviewer did not
            re-raise belong here -- the caller passes the *resolved* ones, which
            is section 32's "verified cycle" and concern 27's definition of
            resolved. Passing a still-open finding is not an error, but the
            lesson would be teaching something the coder has not been told is
            right yet.
        occurrences: how many runs have now raised this same thing, which the
            caller may already know about a proposed duplicate.
        source_task_id, source_run_id: recorded for traceability (rule 3).

    Returns:
        At most ``MAX_CANDIDATE_ISSUES``-sized candidates, grouped by category
        and file so that several findings about one thing become one
        instruction rather than three near-duplicates. Empty when nothing in the
        cycle is eligible, which is the common case and is correct (rule 1).
    """
    eligible: list[ReviewIssue] = []
    skipped: list[str] = []
    for issue in issues:
        ok, reason = is_lesson_candidate(issue)
        if ok:
            eligible.append(issue)
        else:
            skipped.append(f"{_issue_label(issue)}: {reason}")

    groups = _group(eligible)
    # A group larger than the cap does not have its tail thrown away without
    # saying so: those findings were eligible, and a person deciding whether to
    # trust the extraction is entitled to know they were folded in.
    folded = [issue for group in groups for issue in group[MAX_CANDIDATE_ISSUES:]]
    if folded:
        skipped.append(
            f"{len(folded)} further finding(s) in the same category and file were "
            "folded into the first lesson rather than proposed separately"
        )

    return tuple(
        _candidate(
            group[:MAX_CANDIDATE_ISSUES],
            language=language,
            framework=framework,
            source_task_id=source_task_id,
            source_run_id=source_run_id,
            occurrences=occurrences,
            skipped=tuple(skipped),
        )
        for group in groups
    )


def chosen_ids(issues: Iterable[ReviewIssue]) -> tuple[str, ...]:
    return tuple(str(issue.id) for issue in issues if issue.id is not None)


def _group(issues: Sequence[ReviewIssue]) -> list[list[ReviewIssue]]:
    """Group findings by the thing they are about, most severe first.

    The key is category plus file, which is ``issue_fingerprint``'s identity
    without the requirement id: two findings on the same file under the same
    category are about the same thing even when they cite different
    requirements, and a lesson that says both is one instruction where two
    would be noise.
    """
    groups: dict[tuple[str, str], list[ReviewIssue]] = {}
    for issue in issues:
        key = (str(issue.category), (issue.file or "").casefold())
        groups.setdefault(key, []).append(issue)
    ordered = sorted(
        groups.values(),
        key=lambda group: (
            -max(_severity_rank(issue.severity) for issue in group),
            str(group[0].category),
            (group[0].file or "").casefold(),
        ),
    )
    return ordered


def _severity_rank(severity: IssueSeverity) -> int:
    order = list(IssueSeverity)
    return order.index(severity)


def _candidate(
    issues: Sequence[ReviewIssue],
    *,
    language: str | None,
    framework: str | None,
    source_task_id: str | None,
    source_run_id: str | None,
    occurrences: int,
    skipped: tuple[str, ...] = (),
) -> LessonCandidate:
    """Turn a group of findings into one proposed lesson."""
    primary = issues[0]
    title = _title(primary, issues)
    return LessonCandidate(
        title=title,
        lesson=_body(primary, issues),
        category=str(primary.category),
        language=language,
        framework=framework,
        tags=_tags(primary, issues),
        source_issue_ids=chosen_ids(issues),
        source_task_id=source_task_id,
        source_run_id=source_run_id,
        occurrences=occurrences,
        confidence=lesson_confidence(occurrences),
        rationale=_rationale(issues, occurrences),
        skipped=skipped,
    )


def _title(primary: ReviewIssue, issues: Sequence[ReviewIssue]) -> str:
    """A short, stable heading. Derived from the reviewer's own words."""
    heading = f"{primary.category.value}: {_shorten(_requirement_text(primary), 50)}"
    if len(issues) > 1:
        return f"{heading} (and {len(issues) - 1} more)"
    return heading


def _body(primary: ReviewIssue, issues: Sequence[ReviewIssue]) -> str:
    """The instruction, in the reviewer's words rather than a paraphrase.

    Built as an imperative over the reviewer's ``required_fix`` so the lesson
    says what to do, and followed by the other findings' fixes when a group
    carries more than one. A paraphrase here would be a second model's claim
    about what the reviewer meant, with nothing to check it against.
    """
    parts = [_imperative(primary.required_fix)]
    primary_fix = primary.required_fix.strip()
    for issue in issues[1:]:
        fix = issue.required_fix.strip()
        if fix and fix != primary_fix:
            parts.append(_imperative(issue.required_fix))
    location = _location(primary)
    if location:
        parts.append(f"(Raised against {location}.)")
    return " ".join(parts)


def _imperative(text: str) -> str:
    """A required fix as an instruction rather than a description of a defect.

    Reviewers write "add a null check" or "the guard is missing"; a lesson that
    says "the guard is missing" is a description of the past, not something a
    coder can follow on a different file.
    """
    collapsed = " ".join(text.split())
    if not collapsed:
        return collapsed
    # "The null check is missing" -> "Add the null check.", and "the guard is
    # missing" -> "Add the guard.". The article is carried across from the
    # defect rather than re-prepended, so "The null check" does not become
    # "Add the The null check."
    match = re.match(
        r"^(?:the\s+|a\s+|an\s+)?(.*?)\s+(?:is|are|was|were)\s+(?:missing|absent)\b",
        collapsed,
        re.I,
    )
    if match and match.group(1).strip():
        return f"Add the {match.group(1).strip()}."
    return collapsed.rstrip(".") + "."


def _rationale(issues: Sequence[ReviewIssue], occurrences: int) -> str:
    where = ", ".join(
        location for location in (_location(issue) for issue in issues) if location
    )
    parts = [
        f"{len(issues)} finding(s) raised and then confirmed addressed",
        f"category {issues[0].category.value}",
    ]
    if where:
        parts.append(f"at {where}")
    if occurrences > 1:
        parts.append(f"seen across {occurrences} runs")
    if not all(issue.file for issue in issues):
        # An unlocated finding generalises less well, and the reviewer prompt
        # asks for a file (concern 40). Saying so is how a person decides
        # whether to promote it anyway.
        parts.append("at least one finding was unlocated, so this generalises weakly")
    return "; ".join(parts)


def _tags(primary: ReviewIssue, issues: Sequence[ReviewIssue]) -> tuple[str, ...]:
    tags = {f"category:{primary.category.value}"}
    for issue in issues:
        if issue.file:
            tags.add(f"file:{_file_tag(issue.file)}")
        for keyword in lesson_keywords(issue.required_fix) | lesson_keywords(issue.problem):
            tags.add(f"kw:{keyword}")
    return tuple(sorted(tags))


def _file_tag(path: str) -> str:
    """A file reduced to something usable as a tag.

    The extension and the stem, not the whole path: a lesson applies to every
    ``*service.ts``, and a tag that spells out one directory would only ever
    match the file it came from.
    """
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return name
    stem, _, extension = name.rpartition(".")
    return f"{stem}.{extension}" if stem else name


def _location(issue: ReviewIssue) -> str:
    if issue.file and issue.line:
        return f"{issue.file}:{issue.line}"
    return issue.file or ""


def _issue_label(issue: ReviewIssue) -> str:
    """A short, stable name for a finding, for the skipped/considered list."""
    where = _location(issue) or "unlocated"
    return f"[{issue.severity.value}] {issue.category.value} at {where}"


def _requirement_text(issue: ReviewIssue) -> str:
    """What the finding is called, in the reviewer's words.

    The problem statement rather than the required fix: a title is a name for
    the thing, and a required fix is an instruction, which the body already is.
    """
    return _shorten(issue.problem, 80)


def _shorten(text: str, limit: int) -> str:
    """One sentence, one line, bounded -- for a heading."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "(no description)"
    first = _SENTENCE_END.split(collapsed)[0] or collapsed
    return first if len(first) <= limit else first[: limit - 1] + "…"


# -------------------------------------------------------------------- retrieval


def lesson_keywords(text: str, *, limit: int = 12) -> frozenset[str]:
    """Content words from lesson or task text, for section 33's matching."""
    words = {
        word.casefold()
        for word in _WORD_RE.findall(
            _CAMEL_BOUNDARY.sub(" ", text).replace("_", " ").replace("-", " ")
        )
    }
    meaningful = {
        word
        for word in words
        if len(word) > 2 and word not in _STOP_WORDS and not word.isdigit()
    }
    return frozenset(sorted(meaningful)[:limit])


def rank_lessons(
    lessons: Sequence[Lesson],
    *,
    keywords: Iterable[str] = (),
    language: str | None = None,
    framework: str | None = None,
    category: str | None = None,
    limit: int = DEFAULT_RETRIEVAL_LIMIT,
) -> tuple[RetrievedLesson, ...]:
    """Choose the few lessons worth putting in front of a coder (rule 5).

    Section 33's V1 ranking: language, framework, category, then keyword and
    tag matching. Scored rather than filtered, because a lesson that matches
    the project's language but not this task is still better than nothing, and
    filtering it out would leave a project with no guidance at all.

    An **approved** lesson always scores at least one point, for the fact that a
    person approved it. That is the difference between two kinds of unmatched
    lesson: one this project vetted and chose to keep, and one that is merely
    present. Rule 1 is enforced at extraction, where a review comment becomes a
    candidate at all; by the time something is in this list it has already passed
    the gate, so withholding it because it shares no words with today's task
    would hide guidance the project paid for. The one point ranks it below every
    lesson with a real match, and the limit caps how many such fallbacks reach a
    prompt.

    Zero still means drop, and it now means exactly one thing: a lesson that is
    not approved. ``rank_lessons`` is given whatever list the caller has, so the
    check is what stops a candidate being shown by a caller that skipped the
    status filter.

    Ties break towards the lesson with evidence behind it: occurrences first
    (rule 2), then how often it has been applied, then its id, so the same
    input always produces the same package.
    """
    terms = frozenset(word.casefold() for word in keywords)
    scored: list[tuple[int, str, RetrievedLesson]] = []
    for lesson in lessons:
        score, reasons = _score(
            lesson,
            terms=terms,
            language=language,
            framework=framework,
            category=category,
        )
        if score <= 0:
            continue
        tiebreak = f"{lesson.times_applied:09d}-{str(lesson.id)}"
        scored.append((score, tiebreak, RetrievedLesson(lesson, score, reasons)))
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return tuple(entry[2] for entry in scored[: max(0, limit)])


def _score(
    lesson: Lesson,
    *,
    terms: frozenset[str],
    language: str | None,
    framework: str | None,
    category: str | None,
) -> tuple[int, tuple[str, ...]]:
    """Points and reasons for one lesson. Reasons go into the prompt."""
    score = 0
    reasons: list[str] = []
    if language and lesson.language and lesson.language.casefold() == language.casefold():
        score += 4
        reasons.append(f"language {lesson.language}")
    if (
        framework
        and lesson.framework
        and lesson.framework.casefold() == framework.casefold()
    ):
        score += 4
        reasons.append(f"framework {lesson.framework}")
    if category and lesson.category.casefold() == category.casefold():
        score += 3
        reasons.append(f"category {lesson.category}")
    # Recurrence is a ranking input, not just a confidence label: a lesson this
    # project has hit three times is a better bet than one seen once. One point,
    # deliberately below a single keyword match. Section 33's V1 order is
    # language, framework, category, keywords and tags, *then* recurrence, and a
    # weighting that let recurrence outrank a word the task actually used would
    # be quietly substituting "popular" for "relevant".
    if lesson.occurrences > 1:
        score += 1
        reasons.append(f"raised in {lesson.occurrences} runs")
    if lesson.times_applied:
        score += 1
        reasons.append(f"applied {lesson.times_applied} time(s)")

    overlap = terms & (lesson_keywords(lesson.title) | lesson_keywords(lesson.lesson))
    if overlap:
        score += 2 * len(overlap)
        reasons.append(f"keyword match: {', '.join(sorted(overlap)[:4])}")
    for tag in lesson.tags:
        if tag.startswith("kw:") and tag[3:] in terms:
            score += 1
            reasons.append(f"tag {tag[3:]}")
    # A lesson whose own confidence was high has already survived more reviews.
    if lesson.confidence is LessonConfidence.HIGH:
        score += 1
    # The approval floor. Deliberately last so it can never outweigh a real
    # match, and deliberately one point so ten unmatched-but-approved lessons
    # cannot outrank one lesson about this task.
    if lesson.status is LessonStatus.APPROVED:
        score += 1
        reasons.append("approved for this project")
    return score, tuple(reasons)


def retrieval_prompt_lines(retrieved: Sequence[RetrievedLesson]) -> list[str]:
    """How a retrieved lesson is rendered beside the task (rule 5).

    The reason is included because a coder has to be able to tell why this
    piece of advice is in its prompt; advice with no visible reason is
    indistinguishable from boilerplate, which is the failure mode section 32
    rule 1 is about.
    """
    lines: list[str] = []
    for entry in retrieved:
        lesson = entry.lesson
        where = f" ({lesson.language}/{lesson.framework})" if lesson.language else ""
        reason = f"; selected because {', '.join(entry.reasons)}" if entry.reasons else ""
        lines.append(
            f"[{lesson.category}] {lesson.title}{where}: "
            f"{' '.join(lesson.lesson.split())}{reason}"
        )
    return lines


# -------------------------------------------------------------------- approval


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """Whether a candidate may be promoted, and what is missing if it may not."""

    approved: bool
    reasons: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {"approved": self.approved, "reasons": list(self.reasons)}


def check_approval(candidate: LessonCandidate) -> ApprovalCheck:
    """Section 32's rules as conditions on a promotion.

    Not ceremony. Every check here is about reviewability: an unreviewable
    lesson is worse than no lesson, because it reaches a coder carrying the
    weight of guidance with nothing behind it.

    Rule 4 -- project lessons do not become global -- is deliberately *not*
    checked here, because it is not a property of a candidate's text. It is a
    property of where the lesson is stored, and it is enforced there: a lesson
    row carries a ``project_id`` or carries none, and no code path in the
    codebase reassigns it. A check on a candidate could only assert that a
    candidate is well-formed, which is what the rest of this does.
    """
    reasons: list[str] = []
    if not candidate.lesson.strip():
        reasons.append("the lesson text is empty")
    if not candidate.title.strip():
        reasons.append("the lesson has no title")
    if not candidate.source_issue_ids:
        reasons.append(
            "the candidate cites no review issue, so it cannot be traced to a source "
            "(section 32 rule 3)"
        )
    if len(candidate.lesson) < 20:
        reasons.append(
            "the lesson text is too short to be an instruction rather than a title"
        )
    if reasons:
        return ApprovalCheck(False, tuple(reasons))
    return ApprovalCheck(True)


__all__ = [
    "DEFAULT_RETRIEVAL_LIMIT",
    "HIGH_CONFIDENCE_OCCURRENCES",
    "LESSON_CATEGORIES",
    "LESSON_SEVERITIES",
    "MAX_CANDIDATE_ISSUES",
    "MEDIUM_CONFIDENCE_OCCURRENCES",
    "ApprovalCheck",
    "LessonCandidate",
    "RetrievedLesson",
    "check_approval",
    "extract_candidates",
    "is_lesson_candidate",
    "lesson_confidence",
    "lesson_keywords",
    "rank_lessons",
    "retrieval_prompt_lines",
]
