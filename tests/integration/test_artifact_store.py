"""Run artifact store (build.md sections 9 and 46: "artifact writing")."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.enums import TaskStatus
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.repositories import (
    ArtifactRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
)
from apps.orchestrator.services import artifact_store
from apps.orchestrator.services.artifact_store import ArtifactPathRejected
from apps.orchestrator.services.errors import EntityNotFound
from apps.orchestrator.services.runs import create_run

pytestmark = pytest.mark.integration


@pytest.fixture
def run(session: Session, tmp_path: Path) -> TaskRun:
    project = ProjectRepository(session).add(
        Project(name="Fixture", repository_path=str(tmp_path))
    )
    task = TaskRepository(session).add(
        Task(project_id=project.id, external_task_id="TS-001", title="First")
    )
    TaskRepository(session).transition(task.id, TaskStatus.READY)
    return create_run(session, task.id)


def test_a_run_gets_one_stable_identifier_and_one_directory(
    session: Session, run: TaskRun, git_settings: Settings
):
    first = artifact_store.ensure_run_id(session, run.id)
    second = artifact_store.ensure_run_id(session, run.id)

    assert first == second
    assert first.startswith("RUN-")
    assert TaskRunRepository(session).get(run.id).external_run_id == first


def test_identifiers_do_not_repeat_within_a_day(session: Session, run: TaskRun):
    first = artifact_store.allocate_external_run_id(session)
    TaskRunRepository(session).update_fields(run.id, external_run_id=first)
    second = artifact_store.allocate_external_run_id(session)

    assert second != first
    assert int(second.rsplit("-", 1)[1]) == int(first.rsplit("-", 1)[1]) + 1


def test_writing_records_the_path_hash_and_size(
    session: Session, run: TaskRun, git_settings: Settings
):
    stored = artifact_store.write_text(
        session, run.id, "prompt.txt", "hello", settings=git_settings
    )

    assert stored.absolute_path.read_text() == "hello"
    assert stored.size_bytes == 5
    assert stored.relative_path.startswith("runs/RUN-")
    # The database stores a relative path: an absolute one breaks the moment
    # ARTIFACT_ROOT moves or the orchestrator runs in a container.
    assert not Path(stored.relative_path).is_absolute()

    recorded = ArtifactRepository(session).list_for_run(run.id)
    assert [artifact.kind for artifact in recorded] == ["prompt.txt"]
    assert recorded[0].sha256 == stored.sha256
    assert TaskRunRepository(session).get(run.id).artifact_path == str(
        stored.absolute_path.parent.relative_to(git_settings.artifact_root)
    )


def test_rewriting_an_artifact_updates_its_record_rather_than_adding_one(
    session: Session, run: TaskRun, git_settings: Settings
):
    artifact_store.write_text(session, run.id, "outcome.json", "{}", settings=git_settings)
    second = artifact_store.write_text(
        session, run.id, "outcome.json", '{"ok": true}', settings=git_settings
    )

    recorded = ArtifactRepository(session).list_for_run(run.id)
    assert len(recorded) == 1
    assert recorded[0].sha256 == second.sha256


def test_json_is_written_stably_so_two_identical_builds_hash_alike(
    session: Session, run: TaskRun, git_settings: Settings
):
    payload = {"b": 2, "a": [1, 2, 3]}

    first = artifact_store.write_json(session, run.id, "a.json", payload, settings=git_settings)
    second = artifact_store.write_json(
        session, run.id, "b.json", dict(reversed(list(payload.items()))), settings=git_settings
    )

    assert first.sha256 == second.sha256
    assert json.loads(first.read_text()) == payload


@pytest.mark.parametrize("name", ["../escape.txt", "/etc/passwd", "a/../../b.txt"])
def test_a_name_that_would_escape_the_run_directory_is_refused(
    session: Session, run: TaskRun, git_settings: Settings, name: str
):
    with pytest.raises(ArtifactPathRejected):
        artifact_store.write_text(session, run.id, name, "x", settings=git_settings)


def test_writing_for_a_missing_run_is_not_silently_ignored(
    session: Session, git_settings: Settings
):
    from uuid import uuid4

    with pytest.raises(EntityNotFound):
        artifact_store.write_text(session, uuid4(), "a.txt", "x", settings=git_settings)


# --- attempt directories (build.md section 9, concerns 10 and 33) ------------


def test_the_first_attempt_of_a_first_cycle_writes_section_9s_plain_names(
    run: TaskRun,
):
    assert artifact_store.attempt_prefix(run) == ""


def test_a_retry_is_filed_under_the_cycle_it_belongs_to_not_the_one_just_finished(
    run: TaskRun,
):
    """Concern 33. ``review_cycle`` counts the cycles that have *finished* --
    it is deliberately not incremented until a reviewer has answered -- so
    reading the directory name off it filed each cycle's work under the
    previous cycle's name."""
    run.attempt_number = 2
    run.review_cycle = 1

    assert artifact_store.attempt_prefix(run) == "attempt-2-cycle-2/"
    # And a caller that knows better says so.
    assert artifact_store.attempt_prefix(run, cycle=2) == "attempt-2-cycle-2/"


def test_a_retry_after_a_deterministic_failure_stays_in_the_first_cycle(
    run: TaskRun,
):
    """No reviewer was reached, so no cycle was spent. The attempt number is
    what keeps the two turns apart."""
    run.attempt_number = 2
    run.review_cycle = 0

    assert artifact_store.attempt_prefix(run) == "attempt-2-cycle-1/"


def test_no_two_turns_of_a_loop_share_a_directory(run: TaskRun):
    """Every coding attempt advances the attempt number, so a run cannot
    overwrite its own history however its cycles fall."""
    prefixes = set()
    for attempt, cycle in ((1, 0), (2, 0), (3, 1), (4, 2), (5, 2)):
        run.attempt_number = attempt
        run.review_cycle = cycle
        prefixes.add(artifact_store.attempt_prefix(run))

    assert len(prefixes) == 5
