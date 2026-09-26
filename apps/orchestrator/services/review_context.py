"""Gathering the reviewer's bounded package (build.md section 21, phase I item 4).

``domain.review_package`` decides what a package *is* and what fits in it.
This module does the I/O that fills one: read the diff, render the task, load
the architecture decisions the change is measured against, find the few files
the diff cannot be read without, and pull forward the findings from earlier
cycles.

The selection rule is narrower than the context builder's, and deliberately
so. Section 21 says the reviewer *should not receive unrelated repository
contents*, so a file is here only if one of two things is true:

* the task declared it as something to inspect, and the diff does not already
  contain it -- the reviewer needs the contract the change is written against;
* something the diff changes imports it, so the diff cannot be read without
  it.

Nothing is included by keyword match, by ranking, or because it looked
interesting. A reviewer given the same orientation material as the coder
would be reviewing the coder's context rather than the coder's change.

What raises and what degrades: a missing run raises. An unreadable ``.ai/``
directory, a repository with no Git history, a file that has since been
deleted -- those cost the package a section and are recorded in ``omitted``,
because a review with one fewer ADR is worth more than no review at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.architecture import select_decisions
from ..domain.completion import CompletionReport
from ..domain.models import Lesson, ReviewIssue, Task
from ..domain.relevance import (
    extract_import_targets,
    extract_keywords,
    normalise_path,
    resolve_import,
)
from ..domain.review_package import (
    ReviewBudget,
    ReviewPackage,
    ReviewSource,
    assemble_review_package,
)
from ..domain.task_spec import render_task_specification
from ..domain.verification import VerificationReport
from ..repositories import LessonRepository, ReviewRepository, TaskRepository
from .context_builder import RepositoryReader, language_for, load_decisions
from .workspace import DiffCapture, TaskWorkspace, capture_diff, load_run_context

logger = get_logger(__name__)

#: Ceiling on the diff text captured for a review. Larger than the coder's,
#: because the diff is the reviewer's whole subject rather than one item in a
#: context package -- but still a ceiling, because a candidate that produces a
#: megabyte of diff has already failed the diff-size policy.
MAX_REVIEW_DIFF_BYTES = 768_000


def build_review_package(
    session: Session,
    workspace: TaskWorkspace,
    *,
    verification: VerificationReport | None = None,
    completion_report: CompletionReport | None = None,
    diff: DiffCapture | None = None,
    candidate_commit: str | None = None,
    cycle: int = 1,
    budget: ReviewBudget | None = None,
    settings: Settings | None = None,
    include_lessons: bool = True,
) -> ReviewPackage:
    """Assemble the package for one review of ``workspace``.

    Args:
        verification: the pipeline's report. Omitting it is allowed and
            recorded as an omission -- the reviewer is then told nothing was
            verified rather than being left to assume it was.
        completion_report: the coder's account of the attempt, shown to the
            reviewer as a claim to check against the diff.
        diff: a diff already captured by the caller. Re-captured here when
            absent, which is the normal path after verification has run.
        cycle: which review cycle this is, counting from 1.

    Raises:
        EntityNotFound: the run, its task or its project is missing.
    """
    config = settings or get_settings()
    run, task, project = load_run_context(session, workspace.task_run_id)
    captured = diff or capture_diff(workspace, max_bytes=MAX_REVIEW_DIFF_BYTES)
    omitted: list[str] = []

    reader = RepositoryReader(workspace.path, settings=config, git=workspace.git)
    changed = tuple(captured.summary.paths)

    decisions = _decision_sources(reader, task, limit=config.context_max_decisions)
    if not decisions:
        omitted.append("no architecture decisions were found in the repository's .ai/")
    sources = _supporting_sources(reader, task, changed)

    lessons: Sequence[Lesson] = ()
    if include_lessons and config.context_max_lessons:
        lessons = LessonRepository(session).search(
            project_id=project.id, limit=config.context_max_lessons
        )

    unresolved = _unresolved_issues(session, workspace.task_run_id, cycle)

    if verification is None:
        omitted.append(
            "no verification report was supplied with this review, so the "
            "reviewer was told nothing about the project's own checks"
        )
    if completion_report is None:
        omitted.append("the coder produced no completion report for this attempt")
    omitted.extend(reader.warnings)

    package = assemble_review_package(
        external_task_id=task.external_task_id,
        attempt=run.attempt_number,
        cycle=cycle,
        task_specification=render_task_specification(
            task, dependency_titles=_dependency_titles(session, task)
        ),
        starting_commit=run.starting_commit or "(unrecorded)",
        candidate_commit=candidate_commit or run.candidate_commit,
        diff_text=captured.text,
        changed_paths=changed,
        deleted_paths=tuple(captured.summary.deleted_paths),
        files_changed=captured.files_changed,
        diff_lines=captured.line_count,
        verification=_render_verification(verification),
        completion_report=completion_report.render() if completion_report else "",
        unresolved_issues=unresolved,
        decisions=decisions,
        sources=sources,
        lessons=_render_lessons(lessons),
        diff_already_truncated=captured.truncated,
        omitted=omitted,
        budget=budget or budget_from_settings(config),
    )
    logger.info(
        "review_package_built",
        run_id=str(workspace.task_run_id),
        task=task.external_task_id,
        cycle=cycle,
        content_hash=package.content_hash,
        estimated_tokens=package.estimated_tokens,
        files_changed=package.files_changed,
        decisions=len(package.decisions),
        sources=len(package.sources),
        complete=package.complete,
    )
    return package


def budget_from_settings(settings: Settings | None = None) -> ReviewBudget:
    """The configured review budget.

    Derived from the reviewer's own window when one is configured, not the
    coder's: the reviewer is usually the stronger model, and budgeting its
    package against the local coder's window would throw away the headroom
    that made it worth calling.
    """
    config = settings or get_settings()
    window = config.review_context_window or config.local_model_context_window
    return ReviewBudget(
        max_tokens=config.review_max_package_tokens
        or max(1, int(window * config.review_package_share)),
        min_diff_tokens=config.review_min_diff_tokens,
        max_source_tokens=config.context_max_item_tokens,
        max_decisions=config.context_max_decisions,
    )


# --------------------------------------------------------------------- parts


def _decision_sources(
    reader: RepositoryReader, task: Task, *, limit: int
) -> tuple[ReviewSource, ...]:
    """The binding ADRs this change is measured against (sections 16 and 22)."""
    if limit < 1:
        return ()
    decisions = load_decisions(reader)
    keywords = extract_keywords(
        task.external_task_id, task.title, task.instructions, *task.declared_paths
    )
    selected = select_decisions(
        decisions, keywords, limit=limit, include_unmatched=len(decisions) <= limit
    )
    return tuple(
        ReviewSource(
            path=decision.path,
            content=decision.body,
            reason=f"binding architecture decision ({decision.status})",
            source_lines=decision.body.count("\n") + 1,
            language="markdown",
        )
        for decision in selected
    )


def _supporting_sources(
    reader: RepositoryReader, task: Task, changed: Sequence[str]
) -> tuple[ReviewSource, ...]:
    """Only what the diff cannot be read without (section 21's last bullet).

    Two origins, in order: the contracts the task told the coder to read, and
    the modules the changed files import. A file already in the diff is never
    repeated -- the reviewer is looking at its new contents there, and showing
    the old ones beside them invites a review of the wrong version.
    """
    already = {normalise_path(path) for path in changed}
    sources: list[ReviewSource] = []
    seen: set[str] = set(already)

    for declared in task.files_to_inspect:
        path = normalise_path(declared)
        if path in seen:
            continue
        source = _source(reader, path, reason="declared by the task as read-only context")
        if source is not None:
            seen.add(path)
            sources.append(source)

    for path in changed:
        text = reader.text_of(path)
        if text is None:
            continue
        for specifier in extract_import_targets(text):
            target = resolve_import(specifier, from_path=path, known_paths=reader.paths)
            if target is None or target in seen:
                continue
            source = _source(reader, target, reason=f"imported by the changed file {path}")
            if source is not None:
                seen.add(target)
                sources.append(source)

    # Direct callers are as important as imports of the changed file: a
    # signature change cannot be reviewed safely without seeing who invokes
    # it. Resolve imports repository-wide and keep the result bounded by the
    # review package's later source budget.
    changed_set = {normalise_path(path) for path in changed}
    for candidate in reader.paths:
        candidate = normalise_path(candidate)
        if candidate in seen:
            continue
        text = reader.text_of(candidate)
        if text is None:
            continue
        targets = {
            resolve_import(specifier, from_path=candidate, known_paths=reader.paths)
            for specifier in extract_import_targets(text)
        }
        called = sorted(changed_set & {target for target in targets if target})
        if not called:
            continue
        source = _source(
            reader,
            candidate,
            reason=f"imports changed file {called[0]}",
        )
        if source is not None:
            seen.add(candidate)
            sources.append(source)
    return tuple(sources)


def _source(reader: RepositoryReader, path: str, *, reason: str) -> ReviewSource | None:
    text = reader.text_of(path)
    if text is None:
        return None
    return ReviewSource(
        path=path,
        content=text,
        reason=reason,
        source_lines=text.count("\n") + (0 if text.endswith("\n") else 1),
        language=language_for(path),
    )


def _unresolved_issues(
    session: Session, task_run_id: UUID, cycle: int
) -> tuple[ReviewIssue, ...]:
    """Open findings from this run's earlier cycles (section 23).

    Only earlier cycles, and only unresolved ones: a reviewer shown the
    findings of the cycle it is currently performing would be reading its own
    answer back to itself.
    """
    if cycle <= 1:
        return ()
    issues: list[ReviewIssue] = []
    for review in ReviewRepository(session).list_for_run(task_run_id):
        if review.cycle >= cycle:
            continue
        issues.extend(issue for issue in review.issues if not issue.resolved)
    return tuple(issues)


def _render_verification(report: VerificationReport | None) -> str:
    """The pipeline's results, written for a reviewer.

    Every check is listed, skipped ones included. A reviewer that sees only
    the passes cannot tell a project with no lint step from one that linted
    cleanly, and section 17's whole point is that those are different.
    """
    if report is None:
        return ""
    lines = [
        f"Outcome: {report.summary()}",
        f"Verified by executed commands: {'yes' if report.verified else 'no'}",
        "",
    ]
    for step in report.steps:
        entry = f"- [{step.verification_type.value}] {step.status.value}"
        if step.command:
            entry += f" — `{step.command}`"
        if step.exit_code is not None:
            entry += f" (exit {step.exit_code})"
        lines.append(entry)
        if step.detail:
            lines.append(f"  {step.detail}")
    if report.human_review_reasons:
        lines.extend(
            [
                "",
                "The orchestrator has already flagged this change for human "
                "attention:",
                *(f"- {reason}" for reason in report.human_review_reasons),
            ]
        )
    return "\n".join(lines)


def _render_lessons(lessons: Sequence[Lesson]) -> tuple[str, ...]:
    return tuple(
        f"[{lesson.category}] {lesson.title}: {' '.join(lesson.lesson.split())}"
        for lesson in lessons
    )


def _dependency_titles(session: Session, task: Task) -> dict[str, str]:
    if not task.depends_on:
        return {}
    tasks = TaskRepository(session)
    titles: dict[str, str] = {}
    for external_id in task.depends_on:
        dependency = tasks.get_by_external_id(task.project_id, external_id)
        if dependency is not None:
            titles[external_id] = dependency.title
    return titles


__all__ = [
    "MAX_REVIEW_DIFF_BYTES",
    "budget_from_settings",
    "build_review_package",
]
