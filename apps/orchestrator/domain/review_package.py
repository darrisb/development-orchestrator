"""The bounded package a reviewer receives (build.md section 21).

Section 21 lists what goes in, and then one sentence that decides the design:

> Reviewer should not receive unrelated repository contents.

So this is not the coder's context package with a diff bolted on. The
*subject* is the diff, everything else is there to judge it against, and
anything that is not either of those is left out and named in ``omitted``
rather than quietly dropped.

The order the sections are rendered in is the order they matter in, and it is
also the order they survive a tight budget in:

1.  the task and its acceptance criteria -- review is against requirements
    (principle 5), so without this there is nothing to review against;
2.  the candidate diff, clipped last and marked when clipped;
3.  the deterministic verification results, so the reviewer never has to
    guess whether the tests ran (section 17);
4.  the coder's completion report -- its *claims*, to be read against the
    diff (section 14);
5.  open findings from earlier cycles, so a re-review can tell a fix from a
    coincidence;
6.  binding architecture decisions (section 16), which are the whole of the
    architecture-compliance half of section 22;
7.  additional source needed to understand the diff -- and only that;
8.  lessons and policies, which are advice and lose first.

A clipped diff is the one thing that must never pass unannounced: a reviewer
approving half a change it believed was the whole change is a worse outcome
than a review that refuses to conclude. ``complete`` says whether the package
is whole, and the rendered text says so to the reviewer in words.

Pure: no I/O, no model, no repository.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from .context import render_bullet_list
from .models import ReviewIssue
from .redaction import Redactor
from .review import render_unresolved_issues
from .tokens import characters_for_tokens, estimate_tokens

#: Bumped whenever the rendering changes, so a stored review can be read
#: against the package that produced it (section 34).
REVIEW_PACKAGE_VERSION = "review-package/1"

REVIEW_PACKAGE_ARTIFACT = "review-package.txt"

#: Appended where a section was clipped. Worded for a reviewer, not a coder:
#: the point is that its verdict is now about part of a change.
CLIP_MARKER = (
    "\n... [truncated by orchestrator: {shown} of {total} lines shown. "
    "You are seeing part of this content.]\n"
)


@dataclass(frozen=True, slots=True)
class ReviewSource:
    """One extra file, included because the diff cannot be read without it."""

    path: str
    content: str
    reason: str
    truncated: bool = False
    shown_lines: int | None = None
    source_lines: int = 0
    language: str | None = None

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.render())

    def render(self) -> str:
        body = self.content.rstrip()
        if self.language:
            body = f"```{self.language}\n{body}\n```"
        return f"### {self.path}\n\n_{self.reason}_\n\n{body}\n"

    def clipped(self, max_tokens: int) -> ReviewSource:
        clipped, shown, total = _clip(self.content, max_tokens)
        if shown is None:
            return self
        return ReviewSource(
            path=self.path,
            content=clipped,
            reason=self.reason,
            truncated=True,
            shown_lines=shown,
            source_lines=total,
            language=self.language,
        )

    def describe(self) -> dict[str, object]:
        return {
            "path": self.path,
            "reason": self.reason,
            "truncated": self.truncated,
            "shown_lines": self.shown_lines,
            "source_lines": self.source_lines,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True, slots=True)
class ReviewPackage:
    """Everything the reviewer is shown, and the record of what it is not.

    ``content_hash`` covers the rendered text exactly, so two reviews of the
    same candidate can be compared and a reviewer's answer can be traced to
    what it actually saw (section 34).
    """

    external_task_id: str
    attempt: int
    cycle: int
    task_specification: str
    starting_commit: str
    diff_text: str
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    files_changed: int = 0
    diff_lines: int = 0
    candidate_commit: str | None = None
    verification: str = ""
    completion_report: str = ""
    unresolved_issues: tuple[ReviewIssue, ...] = ()
    decisions: tuple[ReviewSource, ...] = ()
    sources: tuple[ReviewSource, ...] = ()
    lessons: tuple[str, ...] = ()
    diff_truncated: bool = False
    #: What the budget or the gatherer left out, each with a reason.
    omitted: tuple[str, ...] = field(default=())
    #: Whether every text field below was passed through ``Redactor`` before
    #: this package was rendered (section 36, concern 28). Recorded rather than
    #: assumed: a reviewer's finding about a masked line has to be readable
    #: later as a finding about a *masked* line.
    redacted: bool = False

    @property
    def complete(self) -> bool:
        """Whether the reviewer is seeing the whole candidate.

        A dropped ADR or a missing lesson does not make a package incomplete
        in the sense that matters: the *change* is still whole. A clipped diff
        or a clipped source file does.
        """
        return not self.diff_truncated and not any(
            source.truncated for source in (*self.sources, *self.decisions)
        )

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.render())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.render().encode()).hexdigest()

    def render(self) -> str:
        """The package as the reviewer sees it."""
        sections: list[str] = [
            f"# Review package — {self.external_task_id} "
            f"(attempt {self.attempt}, review cycle {self.cycle})",
            "## Task and acceptance criteria\n\n" + self.task_specification,
            self._change_section(),
        ]
        if self.verification:
            sections.append(
                "## Deterministic verification results\n\n"
                "These commands were executed by the orchestrator, not by the "
                "coder. Their results are facts, not claims.\n\n" + self.verification
            )
        if self.completion_report:
            sections.append(
                "## What the coder says it did (unverified)\n\n"
                "Read this against the diff. A claim the diff does not support "
                "is itself a finding.\n\n" + self.completion_report
            )
        if self.unresolved_issues:
            sections.append(
                "## Open findings from earlier review cycles\n\n"
                + render_unresolved_issues(self.unresolved_issues)
            )
        if self.decisions:
            sections.append(
                "## Binding architecture decisions\n\n"
                "The change must comply with these (section 22, architecture "
                "compliance).\n\n"
                + "\n\n".join(decision.render() for decision in self.decisions)
            )
        if self.sources:
            sections.append(
                "## Additional source needed to read the diff\n\n"
                "Included only because the diff cannot be judged without it. "
                "It is not under review.\n\n"
                + "\n\n".join(source.render() for source in self.sources)
            )
        if self.lessons:
            sections.append(
                render_bullet_list(
                    "## Lessons and policies from earlier reviews (guidance, "
                    "not requirements)",
                    list(self.lessons),
                )
            )
        if not self.complete:
            sections.append(
                "## Completeness warning\n\n"
                "Part of this package was clipped to fit the reviewer's context "
                "window. Do not approve a change you have not seen in full: if "
                "the clipped content could affect your verdict, answer "
                "HUMAN_REVIEW_REQUIRED and say what you could not see."
            )
        return "\n\n".join(sections)

    def _change_section(self) -> str:
        header = render_bullet_list(
            "## Candidate change",
            [
                f"Starting commit: {self.starting_commit}",
                f"Candidate commit: {self.candidate_commit or 'uncommitted worktree'}",
                f"Files changed: {self.files_changed}; diff lines: {self.diff_lines}",
                "Changed files: "
                + (", ".join(self.changed_paths) if self.changed_paths else "none"),
                "Deleted files: "
                + (", ".join(self.deleted_paths) if self.deleted_paths else "none"),
            ],
        )
        body = self.diff_text.rstrip() or "(the candidate produced an empty diff)"
        return f"{header}\n\n```diff\n{body}\n```"

    def describe(self) -> dict[str, object]:
        """The manifest recorded beside the package (section 9)."""
        return {
            "schema_version": REVIEW_PACKAGE_VERSION,
            "task": self.external_task_id,
            "attempt": self.attempt,
            "cycle": self.cycle,
            "content_hash": self.content_hash,
            "estimated_tokens": self.estimated_tokens,
            "complete": self.complete,
            "starting_commit": self.starting_commit,
            "candidate_commit": self.candidate_commit,
            "files_changed": self.files_changed,
            "diff_lines": self.diff_lines,
            "changed_paths": list(self.changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "diff_truncated": self.diff_truncated,
            "decisions": [decision.describe() for decision in self.decisions],
            "sources": [source.describe() for source in self.sources],
            "lessons": len(self.lessons),
            "unresolved_issues": len(self.unresolved_issues),
            "omitted": list(self.omitted),
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class ReviewBudget:
    """Bounds for a review package.

    Separate from ``ContextBudget`` because the two are filled in opposite
    directions. A context package fills a budget with as many useful files as
    fit; a review package has one mandatory subject -- the diff -- and
    everything else is negotiable around it.
    """

    max_tokens: int = 12000
    #: Floor for the diff. If the rest of the package would push the diff
    #: below this, the rest loses, because a review of a change nobody can see
    #: is worthless while a review without an ADR is merely weaker.
    min_diff_tokens: int = 3000
    max_source_tokens: int = 1500
    max_sources: int = 10
    max_decisions: int = 5

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.min_diff_tokens < 1:
            raise ValueError("min_diff_tokens must be >= 1")

    def describe(self) -> dict[str, int]:
        return {
            "max_tokens": self.max_tokens,
            "min_diff_tokens": self.min_diff_tokens,
            "max_source_tokens": self.max_source_tokens,
            "max_sources": self.max_sources,
            "max_decisions": self.max_decisions,
        }


def assemble_review_package(
    *,
    external_task_id: str,
    attempt: int,
    cycle: int,
    task_specification: str,
    starting_commit: str,
    diff_text: str,
    budget: ReviewBudget | None = None,
    changed_paths: Sequence[str] = (),
    deleted_paths: Sequence[str] = (),
    files_changed: int = 0,
    diff_lines: int = 0,
    candidate_commit: str | None = None,
    verification: str = "",
    completion_report: str = "",
    unresolved_issues: Sequence[ReviewIssue] = (),
    decisions: Sequence[ReviewSource] = (),
    sources: Sequence[ReviewSource] = (),
    lessons: Sequence[str] = (),
    diff_already_truncated: bool = False,
    omitted: Sequence[str] = (),
) -> ReviewPackage:
    """Fit the parts of a review package into ``budget``.

    The order of sacrifice, when the budget is short: lessons, then additional
    source, then architecture decisions, then the diff is clipped. The task
    specification, the verification results and the completion report are
    never dropped -- they are small, and each of them is something the
    reviewer would otherwise have to guess at.

    ``diff_already_truncated`` carries forward a clip that happened upstream
    (``DiffCapture`` has its own byte ceiling), so a package is not reported
    as complete just because this function did not have to cut anything.
    """
    bounds = budget or ReviewBudget()
    dropped: list[str] = list(omitted)

    kept_decisions = tuple(
        decision.clipped(bounds.max_source_tokens)
        for decision in decisions[: bounds.max_decisions]
    )
    if len(decisions) > bounds.max_decisions:
        dropped.append(
            f"{len(decisions) - bounds.max_decisions} architecture decision(s) "
            f"beyond the {bounds.max_decisions} the budget allows"
        )
    kept_sources = tuple(
        source.clipped(bounds.max_source_tokens) for source in sources[: bounds.max_sources]
    )
    if len(sources) > bounds.max_sources:
        dropped.append(
            f"{len(sources) - bounds.max_sources} supporting file(s) beyond the "
            f"{bounds.max_sources} the budget allows"
        )
    kept_lessons = tuple(lessons)

    def build(
        diff: str,
        truncated: bool,
        decision_items: tuple[ReviewSource, ...],
        source_items: tuple[ReviewSource, ...],
        lesson_items: tuple[str, ...],
        omissions: Sequence[str],
    ) -> ReviewPackage:
        return ReviewPackage(
            external_task_id=external_task_id,
            attempt=attempt,
            cycle=cycle,
            task_specification=task_specification,
            starting_commit=starting_commit,
            diff_text=diff,
            changed_paths=tuple(changed_paths),
            deleted_paths=tuple(deleted_paths),
            files_changed=files_changed,
            diff_lines=diff_lines,
            candidate_commit=candidate_commit,
            verification=verification,
            completion_report=completion_report,
            unresolved_issues=tuple(unresolved_issues),
            decisions=decision_items,
            sources=source_items,
            lessons=lesson_items,
            diff_truncated=truncated or diff_already_truncated,
            omitted=tuple(dict.fromkeys(omissions)),
        )

    package = build(
        diff_text, False, kept_decisions, kept_sources, kept_lessons, dropped
    )
    if package.estimated_tokens <= bounds.max_tokens:
        return package

    # Shed the negotiable sections, cumulatively, cheapest loss first. Each
    # step keeps what the step before it kept, so the package never gains a
    # section back on its way down.
    Concession = tuple[str, tuple[ReviewSource, ...], tuple[ReviewSource, ...], tuple[str, ...]]
    concessions: list[Concession] = []
    if kept_lessons:
        concessions.append((f"{len(kept_lessons)} lesson(s)", kept_decisions, kept_sources, ()))
    if kept_sources:
        concessions.append((f"{len(kept_sources)} supporting file(s)", kept_decisions, (), ()))
    if kept_decisions:
        concessions.append((f"{len(kept_decisions)} architecture decision(s)", (), (), ()))

    for description, decision_items, source_items, lesson_items in concessions:
        dropped.append(f"{description} dropped to make room for the diff")
        package = build(
            diff_text, False, decision_items, source_items, lesson_items, dropped
        )
        if package.estimated_tokens <= bounds.max_tokens:
            return package

    # Everything negotiable is gone and it still does not fit: clip the diff.
    overflow = package.estimated_tokens - bounds.max_tokens
    allowance = max(bounds.min_diff_tokens, estimate_tokens(diff_text) - overflow)
    clipped, shown, total = _clip(diff_text, allowance)
    if shown is None:
        return package
    dropped.append(f"the diff was clipped to {shown} of {total} lines")
    return build(clipped, True, (), (), (), dropped)


def _clip(text: str, max_tokens: int) -> tuple[str, int | None, int]:
    """Clip ``text`` at a line boundary. ``(text, shown_lines, total_lines)``.

    ``shown_lines`` is ``None`` when nothing was cut, so a caller can tell
    "fits" from "fits exactly".
    """
    limit = characters_for_tokens(max_tokens)
    lines = text.splitlines()
    if len(text) <= limit:
        return text, None, len(lines)
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    marker = CLIP_MARKER.format(shown=len(kept), total=len(lines))
    return "\n".join(kept) + marker, len(kept), len(lines)


def redact_package(package: ReviewPackage, redactor: Redactor) -> ReviewPackage:
    """A copy of ``package`` with every text field masked (concern 28).

    Everything else that leaves the orchestrator goes through ``Redactor`` --
    worker stdout, worker stderr, log artifacts -- and the review package did
    not, although it carries the raw diff and the raw contents of supporting
    files to whatever ``REVIEW_BASE_URL`` points at, which may be a third-party
    API.

    The inputs are masked rather than the rendered text, so that the package's
    ``content_hash``, its stored artifact and the prompt actually sent are all
    the same bytes. Hashing one thing and sending another would break the only
    link between a reviewer's answer and what it saw (section 34).

    The cost is real and is the reason this is a setting rather than always on:
    a reviewer cannot comment usefully on a line whose contents have been
    replaced by a placeholder. That trade is right for a remote reviewer and
    arguable for one on this machine.

    Not masked: ``unresolved_issues``, which is the orchestrator's own record of
    what earlier reviews said. Those reviews read a package that went through
    this same function, so their text is already masked at source, and rewriting
    stored findings here would mean the issue quoted to the reviewer no longer
    matched the issue in the database.
    """
    if package.redacted:
        return package
    return replace(
        package,
        task_specification=redactor.redact(package.task_specification),
        diff_text=redactor.redact(package.diff_text),
        verification=redactor.redact(package.verification),
        completion_report=redactor.redact(package.completion_report),
        decisions=tuple(_redact_source(source, redactor) for source in package.decisions),
        sources=tuple(_redact_source(source, redactor) for source in package.sources),
        lessons=tuple(redactor.redact(lesson) for lesson in package.lessons),
        redacted=True,
    )


def _redact_source(source: ReviewSource, redactor: Redactor) -> ReviewSource:
    return replace(source, content=redactor.redact(source.content))


__all__ = [
    "CLIP_MARKER",
    "REVIEW_PACKAGE_ARTIFACT",
    "REVIEW_PACKAGE_VERSION",
    "ReviewBudget",
    "ReviewPackage",
    "ReviewSource",
    "assemble_review_package",
    "redact_package",
]
