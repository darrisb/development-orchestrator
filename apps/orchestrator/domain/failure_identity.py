"""Stable failure identities, and the set arithmetic over them (concern 78, stage 2).

Stage 1 stopped the coder from measuring the project's baseline. Something
still has to, or every pre-existing failure becomes a repair instruction: the
orchestrator runs the authoritative suite, gets a non-zero exit code, and has
no way to tell "your change broke three tests" from "this tree was already
failing forty-two". This module is the half that tells them apart.

The unit is a **failure identity**: a string that names the same failure across
two runs of the same command. For pytest that is the node id
(``tests/test_x.py::test_y``); for another runner it is whatever that runner
prints that is equally stable. Counts are deliberately not an identity, because
they are not one:

    baseline  = 42 failures
    candidate = 43 failures

is consistent with one baseline failure being fixed and two new ones appearing,
and also with forty-three entirely different tests failing. ``compare`` works
only on sets of identities, and never on their sizes.

Three rules shape everything here.

* **Extraction may fail, and saying so is the useful answer.** An extractor
  returns ``None`` -- not an empty set -- when it cannot read the output it was
  given. ``None`` means *unknown*, propagates to
  ``VerificationClassification.UNCLASSIFIED_FAILURE``, and is the only honest
  value for a timeout, a truncated log, or a runner nobody has written an
  adapter for. An empty set means *this command reported no failing tests*,
  which is a different and much stronger claim.
* **An extractor cross-checks itself against the runner's own count.** The
  output a step carries is a tail (``MAX_STEP_OUTPUT_CHARS``), so the list of
  ``FAILED`` lines can be clipped while still looking perfectly well-formed.
  Reading forty of forty-two failures and calling the set complete is exactly
  the mistake that would classify a real regression as known. So an adapter
  that cannot reconcile what it parsed with what the runner said it found
  returns ``None``.
* **Identities are never invented.** There is no normalisation that guesses,
  no fuzzy matching, no stripping of parametrisation to make two different
  failures look like one.

Adding a runner means adding a ``FailureExtractor`` to ``FAILURE_EXTRACTORS``.
Stage 2 ships one, for pytest, because that is the runner whose output the
repository already has; the interface exists so the second one is an addition
rather than a redesign. A project whose suite nothing can read is not broken by
this -- it classifies as ``UNCLASSIFIED_FAILURE`` and behaves exactly as it did
before stage 2.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

#: Ceiling on identities carried in one comparison, so a tree with thousands of
#: failures cannot turn a report row or a prompt into a wall of node ids. The
#: *classification* is computed over the full sets; this bounds what is
#: rendered and stored.
MAX_RENDERED_IDENTITIES = 20


@dataclass(frozen=True, slots=True)
class FailureExtractor:
    """One runner's output, read as a set of failure identities.

    ``extract`` returns ``None`` when this extractor does not recognise the
    output, or recognises it but cannot vouch for completeness.
    """

    name: str
    extract: Callable[[str], frozenset[str] | None]


# ----------------------------------------------------------------- pytest

#: ``FAILED tests/test_x.py::test_y - AssertionError`` and the collection-error
#: form ``ERROR tests/test_x.py``. Anchored at the line start: the same words
#: appear inside tracebacks and inside captured output, and a match there would
#: invent an identity.
_PYTEST_FAILURE_LINE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<identity>[^\s:]+(?:::[^\s]+)?)(?:\s+-.*)?\s*$"
)

#: pytest's closing banner: ``==== 42 failed, 1835 passed in 61.2s ====``.
_PYTEST_BANNER = re.compile(r"^=+\s*(?P<body>[^=].*?)\s*=+\s*$")

#: The counts inside that banner that mean "a test did not pass".
_PYTEST_COUNT = re.compile(r"(?P<count>\d+)\s+(?P<kind>failed|error|errors)\b")


def extract_pytest_failures(output: str) -> frozenset[str] | None:
    """pytest node ids, or ``None`` if the output cannot be vouched for.

    The banner is what makes this safe. pytest prints its totals last, so a
    tail that contains the banner contains the end of the run, and the number
    in it is the runner's own count of what failed. If the ``FAILED``/``ERROR``
    lines parsed out of the same text do not add up to that number -- because
    the short summary was clipped off the front, because a plugin reformatted
    it, because the run died mid-way -- then the set is incomplete and the only
    correct answer is that the failures could not be identified.
    """
    if not output:
        return None
    lines = output.splitlines()

    expected: int | None = None
    for line in reversed(lines):
        banner = _PYTEST_BANNER.match(line.strip())
        if banner is None:
            continue
        counts = _PYTEST_COUNT.findall(banner.group("body"))
        if counts or "passed" in banner.group("body"):
            expected = sum(int(count) for count, _ in counts)
            break
    if expected is None or expected == 0:
        # No banner at all: not pytest output, or not the end of a pytest run.
        # ``0`` is equally unusable here -- a command that exited non-zero while
        # pytest reported nothing failing failed for some other reason (a
        # collection abort, a coverage threshold, an internal error), and that
        # reason has no test identity to compare.
        return None

    identities = {
        match.group("identity")
        for line in lines
        if (match := _PYTEST_FAILURE_LINE.match(line.rstrip()))
    }
    if len(identities) != expected:
        return None
    return frozenset(identities)


#: The adapters, tried in order; the first that recognises the output wins.
FAILURE_EXTRACTORS: tuple[FailureExtractor, ...] = (
    FailureExtractor(name="pytest", extract=extract_pytest_failures),
)


def extract_failure_identities(output: str) -> tuple[str, frozenset[str]] | None:
    """The first extractor that can read ``output``, and what it read.

    Returns ``(extractor_name, identities)``, or ``None`` when no adapter can
    produce a set it is willing to call complete.
    """
    for extractor in FAILURE_EXTRACTORS:
        identities = extractor.extract(output)
        if identities is not None:
            return extractor.name, identities
    return None


# ------------------------------------------------------------- comparison


@dataclass(frozen=True, slots=True)
class FailureComparison:
    """Candidate failures set against baseline failures.

    ``available`` is the field every caller must read first. When it is
    ``False`` the three sets are empty and mean nothing: the comparison did not
    happen, and the candidate is unclassified rather than clean.
    """

    #: Whether a comparison was actually performed over complete evidence.
    available: bool = False
    #: Failures present in both: already-known, not the candidate's doing.
    known: frozenset[str] = frozenset()
    #: Failures in the candidate and not in the baseline: the regressions.
    new: frozenset[str] = frozenset()
    #: Failures in the baseline and not in the candidate. Recorded, never
    #: required: a candidate that happens to fix a pre-existing failure is not
    #: asked to put it back.
    resolved: frozenset[str] = frozenset()
    #: Why the comparison was unavailable, or how it was made. For a human.
    detail: str = ""
    #: The repository state the baseline evidence describes.
    baseline_sha: str | None = None
    #: ``(verification_type, command)`` pairs that were compared.
    commands_compared: tuple[str, ...] = ()
    #: Which adapters read the candidate output.
    extractors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_new_failures(self) -> bool:
        return self.available and bool(self.new)

    @property
    def clean_against_baseline(self) -> bool:
        """Evidence says every observed failure was already known."""
        return self.available and not self.new

    def describe(self) -> dict[str, object]:
        return {
            "available": self.available,
            "detail": self.detail,
            "baseline_sha": self.baseline_sha,
            "commands_compared": list(self.commands_compared),
            "extractors": list(self.extractors),
            "counts": {
                "known": len(self.known),
                "new": len(self.new),
                "resolved": len(self.resolved),
            },
            "new": _rendered(self.new),
            "known": _rendered(self.known),
            "resolved": _rendered(self.resolved),
        }


def unavailable(detail: str, *, baseline_sha: str | None = None) -> FailureComparison:
    """A comparison that did not happen, and says why."""
    return FailureComparison(available=False, detail=detail, baseline_sha=baseline_sha)


def compare(
    *,
    baseline: Iterable[str],
    candidate: Iterable[str],
    baseline_sha: str | None = None,
    commands_compared: Iterable[str] = (),
    extractors: Iterable[str] = (),
    detail: str = "",
) -> FailureComparison:
    """The three sets, from two sets of identities.

    Set difference, nothing else. Both arguments must be *complete* for their
    command -- a caller holding a partial set has an unavailable comparison,
    not a comparison with a smaller baseline.
    """
    baseline_set = frozenset(baseline)
    candidate_set = frozenset(candidate)
    return FailureComparison(
        available=True,
        known=candidate_set & baseline_set,
        new=candidate_set - baseline_set,
        resolved=baseline_set - candidate_set,
        detail=detail,
        baseline_sha=baseline_sha,
        commands_compared=tuple(commands_compared),
        extractors=tuple(dict.fromkeys(extractors)),
    )


def _rendered(identities: frozenset[str]) -> list[str]:
    """A bounded, stably ordered sample of a set, for a row or a prompt."""
    ordered = sorted(identities)
    if len(ordered) <= MAX_RENDERED_IDENTITIES:
        return ordered
    return [
        *ordered[:MAX_RENDERED_IDENTITIES],
        f"[... {len(ordered) - MAX_RENDERED_IDENTITIES} more ...]",
    ]


__all__ = [
    "FAILURE_EXTRACTORS",
    "MAX_RENDERED_IDENTITIES",
    "FailureComparison",
    "FailureExtractor",
    "compare",
    "extract_failure_identities",
    "extract_pytest_failures",
    "unavailable",
]
