"""Concern 63 regression tests: the deployment has to be able to say what it is.

`RUN-20260927-000020` was the fourth TS-106 run. It executed against container
image `168a74d323e2`, built roughly 49 minutes before Concern 62 commit
`d50752d`. The host source was correct and the Concern 62 tests were correct and
green; the image contained Concern 61-era code. Both the missing prompt limit and
the old 10137 enforcement had the same cause, so the run's evidence described
code that no longer existed.

The defect was not a bug in the orchestrator. It was that nothing in the system
could tell the difference between "the code under test" and "the code running",
so a green host suite and an invalid experiment looked identical.

These tests pin the two halves of the fix, and the second half is the one that
matters: a check that cannot fail is a comment. So most of what is asserted here
is that the freshness check goes red for each way a deployment can be wrong --
stale, dirty, unidentifiable, unreachable -- and green only for the one case it
is allowed to pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.orchestrator.config import get_settings
from apps.orchestrator.main import create_app
from apps.orchestrator.services.deployment import (
    UNKNOWN_BUILD_TIME,
    UNKNOWN_REVISION,
    SourceIdentity,
    StaleDeploymentError,
    assert_deployment_fresh,
    current_source,
)
from apps.orchestrator.services.health import build_health_report
from scripts.check_deployment_freshness import main as freshness_main
from scripts.check_deployment_freshness import resolve_expected

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_deployment_freshness.py"
CLEAN_SHA = "d50752dfc52d02aa2bcc29a25928fff1c1bcfaa8"
OLDER_SHA = "168a74d323e2b1c1f6f9e4b6d2f1c9e8a7b6c5d4"

# The deployment as it was during the incident: the intended commit's
# predecessor, which reported itself as perfectly healthy.
STALE = SourceIdentity(revision=OLDER_SHA, dirty=False, built_at="2026-09-27T20:38:21Z")


@pytest.fixture
def healthy_report(monkeypatch):
    """A health report with every component stubbed healthy.

    The build identity is the thing under test, not the database.
    """
    from apps.orchestrator.config.settings import Settings
    from apps.orchestrator.schemas.health import ComponentHealth

    ok = ComponentHealth(name="stub", healthy=True)
    for name in ("check_database", "check_artifact_root", "check_worker_backend"):
        monkeypatch.setattr(
            f"apps.orchestrator.services.health.{name}", lambda *_a, **_k: ok
        )
    monkeypatch.setattr(
        "apps.orchestrator.services.health.check_worktrees", lambda *_a, **_k: None
    )

    def report():
        return build_health_report(Settings(_env_file=None))

    return report


@pytest.fixture
def client(engine: Engine, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr("apps.orchestrator.services.health.get_engine", lambda: engine)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKER_BACKEND", "subprocess")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


# =============================================================================
# 1. The image carries its identity.
# =============================================================================


def test_the_developer_build_says_it_does_not_know():
    """The checked-in defaults are the truth: no SHA was determined.

    A placeholder SHA would be worse than none. It would pass a comparison and
    the comparison is the entire mechanism.
    """
    from apps.orchestrator import _build_meta

    assert _build_meta.SOURCE_REVISION == UNKNOWN_REVISION
    assert _build_meta.SOURCE_DIRTY is None
    assert _build_meta.BUILD_TIME == UNKNOWN_BUILD_TIME


def test_the_fallback_is_a_real_module_the_package_ships(monkeypatch):
    """Deleting the generated file still yields a working import.

    An editable checkout has no build step, and a developer build is a
    legitimate thing to run. What it must not be is an import error.
    """
    probe = "import apps.orchestrator._build_meta as m; print(m.SOURCE_REVISION)"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == UNKNOWN_REVISION


@pytest.mark.parametrize(
    "identity, expected",
    [
        (SourceIdentity(revision=CLEAN_SHA, dirty=False), f"clean@{CLEAN_SHA}"),
        (SourceIdentity(revision=CLEAN_SHA, dirty=True), f"dirty@{CLEAN_SHA}"),
        (SourceIdentity(revision=CLEAN_SHA, dirty=None), f"clean@{CLEAN_SHA}"),
        (SourceIdentity(revision=UNKNOWN_REVISION, dirty=None), UNKNOWN_REVISION),
        (SourceIdentity(revision="", dirty=False), UNKNOWN_REVISION),
    ],
)
def test_the_three_states_stay_distinguishable(identity: SourceIdentity, expected: str):
    """Clean, dirty, and unknown are three different answers, not two.

    `dirty@<sha>` exists because a build from a modified tree has a SHA that
    does not name the code in the image. Collapsing it into `clean@<sha>` is
    how a modified artifact gets a passing freshness check.
    """
    assert identity.state == expected


# =============================================================================
# 2. /health reports the identity, additively.
# =============================================================================


def test_health_reports_the_whole_identity(healthy_report, monkeypatch):
    from apps.orchestrator import _build_meta

    monkeypatch.setattr(_build_meta, "SOURCE_REVISION", CLEAN_SHA)
    monkeypatch.setattr(_build_meta, "SOURCE_DIRTY", True)
    monkeypatch.setattr(_build_meta, "BUILD_TIME", "2026-09-27T21:24:36Z")

    report = healthy_report()

    assert report.source_revision == CLEAN_SHA
    assert report.source_dirty is True
    assert report.source_state == f"dirty@{CLEAN_SHA}"
    assert report.build_time == "2026-09-27T21:24:36Z"


def test_health_reads_the_identity_at_request_time(healthy_report, monkeypatch):
    """The values are read per report, not captured at import.

    Captured at import, the module is still correct in production and
    untestable here -- and a mechanism you cannot test is a mechanism nobody
    verifies.
    """
    from apps.orchestrator import _build_meta

    monkeypatch.setattr(_build_meta, "SOURCE_REVISION", CLEAN_SHA)
    monkeypatch.setattr(_build_meta, "SOURCE_DIRTY", False)
    assert healthy_report().source_state == f"clean@{CLEAN_SHA}"

    monkeypatch.setattr(_build_meta, "SOURCE_REVISION", OLDER_SHA)
    assert healthy_report().source_state == f"clean@{OLDER_SHA}"


def test_the_endpoint_carries_the_new_fields(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["source_revision"] == UNKNOWN_REVISION
    assert body["source_dirty"] is None
    assert body["source_state"] == UNKNOWN_REVISION
    assert body["build_time"] == UNKNOWN_BUILD_TIME


def test_the_existing_health_contract_is_untouched(client: TestClient):
    """The new fields are additive; a monitoring check must not start failing.

    `status` still answers for the components, and the components are still the
    ones there were.
    """
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert "version" in body
    assert {c["name"] for c in body["components"]} == {
        "database",
        "artifact_root",
        "worker_backend",
    }


# =============================================================================
# 3. The check goes red. This is the half that would have caught the incident.
# =============================================================================


def test_a_matching_clean_build_passes():
    actual = SourceIdentity(revision=CLEAN_SHA, dirty=False, built_at="whenever")

    assert assert_deployment_fresh(CLEAN_SHA, actual) is actual


def test_a_stale_image_is_refused_even_though_it_reports_healthy():
    """The exact incident: right fields, healthy components, wrong commit.

    Every health check passes on this image. That is the whole reason the
    comparison had to be separate from the health checks.
    """
    with pytest.raises(StaleDeploymentError, match="is the source that was meant"):
        assert_deployment_fresh(CLEAN_SHA, STALE)


def test_the_refusal_names_both_sides_and_the_difference():
    with pytest.raises(StaleDeploymentError) as excinfo:
        assert_deployment_fresh(CLEAN_SHA, STALE)

    message = str(excinfo.value)
    assert CLEAN_SHA in message
    assert OLDER_SHA in message
    assert "image predates" in message


def test_a_dirty_build_is_refused_because_the_sha_does_not_name_the_code():
    actual = SourceIdentity(revision=CLEAN_SHA, dirty=True, built_at="later")

    with pytest.raises(StaleDeploymentError, match="uncommitted changes"):
        assert_deployment_fresh(CLEAN_SHA, actual)


def test_a_dirty_build_can_be_accepted_deliberately():
    """An explicit opt-in, so refusing is a decision rather than a wall.

    Refusing everything is not the same as verifying something, and a check
    that cannot be overridden is a check people turn off.
    """
    actual = SourceIdentity(revision=CLEAN_SHA, dirty=True, built_at="later")

    assert assert_deployment_fresh(CLEAN_SHA, actual, allow_dirty=True) is actual


def test_an_unidentifiable_deployment_is_refused():
    """`unknown/dev` cannot be verified, so it is never allowed to pass.

    This is the developer build. Running one is fine; *experimenting* on one is
    what produced the invalid run, so the check says so in those words.
    """
    actual = SourceIdentity(revision=UNKNOWN_REVISION, dirty=None, built_at="unknown")

    with pytest.raises(StaleDeploymentError, match="does not know what it was built from"):
        assert_deployment_fresh(CLEAN_SHA, actual)


def test_an_expected_value_that_is_not_a_commit_is_refused():
    """The other half of the same hole: a check against nothing passes forever.

    `assert_deployment_fresh("", actual)` must not be a silent success, and
    `unknown/dev` on the intended side must not be either.
    """
    actual = SourceIdentity(revision=CLEAN_SHA, dirty=False, built_at="t")

    for expected in ("", UNKNOWN_REVISION):
        with pytest.raises(StaleDeploymentError, match="not a commit"):
            assert_deployment_fresh(expected, actual)


def test_a_short_sha_is_not_accepted_as_a_match():
    """Prefixes are not equality.

    Comparing the first seven characters would have called a stale image fresh
    the first time two commits shared a prefix, which is exactly the shape of
    bug this whole mechanism exists to prevent.
    """
    actual = SourceIdentity(revision=CLEAN_SHA, dirty=False, built_at="t")

    with pytest.raises(StaleDeploymentError):
        assert_deployment_fresh(CLEAN_SHA[:7], actual)


# =============================================================================
# 4. The check is wired to the running deployment, not just to a function.
# =============================================================================


class _FakeHTTP:
    """A one-route HTTP server, so the script is tested through a socket.

    Monkeypatching `urlopen` would test the script's arithmetic. What is worth
    knowing is that the URL, the JSON, and the exit code survive an actual
    process boundary -- that is the path an operator takes.
    """

    def __init__(self, payload: dict | None, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self) -> _FakeHTTP:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        payload = self.payload
        status = self.status
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if payload is None:
                    server_self.requested_paths.append(self.path)
                    self.send_response(500)
                    self.end_headers()
                    return
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.requested_paths: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"


def _health_payload(identity: SourceIdentity) -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "source_revision": identity.revision,
        "source_dirty": identity.dirty,
        "source_state": identity.state,
        "build_time": identity.built_at,
        "components": [{"name": "database", "healthy": True}],
    }


def test_the_script_exits_zero_for_the_intended_deployment(capsys):
    with _FakeHTTP(_health_payload(SourceIdentity(CLEAN_SHA, False, "t"))) as server:
        code = freshness_main(["--expected", CLEAN_SHA, "--url", server.url])

    assert code == 0
    assert CLEAN_SHA in capsys.readouterr().out


def test_the_script_exits_one_for_a_stale_deployment(capsys):
    with _FakeHTTP(_health_payload(STALE)) as server:
        code = freshness_main(["--expected", CLEAN_SHA, "--url", server.url])

    captured = capsys.readouterr()

    assert code == 1
    assert "STALE DEPLOYMENT" in captured.err
    assert OLDER_SHA in captured.err
    assert CLEAN_SHA in captured.err


def test_an_unreachable_deployment_is_uncheckable_not_fresh(capsys):
    """Exit 2, not 0 and not 1.

    "I could not find out" is not "the image is wrong", and reporting the
    second when the truth is the first is how a real incident gets filed
    against the wrong thing.
    """
    with _FakeHTTP(None) as server:
        code = freshness_main(["--expected", CLEAN_SHA, "--url", server.url])

    assert code == 2
    assert "could not be performed" in capsys.readouterr().err


def test_a_response_without_the_fields_cannot_pass_by_omission():
    """Some other service answering /health is not a passing freshness check."""
    with _FakeHTTP({"status": "ok", "version": "0.1.0"}) as server:
        code = freshness_main(["--expected", CLEAN_SHA, "--url", server.url])

    assert code == 1


def test_HEAD_is_resolved_to_a_full_commit():
    """The operator should not have to type out a SHA to use the check."""
    resolved = resolve_expected("HEAD")

    assert len(resolved) == 40
    assert all(c in "0123456789abcdef" for c in resolved)


def test_a_short_sha_is_passed_through_untouched():
    assert resolve_expected(CLEAN_SHA[:7]) == CLEAN_SHA[:7]


def test_the_script_runs_as_a_standalone_process():
    """It has to work from a shell, not only as an import.

    A check that only works as a Python import is a check nobody runs during
    the one incident it exists for.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "--expected" in proc.stdout


def test_the_script_refuses_when_pointed_at_a_stale_server():
    """End to end, as a subprocess, exiting non-zero on a stale image."""
    with _FakeHTTP(_health_payload(STALE)) as server:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--expected",
                CLEAN_SHA,
                "--url",
                server.url,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    assert proc.returncode == 1
    assert "STALE DEPLOYMENT" in proc.stderr
    assert OLDER_SHA in proc.stderr


# =============================================================================
# 5. The build plumbing passes the identity through honestly.
# =============================================================================


def test_the_dockerfile_writes_all_three_fields():
    """`RUN` shell text, because that is what the image will actually execute."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    for arg in ("ARG SOURCE_REVISION", "ARG SOURCE_DIRTY", "ARG BUILD_TIME"):
        assert arg in dockerfile
    for field in ("SOURCE_REVISION", "SOURCE_DIRTY", "BUILD_TIME"):
        assert f"{field} = " in dockerfile
    assert "_build_meta.py" in dockerfile
    # And it maps the three honest answers onto three different Python values,
    # rather than writing whatever string arrived as if it were a bool.
    for value in ("true", "false", "None"):
        assert value in dockerfile


def test_the_dockerfile_default_build_is_unknown_not_a_fake_sha():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG SOURCE_REVISION=unknown/dev" in dockerfile
    assert "ARG SOURCE_DIRTY=unknown" in dockerfile


def test_compose_passes_the_dirty_flag_through():
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'SOURCE_REVISION: "${SOURCE_REVISION:-unknown/dev}"' in compose
    assert 'SOURCE_DIRTY: "${SOURCE_DIRTY:-unknown}"' in compose
    assert 'BUILD_TIME: "${BUILD_TIME:-unknown}"' in compose


def test_the_generated_module_is_a_plain_module_with_no_git_dependency():
    """The image has no .git and must not need one.

    A runtime `git rev-parse` would have returned a bind-mounted host value --
    the host's HEAD, not the image's -- which is the same class of lie.
    """
    from apps.orchestrator import _build_meta

    source = Path(_build_meta.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "git" not in source.replace("github", "")


def test_current_source_reports_the_module_values(monkeypatch):
    from apps.orchestrator import _build_meta

    monkeypatch.setattr(_build_meta, "SOURCE_REVISION", CLEAN_SHA)
    monkeypatch.setattr(_build_meta, "SOURCE_DIRTY", False)
    monkeypatch.setattr(_build_meta, "BUILD_TIME", "t")

    assert current_source() == SourceIdentity(CLEAN_SHA, False, "t")
