"""The optional runtime contract a project may declare (concern 81).

Build, lint, tests and a security audit all answer questions about the
*source*. None of them answers the one a person asks first: if I start this
application and open the page, does it work? The Development Orchestrator's own
UI campaign reached ``COMPLETE`` with every deterministic check green while a
hand check found that ``GET /api/projects`` returned HTTP 200 with
``Content-Type: text/html`` -- the single-page application's ``index.html``
served in place of the API. Nothing in the source said so, every test passed,
and the defect was only visible in a running browser.

So a project may declare, optionally, a *small* contract: the command that
starts it, a URL that means "up", a page to open, and the handful of
observations that must hold once it is open. This module is that contract and
the arithmetic over it. It is deliberately not a browser-testing framework:
there is no selector language, no interaction, no screenshot, no second page.

Three decisions worth stating:

* **Nothing here is framework-aware.** No port, no route, no framework name and
  no default start command appears in this module or anywhere else in the
  orchestrator. The manifest owns all of it, which is what makes the same
  mechanism usable for Angular, React, Vue or a hand-written server.
* **A status code is not the assertion.** The regression above returned 200.
  What distinguishes a working API from an SPA fallback is the media type, so
  ``expect_requests`` compares status *and* content type, and compares the
  latter by media type rather than by raw header equality -- ``application/
  json; charset=utf-8`` satisfies ``application/json``.
* **An expectation that was never observed fails.** A page that never issued
  the request the contract names has not demonstrated the behaviour; silence is
  not a pass.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .redaction import is_secret_name

#: Ceiling on ``readiness_timeout_seconds``. High enough for a cold
#: production build of a large front end, low enough that a contract cannot
#: quietly consume a whole run's budget. The orchestrator's own invocation
#: deadline still applies on top and may cut it shorter.
MAX_READINESS_TIMEOUT_SECONDS = 600
DEFAULT_READINESS_TIMEOUT_SECONDS = 120

#: How many expectations one contract may declare. V1 is a smoke contract.
MAX_EXPECTED_REQUESTS = 20

#: Schemes a contract may name. A runtime contract addresses the application
#: the orchestrator just started, over the worker's own network namespace.
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Bounds on the evidence carried back from a failure. A dev server's log and a
#: browser's console are both unbounded by nature; what reaches a prompt is not.
MAX_EVIDENCE_REQUESTS = 10
MAX_EVIDENCE_CONSOLE_ENTRIES = 10
MAX_EVIDENCE_CHARS = 2_000

_RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "start",
        "readiness_url",
        "readiness_timeout_seconds",
        "page",
        "expect_requests",
        "expect_text",
        "forbid_console_errors",
        "env",
    }
)
_EXPECT_REQUEST_KEYS: frozenset[str] = frozenset({"url_pattern", "status", "content_type"})

#: Environment names a contract may set. Deliberately conservative: the
#: contract is project-controlled, but it must not be a way to rewrite the
#: worker's own environment (``PATH``, ``HOME``) or to smuggle a credential
#: into a manifest that is committed to the repository.
_ENV_NAME = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z")


class RuntimeContractError(ValueError):
    """A runtime contract is not usable as declared.

    Raised by the parser so the manifest layer can report it like any other
    manifest defect: strictly, at import, before anything runs.
    """


@dataclass(frozen=True, slots=True)
class ExpectedRequest:
    """One request the opened page must make, and what it must come back as."""

    url_pattern: str
    status: int
    content_type: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "url_pattern": self.url_pattern,
            "status": self.status,
            "content_type": self.content_type,
        }

    def summary(self) -> str:
        expected = f"status {self.status}"
        if self.content_type:
            expected += f", content-type {self.content_type}"
        return f"{self.url_pattern} -> {expected}"


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    """What a project declares about its running self (V1).

    Every field but ``start`` is optional, and a contract with no assertions at
    all is still useful: it proves the application starts and answers.
    """

    start: str
    readiness_url: str
    readiness_timeout_seconds: int = DEFAULT_READINESS_TIMEOUT_SECONDS
    page: str | None = None
    expect_requests: tuple[ExpectedRequest, ...] = ()
    expect_text: str | None = None
    forbid_console_errors: bool = False
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def opens_a_browser(self) -> bool:
        """Whether anything here needs a page loaded at all.

        A contract that declares only ``start`` and ``readiness_url`` is
        answered by an HTTP poll, and launching Chromium for it would spend a
        second of startup to observe nothing.
        """
        return bool(
            self.page
            and (
                self.expect_requests
                or self.expect_text
                or self.forbid_console_errors
            )
        )

    def describe(self) -> dict[str, object]:
        return {
            "start": self.start,
            "readiness_url": self.readiness_url,
            "readiness_timeout_seconds": self.readiness_timeout_seconds,
            "page": self.page,
            "expect_requests": [entry.describe() for entry in self.expect_requests],
            "expect_text": self.expect_text,
            "forbid_console_errors": self.forbid_console_errors,
            "env": dict(self.env),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> RuntimeContract | None:
        """Rebuild a contract from stored JSON, or ``None`` when there is none.

        The lenient direction, like ``VerificationProfile.from_mapping``: a row
        written by an older build has no ``runtime`` key and must still load.
        Rejecting a typo is ``parse_runtime_contract``'s job, and it runs at
        import, over the manifest.
        """
        if not payload:
            return None
        return parse_runtime_contract(payload, where="verification.runtime")


def parse_runtime_contract(
    payload: Mapping[str, object] | None, *, where: str = "verification.runtime"
) -> RuntimeContract | None:
    """Validate a declared runtime contract. ``None`` when none was declared.

    Strict in both directions an author gets wrong: an unknown key is a typo
    that would otherwise silently disable an assertion, and a URL or a timeout
    that cannot be honoured is a defect in the manifest rather than a failure
    to discover during a run.

    Raises:
        RuntimeContractError: the contract is unusable as declared.
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise RuntimeContractError(f"{where} must be a mapping")
    if not payload:
        raise RuntimeContractError(
            f"{where} was declared but is empty; remove it or declare a start command"
        )
    _reject_unknown(payload, _RUNTIME_KEYS, where)

    timeout = payload.get("readiness_timeout_seconds")
    forbid = payload.get("forbid_console_errors", False)
    if not isinstance(forbid, bool):
        raise RuntimeContractError(f"{where}.forbid_console_errors must be true or false")

    page = _optional_url(payload.get("page"), f"{where}.page")
    expectations = _parse_expectations(payload.get("expect_requests"), where)
    expect_text = _optional_text(payload.get("expect_text"), f"{where}.expect_text")
    if (expectations or expect_text or forbid) and page is None:
        # An assertion about a page with no page to open could only ever fail,
        # and it would fail at run time rather than at import.
        raise RuntimeContractError(
            f"{where}.page is required when expect_requests, expect_text or "
            f"forbid_console_errors is declared"
        )

    return RuntimeContract(
        start=_required_text(payload.get("start"), f"{where}.start"),
        readiness_url=_required_url(payload.get("readiness_url"), f"{where}.readiness_url"),
        readiness_timeout_seconds=(
            DEFAULT_READINESS_TIMEOUT_SECONDS
            if timeout is None
            else _parse_timeout(timeout, f"{where}.readiness_timeout_seconds")
        ),
        page=page,
        expect_requests=expectations,
        expect_text=expect_text,
        forbid_console_errors=forbid,
        env=_parse_env(payload.get("env"), f"{where}.env"),
    )


def _parse_expectations(value: object, where: str) -> tuple[ExpectedRequest, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise RuntimeContractError(f"{where}.expect_requests must be a list")
    if len(value) > MAX_EXPECTED_REQUESTS:
        raise RuntimeContractError(
            f"{where}.expect_requests declares {len(value)} expectations; at most "
            f"{MAX_EXPECTED_REQUESTS} are permitted"
        )
    return tuple(
        _parse_expectation(entry, f"{where}.expect_requests[{index}]")
        for index, entry in enumerate(value)
    )


def _parse_expectation(value: object, where: str) -> ExpectedRequest:
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{where} must be a mapping")
    _reject_unknown(value, _EXPECT_REQUEST_KEYS, where)
    pattern = _required_text(value.get("url_pattern"), f"{where}.url_pattern")
    _compile_pattern(pattern, f"{where}.url_pattern")
    status = value.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise RuntimeContractError(f"{where}.status must be an integer")
    if not 100 <= status <= 599:
        raise RuntimeContractError(f"{where}.status must be an HTTP status code")
    content_type = _optional_text(value.get("content_type"), f"{where}.content_type")
    if content_type is not None and "/" not in content_type:
        raise RuntimeContractError(
            f"{where}.content_type must be a media type such as application/json"
        )
    return ExpectedRequest(url_pattern=pattern, status=status, content_type=content_type)


def _parse_timeout(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeContractError(f"{where} must be an integer number of seconds")
    if value < 1:
        raise RuntimeContractError(f"{where} must be >= 1")
    if value > MAX_READINESS_TIMEOUT_SECONDS:
        raise RuntimeContractError(
            f"{where} must be <= {MAX_READINESS_TIMEOUT_SECONDS}"
        )
    return value


def _parse_env(value: object, where: str) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeContractError(f"{where} must be a mapping")
    resolved: dict[str, str] = {}
    for key, entry in value.items():
        name = str(key)
        if not _ENV_NAME.match(name):
            raise RuntimeContractError(
                f"{where} key {name!r} must be an upper-case environment variable name"
            )
        if is_secret_name(name):
            # A manifest is committed to the repository. A contract that could
            # set ``API_TOKEN`` would make the manifest the place a credential
            # ends up, and section 36 injects secrets individually instead.
            raise RuntimeContractError(
                f"{where} must not set {name!r}: a credential-shaped variable belongs "
                f"in the orchestrator's secret injection, not in a manifest"
            )
        if not isinstance(entry, str | int | float) or isinstance(entry, bool):
            raise RuntimeContractError(f"{where}.{name} must be a string or a number")
        resolved[name] = str(entry)
    return resolved


def _required_text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(f"{where} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, where)


def _required_url(value: object, where: str) -> str:
    url = _required_text(value, where)
    scheme, separator, rest = url.partition("://")
    if not separator or scheme.casefold() not in ALLOWED_URL_SCHEMES or not rest:
        allowed = ", ".join(sorted(ALLOWED_URL_SCHEMES))
        raise RuntimeContractError(
            f"{where} must be an absolute URL using one of: {allowed}"
        )
    if any(character.isspace() for character in url):
        raise RuntimeContractError(f"{where} must not contain whitespace")
    return url


def _optional_url(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _required_url(value, where)


def _reject_unknown(mapping: Mapping[str, object], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise RuntimeContractError(f"Unknown key(s) in {where}: {', '.join(unknown)}")


# ------------------------------------------------------------------ matching


def _compile_pattern(pattern: str, where: str) -> re.Pattern[str]:
    """A URL glob as a regular expression.

    The three wildcards a URL expectation needs and nothing else: ``**`` spans
    path separators, ``*`` does not, ``?`` is one character. Everything else is
    literal, so a pattern cannot accidentally be a regular expression -- a
    ``.`` in a host name would otherwise match any character.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if pattern.startswith("**", index):
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
        index += 1
    try:
        return re.compile("".join(parts), re.IGNORECASE)
    except re.error as error:  # pragma: no cover - the escaping prevents this
        raise RuntimeContractError(f"{where} is not a usable pattern: {error}") from error


def url_matches(pattern: str, url: str) -> bool:
    """Whether ``url`` satisfies a contract's ``url_pattern``.

    A full match, so ``**/api/projects`` does not match ``/api/projects/7``.
    An author who wants the prefix writes ``**/api/projects**``.
    """
    return _compile_pattern(pattern, "url_pattern").fullmatch(url) is not None


def media_type(header: str | None) -> str:
    """The media type of a ``Content-Type`` header, without its parameters.

    ``application/json; charset=utf-8`` is ``application/json``. Comparing raw
    headers would make a contract depend on whether a server bothered to
    declare a charset, which is not the thing being asserted.
    """
    if not header:
        return ""
    return header.split(";", 1)[0].strip().casefold()


def content_type_matches(expected: str | None, observed: str | None) -> bool:
    """Media-type equivalence, which is what an expectation is really about."""
    if not expected:
        return True
    return media_type(expected) == media_type(observed)


# --------------------------------------------------------------- observations


@dataclass(frozen=True, slots=True)
class ObservedResponse:
    """One response the browser saw, as the probe reported it."""

    url: str
    status: int
    content_type: str = ""

    def describe(self) -> dict[str, object]:
        return {"url": self.url, "status": self.status, "content_type": self.content_type}

    def summary(self) -> str:
        return f"{self.url} -> status {self.status}, content-type {self.content_type or '(none)'}"


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """What the probe saw after loading the configured page.

    Facts only. Whether they satisfy the contract is ``evaluate``'s question,
    which is why this type can be constructed in a test without a browser.
    """

    page_url: str = ""
    loaded: bool = True
    load_error: str = ""
    page_text: str = ""
    responses: tuple[ObservedResponse, ...] = ()
    console_errors: tuple[str, ...] = ()
    page_errors: tuple[str, ...] = ()
    #: Responses the probe dropped because it had already kept its ceiling.
    responses_omitted: int = 0

    def describe(self) -> dict[str, object]:
        return {
            "page_url": self.page_url,
            "loaded": self.loaded,
            "load_error": _clip(self.load_error),
            "responses": [
                entry.describe() for entry in self.responses[:MAX_EVIDENCE_REQUESTS]
            ],
            "responses_observed": len(self.responses),
            "responses_omitted": self.responses_omitted,
            "console_errors": [
                _clip(entry) for entry in self.console_errors[:MAX_EVIDENCE_CONSOLE_ENTRIES]
            ],
            "page_errors": [
                _clip(entry) for entry in self.page_errors[:MAX_EVIDENCE_CONSOLE_ENTRIES]
            ],
            "page_text_length": len(self.page_text),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RuntimeObservation:
        """Build an observation from the probe's JSON. Tolerant by design.

        The probe is the orchestrator's own program, but it runs inside a
        worker and reports over stdout; a missing field is treated as absent
        rather than raising, because the assertions below then fail with
        readable evidence instead of the pipeline raising an infrastructure
        error over a browser quirk.
        """
        responses = payload.get("responses")
        entries: list[ObservedResponse] = []
        if isinstance(responses, Sequence) and not isinstance(responses, str | bytes):
            for entry in responses:
                if not isinstance(entry, Mapping):
                    continue
                try:
                    status = int(entry.get("status") or 0)
                except (TypeError, ValueError):
                    status = 0
                entries.append(
                    ObservedResponse(
                        url=str(entry.get("url") or ""),
                        status=status,
                        content_type=str(entry.get("content_type") or ""),
                    )
                )
        return cls(
            page_url=str(payload.get("page_url") or ""),
            loaded=bool(payload.get("loaded", False)),
            load_error=str(payload.get("load_error") or ""),
            page_text=str(payload.get("page_text") or ""),
            responses=tuple(entries),
            console_errors=_strings(payload.get("console_errors")),
            page_errors=_strings(payload.get("page_errors")),
            responses_omitted=int(payload.get("responses_omitted") or 0),
        )


@dataclass(frozen=True, slots=True)
class AssertionFailure:
    """One unmet expectation, written so a coder can act on it.

    ``expected`` and ``observed`` are separate fields rather than a sentence,
    because the regression this exists for is exactly a difference between two
    values: ``application/json`` against ``text/html``.
    """

    assertion: str
    expected: str
    observed: str
    #: The request this is about, when it is about one.
    request_url: str = ""
    status: int | None = None
    content_type: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "assertion": self.assertion,
            "expected": _clip(self.expected),
            "observed": _clip(self.observed),
            "request_url": self.request_url,
            "status": self.status,
            "content_type": self.content_type,
        }

    def summary(self) -> str:
        subject = f" for {self.request_url}" if self.request_url else ""
        return (
            f"{self.assertion}{subject}: expected {self.expected}, "
            f"observed {self.observed}"
        )


def evaluate(
    contract: RuntimeContract, observation: RuntimeObservation
) -> tuple[AssertionFailure, ...]:
    """The contract's verdict over one observation, as a list of failures.

    Every assertion is evaluated rather than stopping at the first: a page that
    served HTML for its API *and* rendered no text has two facts worth telling
    the coder, and running the browser again to find the second one would cost
    the same minute twice.
    """
    failures: list[AssertionFailure] = []
    if not observation.loaded:
        failures.append(
            AssertionFailure(
                assertion="page load",
                expected=f"{contract.page} loads",
                observed=observation.load_error or "the page did not load",
            )
        )
        # Everything below is about a page that is open. Reporting "expected
        # text missing" about a page that never loaded would bury the cause.
        return tuple(failures)

    for expectation in contract.expect_requests:
        failures.extend(_evaluate_request(expectation, observation))

    if contract.expect_text and contract.expect_text not in observation.page_text:
        failures.append(
            AssertionFailure(
                assertion="rendered text",
                expected=f"the page text contains {contract.expect_text!r}",
                observed=(
                    f"it does not ({len(observation.page_text)} character(s) rendered)"
                ),
            )
        )

    if contract.forbid_console_errors:
        for entry in observation.console_errors[:MAX_EVIDENCE_CONSOLE_ENTRIES]:
            failures.append(
                AssertionFailure(
                    assertion="console error",
                    expected="no console.error call",
                    observed=_clip(entry),
                )
            )
        for entry in observation.page_errors[:MAX_EVIDENCE_CONSOLE_ENTRIES]:
            failures.append(
                AssertionFailure(
                    assertion="uncaught page error",
                    expected="no uncaught error",
                    observed=_clip(entry),
                )
            )
    return tuple(failures)


def _evaluate_request(
    expectation: ExpectedRequest, observation: RuntimeObservation
) -> list[AssertionFailure]:
    matching = [
        response
        for response in observation.responses
        if url_matches(expectation.url_pattern, response.url)
    ]
    if not matching:
        return [
            AssertionFailure(
                assertion="expected request",
                expected=expectation.summary(),
                observed="the page made no matching request",
            )
        ]
    # Any one matching response satisfying the whole expectation is a pass: a
    # page may legitimately issue the same request twice, and a retry after a
    # success is not a defect. The evidence for a failure is the first match,
    # which is the one the application acted on.
    if any(
        response.status == expectation.status
        and content_type_matches(expectation.content_type, response.content_type)
        for response in matching
    ):
        return []
    first = matching[0]
    failures: list[AssertionFailure] = []
    if first.status != expectation.status:
        failures.append(
            AssertionFailure(
                assertion="response status",
                expected=str(expectation.status),
                observed=str(first.status),
                request_url=first.url,
                status=first.status,
                content_type=first.content_type,
            )
        )
    if not content_type_matches(expectation.content_type, first.content_type):
        failures.append(
            AssertionFailure(
                assertion="response content-type",
                expected=str(expectation.content_type),
                observed=first.content_type or "(none)",
                request_url=first.url,
                status=first.status,
                content_type=first.content_type,
            )
        )
    return failures


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(entry) for entry in value if str(entry).strip())
    return ()


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} character(s) omitted ...]"


__all__ = [
    "ALLOWED_URL_SCHEMES",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_CONSOLE_ENTRIES",
    "MAX_EVIDENCE_REQUESTS",
    "MAX_EXPECTED_REQUESTS",
    "MAX_READINESS_TIMEOUT_SECONDS",
    "AssertionFailure",
    "ExpectedRequest",
    "ObservedResponse",
    "RuntimeContract",
    "RuntimeContractError",
    "RuntimeObservation",
    "content_type_matches",
    "evaluate",
    "media_type",
    "parse_runtime_contract",
    "url_matches",
]
