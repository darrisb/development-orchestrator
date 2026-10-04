"""The runtime contract inside the verification pipeline (concern 81).

What these tests own is the *lifecycle and the classification*: where the
runtime step sits in section 17's order, that it never runs after a failing
command, that the application it starts is always stopped, that everything the
application wrote into the worktree is discarded by concern 80's boundary, and
that a candidate failure and an infrastructure failure stay two different
answers.

They run on the subprocess worker backend, like the rest of phase H, and they
supply their own probe. That is deliberate: the browser mechanism is pinned
down separately, against a real Chromium, in ``test_runtime_browser.py``. A
lifecycle test that needed a browser would be a browser test, and would stop
running on the machines that most need these invariants held.

The application started here is a real managed background process -- a real
pid, a real signal, a real exit code -- so the cleanup assertions are about
something that actually had to be killed.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.enums import (
    FailureAction,
    FailureReason,
    TaskStatus,
    VerificationStatus,
    VerificationType,
    WorkerProfile,
)
from apps.orchestrator.domain.failure_policy import action_for
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.domain.runtime_contract import (
    ObservedResponse,
    RuntimeContract,
    RuntimeObservation,
    parse_runtime_contract,
)
from apps.orchestrator.domain.verification import VerificationProfile
from apps.orchestrator.repositories import (
    ProjectRepository,
    TaskRepository,
    VerificationRunRepository,
)
from apps.orchestrator.services.runs import create_run
from apps.orchestrator.services.runtime_verification import (
    PROBE_DIRECTORY,
    ProbeObservation,
    ReadinessPoll,
)
from apps.orchestrator.services.verification import VERIFICATION_ARTIFACT, verify_candidate
from apps.orchestrator.services.workspace import TaskWorkspace, prepare_workspace
from tests.conftest import run_git

pytestmark = pytest.mark.integration

PYTHON = "python3"

pytest.importorskip("sqlalchemy")
if shutil.which(PYTHON) is None:  # pragma: no cover - unusual host
    pytest.skip(f"{PYTHON} is not on PATH", allow_module_level=True)

#: Stands in for `npm start`. It writes its pid where a test can read it after
#: the pipeline has finished -- which is how "the application was terminated"
#: becomes an assertion about a process rather than about a flag -- and drops a
#: cache file into the worktree, which is what concern 80's boundary must
#: discard.
_SERVER = """\
import pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(__import__("os").getpid()))
cache = pathlib.Path(".runtime-cache")
cache.mkdir(exist_ok=True)
(cache / "served.txt").write_text("written while serving\\n")
print("listening", flush=True)
time.sleep(300)
"""

#: A server that dies on startup -- a port already taken, a missing config.
_DIES = """\
import sys
print("FATAL: port already in use", file=sys.stderr, flush=True)
sys.exit(3)
"""

#: A server that prints an injected secret. Nothing the pipeline stores may
#: contain it (section 36).
_LEAKY = """\
import os, sys, time
print("token is " + os.environ.get("API_TOKEN", "unset"), flush=True)
time.sleep(300)
"""

_COMPILE = """\
import sys
source = open("src/nav.py").read()
if "SYNTAX ERROR" in source:
    print("src/nav.py:1: invalid syntax", file=sys.stderr)
    sys.exit(2)
print("compiled 1 file")
"""

_TEST = """\
import sys
source = open("src/nav.py").read()
if "return None" in source:
    print("FAILED tests/test_nav.py::test_navigate", file=sys.stderr)
    sys.exit(1)
print("2 passed")
"""


# --------------------------------------------------------------------- probes


@dataclass
class FakeProbe:
    """A probe that reports whatever a test needs, and records what it was asked.

    It stands exactly where Chromium stands, which is the seam the production
    code was given on purpose: the lifecycle around it is the real one.
    """

    observation: RuntimeObservation | None = None
    infrastructure_error: str = ""
    ready_after: int = 2
    never_ready: bool = False
    #: A real probe is a process that takes time to answer. The fake takes
    #: some too, because an instant readiness poll would have the lifecycle
    #: finish before the application it started had finished starting -- and a
    #: cleanup assertion against a process that never ran proves nothing.
    delay_seconds: float = 0.2
    polls: int = 0
    observations: int = 0
    pages: list[str] = field(default_factory=list)
    #: A path inside the worktree to look for while the application is up.
    #: It is how a test proves the side effect it later asserts was *removed*
    #: ever existed -- otherwise "the cache is gone" passes trivially.
    witness: Path | None = None
    witnessed: bool = False

    def poll_readiness(self, url: str, *, budget_seconds: float) -> ReadinessPoll:
        self.polls += 1
        time.sleep(min(self.delay_seconds, max(0.0, budget_seconds)))
        if self.witness is not None and self.witness.exists():
            self.witnessed = True
        if self.never_ready:
            return ReadinessPoll(ready=False, error="connect ECONNREFUSED")
        if self.polls < self.ready_after:
            return ReadinessPoll(ready=False, error="connect ECONNREFUSED")
        return ReadinessPoll(ready=True, status=200, attempts=1)

    def observe(self, contract: RuntimeContract) -> ProbeObservation:
        self.observations += 1
        self.pages.append(contract.page or "")
        if self.infrastructure_error:
            return ProbeObservation(infrastructure_error=self.infrastructure_error)
        return ProbeObservation(observation=self.observation or _passing_observation())


def _passing_observation() -> RuntimeObservation:
    return RuntimeObservation(
        page_url="http://127.0.0.1:7391/items",
        loaded=True,
        page_text="Items\nalpha\n",
        responses=(
            ObservedResponse(
                url="http://127.0.0.1:7391/api/items",
                status=200,
                content_type="application/json; charset=utf-8",
            ),
        ),
    )


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def verification_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        artifact_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        worker_backend=WorkerBackend.SUBPROCESS,
        worker_command_timeout_seconds=30,
        worker_background_stop_grace_seconds=2,
    )


@pytest.fixture
def pid_file(tmp_path: Path) -> Path:
    """Outside the worktree on purpose: the worktree is reset by the pipeline."""
    return tmp_path / "application.pid"


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "widgets"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "nav.py").write_text("def navigate(target):\n    return target\n")
    (repo / "tools" / "compile.py").write_text(_COMPILE)
    (repo / "tools" / "test.py").write_text(_TEST)
    (repo / "tools" / "server.py").write_text(_SERVER)
    (repo / "tools" / "dies.py").write_text(_DIES)
    (repo / "tools" / "leaky.py").write_text(_LEAKY)
    run_git(repo, "init", "--initial-branch=main", "--quiet")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


def _contract(start: str, **overrides) -> RuntimeContract:
    """A contract declared the way a project declares one. No defaults here
    are the orchestrator's: every value comes from this fixture project."""
    declared = {
        "start": start,
        "readiness_url": "http://127.0.0.1:7391/",
        "readiness_timeout_seconds": 5,
        "page": "http://127.0.0.1:7391/items",
        "expect_requests": [
            {
                "url_pattern": "**/api/items",
                "status": 200,
                "content_type": "application/json",
            }
        ],
        "expect_text": "Items",
        "forbid_console_errors": True,
        **overrides,
    }
    return parse_runtime_contract(declared)


def _profile(contract: RuntimeContract | None, *, tests: bool = True) -> VerificationProfile:
    return VerificationProfile(
        build=(f"{PYTHON} tools/compile.py",),
        tests=(f"{PYTHON} tools/test.py",) if tests else (),
        runtime=contract,
    )


def _project(session: Session, repo: Path, profile: VerificationProfile) -> Project:
    return ProjectRepository(session).add(
        Project(
            name="Widgets",
            repository_path=str(repo),
            default_branch="main",
            worker_profile=WorkerProfile.PYTHON,
            verification=profile,
        )
    )


def _workspace(
    session: Session, project: Project, settings: Settings
) -> tuple[TaskWorkspace, Task, TaskRun]:
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(
            project_id=project.id,
            external_task_id="W-1",
            title="Implement navigation",
            files_to_modify=["src/nav.py"],
        )
    )
    tasks.transition(task.id, TaskStatus.READY)
    run = create_run(session, task.id)
    tasks.transition(task.id, TaskStatus.CODING)
    task.status = TaskStatus.CODING
    workspace = prepare_workspace(session, run.id, settings=settings)
    (workspace.path / "src" / "nav.py").write_text(
        "def navigate(target):\n    return target.strip()\n", encoding="utf-8"
    )
    return workspace, task, run


@pytest.fixture
def runtime_case(session: Session, project_repo: Path, verification_settings: Settings):
    """Assemble a project with whatever contract and profile a test wants."""

    def build(contract: RuntimeContract | None, *, tests: bool = True, broken: str | None = None):
        project = _project(session, project_repo, _profile(contract, tests=tests))
        workspace, task, run = _workspace(session, project, verification_settings)
        if broken is not None:
            (workspace.path / "src" / "nav.py").write_text(broken, encoding="utf-8")
        return workspace, task, run

    return build


def _survivor(pid_file: Path) -> bool:
    """Whether the process that wrote ``pid_file`` is still alive."""
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
    except (ProcessLookupError, PermissionError, ValueError):
        return False
    return True


# --- the order ---------------------------------------------------------------


def test_runtime_runs_after_every_command_category_and_last_of_them(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))
    probe = FakeProbe()

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    assert report.passed, report.summary()
    executed = [step.verification_type for step in report.commands_run]
    assert executed == [
        VerificationType.BUILD,
        VerificationType.TESTS,
        VerificationType.RUNTIME,
    ]
    assert probe.observations == 1
    assert probe.pages == ["http://127.0.0.1:7391/items"]


def test_runtime_does_not_run_after_a_failing_build(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    """An application that does not compile cannot be started, and starting one
    to find that out would be a minute spent on a known answer."""
    workspace, _, _ = runtime_case(
        _contract(f"{PYTHON} tools/server.py {pid_file}"),
        broken="def navigate(target):  # SYNTAX ERROR\n    return\n",
    )
    probe = FakeProbe()

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    assert report.failure_reason is FailureReason.BUILD_FAILED
    assert report.step_for(VerificationType.RUNTIME) is None
    assert (probe.polls, probe.observations) == (0, 0)
    assert not pid_file.exists()


def test_runtime_does_not_run_after_a_failing_test(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    workspace, _, _ = runtime_case(
        _contract(f"{PYTHON} tools/server.py {pid_file}"),
        broken="def navigate(target):\n    return None\n",
    )
    probe = FakeProbe()

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    assert report.failure_reason is FailureReason.TEST_FAILED
    assert report.step_for(VerificationType.RUNTIME) is None
    assert probe.observations == 0
    assert not pid_file.exists()


def test_a_project_with_no_runtime_contract_behaves_exactly_as_before(
    session: Session, runtime_case, verification_settings: Settings
):
    """The compatibility requirement. No runtime step, no new category in the
    report, nothing executed, nothing recorded."""
    workspace, task, _ = runtime_case(None)

    report = verify_candidate(session, workspace, settings=verification_settings)

    assert report.passed and report.verified
    assert report.steps_for(VerificationType.RUNTIME) == ()
    assert VerificationType.RUNTIME not in report.unverified_categories
    assert TaskRepository(session).get(task.id).status is TaskStatus.REVIEW_PENDING
    assert "RUNTIME" not in str(report.describe()["unverified_categories"])


def test_a_runtime_contract_alone_is_enough_to_start_and_check_the_application(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    """A contract with no browser assertions is answered by readiness alone --
    no Chromium is launched for it."""
    workspace, _, _ = runtime_case(
        parse_runtime_contract(
            {
                "start": f"{PYTHON} tools/server.py {pid_file}",
                "readiness_url": "http://127.0.0.1:7391/",
                "readiness_timeout_seconds": 5,
            }
        )
    )
    probe = FakeProbe()

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    assert report.step_for(VerificationType.RUNTIME).passed
    assert probe.observations == 0
    assert not _survivor(pid_file)


# --- candidate failures ------------------------------------------------------


def test_a_runtime_assertion_failure_goes_back_to_the_coder(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    """The V1 policy mapping: a runtime contract the candidate did not satisfy
    is routed through ``TEST_FAILED`` so it reaches the coder with its
    evidence. **Temporary** -- the step is still ``RUNTIME``, and a later
    concern is expected to give it ``RUNTIME_FAILED`` of its own."""
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))
    probe = FakeProbe(
        observation=RuntimeObservation(
            page_url="http://127.0.0.1:7391/items",
            loaded=True,
            page_text="Items",
            responses=(
                ObservedResponse(
                    url="http://127.0.0.1:7391/api/items",
                    status=200,
                    content_type="text/html; charset=utf-8",
                ),
            ),
        )
    )

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    step = report.step_for(VerificationType.RUNTIME)
    assert step.status is VerificationStatus.FAILED
    assert step.verification_type is VerificationType.RUNTIME
    assert report.failure_reason is FailureReason.TEST_FAILED
    assert action_for(report.failure_reason) is FailureAction.SEND_TO_CODER
    assert "application/json" in step.detail and "text/html" in step.detail
    assert "/api/items" in report.feedback


def test_an_application_that_exits_before_readiness_fails_with_its_process_evidence(
    session: Session, runtime_case, verification_settings: Settings
):
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/dies.py"))
    probe = FakeProbe(never_ready=True)

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    step = report.step_for(VerificationType.RUNTIME)
    assert step.status is VerificationStatus.FAILED
    assert "exited before it became ready" in step.detail
    assert step.evidence["application_exit_code"] == 3
    assert "port already in use" in step.output
    # Never observed: there was nothing to open a page against.
    assert probe.observations == 0


def test_a_readiness_timeout_is_a_bounded_failure_with_the_last_error(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    workspace, _, _ = runtime_case(
        _contract(f"{PYTHON} tools/server.py {pid_file}", readiness_timeout_seconds=1)
    )
    probe = FakeProbe(never_ready=True)

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    step = report.step_for(VerificationType.RUNTIME)
    assert step.status is VerificationStatus.FAILED
    assert "did not answer http://127.0.0.1:7391/ within 1s" in step.detail
    assert step.evidence["readiness"]["ready"] is False
    assert "ECONNREFUSED" in step.evidence["readiness"]["last_error"]
    assert probe.polls >= 1
    assert not _survivor(pid_file)


# --- infrastructure failures -------------------------------------------------


def test_a_browser_that_cannot_launch_is_an_infrastructure_error_not_a_defect(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    """Fail closed, and keep the two answers apart: sending a coder to fix an
    application that works because the image has no browser is worse than
    stopping."""
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))
    probe = FakeProbe(
        infrastructure_error="no Chromium executable could be started (chromium: ENOENT)"
    )

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    step = report.step_for(VerificationType.RUNTIME)
    assert step.status is VerificationStatus.ERROR
    assert step.failure_reason is FailureReason.WORKER_FAILURE
    assert action_for(step.failure_reason) is FailureAction.RETRY
    assert report.failure_reason is FailureReason.WORKER_FAILURE
    assert not report.no_new_regressions
    assert "Chromium" in step.detail
    assert not _survivor(pid_file)


def test_a_start_command_the_policy_refuses_is_an_infrastructure_error(
    session: Session, runtime_case, verification_settings: Settings
):
    workspace, _, _ = runtime_case(_contract("npm start && echo done"))

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=FakeProbe()
    )

    step = report.step_for(VerificationType.RUNTIME)
    assert step.status is VerificationStatus.ERROR
    assert step.failure_reason is FailureReason.WORKER_FAILURE
    assert "not permitted" in step.detail


# --- evidence and the artifact ----------------------------------------------


def test_the_verification_artifact_carries_the_runtime_step_and_its_evidence(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    workspace, _, run = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))
    probe = FakeProbe(
        observation=RuntimeObservation(
            page_url="http://127.0.0.1:7391/items",
            loaded=True,
            page_text="Items",
            responses=(
                ObservedResponse(
                    url="http://127.0.0.1:7391/api/items", status=200, content_type="text/html"
                ),
            ),
        )
    )

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    stored = (
        verification_settings.artifact_root
        / report.artifacts[VERIFICATION_ARTIFACT]
    ).read_text()
    assert '"verification_type": "RUNTIME"' in stored
    assert "/api/items" in stored
    assert "application/json" in stored and "text/html" in stored
    # The transcript is on disk and referenced by path, not pasted into the
    # report: the same convention as every command log.
    step = report.step_for(VerificationType.RUNTIME)
    assert step.log_artifact and step.log_artifact.endswith("runtime/runtime.log")
    assert (verification_settings.artifact_root / step.log_artifact).exists()


def test_the_runtime_step_is_recorded_as_a_verification_run_row(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    workspace, _, run = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))

    verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=FakeProbe()
    )

    rows = VerificationRunRepository(session).list_for_run(run.id)
    runtime_rows = [row for row in rows if row.verification_type is VerificationType.RUNTIME]
    assert len(runtime_rows) == 1
    assert runtime_rows[0].status is VerificationStatus.PASSED


def test_an_injected_secret_printed_by_the_application_never_reaches_the_evidence(
    session: Session, runtime_case, verification_settings: Settings
):
    workspace, _, _ = runtime_case(
        _contract(f"{PYTHON} tools/leaky.py", readiness_timeout_seconds=1)
    )

    report = verify_candidate(
        session,
        workspace,
        settings=verification_settings,
        secrets={"API_TOKEN": "sk-super-secret-value"},
        runtime_probe=FakeProbe(never_ready=True),
    )

    step = report.step_for(VerificationType.RUNTIME)
    assert step.status is VerificationStatus.FAILED
    assert "token is" in step.output
    assert "sk-super-secret-value" not in step.output
    stored = (
        verification_settings.artifact_root
        / report.artifacts[VERIFICATION_ARTIFACT]
    ).read_text()
    assert "sk-super-secret-value" not in stored
    assert (
        "sk-super-secret-value"
        not in (verification_settings.artifact_root / step.log_artifact).read_text()
    )


# --- concern 80: the candidate is what was measured --------------------------


def test_what_the_running_application_wrote_into_the_worktree_is_discarded(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    """Concern 80's invariant, over the runtime step. The application wrote a
    cache directory and the orchestrator installed a probe; the candidate that
    goes to a reviewer must be the one that was measured, byte for byte."""
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))
    candidate = (workspace.path / "src" / "nav.py").read_text()
    before = workspace.git.get_diff(workspace.starting_commit, binary=True)
    probe = FakeProbe(witness=workspace.path / ".runtime-cache" / "served.txt")

    report = verify_candidate(
        session, workspace, settings=verification_settings, runtime_probe=probe
    )

    assert report.passed, report.summary()
    # The application really did write into the candidate worktree while it was
    # up -- seen from inside the lifecycle, before the restore.
    assert probe.witnessed
    # And none of what it or the probe wrote survived.
    assert not (workspace.path / ".runtime-cache").exists()
    assert not (workspace.path / PROBE_DIRECTORY).exists()
    assert (workspace.path / "src" / "nav.py").read_text() == candidate
    assert workspace.git.get_diff(workspace.starting_commit, binary=True) == before
    # And the diff checks that run after it saw a clean candidate.
    assert report.step_for(VerificationType.DIFF_POLICY).passed


def test_the_candidate_is_restored_even_when_the_runtime_step_fails(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings
):
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))
    candidate = (workspace.path / "src" / "nav.py").read_text()

    verify_candidate(
        session,
        workspace,
        settings=verification_settings,
        runtime_probe=FakeProbe(infrastructure_error="chromium is unavailable"),
    )

    assert (workspace.path / "src" / "nav.py").read_text() == candidate
    assert not (workspace.path / ".runtime-cache").exists()
    assert not (workspace.path / PROBE_DIRECTORY).exists()


# --- nothing outlives verification ------------------------------------------


@pytest.mark.parametrize(
    "probe",
    [
        pytest.param(FakeProbe(), id="success"),
        pytest.param(
            FakeProbe(
                observation=RuntimeObservation(
                    page_url="http://127.0.0.1:7391/items", loaded=True, page_text="nothing"
                )
            ),
            id="assertion-failure",
        ),
        pytest.param(
            FakeProbe(infrastructure_error="chromium could not be started"),
            id="browser-failure",
        ),
    ],
)
def test_the_application_is_terminated_however_verification_ends(
    session: Session, runtime_case, pid_file: Path, verification_settings: Settings, probe
):
    """Section 11, over the new primitive: no server survives verification, on
    the passing path, the failing path or the broken-browser path."""
    workspace, _, _ = runtime_case(_contract(f"{PYTHON} tools/server.py {pid_file}"))

    verify_candidate(session, workspace, settings=verification_settings, runtime_probe=probe)

    assert pid_file.exists(), "the application never started, so the test proves nothing"
    assert not _survivor(pid_file)
