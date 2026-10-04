"""The runtime contract against a real browser, on the real Docker path.

Concern 81's mechanism, end to end and unfaked: a managed background server in
a sibling container, Chromium in the worker container, the two sharing one
network namespace with **no published host port**, and the DevTools Protocol
in between. Everything the lifecycle tests stub out is real here.

The fixture application is forty lines of dependency-free Node, serving one
page and one API route, and its behaviour is switched by a variable the
*contract* declares. It is deliberately not Angular, React or Vue: the point
of this feature is that the orchestrator knows nothing about any of them.

The case that matters most is ``spa_fallback``. It reproduces exactly what the
Development Orchestrator's own UI campaign shipped past every deterministic
check: ``/api/items`` answering HTTP **200** with ``Content-Type: text/html``
-- the index page served where the API should have been. A status assertion
passes it. Only the media type catches it.

These tests need Docker and the Node worker image, and skip cleanly without
them. The operator procedure for running them by hand is in the concern's
completion report.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from apps.orchestrator.config.settings import Settings, WorkerBackend
from apps.orchestrator.domain.enums import (
    FailureReason,
    VerificationStatus,
    WorkerProfile,
)
from apps.orchestrator.domain.runtime_contract import (
    RuntimeContract,
    parse_runtime_contract,
)
from apps.orchestrator.services.runtime_verification import (
    PROBE_DIRECTORY,
    verify_runtime,
)
from apps.orchestrator.services.worker_service import worker_session

pytestmark = [pytest.mark.integration, pytest.mark.docker]

#: The worker image that carries Chromium. The same image the rest of the
#: Node profile's browser tooling already uses.
WORKER_IMAGE = "orchestrator-worker-node:latest"

#: A port inside the worker's own network namespace. It is never published, so
#: it cannot collide with anything on the host or in another worker -- which is
#: the isolation property these tests exist to demonstrate.
FIXTURE_PORT = "7391"

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "runtime_fixture_server.mjs"


def _docker_unavailable() -> str:
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    probe = subprocess.run(  # noqa: S603, S607 - fixed argv, test-only
        ["docker", "image", "inspect", WORKER_IMAGE],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        return f"{WORKER_IMAGE} is not built on this host"
    return ""


_SKIP = _docker_unavailable()
pytestmark.append(pytest.mark.skipif(bool(_SKIP), reason=_SKIP or "docker available"))


@pytest.fixture
def worktree() -> Path:
    """A worktree-shaped directory holding only the fixture application."""
    root = Path(tempfile.mkdtemp(prefix="runtime-browser-")) / "worktrees"
    tree = root / "project" / "run1"
    tree.mkdir(parents=True)
    shutil.copy(FIXTURE, tree / "server.mjs")
    yield tree
    shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture
def docker_settings(worktree: Path) -> Settings:
    return Settings(
        _env_file=None,
        worktree_root=worktree.parents[1],
        worker_backend=WorkerBackend.DOCKER,
        worker_node_image=WORKER_IMAGE,
        worker_command_timeout_seconds=300,
        worker_background_stop_grace_seconds=3,
    )


def _contract(mode: str, **overrides) -> RuntimeContract:
    """The contract, declared the way a project declares one.

    Every URL, port and route here comes from this fixture. Nothing in the
    orchestrator supplies a default for any of them.
    """
    declared = {
        "start": "node server.mjs",
        "readiness_url": f"http://127.0.0.1:{FIXTURE_PORT}/",
        "readiness_timeout_seconds": 30,
        "page": f"http://127.0.0.1:{FIXTURE_PORT}/items",
        "expect_requests": [
            {
                "url_pattern": "**/api/items",
                "status": 200,
                "content_type": "application/json",
            }
        ],
        "expect_text": "Items",
        "forbid_console_errors": True,
        "env": {"FIXTURE_MODE": mode, "FIXTURE_PORT": FIXTURE_PORT},
        **overrides,
    }
    return parse_runtime_contract(declared)


def _run(worktree: Path, settings: Settings, contract: RuntimeContract):
    with worker_session(
        worktree, profile=WorkerProfile.NODE, settings=settings, worker_network="none"
    ) as worker:
        return verify_runtime(worker, contract, worktree=worktree)


# --- the mechanism works -----------------------------------------------------


def test_a_working_application_passes_every_declared_assertion(
    worktree: Path, docker_settings: Settings
):
    """The whole mechanism at once, and the network proof with it: the server
    runs in a sibling container, Chromium runs in the worker, they reach each
    other over loopback in a shared namespace, and no host port is published
    for either of them."""
    outcome = _run(worktree, docker_settings, _contract("json"))

    assert outcome.status is VerificationStatus.PASSED, outcome.detail
    assert not outcome.infrastructure
    observation = outcome.evidence["observation"]
    assert observation["loaded"] is True
    seen = {entry["url"]: entry for entry in observation["responses"]}
    api = seen[f"http://127.0.0.1:{FIXTURE_PORT}/api/items"]
    assert api["status"] == 200
    assert api["content_type"].startswith("application/json")
    assert outcome.evidence["readiness"]["ready"] is True
    # Nothing was published to the host: the port is only reachable inside the
    # worker's namespace, which is why this contract needs no network at all.
    assert _published_ports() == []


def _published_ports() -> list[str]:
    listed = subprocess.run(  # noqa: S603, S607 - fixed argv, test-only
        ["docker", "ps", "--filter", "name=orchestrator-worker", "--format", "{{.Ports}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return [line for line in listed.stdout.split("\n") if line.strip()]


def test_the_probe_leaves_nothing_in_the_worktree(worktree: Path, docker_settings: Settings):
    _run(worktree, docker_settings, _contract("json"))

    assert not (worktree / PROBE_DIRECTORY).exists()
    assert sorted(path.name for path in worktree.iterdir()) == ["server.mjs"]


# --- the named regression ----------------------------------------------------


def test_an_api_route_answering_with_the_index_page_fails_on_its_content_type(
    worktree: Path, docker_settings: Settings
):
    """**The regression this concern exists for.** The route answers HTTP 200,
    so every status-only check passes it; what is wrong is that the body is the
    single-page application's HTML instead of the API's JSON. The evidence must
    make that unmissable."""
    outcome = _run(worktree, docker_settings, _contract("spa_fallback"))

    assert outcome.status is VerificationStatus.FAILED
    assert not outcome.infrastructure
    failures = outcome.evidence["failed_assertions"]
    assert [failure["assertion"] for failure in failures] == ["response content-type"]
    assert failures[0]["expected"] == "application/json"
    assert failures[0]["observed"].startswith("text/html")
    assert failures[0]["request_url"].endswith("/api/items")
    assert failures[0]["status"] == 200
    assert "application/json" in outcome.detail and "text/html" in outcome.detail


# --- the other assertions, against a real page -------------------------------


def test_an_expected_request_the_page_never_makes_fails(
    worktree: Path, docker_settings: Settings
):
    outcome = _run(worktree, docker_settings, _contract("no_request"))

    assert outcome.status is VerificationStatus.FAILED
    failures = outcome.evidence["failed_assertions"]
    assert [failure["assertion"] for failure in failures] == ["expected request"]


def test_a_wrong_status_fails(worktree: Path, docker_settings: Settings):
    outcome = _run(worktree, docker_settings, _contract("server_error"))

    assert outcome.status is VerificationStatus.FAILED
    failures = outcome.evidence["failed_assertions"]
    assert [failure["assertion"] for failure in failures] == ["response status"]
    assert (failures[0]["expected"], failures[0]["observed"]) == ("200", "500")


def test_missing_rendered_text_fails(worktree: Path, docker_settings: Settings):
    outcome = _run(worktree, docker_settings, _contract("other_heading"))

    assert outcome.status is VerificationStatus.FAILED
    assert "rendered text" in {
        failure["assertion"] for failure in outcome.evidence["failed_assertions"]
    }


def test_a_console_error_fails_when_the_contract_forbids_one(
    worktree: Path, docker_settings: Settings
):
    outcome = _run(worktree, docker_settings, _contract("console_error"))

    assert outcome.status is VerificationStatus.FAILED
    failures = outcome.evidence["failed_assertions"]
    assert [failure["assertion"] for failure in failures] == ["console error"]
    assert "TypeError" in failures[0]["observed"]


def test_an_uncaught_page_error_fails_when_the_contract_forbids_console_errors(
    worktree: Path, docker_settings: Settings
):
    outcome = _run(worktree, docker_settings, _contract("page_error"))

    assert outcome.status is VerificationStatus.FAILED
    failures = outcome.evidence["failed_assertions"]
    assert [failure["assertion"] for failure in failures] == ["uncaught page error"]


def test_ordinary_console_output_does_not_fail(worktree: Path, docker_settings: Settings):
    """``forbid_console_errors`` forbids errors. A chatty application is not a
    broken one."""
    outcome = _run(worktree, docker_settings, _contract("console_log"))

    assert outcome.status is VerificationStatus.PASSED, outcome.detail


# --- the process, not the page ----------------------------------------------


def test_an_application_that_exits_before_readiness_fails_with_its_exit_code(
    worktree: Path, docker_settings: Settings
):
    outcome = _run(worktree, docker_settings, _contract("exit_on_start"))

    assert outcome.status is VerificationStatus.FAILED
    assert "exited before it became ready" in outcome.detail
    assert outcome.evidence["application_exit_code"] == 3
    assert "port already in use" in outcome.output


def test_an_application_that_never_listens_fails_at_its_bounded_timeout(
    worktree: Path, docker_settings: Settings
):
    outcome = _run(
        worktree, docker_settings, _contract("never_listens", readiness_timeout_seconds=6)
    )

    assert outcome.status is VerificationStatus.FAILED
    assert "did not answer" in outcome.detail
    assert outcome.evidence["readiness"]["ready"] is False
    assert outcome.evidence["readiness"]["polls"] >= 1


def test_no_container_survives_a_runtime_check(worktree: Path, docker_settings: Settings):
    """Section 11 over the new primitive, on the backend where it is a real
    container: neither the application's sibling container nor the worker's own
    is left behind, whatever the verdict was."""
    before = _containers()

    _run(worktree, docker_settings, _contract("spa_fallback"))

    assert _containers() == before


def _containers() -> list[str]:
    listed = subprocess.run(  # noqa: S603, S607 - fixed argv, test-only
        ["docker", "ps", "--all", "--filter", "name=orchestrator-worker", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return sorted(line for line in listed.stdout.split("\n") if line.strip())


def test_a_browser_that_cannot_be_launched_fails_closed_as_infrastructure(
    worktree: Path, docker_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    """The distinction that must never blur. The application here is perfectly
    good; the orchestrator simply has no browser. Calling that a candidate
    failure would send a coder to fix working code, so it fails closed as an
    infrastructure error instead."""
    monkeypatch.setattr(
        "apps.orchestrator.services.runtime_verification.CHROMIUM_CANDIDATES",
        ("/nonexistent/no-such-browser",),
    )

    outcome = _run(worktree, docker_settings, _contract("json"))

    assert outcome.status is VerificationStatus.ERROR
    assert outcome.infrastructure
    assert outcome.failure_reason_override is FailureReason.WORKER_FAILURE
    assert "Chromium" in outcome.detail
    # The application was still started and still stopped: the failure is
    # about the browser, and the lifecycle promise does not depend on it.
    assert outcome.evidence["readiness"]["ready"] is True
    assert _containers() == []
