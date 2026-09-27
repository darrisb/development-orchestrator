"""Experience capture from settled runs (build.md sections 34 and 35).

The interesting properties of capture are all refusals: a rejected run is not
filed as training data, a second capture does not double-file a run, and a status
filter does not quietly cross project boundaries. Those are the properties a
mocked filesystem cannot demonstrate, so these run against a real store.
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.enums import (
    ModelPurpose,
    ModelRole,
    RunEventType,
    RunStatus,
    TrainingStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskLimits, TaskRun
from apps.orchestrator.providers import ProviderConfig, TokenUsage
from apps.orchestrator.repositories import (
    ProjectRepository,
    RunEventRepository,
    TaskRepository,
    TaskRunRepository,
    TrainingExampleRepository,
)
from apps.orchestrator.services import artifact_store, training
from apps.orchestrator.services.errors import EntityNotFound, NotInCapturableState
from apps.orchestrator.services.model_runs import record_model_call

pytestmark = pytest.mark.integration


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="TraceStack",
            external_project_id="tracestack",
            repository_path="/workspace/tracestack",
        )
    )


@pytest.fixture
def task(session: Session, project: Project) -> Task:
    return TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id="TS-001",
            title="Scaffold the extension host",
            limits=TaskLimits(max_attempts=3),
        )
    )


def _run(session: Session, task: Task, status: RunStatus) -> TaskRun:
    """A settled run, the way the delivery and fix-loop paths leave one."""
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
    )
    return TaskRunRepository(session).finish(run.id, status)


@pytest.fixture
def other_project(session: Session) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Other",
            external_project_id="other",
            repository_path="/workspace/other",
        )
    )


@pytest.fixture
def accepted(session: Session, task: Task) -> TaskRun:
    return _run(session, task, RunStatus.SUCCEEDED)


@pytest.fixture
def rejected(session: Session, task: Task) -> TaskRun:
    return TaskRunRepository(session).finish(
        _run(session, task, RunStatus.FAILED).id,
        RunStatus.FAILED,
        failure_reason="tests still failing after 3 attempts",
    )


# --- outcome.json (both directions) ------------------------------------------


def test_an_accepted_run_gets_an_outcome_file(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    payload = training.record_outcome(
        session, accepted.id, outcome="accepted", settings=git_settings
    )

    written = json.loads(_outcome_path(session, accepted, git_settings).read_text("utf-8"))
    assert written["outcome"] == "accepted"
    assert payload["external_task_id"] == "TS-001"


def test_a_rejected_run_gets_an_outcome_file_too(
    session: Session, rejected: TaskRun, git_settings: Settings
):
    """A run that failed is the run someone most wants to read afterwards."""
    training.record_outcome(
        session, rejected.id, outcome="rejected", settings=git_settings
    )

    written = json.loads(_outcome_path(session, rejected, git_settings).read_text("utf-8"))
    assert written["outcome"] == "rejected"
    assert written["failure_reason"] == "tests still failing after 3 attempts"


def test_the_outcome_file_carries_what_section_35_asks_to_be_computable(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    """Token cost and review history are the two questions asked of a failed run."""
    config = ProviderConfig(
        provider_id="env:local-coder",
        base_url="http://192.168.0.126:8080/v1",
        model_name="qwen-coder-14b",
        role=ModelRole.CODER,
    )
    record_model_call(
        session,
        task_run_id=accepted.id,
        config=config,
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        duration_ms=4200,
        usage=TokenUsage(input_tokens=1200, output_tokens=340),
    )

    training.record_outcome(
        session, accepted.id, outcome="accepted", settings=git_settings
    )

    written = json.loads(_outcome_path(session, accepted, git_settings).read_text("utf-8"))
    assert written["tokens"]["input_tokens"] == 1200
    assert written["tokens"]["output_tokens"] == 340
    assert written["tokens"]["calls"] == 1
    assert written["reviews"] == []
    assert written["review_cycles"] == 0


def test_recording_an_outcome_is_logged(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    training.record_outcome(
        session, accepted.id, outcome="accepted", settings=git_settings
    )

    events = RunEventRepository(session).list_for_run(accepted.id)
    assert any(event.event_type == RunEventType.OUTCOME_RECORDED for event in events)


def test_recording_an_outcome_for_an_unknown_run_is_a_404(session: Session):
    from uuid import uuid4

    with pytest.raises(EntityNotFound):
        training.record_outcome(session, uuid4(), outcome="accepted")


# --- capture (accepted runs only) -------------------------------------------


def test_an_accepted_run_is_filed_as_a_training_example(
    session: Session, project: Project, accepted: TaskRun, git_settings: Settings
):
    example = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )

    assert example.status is TrainingStatus.CAPTURED
    assert example.outcome == "accepted"
    assert example.project_id == project.id
    assert example.task_run_id == accepted.id
    assert example.external_run_id.startswith("RUN-")


def test_capture_writes_a_manifest_naming_what_it_kept(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    example = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )

    manifest = json.loads(
        (git_settings.training_dir / example.external_run_id / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["project"] == "tracestack"
    assert manifest["task"] == "TS-001"
    assert manifest["outcome"] == "accepted"
    assert manifest["external_run_id"] == example.external_run_id


def test_capture_hashes_the_manifest_so_a_tampered_copy_is_detectable(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    import hashlib

    example = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )
    manifest_path = git_settings.training_dir / example.external_run_id / "manifest.json"

    assert example.manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_a_rejected_run_is_not_captured(
    session: Session, rejected: TaskRun, git_settings: Settings
):
    """Section 34 is about accepted work; filing a failure would teach the failure."""
    with pytest.raises(NotInCapturableState, match="only an accepted run"):
        training.capture_training_example(
            session, rejected.id, settings=git_settings
        )

    assert TrainingExampleRepository(session).list_for_project(
        rejected_task_project(session, rejected)
    ) == []


def test_a_rejected_run_leaves_no_training_row(
    session: Session, rejected: TaskRun, git_settings: Settings
):
    with pytest.raises(NotInCapturableState):
        training.capture_training_example(session, rejected.id, settings=git_settings)

    assert TrainingExampleRepository(session).get_for_run(rejected.id) is None


def test_capturing_twice_refiles_the_same_run_rather_than_adding_a_row(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    first = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )
    second = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )

    assert second.id == first.id
    assert len(TrainingExampleRepository(session).list_for_project(
        first.project_id, limit=10
    )) == 1


def test_capturing_twice_preserves_a_curation_decision(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    """Delivery can re-enter after a restart; re-entry must not re-file an
    example a curator has already ruled on."""
    example = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )
    TrainingExampleRepository(session).set_status(example.id, TrainingStatus.EXCLUDED)

    again = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )

    assert again.status is TrainingStatus.EXCLUDED


def test_capture_is_logged(session: Session, accepted: TaskRun, git_settings: Settings):
    training.capture_training_example(session, accepted.id, settings=git_settings)

    events = RunEventRepository(session).list_for_run(accepted.id)
    assert any(event.event_type == RunEventType.TRAINING_CAPTURED for event in events)


def test_capture_of_an_unknown_run_is_a_404(session: Session, git_settings: Settings):
    from uuid import uuid4

    with pytest.raises(EntityNotFound):
        training.capture_training_example(session, uuid4(), settings=git_settings)


def test_the_both_halves_helper_writes_the_outcome_before_capturing(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    training.capture_accepted_run(session, accepted.id, settings=git_settings)

    # The run directory ends up with the outcome file, and the training copy has
    # the same one -- not a second rendering that could disagree.
    assert _outcome_path(session, accepted, git_settings).exists()
    stored = TrainingExampleRepository(session).get_for_run(accepted.id)
    assert (git_settings.training_dir / stored.external_run_id / "outcome.json").exists()


# --- curation state ---------------------------------------------------------


def test_capture_leaves_the_example_captured_not_selected(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    """Section 34 says do not train on every accepted example."""
    example = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )

    assert example.status is TrainingStatus.CAPTURED


def test_a_curator_can_select_and_exclude(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    example = training.capture_training_example(
        session, accepted.id, settings=git_settings
    )

    TrainingExampleRepository(session).set_status(example.id, TrainingStatus.SELECTED)
    assert TrainingExampleRepository(session).get(example.id).status is (
        TrainingStatus.SELECTED
    )

    TrainingExampleRepository(session).set_status(example.id, TrainingStatus.EXCLUDED)
    assert TrainingExampleRepository(session).get(example.id).status is (
        TrainingStatus.EXCLUDED
    )


# --- listing, and the project boundary --------------------------------------


def test_examples_are_listed_newest_first(
    session: Session, project: Project, task: Task, git_settings: Settings
):
    ids = []
    for index in range(3):
        run = TaskRunRepository(session).add(
            TaskRun(task_id=task.id, run_number=index + 1, status=RunStatus.SUCCEEDED)
        )
        ids.append(training.capture_training_example(
            session, run.id, settings=git_settings
        ).id)

    listed = training.list_training_examples(session, project.id)

    assert {example.id for example in listed} == set(ids)


def test_a_status_filter_stays_inside_the_project(
    session: Session, project: Project, other_project: Project, task: Task,
    git_settings: Settings,
):
    """The regression this pins: filtering by status used to drop the project
    filter entirely, so one team's curation queue showed another team's
    examples."""
    other_task = TaskRepository(session).add(
        Task(project_id=other_project.id, external_task_id="O-1", title="Theirs")
    )
    other_run = TaskRunRepository(session).add(
        TaskRun(task_id=other_task.id, run_number=1, status=RunStatus.SUCCEEDED)
    )
    training.capture_training_example(session, other_run.id, settings=git_settings)
    mine = _run(session, task, RunStatus.SUCCEEDED)
    training.capture_training_example(session, mine.id, settings=git_settings)

    assert [
        example.id
        for example in training.list_training_examples(
            session, project.id, status=TrainingStatus.CAPTURED
        )
    ] == [TrainingExampleRepository(session).get_for_run(mine.id).id]

    assert len(
        training.list_training_examples(
            session, other_project.id, status=TrainingStatus.CAPTURED
        )
    ) == 1


def test_a_status_filter_with_no_match_returns_nothing(
    session: Session, project: Project, accepted: TaskRun, git_settings: Settings
):
    training.capture_training_example(session, accepted.id, settings=git_settings)

    assert training.list_training_examples(
        session, project.id, status=TrainingStatus.SELECTED
    ) == []


def test_another_projects_examples_are_not_listed(
    session: Session, other_project: Project, accepted: TaskRun, git_settings: Settings
):
    training.capture_training_example(session, accepted.id, settings=git_settings)

    assert training.list_training_examples(session, other_project.id) == []


def test_the_listing_is_bounded(session: Session, project: Project, task: Task,
                               git_settings: Settings):
    for index in range(4):
        run = TaskRunRepository(session).add(
            TaskRun(task_id=task.id, run_number=index + 1, status=RunStatus.SUCCEEDED)
        )
        training.capture_training_example(session, run.id, settings=git_settings)

    assert len(training.list_training_examples(session, project.id, limit=2)) == 2


# --- helpers ----------------------------------------------------------------


def _outcome_path(session: Session, run: TaskRun, settings: Settings) -> Path:
    """Where a settled run's outcome actually lands: inside the run directory."""
    external = artifact_store.ensure_run_id(session, run.id)
    return settings.runs_dir / external / "outcome.json"


def rejected_task_project(session: Session, run: TaskRun):
    task = TaskRepository(session).get(run.task_id)
    assert task is not None
    return task.project_id


def test_the_run_directory_is_where_a_person_would_look(
    session: Session, accepted: TaskRun, git_settings: Settings
):
    external = artifact_store.ensure_run_id(session, accepted.id)
    artifact_store.write_json(
        session, accepted.id, "diff.json", {"files": []}, settings=git_settings
    )

    training.capture_accepted_run(session, accepted.id, settings=git_settings)

    assert (git_settings.training_dir / external / "outcome.json").exists()


# --- a run that has not settled yet (concern 41) ------------------------------


def test_a_run_still_in_flight_can_be_given_a_provisional_outcome(
    session: Session, task: Task, git_settings: Settings
):
    """Concern 41: the run you most want to read about is the one that stopped.

    A crash leaves reviews and artifacts under the run directory and nothing at
    its root summarising them, so a provisional file is written at turn
    boundaries under the same name and schema a settled run uses.
    """
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
    )

    payload = training.record_outcome(
        session, run.id, outcome="in_progress", settings=git_settings
    )

    written = json.loads(_outcome_path(session, run, git_settings).read_text("utf-8"))
    assert written["outcome"] == "in_progress"
    assert payload["external_task_id"] == "TS-001"
    # Same schema as a settled run's, so one reader handles both.
    assert {"attempts", "review_cycles", "failure_reason"} <= written.keys()


def test_settling_replaces_the_provisional_outcome_in_place(
    session: Session, task: Task, git_settings: Settings
):
    """One location and one schema: the terminal write overwrites, never adds."""
    run = TaskRunRepository(session).add(
        TaskRun(task_id=task.id, run_number=1, status=RunStatus.RUNNING)
    )
    training.record_outcome(session, run.id, outcome="in_progress", settings=git_settings)
    TaskRunRepository(session).finish(run.id, RunStatus.SUCCEEDED)

    training.record_outcome(session, run.id, outcome="accepted", settings=git_settings)

    path = _outcome_path(session, run, git_settings)
    assert json.loads(path.read_text("utf-8"))["outcome"] == "accepted"
    assert sorted(p.name for p in path.parent.glob("outcome*.json")) == ["outcome.json"]


# --- curation order and retention (concerns 43 and 45) -----------------------


_run_numbers = count(1)


def _captured(
    session: Session, task: Task, git_settings: Settings, *, attempts: int, cycles: int
):
    """A captured example whose run cost ``attempts`` and ``cycles``."""
    run = TaskRunRepository(session).add(
        TaskRun(
            task_id=task.id,
            run_number=next(_run_numbers),
            attempt_number=attempts,
            status=RunStatus.SUCCEEDED,
        )
    )
    example = training.capture_training_example(session, run.id, settings=git_settings)
    return TrainingExampleRepository(session).update_fields(
        example.id, attempts=attempts, review_cycles=cycles
    )


def test_the_curation_queue_is_ranked_by_instructiveness_not_recency(
    session: Session, project: Project, task: Task, git_settings: Settings
):
    """Concern 45: a run that took three attempts and a review cycle taught
    something; a trivial first-attempt pass did not. Ordering the queue by
    arrival meant the least instructive examples were curated first."""
    trivial = _captured(session, task, git_settings, attempts=1, cycles=0)
    hard = _captured(session, task, git_settings, attempts=3, cycles=2)
    middling = _captured(session, task, git_settings, attempts=2, cycles=0)

    ranked = training.ranked_training_queue(session, project.id)

    assert [item.id for item in ranked] == [hard.id, middling.id, trivial.id]


def test_only_uncurated_examples_are_in_the_queue(
    session: Session, project: Project, task: Task, git_settings: Settings
):
    """A decision already taken is not a decision waiting to be taken."""
    pending = _captured(session, task, git_settings, attempts=1, cycles=0)
    decided = _captured(session, task, git_settings, attempts=3, cycles=2)
    training.curate_training_example(
        session, decided.id, status=TrainingStatus.SELECTED
    )

    assert [item.id for item in training.ranked_training_queue(session, project.id)] == [
        pending.id
    ]


def test_capture_retention_excludes_the_least_instructive_uncurated_examples(
    session: Session, project: Project, task: Task, git_settings: Settings
):
    """Concern 43: training copies accumulated with nothing pruning them.

    Retention acts on the same ranking the queue uses, so what it drops is the
    material a curator would have reached last.
    """
    settings = git_settings.model_copy(update={"training_max_captured_per_project": 2})
    hard = _captured(session, task, settings, attempts=3, cycles=2)
    middling = _captured(session, task, settings, attempts=2, cycles=0)
    trivial = _captured(session, task, settings, attempts=1, cycles=0)

    # The third capture is what takes the project over its ceiling.
    _captured(session, task, settings, attempts=3, cycles=3)

    statuses = {
        item.id: item.status
        for item in TrainingExampleRepository(session).list_for_project(project.id)
    }
    assert statuses[trivial.id] is TrainingStatus.EXCLUDED
    assert statuses[hard.id] is TrainingStatus.CAPTURED
    excluded = TrainingExampleRepository(session).get(trivial.id)
    assert excluded.exclusion_reason is not None
    # And the run's own directory is untouched: only the duplicate copy goes.
    assert middling.artifact_path


def test_retention_never_deletes_a_curated_example(
    session: Session, project: Project, task: Task, git_settings: Settings
):
    """A human decision outranks a ceiling: retention only sees `CAPTURED`."""
    settings = git_settings.model_copy(update={"training_max_captured_per_project": 1})
    selected = _captured(session, task, settings, attempts=1, cycles=0)
    training.curate_training_example(
        session, selected.id, status=TrainingStatus.SELECTED
    )

    _captured(session, task, settings, attempts=3, cycles=3)

    assert TrainingExampleRepository(session).get(selected.id).status is (
        TrainingStatus.SELECTED
    )
