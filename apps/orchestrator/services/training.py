"""Outcome recording and training capture (build.md sections 9, 34 and 35).

Phase L's exit condition is that accepted *and* rejected runs leave useful
history, so this module is written for the rejected case as carefully as for the
accepted one. Two artefacts come out of it, and they are different things:

* ``outcome.json`` is written for **every settled run**. It is the answer to
  "what happened to this attempt", and a failed run that leaves only a
  ``failure_reason`` on its row is not queryable history in any useful sense --
  the reason does not say which reviewer asked for what, how many cycles it
  took, or what the run left behind.
* A training example is written for **accepted runs only**, under
  ``TRAINING_ROOT``, indexed by one ``training_examples`` row.

The asymmetry is the specification's, not a shortcut. Section 34 lists what to
preserve for a *successful* task, and a rejected run's artifacts are preserved
as run artifacts instead: it is evidence about the reviewer and the fix loop,
not a demonstration of work worth copying.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..config.settings import Settings, get_settings
from ..domain.enums import ModelPurpose, RunEventType, RunStatus, TrainingStatus
from ..domain.models import Project, Review, RunEvent, Task, TaskRun, TrainingExample
from ..repositories import (
    ModelRunRepository,
    ProjectRepository,
    ReviewRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
    TrainingExampleRepository,
)
from . import artifact_store
from .errors import EntityNotFound, NotInCapturableState

logger = get_logger(__name__)

#: Files copied into a training example. A fixed list rather than a copy of the
#: run directory: the run directory also holds the coder's full context bundle
#: and the raw provider responses, and section 34 asks for the prompt, the diff,
#: the review, the outcome and the verification results -- not every byte the run
#: happened to write. Copying the lot would make each example a copy of the
#: artifact store with a second index in front of it.
CAPTURED_FILES = (
    "coder_prompt.txt",
    "coder_response.txt",
    "diff.patch",
    "review.json",
    "verification.json",
    "outcome.json",
)

#: The manifest is hashed into ``training_examples.manifest_sha256``. If a
#: captured example's bytes change after the fact -- a curation script editing a
#: response, a filesystem accident -- the hash stops matching and the example is
#: no longer the artefact the index describes. That is the only thing the index
#: has to be able to say.
MANIFEST_NAME = "manifest.json"

#: The purposes that count as coding work. Section 35 compares a model's first
#: attempts with its corrections, so the metrics service needs to know which
#: rows are coding calls, and defining it here keeps the two from disagreeing
#: about what "a coding call" is.
CODING_PURPOSES = (ModelPurpose.CODE, ModelPurpose.FIX)


# --------------------------------------------------------------------- outcome


def record_outcome(
    session: Session,
    task_run_id: UUID,
    *,
    outcome: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Write ``outcome.json`` for a run and log ``OUTCOME_RECORDED``.

    ``in_progress`` is written at durable turn boundaries and terminal calls
    atomically replace it with ``accepted``, ``rejected`` or ``escalated``.
    The payload is the same shape either way, so post-crash inspection has one
    stable location and schema.

    Raises:
        EntityNotFound: no such run.
    """
    run = TaskRunRepository(session).get(task_run_id)
    if run is None:
        raise EntityNotFound("Run", task_run_id)
    task = TaskRepository(session).get(run.task_id)
    if task is None:
        raise EntityNotFound("Task", run.task_id)
    project = ProjectRepository(session).get(task.project_id)
    if project is None:
        raise EntityNotFound("Project", task.project_id)

    reviews = ReviewRepository(session).list_for_run(run.id)
    payload = _outcome_payload(session, run, task, project, reviews, outcome=outcome)
    artifact_store.write_json(
        session,
        run.id,
        "outcome.json",
        payload,
        kind="outcome",
        settings=settings,
    )
    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=task.project_id,
            task_id=task.id,
            event_type=RunEventType.OUTCOME_RECORDED,
            attempt=run.attempt_number,
            payload={
                "outcome": outcome,
                "review_cycles": payload["review_cycles"],
                "attempts": payload["attempts"],
                "failure_reason": run.failure_reason,
            },
        )
    )
    logger.info(
        "outcome_recorded",
        run_id=str(run.id),
        task=task.external_task_id,
        outcome=outcome,
        review_cycles=payload["review_cycles"],
    )
    return payload


def _outcome_payload(
    session: Session,
    run: TaskRun,
    task: Task,
    project: Project,
    reviews: list[Review],
    *,
    outcome: str,
) -> dict[str, Any]:
    """The contents of ``outcome.json``.

    Includes the review history and the token cost, because those are the two
    things section 35 wants to be able to compute later and the two things a
    person reading a failed run actually asks about. The model-run rows stay
    authoritative; the numbers here are the same ones, denormalised so the file
    is useful on its own once the database is gone.
    """
    usage = _token_totals(session, run.id)
    return {
        "run_id": str(run.id),
        "external_run_id": run.external_run_id,
        "project_id": str(project.id),
        "external_project_id": project.external_project_id,
        "task_id": str(task.id),
        "external_task_id": task.external_task_id,
        "outcome": outcome,
        "run_status": run.status.value,
        "failure_reason": run.failure_reason,
        "attempts": run.attempt_number,
        "review_cycles": run.review_cycle,
        "coder_model_id": str(run.coder_model_id) if run.coder_model_id else None,
        "prompt_version": run.prompt_version,
        "starting_commit": run.starting_commit,
        "candidate_commit": run.candidate_commit,
        "duration_ms": _duration_ms(run),
        "tokens": usage,
        "reviews": [
            {
                "cycle": review.cycle,
                "decision": review.decision.value,
                "reviewer_model": review.reviewer_model,
                "confidence": review.confidence,
                "risk": review.risk.value if review.risk else None,
                "summary": review.summary,
                "issue_count": len(review.issues),
                "blocking_issues": len(review.blocking_issues),
                "issues": [
                    {
                        "id": str(issue.id),
                        "severity": issue.severity.value,
                        "category": issue.category.value,
                        "file": issue.file,
                        "line": issue.line,
                        "requirement_id": issue.requirement_id,
                        "problem": issue.problem,
                        "required_fix": issue.required_fix,
                        "resolved": issue.resolved,
                    }
                    for issue in review.issues
                ],
            }
            for review in reviews
        ],
    }


def _token_totals(session: Session, task_run_id: UUID) -> dict[str, int]:
    runs = ModelRunRepository(session).list_for_run(task_run_id)
    return {
        "calls": len(runs),
        "input_tokens": sum(run.input_tokens or 0 for run in runs),
        "output_tokens": sum(run.output_tokens or 0 for run in runs),
        "duration_ms": sum(run.duration_ms or 0 for run in runs),
    }


def _duration_ms(run: TaskRun) -> int | None:
    if run.started_at is None or run.completed_at is None:
        return None
    return int((run.completed_at - run.started_at).total_seconds() * 1000)


# --------------------------------------------------------------------- capture


def capture_training_example(
    session: Session,
    task_run_id: UUID,
    *,
    outcome: str = "accepted",
    settings: Settings | None = None,
) -> TrainingExample:
    """Preserve an accepted run's artefacts and index them (section 34).

    Idempotent per run: a second call rewrites the same directory and the same
    row. Capture is called from the delivery path, which a restart can re-enter,
    and a run must not end up with two rows claiming to be the same accepted
    work.

    Raises:
        EntityNotFound: no such run or task.
        NotInCapturableState: the run is not in a capturable state.
    """
    config = settings or get_settings()
    run = TaskRunRepository(session).get(task_run_id)
    if run is None:
        raise EntityNotFound("Run", task_run_id)
    task = TaskRepository(session).get(run.task_id)
    if task is None:
        raise EntityNotFound("Task", run.task_id)
    project = ProjectRepository(session).get(task.project_id)
    if project is None:
        raise EntityNotFound("Project", task.project_id)
    if run.status is not RunStatus.SUCCEEDED:
        # Section 34 is about accepted work. A rejected run's artefacts are
        # still on disk under the run directory; filing it as a training
        # example would teach a model to produce the run that got rejected.
        # A state problem rather than a bad argument, so it is reported as one:
        # 409 through the shared translation layer instead of a 500 from an
        # unhandled ValueError.
        raise NotInCapturableState(
            f"Run {task_run_id} is {run.status}; only an accepted run is captured "
            "for training (build.md section 34)"
        )
    external_run_id = artifact_store.ensure_run_id(session, run.id)

    destination = config.training_dir / external_run_id
    destination.mkdir(parents=True, exist_ok=True)
    source = artifact_store.run_directory(external_run_id, settings=config)
    copied = _copy_artifacts(source, destination)
    reviews = ReviewRepository(session).list_for_run(run.id)
    manifest = {
        "run_id": str(run.id),
        "external_run_id": external_run_id,
        "project": project.external_project_id,
        "task": task.external_task_id,
        "outcome": outcome,
        "prompt_version": run.prompt_version,
        "coder_model_id": str(run.coder_model_id) if run.coder_model_id else None,
        "reviewer_model": reviews[-1].reviewer_model if reviews else None,
        "attempts": run.attempt_number,
        "review_cycles": run.review_cycle,
        "files": copied,
    }
    manifest_path = destination / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    relative = destination.relative_to(config.artifact_root).as_posix()
    usage = _token_totals(session, run.id)
    example = TrainingExample(
        task_run_id=run.id,
        project_id=task.project_id,
        task_id=task.id,
        external_project_id=project.external_project_id or project.name,
        external_task_id=task.external_task_id,
        external_run_id=external_run_id,
        artifact_path=relative,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        outcome=outcome,
        coder_model_id=run.coder_model_id,
        reviewer_model=reviews[-1].reviewer_model if reviews else None,
        prompt_version=run.prompt_version,
        attempts=run.attempt_number,
        review_cycles=run.review_cycle,
        duration_ms=usage["duration_ms"] or None,
        input_tokens=usage["input_tokens"] or None,
        output_tokens=usage["output_tokens"] or None,
        status=TrainingStatus.CAPTURED,
    )
    stored = TrainingExampleRepository(session).get_for_run(run.id)
    if stored is None:
        stored = TrainingExampleRepository(session).add(example)
    else:
        stored = _update_existing(TrainingExampleRepository(session), stored, example)

    RunEventRepository(session).append(
        RunEvent(
            task_run_id=run.id,
            project_id=task.project_id,
            task_id=task.id,
            event_type=RunEventType.TRAINING_CAPTURED,
            attempt=run.attempt_number,
            payload={
                "example_id": str(stored.id),
                "artifact_path": relative,
                "manifest_sha256": stored.manifest_sha256,
                "files": copied,
                "status": stored.status.value,
            },
        )
    )
    logger.info(
        "training_example_captured",
        run_id=str(run.id),
        example_id=str(stored.id),
        external_run_id=external_run_id,
        artifact_path=relative,
        files=len(copied),
    )
    _enforce_capture_retention(session, task.project_id, config=config)
    return stored


def _update_existing(
    repository: TrainingExampleRepository, stored: TrainingExample, fresh: TrainingExample
) -> TrainingExample:
    """Refresh an existing example rather than adding a second row for a run.

    The curation state is deliberately *not* carried over: a person who excluded
    an example from curation, and then a restart recaptured the run, must not
    have their decision silently reverted to ``captured``.
    """
    return repository.update_fields(
        stored.id,
        artifact_path=fresh.artifact_path,
        manifest_sha256=fresh.manifest_sha256,
        outcome=fresh.outcome,
        coder_model_id=fresh.coder_model_id,
        reviewer_model=fresh.reviewer_model,
        prompt_version=fresh.prompt_version,
        attempts=fresh.attempts,
        review_cycles=fresh.review_cycles,
        duration_ms=fresh.duration_ms,
        input_tokens=fresh.input_tokens,
        output_tokens=fresh.output_tokens,
    )


def _copy_artifacts(source: Path, destination: Path) -> dict[str, dict[str, object]]:
    """Copy the named artefacts in, reporting which were present and hashed.

    A missing file is recorded as absent rather than treated as an error. The
    names in ``CAPTURED_FILES`` are the section 34 list, and which of them exist
    depends on how the run went: a run that passed first time has no fix diff to
    record, and a failed verification has no review. Failing the whole capture
    over that would mean the runs with the most to teach are the ones that lose
    their evidence.
    """
    copied: dict[str, dict[str, object]] = {}
    for name in CAPTURED_FILES:
        origin = source / name
        if not origin.is_file():
            copied[name] = {"present": False}
            continue
        target = destination / name
        shutil.copy2(origin, target)
        data = target.read_bytes()
        copied[name] = {
            "present": True,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
    return copied


def capture_accepted_run(
    session: Session,
    task_run_id: UUID,
    *,
    settings: Settings | None = None,
) -> TrainingExample:
    """Both halves, in the order a person reading a run directory expects them.

    ``outcome.json`` is written into the run directory first, so the file copied
    into the training example is the same artefact the run is left with, rather
    than a second rendering of it.
    """
    record_outcome(session, task_run_id, outcome="accepted", settings=settings)
    return capture_training_example(
        session, task_run_id, outcome="accepted", settings=settings
    )


def list_training_examples(
    session: Session,
    project_id: UUID,
    *,
    status: TrainingStatus | None = None,
    limit: int = 100,
) -> list[TrainingExample]:
    """The captured examples for one project, newest first.

    ``status=None`` lists every curation state, which is what a project view
    wants; "the last hundred examples" and "the last hundred captured examples"
    are different questions.

    The project is enforced on both branches. A ``status`` filter that dropped
    the project would answer "which examples are captured?" across the whole
    deployment, which is not the question this function takes a ``project_id``
    to ask.
    """
    if status is not None:
        return TrainingExampleRepository(session).list_by_status(
            status, project_id=project_id, limit=limit
        )
    return TrainingExampleRepository(session).list_for_project(project_id, limit=limit)


def ranked_training_queue(
    session: Session, project_id: UUID, *, limit: int = 100
) -> list[TrainingExample]:
    """Captured examples ranked by instructiveness, not arrival time."""
    examples = TrainingExampleRepository(session).list_by_status(
        TrainingStatus.CAPTURED, project_id=project_id, limit=1000
    )
    return sorted(
        examples,
        key=lambda item: (
            -_instructiveness(item),
            item.created_at.isoformat() if item.created_at else "",
            str(item.id),
        ),
    )[:limit]


def _instructiveness(example: TrainingExample) -> int:
    return example.review_cycles * 3 + max(0, example.attempts - 1) * 2


def _enforce_capture_retention(
    session: Session, project_id: UUID, *, config: Settings
) -> None:
    """Bound duplicate training copies without deleting authoritative runs."""
    repository = TrainingExampleRepository(session)
    captured = repository.list_by_status(
        TrainingStatus.CAPTURED, project_id=project_id, limit=100_000
    )
    excess = len(captured) - config.training_max_captured_per_project
    if excess <= 0:
        return
    least_useful = sorted(
        captured,
        key=lambda item: (
            _instructiveness(item),
            item.created_at.isoformat() if item.created_at else "",
            str(item.id),
        ),
    )[:excess]
    for example in least_useful:
        repository.set_status(
            example.id,
            TrainingStatus.EXCLUDED,
            reason="automatic retention: lower-ranked uncurated example",
        )
        directory = config.artifact_root / example.artifact_path
        if directory.is_dir() and directory.is_relative_to(config.training_dir):
            shutil.rmtree(directory)


def curate_training_example(
    session: Session,
    example_id: UUID,
    *,
    status: TrainingStatus,
    reason: str | None = None,
) -> TrainingExample:
    """Apply an explicit human selection/exclusion decision."""
    if status not in (TrainingStatus.SELECTED, TrainingStatus.EXCLUDED):
        raise ValueError("curation status must be selected or excluded")
    if TrainingExampleRepository(session).get(example_id) is None:
        raise EntityNotFound("Training example", example_id)
    return TrainingExampleRepository(session).set_status(
        example_id, status, reason=reason
    )


__all__ = [
    "CAPTURED_FILES",
    "CODING_PURPOSES",
    "MANIFEST_NAME",
    "capture_accepted_run",
    "capture_training_example",
    "list_training_examples",
    "ranked_training_queue",
    "curate_training_example",
    "record_outcome",
]
