"""The experience-capture API: lessons, review history, training, metrics.

Four surfaces, one per phase L requirement that needs a person to be able to ask
the system a question:

* ``/lessons`` -- the approval queue and the decisions on it (section 32).
* ``/tasks/{id}/review-history`` and ``/projects/{id}/recurring-findings`` --
  what reviewers have said across a task's retries, and which findings keep
  coming back.
* ``/projects/{id}/training`` -- what was preserved and in what state (34).
* ``/projects/{id}/metrics`` and ``/models/metrics`` -- the section 35 figures.

Nothing here can move a lesson between projects. No endpoint takes a project id
for a lesson, because section 32 rule 4 is structural: a lesson's project is set
when it is extracted and is not a field any request can change.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..domain.enums import LessonStatus, TrainingStatus
from ..repositories import (
    LessonRepository,
    ProjectRepository,
    ReviewRepository,
    TaskRepository,
    TaskRunRepository,
    TrainingExampleRepository,
)
from ..schemas.experience import (
    ApproveLessonRequest,
    ExcludeTrainingExampleRequest,
    LessonMetricsResponse,
    LessonResponse,
    LessonSummary,
    LessonUsefulnessResponse,
    ModelComparisonResponse,
    ProjectMetricsResponse,
    RecurringFindingResponse,
    RejectLessonRequest,
    RunMetricsResponse,
    TrainingExampleResponse,
    TrainingMetricsResponse,
)
from ..schemas.reviews import ReviewResponse
from ..services import lessons as lesson_service
from ..services import metrics as metrics_service
from ..services import training as training_service
from ..services.errors import EntityNotFound
from ..services.metrics import lesson_usefulness
from ..services.runs import list_runs_for_task

router = APIRouter(tags=["experience"])


# --------------------------------------------------------------------- lessons


@router.get("/lessons", response_model=list[LessonSummary])
def list_lessons(
    project_id: UUID | None = None,
    status: LessonStatus = LessonStatus.PROPOSED,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_db),
) -> list[LessonSummary]:
    """The approval queue, or any other status, for one project or all of them.

    Defaults to ``PROPOSED`` because the queue is what somebody opening this
    endpoint wants. The global project-lessons view is the same call with the id
    omitted, and it does not make those lessons global.
    """
    return [
        LessonSummary.from_domain(lesson)
        for lesson in lesson_service.list_approval_queue(
            session, project_id, status=status, limit=limit
        )
    ]


@router.get("/lessons/{lesson_id}", response_model=LessonResponse)
def get_lesson(lesson_id: UUID, session: Session = Depends(get_db)) -> LessonResponse:
    """One lesson, with the source a person needs to judge it."""
    lesson = LessonRepository(session).get(lesson_id)
    if lesson is None:
        raise EntityNotFound("Lesson", lesson_id)
    return LessonResponse.from_domain(lesson)


@router.post("/lessons/{lesson_id}/approve", response_model=LessonResponse)
def approve_lesson(
    lesson_id: UUID,
    payload: ApproveLessonRequest,
    session: Session = Depends(get_db),
) -> LessonResponse:
    """Promote a candidate so a coder may be shown it.

    Refuses with 409 a candidate that cannot be traced to a review issue: an
    untraceable lesson is worse than no lesson, and clicking approve should not
    be a way past that.
    """
    return LessonResponse.from_domain(
        lesson_service.approve_lesson(session, lesson_id, approved_by=payload.approved_by)
    )


@router.post("/lessons/{lesson_id}/reject", response_model=LessonResponse)
def reject_lesson(
    lesson_id: UUID,
    payload: RejectLessonRequest,
    session: Session = Depends(get_db),
) -> LessonResponse:
    """Decline a lesson. The row is kept, so the decision survives."""
    return LessonResponse.from_domain(
        lesson_service.reject_lesson(session, lesson_id, reason=payload.reason)
    )


@router.post("/lessons/{lesson_id}/retire", response_model=LessonResponse)
def retire_lesson(
    lesson_id: UUID,
    payload: RejectLessonRequest,
    session: Session = Depends(get_db),
) -> LessonResponse:
    """Withdraw an approved lesson that turned out to be wrong."""
    return LessonResponse.from_domain(
        lesson_service.retire_lesson(session, lesson_id, reason=payload.reason)
    )


@router.get("/lessons/{lesson_id}/usefulness", response_model=LessonUsefulnessResponse)
def lesson_usefulness_endpoint(
    lesson_id: UUID, session: Session = Depends(get_db)
) -> LessonUsefulnessResponse:
    """Section 32 rule 6 for one lesson: how often it was sent, and followed."""
    return LessonUsefulnessResponse(**lesson_usefulness(session, lesson_id))


@router.post("/runs/{run_id}/lessons/propose", response_model=list[LessonResponse])
def propose_run_lessons(run_id: UUID, session: Session = Depends(get_db)) -> list[LessonResponse]:
    """Propose lessons from a run's resolved findings.

    The workflow calls this on delivery; it is exposed because the findings a run
    resolved are only known once the fix loop has finished, and re-running it
    afterwards is how a project fills in history it missed. Idempotent: a finding
    that already has a lesson increments that lesson's ``occurrences`` rather
    than proposing a second one.
    """
    return [
        LessonResponse.from_domain(lesson)
        for lesson in lesson_service.propose_lessons_for_run(session, run_id)
    ]


# -------------------------------------------------------------- review history


@router.get("/tasks/{task_id}/review-history", response_model=list[ReviewResponse])
def task_review_history(task_id: UUID, session: Session = Depends(get_db)) -> list[ReviewResponse]:
    """Every review of a task, across all of its runs, oldest first.

    A task is retried rather than abandoned, so the reviews explaining how it
    reached ``COMPLETE`` are spread over several runs. Reading only the last one
    is how a reviewer becomes "it keeps asking for the same thing" with no way to
    see that it was addressed on the third run.
    """
    if TaskRepository(session).get(task_id) is None:
        raise EntityNotFound("Task", task_id)
    return [
        ReviewResponse.from_domain(review)
        for review in ReviewRepository(session).list_for_task(task_id)
    ]


@router.get("/projects/{project_id}/review-history", response_model=list[ReviewResponse])
def project_review_history(
    project_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> list[ReviewResponse]:
    """A project's reviews, newest first.

    A mistyped project id is a 404 rather than an empty list: "this project has
    no reviews" and "this project does not exist" are different answers, and only
    one of them is true.
    """
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    return [
        ReviewResponse.from_domain(review)
        for review in ReviewRepository(session).list_for_project(project_id, limit=limit)
    ]


@router.get(
    "/projects/{project_id}/recurring-findings",
    response_model=list[RecurringFindingResponse],
)
def recurring_findings(
    project_id: UUID,
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_db),
) -> list[RecurringFindingResponse]:
    """Which findings this project keeps producing, most frequent first.

    A separate shape from the raw history on purpose. The question is not "what
    did the reviewer say" but "what does this project keep getting wrong", and
    answering that from the history means reading every review by hand.
    """
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    reviews = ReviewRepository(session).list_for_project(project_id, limit=1000)
    return [
        RecurringFindingResponse(**entry)
        for entry in metrics_service.recurring_findings(reviews, limit=limit)
    ]


# -------------------------------------------------------------------- training


@router.get("/projects/{project_id}/training", response_model=list[TrainingExampleResponse])
def list_training_examples(
    project_id: UUID,
    status: TrainingStatus | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> list[TrainingExampleResponse]:
    """The preserved examples for a project, newest first.

    ``status`` is unset by default and unset means every curation state. Most
    rows will be ``CAPTURED``: section 34 says not to train on every accepted
    example, and V1 has no curation process to move them on.

    A project that does not exist is a 404 rather than an empty list, as it is
    for every other project-scoped route here. An unknown project and a project
    that has captured nothing are different answers.
    """
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    return [
        TrainingExampleResponse.from_domain(example)
        for example in training_service.list_training_examples(
            session, project_id, status=status, limit=limit
        )
    ]


@router.get(
    "/projects/{project_id}/training/queue",
    response_model=list[TrainingExampleResponse],
)
def training_curation_queue(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> list[TrainingExampleResponse]:
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    return [
        TrainingExampleResponse.from_domain(example)
        for example in training_service.ranked_training_queue(
            session, project_id, limit=limit
        )
    ]


@router.post(
    "/training/{example_id}/select", response_model=TrainingExampleResponse
)
def select_training_example(
    example_id: UUID, session: Session = Depends(get_db)
) -> TrainingExampleResponse:
    return TrainingExampleResponse.from_domain(
        training_service.curate_training_example(
            session, example_id, status=TrainingStatus.SELECTED
        )
    )


@router.post(
    "/training/{example_id}/exclude", response_model=TrainingExampleResponse
)
def exclude_training_example(
    example_id: UUID,
    payload: ExcludeTrainingExampleRequest,
    session: Session = Depends(get_db),
) -> TrainingExampleResponse:
    return TrainingExampleResponse.from_domain(
        training_service.curate_training_example(
            session,
            example_id,
            status=TrainingStatus.EXCLUDED,
            reason=payload.reason,
        )
    )


@router.get("/runs/{run_id}/training", response_model=TrainingExampleResponse | None)
def run_training_example(
    run_id: UUID, session: Session = Depends(get_db)
) -> TrainingExampleResponse | None:
    """The example captured for a run, or ``null`` if the run was not accepted.

    Null rather than 404 for a run that exists but was not captured: "this run
    was rejected" is a real answer, and a rejected run is not a client error. A
    run that does not exist at all is still a 404.
    """
    if TaskRunRepository(session).get(run_id) is None:
        raise EntityNotFound("Run", run_id)
    example = TrainingExampleRepository(session).get_for_run(run_id)
    return TrainingExampleResponse.from_domain(example) if example else None


@router.post("/runs/{run_id}/training/capture", response_model=TrainingExampleResponse)
def capture_run_training_example(
    run_id: UUID, session: Session = Depends(get_db)
) -> TrainingExampleResponse:
    """Capture an accepted run's artefacts now.

    Capture normally happens as part of delivery. This exists for a backfill --
    a project registered before phase L, or a run delivered while capture was
    failing -- and refuses a run that is not ``SUCCEEDED``, because a rejected
    run is not an example.
    """
    return TrainingExampleResponse.from_domain(
        training_service.capture_training_example(session, run_id)
    )


# --------------------------------------------------------------------- metrics


@router.get("/projects/{project_id}/metrics", response_model=ProjectMetricsResponse)
def project_metrics(project_id: UUID, session: Session = Depends(get_db)) -> ProjectMetricsResponse:
    """Everything section 35 asks for, for one project.

    All derived from the rows the work produced. Nothing here is a stored
    counter, so every figure can be re-derived and will not drift from the runs
    it describes.
    """
    return ProjectMetricsResponse(**metrics_service.project_metrics(session, project_id))


@router.get("/tasks/{task_id}/metrics", response_model=RunMetricsResponse)
def task_metrics(task_id: UUID, session: Session = Depends(get_db)) -> RunMetricsResponse:
    """Section 35's run figures for one task, across its retries.

    A task with no runs is a real answer -- a task that has not been picked up
    yet -- but a task that does not exist is a 404, and would otherwise be
    indistinguishable from one.
    """
    if TaskRepository(session).get(task_id) is None:
        raise EntityNotFound("Task", task_id)
    return RunMetricsResponse(
        **metrics_service.task_metrics(session, list_runs_for_task(session, task_id)).describe()
    )


@router.get("/models/metrics", response_model=ModelComparisonResponse)
def model_comparison(
    project_id: UUID | None = None, session: Session = Depends(get_db)
) -> ModelComparisonResponse:
    """Per-model cost and outcome, installation-wide or for one project.

    The comparison section 35 is really after: which registered model is cheaper
    per accepted task. The per-purpose breakdown is included because a model that
    is only ever asked to fix things looks cheap on an average that mixes first
    attempts with corrections.
    """
    return ModelComparisonResponse(**metrics_service.model_metrics(session, project_id))


@router.get("/projects/{project_id}/lesson-metrics", response_model=LessonMetricsResponse)
def project_lesson_metrics(
    project_id: UUID, session: Session = Depends(get_db)
) -> LessonMetricsResponse:
    """Lesson counts by status, and how many approved ones nobody has used."""
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    return LessonMetricsResponse(**metrics_service.lesson_metrics(session, project_id))


@router.get(
    "/projects/{project_id}/training-metrics", response_model=TrainingMetricsResponse
)
def project_training_metrics(
    project_id: UUID, session: Session = Depends(get_db)
) -> TrainingMetricsResponse:
    """What has been captured for a project, by curation state."""
    if ProjectRepository(session).get(project_id) is None:
        raise EntityNotFound("Project", project_id)
    return TrainingMetricsResponse(**metrics_service.training_metrics(session, project_id))


__all__ = ["router"]
