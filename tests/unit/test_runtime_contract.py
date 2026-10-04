"""The optional runtime contract: what may be declared, and what it means.

Concern 81. The point of keeping ``domain.runtime_contract`` pure is visible
here: every assertion this feature makes -- including the regression it exists
for, an API route answering with ``text/html`` -- is decided by a function over
plain data, so it is pinned down without a browser, a worker, a container or a
network.

Nothing in this file names a framework, and the ports and routes that appear
are a fixture project's, declared the way a manifest declares them.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.errors import ManifestError
from apps.orchestrator.domain.manifest import parse_manifest
from apps.orchestrator.domain.runtime_contract import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    MAX_READINESS_TIMEOUT_SECONDS,
    ExpectedRequest,
    ObservedResponse,
    RuntimeContract,
    RuntimeContractError,
    RuntimeDependency,
    RuntimeObservation,
    content_type_matches,
    evaluate,
    media_type,
    parse_runtime_contract,
    url_matches,
)
from apps.orchestrator.domain.verification import VerificationProfile

#: The contract the orchestrator's own UI campaign should have been made to
#: declare. Written out once, in full, because it is also the documentation of
#: what V1 supports.
_DECLARED = {
    "start": "npm start",
    "readiness_url": "http://localhost:4200/",
    "readiness_timeout_seconds": 90,
    "page": "http://localhost:4200/projects",
    "expect_requests": [
        {
            "url_pattern": "**/api/projects",
            "status": 200,
            "content_type": "application/json",
        }
    ],
    "expect_text": "Projects",
    "forbid_console_errors": True,
    "env": {"PORT": "4200"},
}


def _manifest(verification: dict | None) -> dict:
    document = {
        "version": 1,
        "project": {"id": "proj", "name": "Proj", "repository": "/tmp/proj"},
        "tasks": [{"id": "T-1", "title": "A task"}],
    }
    if verification is not None:
        document["verification"] = verification
    return document


# --- what may be declared ----------------------------------------------------


def test_a_declared_runtime_contract_parses_into_every_v1_field():
    contract = parse_runtime_contract(_DECLARED)

    assert contract == RuntimeContract(
        start="npm start",
        readiness_url="http://localhost:4200/",
        readiness_timeout_seconds=90,
        page="http://localhost:4200/projects",
        expect_requests=(
            ExpectedRequest(
                url_pattern="**/api/projects", status=200, content_type="application/json"
            ),
        ),
        expect_text="Projects",
        forbid_console_errors=True,
        env={"PORT": "4200"},
    )
    assert contract.opens_a_browser


def test_a_manifest_that_declares_a_runtime_contract_carries_it_on_the_profile():
    manifest = parse_manifest(
        _manifest({"build": ["npm run build"], "runtime": dict(_DECLARED)})
    )

    assert manifest.verification.build == ("npm run build",)
    assert manifest.verification.has_runtime
    assert manifest.verification.runtime.page == "http://localhost:4200/projects"


def test_a_manifest_without_a_runtime_contract_stays_valid_and_declares_none():
    """The compatibility requirement, stated as a test: ``runtime`` is optional
    in the strongest sense -- absent, nothing about the project changes."""
    manifest = parse_manifest(_manifest({"build": ["npm run build"]}))

    assert manifest.verification.runtime is None
    assert not manifest.verification.has_runtime
    assert parse_manifest(_manifest(None)).verification.runtime is None


def test_the_minimum_contract_is_a_command_and_a_readiness_url():
    contract = parse_runtime_contract(
        {"start": "npm start", "readiness_url": "http://127.0.0.1:8080/health"}
    )

    assert contract.readiness_timeout_seconds == DEFAULT_READINESS_TIMEOUT_SECONDS
    assert contract.expect_requests == ()
    # Nothing to see in a browser, so no browser is launched for it.
    assert not contract.opens_a_browser


def test_a_runtime_contract_may_declare_dependencies():
    contract = parse_runtime_contract(
        {
            "dependencies": [
                {
                    "name": "fixture-api",
                    "start": "python3 tools/api.py",
                    "readiness_url": "http://127.0.0.1:8000/health",
                    "readiness_timeout_seconds": 30,
                    "env": {"PORT": "8000"},
                }
            ],
            "start": "npm start",
            "readiness_url": "http://127.0.0.1:4200/",
        }
    )

    assert contract.dependencies == (
        RuntimeDependency(
            name="fixture-api",
            start="python3 tools/api.py",
            readiness_url="http://127.0.0.1:8000/health",
            readiness_timeout_seconds=30,
            env={"PORT": "8000"},
        ),
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"start": "npm start", "readiness_url": "http://x/", "oops": 1}, "oops"),
        ({"start": "npm start"}, "readiness_url"),
        ({"readiness_url": "http://x/"}, "start"),
        ({"start": "npm start", "readiness_url": "ftp://x/"}, "absolute URL"),
        ({"start": "npm start", "readiness_url": "localhost:4200"}, "absolute URL"),
        ({"start": " ", "readiness_url": "http://x/"}, "start"),
        (
            {"start": "npm start", "readiness_url": "http://x/", "forbid_console_errors": "yes"},
            "true or false",
        ),
        # An assertion with no page to open could only ever fail, and it would
        # fail during a run rather than at import.
        (
            {"start": "npm start", "readiness_url": "http://x/", "expect_text": "Hi"},
            "page is required",
        ),
        ({}, "empty"),
    ],
)
def test_an_unusable_contract_is_refused_with_the_key_that_is_wrong(payload, expected):
    with pytest.raises(RuntimeContractError, match=expected):
        parse_runtime_contract(payload)


@pytest.mark.parametrize(
    ("dependency", "expected"),
    [
        ({"start": "python3 tools/api.py", "readiness_url": "http://x/"}, "name"),
        ({"name": "fixture-api", "readiness_url": "http://x/"}, "start"),
        ({"name": "fixture-api", "start": "python3 tools/api.py"}, "readiness_url"),
        (
            {
                "name": "fixture-api",
                "start": "python3 tools/api.py",
                "readiness_url": "http://x/",
                "oops": 1,
            },
            "oops",
        ),
        (
            {"name": "bad name", "start": "python3 tools/api.py", "readiness_url": "http://x/"},
            "name",
        ),
        (
            {"name": "fixture-api", "start": " ", "readiness_url": "http://x/"},
            "start",
        ),
        (
            {"name": "fixture-api", "start": "python3 tools/api.py", "readiness_url": "ftp://x/"},
            "absolute URL",
        ),
        (
            {
                "name": "fixture-api",
                "start": "python3 tools/api.py",
                "readiness_url": "http://x/",
                "readiness_timeout_seconds": 0,
            },
            "readiness_timeout_seconds",
        ),
        (
            {
                "name": "fixture-api",
                "start": "python3 tools/api.py",
                "readiness_url": "http://x/",
                "env": {"API_TOKEN": "secret"},
            },
            "API_TOKEN",
        ),
    ],
)
def test_an_unusable_dependency_is_refused_with_the_key_that_is_wrong(
    dependency, expected
):
    with pytest.raises(RuntimeContractError, match=expected):
        parse_runtime_contract(
            {
                "dependencies": [dependency],
                "start": "npm start",
                "readiness_url": "http://x/",
            }
        )


def test_duplicate_dependency_names_are_refused():
    with pytest.raises(RuntimeContractError, match="duplicates"):
        parse_runtime_contract(
            {
                "dependencies": [
                    {
                        "name": "fixture-api",
                        "start": "python3 tools/api.py",
                        "readiness_url": "http://x/",
                    },
                    {
                        "name": "fixture-api",
                        "start": "python3 tools/other.py",
                        "readiness_url": "http://x/other",
                    },
                ],
                "start": "npm start",
                "readiness_url": "http://x/app",
            }
        )


@pytest.mark.parametrize("timeout", [0, -1, MAX_READINESS_TIMEOUT_SECONDS + 1, "90", 1.5, True])
def test_an_unusable_readiness_timeout_is_refused(timeout):
    with pytest.raises(RuntimeContractError, match="readiness_timeout_seconds"):
        parse_runtime_contract(
            {
                "start": "npm start",
                "readiness_url": "http://x/",
                "readiness_timeout_seconds": timeout,
            }
        )


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"url_pattern": "**/api", "status": 200, "oops": 1}, "oops"),
        ({"status": 200}, "url_pattern"),
        ({"url_pattern": "**/api"}, "status"),
        ({"url_pattern": "**/api", "status": 99}, "HTTP status"),
        ({"url_pattern": "**/api", "status": "200"}, "integer"),
        ({"url_pattern": "**/api", "status": 200, "content_type": "json"}, "media type"),
    ],
)
def test_an_unusable_request_expectation_is_refused(entry, expected):
    with pytest.raises(RuntimeContractError, match=expected):
        parse_runtime_contract(
            {
                "start": "npm start",
                "readiness_url": "http://x/",
                "page": "http://x/p",
                "expect_requests": [entry],
            }
        )


def test_an_unknown_runtime_key_fails_manifest_validation_like_any_other_typo():
    with pytest.raises(ManifestError, match="Unknown key.*expect_requets"):
        parse_manifest(
            _manifest(
                {
                    "runtime": {
                        "start": "npm start",
                        "readiness_url": "http://x/",
                        "expect_requets": [],
                    }
                }
            )
        )


def test_a_contract_may_not_carry_a_credential_shaped_variable():
    """A manifest is committed. Secrets are injected individually (section 36)."""
    with pytest.raises(RuntimeContractError, match="API_TOKEN"):
        parse_runtime_contract(
            {"start": "npm start", "readiness_url": "http://x/", "env": {"API_TOKEN": "t"}}
        )


# --- how it is stored --------------------------------------------------------


def test_a_contract_round_trips_through_the_stored_profile():
    profile = VerificationProfile(
        build=("npm run build",), runtime=parse_runtime_contract(_DECLARED)
    )

    assert VerificationProfile.from_mapping(profile.describe()) == profile


def test_a_profile_without_a_contract_stores_exactly_what_it_always_stored():
    """What keeps the importer from seeing a change on every import, and a
    legacy row from loading differently than it did before."""
    profile = VerificationProfile(build=("npm run build",), tests=("npm test",))

    assert profile.describe() == {
        "build": ["npm run build"],
        "lint": [],
        "tests": ["npm test"],
        "security": [],
    }
    assert VerificationProfile.from_mapping(profile.describe()) == profile


def test_a_stored_row_written_before_this_feature_still_loads():
    legacy = {"build": ["npm run build"], "lint": [], "tests": ["npm test"], "security": []}

    profile = VerificationProfile.from_mapping(legacy)

    assert profile.runtime is None
    assert profile.tests == ("npm test",)
    assert not profile.is_empty


def test_the_contract_survives_a_task_adding_its_own_verification_commands():
    profile = VerificationProfile(tests=("npm test",), runtime=parse_runtime_contract(_DECLARED))

    assert profile.with_task_commands(["npm run e2e"]).runtime == profile.runtime


# --- url patterns ------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "url", "matches"),
    [
        ("**/api/projects", "http://localhost:4200/api/projects", True),
        ("**/api/projects", "https://example.test:8080/api/projects", True),
        ("**/api/projects", "http://localhost:4200/api/projects/7", False),
        ("**/api/projects**", "http://localhost:4200/api/projects/7", True),
        ("**/api/*", "http://localhost:4200/api/projects", True),
        ("**/api/*", "http://localhost:4200/api/projects/7", False),
        ("http://localhost:4200/api/projects", "http://localhost:4200/api/projects", True),
        # A literal is a literal: a dot in a host must not behave like a
        # regular expression's any-character.
        ("**/a.b", "http://x/axb", False),
        ("**/api/projects", "HTTP://LOCALHOST:4200/API/PROJECTS", True),
    ],
)
def test_a_url_pattern_matches_what_it_says_and_nothing_more(pattern, url, matches):
    assert url_matches(pattern, url) is matches


# --- content types -----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("application/json", "application/json"),
        ("application/json; charset=utf-8", "application/json"),
        ("  TEXT/HTML ; charset=UTF-8", "text/html"),
        (None, ""),
        ("", ""),
    ],
)
def test_a_content_type_is_compared_by_media_type_not_by_raw_header(header, expected):
    assert media_type(header) == expected


def test_a_charset_parameter_does_not_break_an_expectation():
    assert content_type_matches("application/json", "application/json; charset=utf-8")
    assert content_type_matches("application/json", "application/json")
    assert not content_type_matches("application/json", "text/html; charset=utf-8")
    # No expectation declared is no assertion made.
    assert content_type_matches(None, "text/html")


# --- the verdict -------------------------------------------------------------


def _observation(**overrides) -> RuntimeObservation:
    defaults = {
        "page_url": "http://localhost:4200/projects",
        "loaded": True,
        "page_text": "Projects\nTraceStack\n",
        "responses": (
            ObservedResponse(
                url="http://localhost:4200/projects", status=200, content_type="text/html"
            ),
            ObservedResponse(
                url="http://localhost:4200/api/projects",
                status=200,
                content_type="application/json; charset=utf-8",
            ),
        ),
    }
    return RuntimeObservation(**{**defaults, "responses": defaults["responses"], **overrides})


def test_a_working_application_satisfies_its_contract():
    assert evaluate(parse_runtime_contract(_DECLARED), _observation()) == ()


def test_the_spa_fallback_regression_fails_on_the_content_type():
    """**The named regression.** The Development Orchestrator's UI campaign
    reached COMPLETE while ``GET /api/projects`` returned HTTP 200 with
    ``Content-Type: text/html`` -- the single-page application's index served
    where the API should have been. Status alone says nothing; the media type
    is the whole assertion."""
    observation = _observation(
        responses=(
            ObservedResponse(
                url="http://localhost:4200/api/projects",
                status=200,
                content_type="text/html; charset=utf-8",
            ),
        )
    )

    failures = evaluate(parse_runtime_contract(_DECLARED), observation)

    content_type = [f for f in failures if f.assertion == "response content-type"]
    assert len(content_type) == 1
    assert content_type[0].expected == "application/json"
    assert content_type[0].observed == "text/html; charset=utf-8"
    assert content_type[0].request_url == "http://localhost:4200/api/projects"
    assert content_type[0].status == 200
    # And no status failure, because the status really was 200: the evidence
    # must point at the thing that is wrong.
    assert not [f for f in failures if f.assertion == "response status"]


def test_an_expected_request_that_never_happened_fails():
    observation = _observation(
        responses=(
            ObservedResponse(
                url="http://localhost:4200/projects", status=200, content_type="text/html"
            ),
        )
    )

    failures = evaluate(parse_runtime_contract(_DECLARED), observation)

    assert [f.assertion for f in failures] == ["expected request"]
    assert "no matching request" in failures[0].observed
    assert "**/api/projects" in failures[0].expected


def test_a_wrong_status_fails_and_says_both_numbers():
    observation = _observation(
        responses=(
            ObservedResponse(
                url="http://localhost:4200/api/projects",
                status=500,
                content_type="application/json",
            ),
        )
    )

    failures = evaluate(parse_runtime_contract(_DECLARED), observation)

    assert [f.assertion for f in failures] == ["response status"]
    assert (failures[0].expected, failures[0].observed) == ("200", "500")


def test_a_request_satisfied_by_any_one_response_passes():
    """A page may issue the same request twice; a retry after a success is not
    a defect."""
    observation = _observation(
        responses=(
            ObservedResponse(
                url="http://localhost:4200/api/projects", status=503, content_type="text/html"
            ),
            ObservedResponse(
                url="http://localhost:4200/api/projects",
                status=200,
                content_type="application/json",
            ),
        )
    )

    assert not [
        f for f in evaluate(parse_runtime_contract(_DECLARED), observation)
        if f.assertion.startswith("response")
    ]


def test_missing_rendered_text_fails():
    failures = evaluate(parse_runtime_contract(_DECLARED), _observation(page_text="Loading..."))

    assert [f.assertion for f in failures] == ["rendered text"]
    assert "'Projects'" in failures[0].expected


def test_a_console_error_fails_when_the_contract_forbids_one():
    failures = evaluate(
        parse_runtime_contract(_DECLARED),
        _observation(console_errors=("TypeError: x is not a function",)),
    )

    assert [f.assertion for f in failures] == ["console error"]
    assert "TypeError" in failures[0].observed


def test_an_uncaught_page_error_fails_when_the_contract_forbids_console_errors():
    failures = evaluate(
        parse_runtime_contract(_DECLARED), _observation(page_errors=("Error: boom",))
    )

    assert [f.assertion for f in failures] == ["uncaught page error"]


def test_ordinary_console_output_is_not_a_failure():
    """``forbid_console_errors`` forbids errors. A chatty application is not a
    broken one, and the probe reports only ``console.error`` as an error."""
    assert evaluate(parse_runtime_contract(_DECLARED), _observation(console_errors=())) == ()


def test_console_errors_are_ignored_when_the_contract_permits_them():
    permissive = parse_runtime_contract({**_DECLARED, "forbid_console_errors": False})

    assert evaluate(permissive, _observation(console_errors=("TypeError: x",))) == ()


def test_a_page_that_did_not_load_fails_once_and_says_why():
    """Reporting "expected text missing" about a page that never opened would
    bury the cause under its consequences."""
    failures = evaluate(
        parse_runtime_contract(_DECLARED),
        _observation(loaded=False, load_error="net::ERR_CONNECTION_REFUSED", page_text=""),
    )

    assert [f.assertion for f in failures] == ["page load"]
    assert "ERR_CONNECTION_REFUSED" in failures[0].observed


def test_every_unmet_assertion_is_reported_not_just_the_first():
    observation = _observation(
        responses=(
            ObservedResponse(
                url="http://localhost:4200/api/projects", status=200, content_type="text/html"
            ),
        ),
        page_text="",
        console_errors=("TypeError: x",),
    )

    assertions = {f.assertion for f in evaluate(parse_runtime_contract(_DECLARED), observation)}

    assert assertions == {"response content-type", "rendered text", "console error"}


def test_an_observation_from_the_probes_json_is_read_tolerantly():
    observation = RuntimeObservation.from_mapping(
        {
            "page_url": "http://localhost:4200/projects",
            "loaded": True,
            "page_text": "Projects",
            "responses": [
                {"url": "http://localhost:4200/api/projects", "status": "200"},
                "not a mapping",
            ],
        }
    )

    assert observation.responses[0].status == 200
    assert observation.responses[0].content_type == ""
    assert observation.console_errors == ()
    assert len(observation.responses) == 1
