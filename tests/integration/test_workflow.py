"""Phase K's exit condition: the complete loop executes through LangGraph."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.db.base import Base
from apps.orchestrator.db.models import WorkflowCheckpointRow
from apps.orchestrator.db.session import create_db_engine
from apps.orchestrator.domain.enums import Complexity, TaskStatus, WorkerProfile
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import ProjectRepository, TaskRepository, TaskRunRepository
from apps.orchestrator.workflow import WorkflowRunner
from tests.conftest import run_git
from tests.integration.test_fix_loop import WORKING, ScriptedModel, _code, _review, reviewer

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_approved_candidate_is_committed_completed_and_checkpointed(tmp_path: Path):
    repository = tmp_path / "project"
    (repository / "src").mkdir(parents=True)
    (repository / "tools").mkdir()
    (repository / "src" / "nav.py").write_text(
        "def navigate(target):\n    pass\n", encoding="utf-8"
    )
    (repository / "tools" / "test.py").write_text(
        "assert 'return target' in open('src/nav.py').read()\n", encoding="utf-8"
    )
    run_git(repository, "init", "--initial-branch=main", "--quiet")
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "-m", "initial")

    engine = create_db_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
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
                external_task_id="TS-001",
                title="implement navigation",
                complexity=Complexity.LOW,
                files_to_modify=["src/nav.py"],
            )
        )
        task = TaskRepository(session).transition(task.id, TaskStatus.READY)

    runner = WorkflowRunner(
        factory,
        coder=ScriptedModel(_code(WORKING)),
        reviewer=reviewer(_review(taskId="TS-001")),
        settings=settings,
    )
    state = await runner.run_task(task.id)

    assert state["outcome"] == "COMPLETED"
    assert state["commit_sha"]
    assert state["worktree_released"] is True
    with factory() as session:
        stored = TaskRepository(session).get(task.id)
        runs = TaskRunRepository(session).list_for_task(task.id)
        checkpoints = session.scalar(select(func.count()).select_from(WorkflowCheckpointRow))
    assert stored is not None and stored.status is TaskStatus.COMPLETE
    assert len(runs) == 1 and runs[0].candidate_commit == state["commit_sha"]
    assert checkpoints and checkpoints > 1
    assert not any(settings.worktree_root.rglob("src"))
    engine.dispose()
