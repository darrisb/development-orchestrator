#!/usr/bin/env python3
"""Fail loudly when the running deployment is not the source that was intended.

Concern 63 exists because this comparison was never made. `RUN-20260927-000020`
produced a full set of evidence against an image built roughly 49 minutes before
the commit it was supposed to be testing, and every host test was green, because
the host tests were not what the experiment ran.

The check is three steps and no cleverness: ask the running deployment what it
is, compare that to the commit you meant to run, and exit non-zero if they
differ. Anything more elaborate would be a thing to debug at the moment it
matters, which is the worst possible time.

    scripts/check_deployment_freshness.py --expected HEAD
    scripts/check_deployment_freshness.py --expected d50752d --url http://localhost:8000

Exit codes: 0 the deployment is the intended source, 1 it is not, 2 the check
could not be performed (unreachable, or not JSON). The distinction is between
"the image is wrong" and "I could not find out", because only one of those is a
stale-deployment incident and conflating them teaches people to ignore it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin

if __package__ in (None, ""):  # run as a script, without installing first
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from apps.orchestrator.services.deployment import (  # noqa: E402
    UNKNOWN_BUILD_TIME,
    UNKNOWN_REVISION,
    SourceIdentity,
    StaleDeploymentError,
    assert_deployment_fresh,
)

EXIT_STALE = 1
EXIT_UNCHECKABLE = 2


def resolve_expected(expected: str) -> str:
    """Turn ``HEAD`` or a short SHA into the full commit it names.

    Git is used here, on the host, and only here. The image must not need a
    ``.git`` directory to know what it is; the machine that decides what *should*
    be running is a different machine, and it is allowed to have a checkout.
    """
    if expected and all(c in "0123456789abcdefABCDEF" for c in expected):
        return expected.lower()
    try:
        out = subprocess.run(
            ["git", "rev-parse", expected],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise StaleDeploymentError(
            f"could not resolve {expected!r} to a commit with git: {exc}"
        ) from exc
    return out.stdout.strip()


def fetch_health(base_url: str, timeout: float) -> dict:
    url = urljoin(base_url.rstrip("/") + "/", "health")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise LookupError(f"could not read {url}: {exc}") from exc


def identity_from_health(payload: dict) -> SourceIdentity:
    """Build the identity out of a /health response.

    Missing fields are read as unknown rather than defaulted to a value that
    would look like success: a response from something that is not this
    orchestrator must not be able to pass by omission.
    """
    return SourceIdentity(
        revision=payload.get("source_revision") or UNKNOWN_REVISION,
        dirty=payload.get("source_dirty"),
        built_at=payload.get("build_time") or UNKNOWN_BUILD_TIME,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the running deployment against the intended source.",
    )
    parser.add_argument(
        "--expected",
        required=True,
        help="the commit that was meant to be running (a SHA, or HEAD)",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="base URL of the running deployment (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="accept an image built from a commit with uncommitted changes",
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="seconds to wait for /health"
    )
    args = parser.parse_args(argv)

    try:
        expected = resolve_expected(args.expected)
        payload = fetch_health(args.url, args.timeout)
    except (StaleDeploymentError, LookupError) as exc:
        print(f"freshness check could not be performed: {exc}", file=sys.stderr)
        return EXIT_UNCHECKABLE

    actual = identity_from_health(payload)
    try:
        assert_deployment_fresh(expected, actual, allow_dirty=args.allow_dirty)
    except StaleDeploymentError as exc:
        print(f"STALE DEPLOYMENT: {exc}", file=sys.stderr)
        return EXIT_STALE

    print(f"fresh: deployment is {actual.state}, built {actual.built_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
