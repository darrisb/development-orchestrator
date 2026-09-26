"""Project and model metrics (build.md section 35).

Every number here is computed from rows written as the work happened, rather
than accumulated into a counter. That is the design: a success count that is
incremented when something succeeds is a number nobody can audit, and section
35's requirements -- average attempts, retry rate, token spend, first-pass
success rate -- are all figures you want to be able to re-derive later and get
the same answer.

The cost of computing on read is that these queries touch the project's runs. At
the size a single-machine orchestrator handles that is cheaper than the
alternative, and section 35 lists the metrics as things the orchestrator must
provide rather than as a table to maintain.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import LessonStatus, RunStatus
from ..domain.models import ModelRun, Review, TaskRun
from ..repositories import (
    LessonRepository,
    ModelRunRepository,
    ProjectRepository,
    ReviewRepository,
    TaskRunRepository,
    TrainingExampleRepository,
)
from .errors import EntityNotFound
from .training import CODING_PURPOSES

logger = get_logger(__name__)

#: The states a run is counted in. Anything else -- queued, or a retry still in
#: flight -- is not an outcome, and is left out of every denominator rather than
#: being rounded away.
TERMINAL_STATUSES = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ABANDONED)

#: How many times a finding has to appear before it counts as recurring. Two is
#: the lowest number that can distinguish "the reviewer keeps saying this" from
#: "the reviewer said this once".
MIN_RECURRENCE_COUNT = 2


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Section 35's per-run figures.

    ``first_pass_successes`` is kept beside ``successes`` because section 35
    asks for both: a project where most tasks pass on attempt 3 and one where
    most pass on attempt 1 have the same success rate, and only the first is
    producing work that was right the first time.
    """

    total_runs: int = 0
    successes: int = 0
    failures: int = 0
    abandoned: int = 0
    first_pass_successes: int = 0
    retried_runs: int = 0
    total_attempts: int = 0
    total_review_cycles: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def _finished(self) -> int:
        return self.total_runs

    @property
    def success_rate(self) -> float:
        """Succeeded over everything that finished, abandoned included.

        Abandoned is in the denominator on purpose: a run nobody finished is a
        run the orchestrator did not deliver, and excluding it turns the rate
        into a measure of how often *finished* work passed.
        """
        return self.successes / self._finished if self._finished else 0.0

    @property
    def first_pass_success_rate(self) -> float:
        return (
            self.first_pass_successes / self._finished if self._finished else 0.0
        )

    @property
    def average_attempts(self) -> float:
        return self.total_attempts / self._finished if self._finished else 0.0

    @property
    def average_review_cycles(self) -> float:
        return self.total_review_cycles / self._finished if self._finished else 0.0

    @property
    def retry_rate(self) -> float:
        """Share of finished runs that needed more than one attempt."""
        return self.retried_runs / self._finished if self._finished else 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def describe(self) -> dict[str, object]:
        return {
            "total_runs": self.total_runs,
            "successes": self.successes,
            "failures": self.failures,
            "abandoned": self.abandoned,
            "first_pass_successes": self.first_pass_successes,
            "success_rate": round(self.success_rate, 4),
            "first_pass_success_rate": round(self.first_pass_success_rate, 4),
            "retry_rate": round(self.retry_rate, 4),
            "average_attempts": round(self.average_attempts, 3),
            "average_review_cycles": round(self.average_review_cycles, 3),
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "statuses": dict(self.statuses),
        }


@dataclass(slots=True)
class ModelCost:
    """One model's calls, cost and outcome. An accumulator, not a report."""

    model_id: UUID
    calls: int = 0
    succeeded: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    by_purpose: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, run: ModelRun) -> None:
        self.calls += 1
        if run.status is RunStatus.SUCCEEDED:
            self.succeeded += 1
        else:
            self.failed += 1
        self.input_tokens += run.input_tokens or 0
        self.output_tokens += run.output_tokens or 0
        self.duration_ms += run.duration_ms or 0
        bucket = self.by_purpose.setdefault(
            str(run.purpose), {"calls": 0, "tokens": 0, "failed": 0}
        )
        bucket["calls"] += 1
        bucket["tokens"] += (run.input_tokens or 0) + (run.output_tokens or 0)
        if run.status is not RunStatus.SUCCEEDED:
            bucket["failed"] += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def describe(self) -> dict[str, object]:
        coding_tokens = sum(
            bucket["tokens"]
            for purpose, bucket in self.by_purpose.items()
            if purpose in {p.value for p in CODING_PURPOSES}
        )
        return {
            "model_id": str(self.model_id),
            "calls": self.calls,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "avg_tokens_per_call": (
                round(self.total_tokens / self.calls, 1) if self.calls else 0.0
            ),
            "coding_tokens": coding_tokens,
            "by_purpose": {
                purpose: dict(bucket) for purpose, bucket in sorted(self.by_purpose.items())
            },
        }


def _measure(session: Session, runs: list[TaskRun]) -> RunMetrics:
    """Aggregate a set of runs.

    Only terminal runs reach the denominators; ``statuses`` keeps the full
    distribution including the non-terminal ones so a caller can see that a
    project has eight finished runs and two in flight rather than wondering why
    the total moved.
    """
    finished = [run for run in runs if run.status in TERMINAL_STATUSES]
    model_runs = ModelRunRepository(session)
    input_tokens = 0
    output_tokens = 0
    for run in finished:
        call_input, call_output = model_runs.tokens_for_run(run.id)
        input_tokens += call_input
        output_tokens += call_output
    durations = [
        int((run.completed_at - run.started_at).total_seconds() * 1000)
        for run in finished
        if run.started_at is not None and run.completed_at is not None
    ]
    return RunMetrics(
        total_runs=len(finished),
        successes=sum(1 for run in finished if run.status is RunStatus.SUCCEEDED),
        failures=sum(1 for run in finished if run.status is RunStatus.FAILED),
        abandoned=sum(1 for run in finished if run.status is RunStatus.ABANDONED),
        first_pass_successes=sum(
            1
            for run in finished
            if run.status is RunStatus.SUCCEEDED and run.attempt_number == 1
        ),
        retried_runs=sum(1 for run in finished if run.attempt_number > 1),
        total_attempts=sum(run.attempt_number for run in finished),
        total_review_cycles=sum(run.review_cycle for run in finished),
        duration_ms=sum(durations),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        statuses=dict(Counter(run.status.value for run in runs)),
    )


def project_metrics(session: Session, project_id: UUID) -> dict[str, object]:
    """Everything section 35 asks for, for one project.

    Raises:
        EntityNotFound: no such project.
    """
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    runs = TaskRunRepository(session).list_for_project(project_id)
    reviews = ReviewRepository(session).list_for_project(project_id)
    return {
        "project_id": str(project_id),
        "runs": _measure(session, runs).describe(),
        "reviews": review_metrics(reviews),
        "lessons": lesson_metrics(session, project_id),
        "training": training_metrics(session, project_id),
        "models": model_metrics(session, project_id)["models"],
    }


def task_metrics(session: Session, runs: list[TaskRun]) -> RunMetrics:
    """Section 35's figures for one task's runs, across its retries.

    Takes the runs rather than a task id because the caller usually has them
    already, and because this is also how a caller scopes the figures to a
    single run by passing a one-element list.
    """
    return _measure(session, runs)


def review_metrics(reviews: list[Review]) -> dict[str, object]:
    """Review distribution, plus the findings that keep coming back.

    Recurrence by category is here and not only in the lessons table, because a
    finding can recur without ever becoming an approved lesson -- and that is
    precisely the finding a project has not solved.
    """
    categories: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    blocking_issues = 0
    for review in reviews:
        for issue in review.issues:
            categories[issue.category.value] += 1
            severities[issue.severity.value] += 1
        blocking_issues += len(review.blocking_issues)
    return {
        "total": len(reviews),
        "decisions": dict(Counter(review.decision.value for review in reviews)),
        "issue_categories": dict(categories.most_common()),
        "issue_severities": dict(severities.most_common()),
        "blocking_issues": blocking_issues,
        "average_issues_per_review": (
            round(sum(len(review.issues) for review in reviews) / len(reviews), 3)
            if reviews
            else 0.0
        ),
    }


def recurring_findings(reviews: list[Review], *, limit: int = 20) -> list[dict[str, object]]:
    """The findings a project keeps producing, most frequent first.

    Grouped by the same identity ``domain.review.issue_fingerprint`` uses --
    requirement, file, category -- so "the reviewer keeps saying the same thing"
    is a number rather than an impression from scrolling. A finding that
    appeared in four reviews is a different problem from a project with four
    unrelated findings, and only the count distinguishes them.

    ``runs`` is deliberately not in the output even though it is computable: the
    same review history is what a reader will check the count against, and
    quoting a run count that disagrees with it would be worse than not quoting
    one.

    Sorted by count, then by the most severe severity seen, then by identity, so
    the list is stable for two identical histories.
    """
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for review in reviews:
        for issue in review.issues:
            key = (
                issue.category.value,
                issue.file or "",
                issue.requirement_id or "",
            )
            entry = grouped.setdefault(
                key,
                {
                    "category": issue.category.value,
                    "file": issue.file,
                    "requirement_id": issue.requirement_id,
                    "count": 0,
                    "resolved": 0,
                    "still_open": 0,
                    "severities": {},
                    "example_problem": issue.problem,
                    "required_fix": issue.required_fix,
                },
            )
            entry["count"] = int(entry["count"]) + 1  # type: ignore[arg-type]
            if issue.resolved:
                entry["resolved"] = int(entry["resolved"]) + 1  # type: ignore[arg-type]
            else:
                entry["still_open"] = int(entry["still_open"]) + 1  # type: ignore[arg-type]
            severities = entry["severities"]  # type: ignore[assignment]
            severities[issue.severity.value] = severities.get(issue.severity.value, 0) + 1
    ordered = sorted(
        grouped.values(),
        key=lambda entry: (
            -int(entry["count"]),  # type: ignore[arg-type]
            str(entry["file"] or ""),
            str(entry["requirement_id"] or ""),
        ),
    )
    # Only findings that came back. A count of one is a reviewer's opinion, not a
    # pattern, and section 35 asks for the recurring ones: the list is for
    # deciding what to fix structurally, and a list of every finding ever raised
    # buries the two that have now happened four times each.
    recurring = [entry for entry in ordered if int(entry["count"]) >= MIN_RECURRENCE_COUNT]  # type: ignore[arg-type]
    return recurring[:limit]


def lesson_metrics(session: Session, project_id: UUID) -> dict[str, object]:
    """Lesson counts by status, plus the approved ones nobody has used.

    ``unused`` counts lessons that have never been retrieved. Section 32 rule 6
    asks for usefulness to be tracked, and the first half of usefulness is
    knowing that a lesson is being ignored outright.
    """
    lessons = LessonRepository(session)
    counts: dict[str, int] = {}
    unused: list[str] = []
    for status in LessonStatus:
        rows = lessons.list_by_status(status, project_id=project_id, limit=1000)
        counts[status.value] = len(rows)
        if status is LessonStatus.APPROVED:
            unused = [str(row.id) for row in rows if row.times_retrieved == 0]
    total_proposals = sum(counts.values())
    accepted_proposals = counts.get("approved", 0) + counts.get("retired", 0)
    approval_rate = (
        round(accepted_proposals / total_proposals, 3) if total_proposals else None
    )
    if total_proposals < 10:
        calibration = "insufficient_observed_output"
    elif approval_rate is not None and approval_rate < 0.25:
        calibration = "review_extraction_phrasing_and_filters"
    elif counts.get("proposed", 0) == 0:
        calibration = "review_filters_may_be_too_narrow"
    else:
        calibration = "within_observed_range"
    return {
        "by_status": counts,
        "unused_approved": len(unused),
        "unused_approved_ids": sorted(unused)[:20],
        "total_proposals": total_proposals,
        "approval_rate": approval_rate,
        "calibration": calibration,
    }


def training_metrics(session: Session, project_id: UUID) -> dict[str, object]:
    """What section 34 captured, by curation state.

    ``captured_but_not_selected`` is the honest reading of "do not automatically
    train on every accepted example": most examples will sit at ``captured``
    until a curation process exists, and the count is how many.
    """
    examples = TrainingExampleRepository(session).list_for_project(project_id, limit=1000)
    statuses = Counter(example.status.value for example in examples)
    return {
        "total": len(examples),
        "by_status": dict(statuses),
        "captured_but_not_selected": statuses.get("captured", 0),
        "total_input_tokens": sum(example.input_tokens or 0 for example in examples),
        "total_output_tokens": sum(example.output_tokens or 0 for example in examples),
        "average_attempts": (
            round(sum(example.attempts for example in examples) / len(examples), 3)
            if examples
            else 0.0
        ),
    }


def model_metrics(session: Session, project_id: UUID | None = None) -> dict[str, object]:
    """Per-model cost and outcome, optionally restricted to a project.

    ``None`` covers the whole installation, which is the comparison section 35
    is really after: which registered model is cheaper per accepted task. The
    per-purpose breakdown is kept because section 35 distinguishes a first
    attempt from a correction, and an average that mixes the two hides a model
    that is only ever asked to fix things.
    """
    repository = ModelRunRepository(session)
    rows = (
        repository.list_for_project(project_id)
        if project_id is not None
        else repository.list_all()
    )
    costs: dict[UUID, ModelCost] = {}
    for row in rows:
        costs.setdefault(row.model_id, ModelCost(row.model_id)).add(row)
    described = [cost.describe() for cost in costs.values()]
    return {
        "project_id": str(project_id) if project_id else None,
        "coding_purposes": [purpose.value for purpose in CODING_PURPOSES],
        "models": described,
        "totals": {
            "calls": sum(int(entry["calls"]) for entry in described),
            "input_tokens": sum(int(entry["input_tokens"]) for entry in described),
            "output_tokens": sum(int(entry["output_tokens"]) for entry in described),
            "total_tokens": sum(int(entry["total_tokens"]) for entry in described),
        },
    }


def lesson_usefulness(session: Session, lesson_id: UUID) -> dict[str, object]:
    """Section 32 rule 6 for one lesson: sent, applied, and the ratio.

    Takes an id and reads the row, because the counters only mean anything
    current: a ``Lesson`` passed in by a caller may predate the retrieval that
    was just counted.

    ``applied_per_retrieval`` is allowed to exceed 1. The counters are not
    per-retrieval: an applied lesson is credited once per accepted run, and a
    run that retried can show the same lesson in two retrievals before being
    accepted. What the ratio says is whether the guidance is landing at all, not
    whether every instance of it was used.

    Raises:
        EntityNotFound: no such lesson.
    """
    lesson = LessonRepository(session).get(lesson_id)
    if lesson is None:
        raise EntityNotFound("Lesson", lesson_id)
    retrieved = lesson.times_retrieved
    applied = lesson.times_applied
    return {
        "lesson_id": str(lesson.id),
        "status": lesson.status.value,
        "occurrences": lesson.occurrences,
        "confidence": lesson.confidence.value,
        "times_retrieved": retrieved,
        "times_applied": applied,
        "applied_per_retrieval": round(applied / retrieved, 3) if retrieved else None,
        "ever_retrieved": retrieved > 0,
        "ever_applied": applied > 0,
    }


__all__ = [
    "TERMINAL_STATUSES",
    "ModelCost",
    "RunMetrics",
    "lesson_metrics",
    "lesson_usefulness",
    "model_metrics",
    "project_metrics",
    "recurring_findings",
    "review_metrics",
    "task_metrics",
    "training_metrics",
]
