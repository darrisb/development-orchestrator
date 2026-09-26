"""Lesson, training and metrics responses (build.md sections 32-35).

The lesson response carries the two things a person deciding whether to approve
needs and an API that only returned the text would not give them: where it came
from, and how often it has been raised. Section 32 rule 3 is traceability, and
an approval queue that shows a title and a sentence is not offering a review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from ..domain.enums import LessonConfidence, LessonStatus, TrainingStatus
from ..domain.models import Lesson, TrainingExample

#: A short reason a person typed when declining or retiring a lesson. Bounded
#: because it is free text going into a log; not bounded because the useful
#: ones are sentences.
ReasonText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class LessonResponse(BaseModel):
    id: UUID
    project_id: UUID | None
    language: str | None
    framework: str | None
    category: str
    title: str
    lesson: str
    tags: list[str]
    #: Section 32 rule 3. A lesson that cannot name its source is not
    #: reviewable, and these three are the whole of the trace.
    source_review_issue_id: UUID | None
    source_task_id: UUID | None
    source_run_id: UUID | None
    requirement_id: str | None
    source_file: str | None
    confidence: LessonConfidence
    occurrences: int
    status: LessonStatus
    approved_by: str | None
    approved_at: datetime | None
    rejection_reason: str | None
    times_retrieved: int
    times_applied: int
    #: Section 32 rule 4, visible: a null project is what "global" means, and
    #: nothing in the API can turn a project lesson into one.
    is_global: bool
    is_retrievable: bool
    created_at: datetime | None

    @classmethod
    def from_domain(cls, lesson: Lesson) -> LessonResponse:
        return cls(
            id=lesson.id,
            project_id=lesson.project_id,
            language=lesson.language,
            framework=lesson.framework,
            category=lesson.category,
            title=lesson.title,
            lesson=lesson.lesson,
            tags=list(lesson.tags),
            source_review_issue_id=lesson.source_review_issue_id,
            source_task_id=lesson.source_task_id,
            source_run_id=lesson.source_run_id,
            requirement_id=lesson.requirement_id,
            source_file=lesson.source_file,
            confidence=lesson.confidence,
            occurrences=lesson.occurrences,
            status=lesson.status,
            approved_by=lesson.approved_by,
            approved_at=lesson.approved_at,
            rejection_reason=lesson.rejection_reason,
            times_retrieved=lesson.times_retrieved,
            times_applied=lesson.times_applied,
            is_global=lesson.is_global,
            is_retrievable=lesson.is_retrievable,
            created_at=lesson.created_at,
        )


class LessonSummary(BaseModel):
    """A lesson without its text, for a queue that may be long."""

    id: UUID
    project_id: UUID | None
    category: str
    title: str
    confidence: LessonConfidence
    occurrences: int
    status: LessonStatus
    times_retrieved: int
    times_applied: int

    @classmethod
    def from_domain(cls, lesson: Lesson) -> LessonSummary:
        return cls(
            id=lesson.id,
            project_id=lesson.project_id,
            category=lesson.category,
            title=lesson.title,
            confidence=lesson.confidence,
            occurrences=lesson.occurrences,
            status=lesson.status,
            times_retrieved=lesson.times_retrieved,
            times_applied=lesson.times_applied,
        )


class ApproveLessonRequest(BaseModel):
    #: Who approved it. Free text rather than an account, because there is no
    #: account system in V1 and an absent name is better than a fake one.
    approved_by: str | None = Field(
        default=None, max_length=200, description="recorded as the approver"
    )


class RejectLessonRequest(BaseModel):
    #: Optional in the schema and expected in practice. A rejection with no
    #: reason is allowed because refusing to answer is an answer, and the row
    #: records that too -- but the approval queue shows the reason when there is
    #: one, and its absence is conspicuous.
    reason: ReasonText | None = None


class LessonUsefulnessResponse(BaseModel):
    """Section 32 rule 6 for one lesson."""

    lesson_id: UUID
    status: LessonStatus
    occurrences: int
    confidence: LessonConfidence
    times_retrieved: int
    times_applied: int
    applied_per_retrieval: float | None
    ever_retrieved: bool
    ever_applied: bool


class TrainingExampleResponse(BaseModel):
    id: UUID
    task_run_id: UUID
    project_id: UUID
    task_id: UUID
    external_project_id: str
    external_task_id: str
    external_run_id: str
    artifact_path: str
    manifest_sha256: str
    outcome: str
    coder_model_id: UUID | None
    reviewer_model: str | None
    prompt_version: str | None
    attempts: int
    review_cycles: int
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    #: Section 34: capture and selection are different acts. Everything starts
    #: ``CAPTURED`` and stays there until a curation process moves it.
    status: TrainingStatus
    exclusion_reason: str | None
    selectable: bool
    created_at: datetime | None

    @classmethod
    def from_domain(cls, example: TrainingExample) -> TrainingExampleResponse:
        return cls(
            id=example.id,
            task_run_id=example.task_run_id,
            project_id=example.project_id,
            task_id=example.task_id,
            external_project_id=example.external_project_id,
            external_task_id=example.external_task_id,
            external_run_id=example.external_run_id,
            artifact_path=example.artifact_path,
            manifest_sha256=example.manifest_sha256,
            outcome=example.outcome,
            coder_model_id=example.coder_model_id,
            reviewer_model=example.reviewer_model,
            prompt_version=example.prompt_version,
            attempts=example.attempts,
            review_cycles=example.review_cycles,
            duration_ms=example.duration_ms,
            input_tokens=example.input_tokens,
            output_tokens=example.output_tokens,
            status=example.status,
            exclusion_reason=example.exclusion_reason,
            selectable=example.selectable,
            created_at=example.created_at,
        )


class ExcludeTrainingExampleRequest(BaseModel):
    reason: ReasonText


class RecurringFindingResponse(BaseModel):
    """One finding a project keeps producing, and how often.

    The count is over reviews, which is the record a reader can check against.
    A project with four unrelated findings and a project where the same finding
    appeared four times both have four issues; only the grouping tells them
    apart.
    """

    category: str
    file: str | None
    requirement_id: str | None
    count: int
    resolved: int
    still_open: int
    severities: dict[str, int]
    example_problem: str
    required_fix: str


class RunMetricsResponse(BaseModel):
    """Section 35's run figures. Every field is derived, never accumulated."""

    total_runs: int
    successes: int
    failures: int
    abandoned: int
    first_pass_successes: int
    success_rate: float
    first_pass_success_rate: float
    retry_rate: float
    average_attempts: float
    average_review_cycles: float
    duration_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    statuses: dict[str, int]


class ReviewMetricsResponse(BaseModel):
    total: int
    decisions: dict[str, int]
    issue_categories: dict[str, int]
    issue_severities: dict[str, int]
    blocking_issues: int
    average_issues_per_review: float


class LessonMetricsResponse(BaseModel):
    by_status: dict[str, int]
    unused_approved: int
    unused_approved_ids: list[str]
    total_proposals: int
    approval_rate: float | None
    calibration: str


class TrainingMetricsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    captured_but_not_selected: int
    total_input_tokens: int
    total_output_tokens: int
    average_attempts: float


class ModelMetricsResponse(BaseModel):
    model_id: UUID
    calls: int
    succeeded: int
    failed: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    duration_ms: int
    avg_tokens_per_call: float
    coding_tokens: int
    by_purpose: dict[str, dict[str, int]]


class ModelComparisonResponse(BaseModel):
    project_id: UUID | None
    coding_purposes: list[str]
    models: list[ModelMetricsResponse]
    totals: dict[str, int]


class ProjectMetricsResponse(BaseModel):
    project_id: UUID
    runs: RunMetricsResponse
    reviews: ReviewMetricsResponse
    lessons: LessonMetricsResponse
    training: TrainingMetricsResponse
    models: list[ModelMetricsResponse]


__all__ = [
    "ApproveLessonRequest",
    "LessonMetricsResponse",
    "LessonResponse",
    "LessonSummary",
    "LessonUsefulnessResponse",
    "ModelComparisonResponse",
    "ModelMetricsResponse",
    "ProjectMetricsResponse",
    "RecurringFindingResponse",
    "RejectLessonRequest",
    "ReviewMetricsResponse",
    "RunMetricsResponse",
    "TrainingExampleResponse",
    "TrainingMetricsResponse",
]
