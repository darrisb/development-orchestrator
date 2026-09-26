"""Phase L's exit condition: the loop that landed is the loop that taught.

Everything else in phase L is tested against a service. This file runs the whole
thing -- a real worktree, a real Git commit, a scripted coder and a scripted
reviewer through LangGraph -- and asks the question the unit tests cannot: does
the capture hook in delivery actually fire, on a run that was accepted for real
reasons, and does a fix cycle that corrected a finding produce a lesson?

A hook that never fires is invisible to every other test here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import Complexity, LessonStatus, TaskStatus, WorkerProfile
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    LessonRepository,
    ProjectRepository,
    TaskRepository,
    TaskRunRepository,
    TrainingExampleRepository,
)
from apps.orchestrator.workflow import WorkflowRunner
from tests.conftest import run_git
from tests.integration.test_fix_loop import (
    _MISSING_GUARD,
    BROKEN,
    REVIEWED,
    STUB,
    WORKING,
    ScriptedModel,
    _code,
    _review,
    reviewer,
)

pytestmark = pytest.mark.integration


def _build(tmp_path: Path, *, require_guard: bool) -> tuple[sessionmaker, Settings, Project, Task]:
    """A project, its task, and a suite that either demands the null guard or
    is already satisfied by the first candidate.

    Both exist because they are the two shapes phase L has to tell apart: a run
    that passed first time has no review cycle and so nothing to learn from,
    while a run that had to be corrected twice is the only thing that can.
    """
    repository = _repository(tmp_path, require_guard=require_guard)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'experience.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
    )
    with factory.begin() as session:
        project = ProjectRepository(session).add(
            Project(
                name="fixture",
                repository_path=str(repository),
                worker_profile=WorkerProfile.PYTHON,
                verification=VerificationProfile(tests=("python3 tools/test.py",)),
            )
        )
        task = TaskRepository(session).add(
            Task(
                project_id=project.id,
                external_task_id="TS-004",
                title="implement navigation",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
            )
        )
        TaskRepository(session).transition(task.id, TaskStatus.READY)
    return factory, settings, project, task


def _repository(tmp_path: Path, *, require_guard: bool) -> Path:
    """A committed repository. The suite is part of the initial commit rather
    than written afterwards, because a worktree that starts dirty is refused."""
    path = tmp_path / "project"
    (path / "src").mkdir(parents=True)
    (path / "tools").mkdir()
    (path / "src" / "nav.py").write_text(STUB, encoding="utf-8")
    (path / "tools" / "test.py").write_text(
        "assert 'raise ValueError' in open('src/nav.py').read()\n"
        if require_guard
        else "assert 'return target' in open('src/nav.py').read()\n",
        encoding="utf-8",
    )
    run_git(path, "init", "--initial-branch=main", "--quiet")
    run_git(path, "add", "-A")
    run_git(path, "commit", "--quiet", "-m", "initial")
    return path


@pytest.fixture
def harness(tmp_path: Path):
    """A suite that rejects anything without the null guard."""
    yield _build(tmp_path, require_guard=True)


@pytest.fixture
def passing_harness(tmp_path: Path):
    """A suite the first correct candidate already satisfies."""
    yield _build(tmp_path, require_guard=False)


@pytest.mark.asyncio
async def test_a_delivered_run_is_captured_and_its_outcome_recorded(passing_harness):
    factory, settings, project, task = passing_harness

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-004")),
        settings=settings,
    )
    state = await runner.run_task(task.id)
    assert state["outcome"] == "COMPLETED"

    with factory() as session:
        runs = TaskRunRepository(session).list_for_task(task.id)
        example = TrainingExampleRepository(session).get_for_run(runs[0].id)
        external_run_id = runs[0].external_run_id

    # The run landed, and it is also evidence.
    assert example is not None
    assert example.outcome == "accepted"
    assert example.status.value == "captured"
    assert example.project_id == project.id

    # Its artifacts were copied, with a manifest naming what was kept.
    training_dir = settings.training_dir / external_run_id
    assert (training_dir / "manifest.json").exists()
    manifest = json.loads((training_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["outcome"] == "accepted"
    assert manifest["external_run_id"] == external_run_id

    # And the outcome file is in the run directory itself, because that is where
    # somebody reading a finished run will look.
    assert (settings.runs_dir / external_run_id / "outcome.json").exists()
    outcome = json.loads(
        (settings.runs_dir / external_run_id / "outcome.json").read_text(encoding="utf-8")
    )
    assert outcome["outcome"] == "accepted"
    assert outcome["external_task_id"] == "TS-004"


@pytest.mark.asyncio
async def test_a_run_that_passed_first_time_teaches_nothing(passing_harness):
    """No fix cycle means no verified finding, and rule 1 says do not guess.

    This run's suite passes on the first attempt, so there is no review to learn
    from even though the run was accepted.
    """
    factory, settings, project, task = passing_harness

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-004")),
        settings=settings,
    )
    state = await runner.run_task(task.id)
    assert state["outcome"] == "COMPLETED"

    with factory() as session:
        assert LessonRepository(session).list_by_status(LessonStatus.PROPOSED) == []
        # The run was still captured: accepted work is evidence whether or not
        # anything was learned from it.
        runs = TaskRunRepository(session).list_for_task(task.id)
        assert TrainingExampleRepository(session).get_for_run(runs[0].id) is not None


@pytest.mark.asyncio
async def test_a_corrected_finding_becomes_a_proposed_lesson(passing_harness):
    """The one path that produces a lesson, end to end.

    Three turns, and each is necessary. The first candidate fails the suite and
    teaches nothing, because nothing was learned about the design. The second
    passes the suite and is sent back by the reviewer. Only what the third turn
    fixed is a lesson -- which is rule 2, and it is why this test needs a real
    review cycle rather than just a failed test.
    """
    factory, settings, project, task = passing_harness

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(
            _code(BROKEN),  # the suite rejects it
            _code(WORKING),  # verified, but the reviewer wants the guard
            _code(REVIEWED),  # the fix
        ),
        reviewer=reviewer(
            _review(
                decision="CHANGES_REQUESTED",
                summary="A null target is still accepted.",
                issues=[_MISSING_GUARD],
            ),
            _review(summary="The guard rejects a null target."),
        ),
        settings=settings,
    )
    state = await runner.run_task(task.id)
    assert state["outcome"] == "COMPLETED"

    with factory() as session:
        proposed = LessonRepository(session).list_by_status(LessonStatus.PROPOSED)

    assert len(proposed) == 1
    lesson = proposed[0]
    assert lesson.project_id == project.id
    # Rule 3: the person approving this can see what it was distilled from.
    assert lesson.source_review_issue_id is not None
    assert lesson.requirement_id == "TS-004-R2"
    assert lesson.source_file == "src/nav.py"
    assert "raise" in lesson.lesson.lower()
    # And it is not in front of any coder yet.
    assert not lesson.is_retrievable


@pytest.mark.asyncio
async def test_a_capture_failure_does_not_discard_work_that_landed(
    passing_harness, monkeypatch: pytest.MonkeyPatch
):
    """The work is in the repository. Bookkeeping that fails is a gap in the
    orchestrator's memory, not a reason to throw the commit away.

    Asserted on what happened to the run, not on the log: a swallowed exception
    is exactly what would also make a log-only test pass while the delivery
    quietly failed.
    """
    factory, settings, project, task = passing_harness
    calls: list[object] = []

    def _explode(session, run_id, **_kwargs):
        calls.append(run_id)
        raise OSError("disk full")

    monkeypatch.setattr(
        "apps.orchestrator.services.training.capture_accepted_run", _explode
    )

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-004")),
        settings=settings,
    )
    state = await runner.run_task(task.id)

    assert state["outcome"] == "COMPLETED"
    assert state["commit_sha"]
    with factory() as session:
        stored = TaskRepository(session).get(task.id)
        runs = TaskRunRepository(session).list_for_task(task.id)
        assert TrainingExampleRepository(session).get_for_run(runs[0].id) is None

    assert stored is not None and stored.status is TaskStatus.COMPLETE
    assert runs[0].candidate_commit == state["commit_sha"]
    # It tried, and the failure was confined to the capture.
    assert calls == [runs[0].id]
    assert not (settings.training_dir / runs[0].external_run_id).exists()


@pytest.mark.asyncio
async def test_a_run_that_nobody_could_finish_still_records_its_outcome(harness):
    """A failure is the run somebody most wants to read afterwards, so the
    rejected path writes the same file the accepted one does -- and never a
    training example."""
    factory, settings, project, task = harness

    # This suite demands the guard and every attempt omits it, so verification
    # fails until the budget is spent and nothing is ever delivered. The
    # reviewer is scripted as well as the coder, and is never reached -- which
    # is itself the point: a run that never gets reviewed has no findings, and
    # a run with no findings has no lessons.
    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(*[_code(BROKEN) for _ in range(3)]),
        reviewer=reviewer(*[_review(taskId="TS-004") for _ in range(3)]),
        settings=settings,
    )
    state = await runner.run_task(task.id)

    assert state["outcome"] != "COMPLETED"
    with factory() as session:
        runs = TaskRunRepository(session).list_for_task(task.id)
        stored = TaskRepository(session).get(task.id)
        assert all(
            TrainingExampleRepository(session).get_for_run(run.id) is None
            for run in runs
        )

    assert stored is not None and stored.status in {
        TaskStatus.HUMAN_REVIEW,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    }
    for run in runs:
        outcome_path = settings.runs_dir / run.external_run_id / "outcome.json"
        assert outcome_path.exists(), f"no outcome for {run.external_run_id}"
        payload = json.loads(outcome_path.read_text(encoding="utf-8"))
        assert payload["outcome"] in {"rejected", "escalated"}
