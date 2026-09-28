"""What is running, and whether it is the source that was meant to be running.

Concern 63's finding was not that the orchestrator had a bug. It was that the
code under test and the code running in the container were different code and
nothing said so: the host suite was green, `RUN-20260927-000020` still ran
Concern 61-era code, and the run's evidence was worthless. Detecting that by
hand meant comparing image digests, which nobody does twice.

So the deployment carries its own identity, and this module is the one place that
decides what that identity means and whether it is the identity that was
intended. The comparison lives here rather than in a shell script on purpose: a
check that only exists in a script is a check that can be quietly skipped, and
this one is supposed to be the thing that makes a stale image impossible to
believe.

Three states, kept distinct on purpose:

* ``clean@<sha>`` -- built from a known commit, with no uncommitted changes.
* ``dirty@<sha>`` -- built from a known commit that had local modifications, so
  the SHA names something other than what is in the image.
* ``unknown/dev`` -- the build could not determine its source. Not an error
  condition in itself (a developer build is a legitimate thing to run), but
  never something that can be *verified*, and so never something to experiment
  against.
"""

from __future__ import annotations

from dataclasses import dataclass

UNKNOWN_REVISION = "unknown/dev"
UNKNOWN_BUILD_TIME = "unknown"


@dataclass(frozen=True)
class SourceIdentity:
    """Where the running code came from, as recorded at build time."""

    revision: str = UNKNOWN_REVISION
    dirty: bool | None = None
    built_at: str = UNKNOWN_BUILD_TIME

    @property
    def is_known(self) -> bool:
        return bool(self.revision) and self.revision != UNKNOWN_REVISION

    @property
    def state(self) -> str:
        """The one-line identity a human or a log can be checked against.

        ``unknown/dev`` rather than ``clean@unknown`` for the unbuildable case,
        because "clean" is a claim about a tree, and there is no tree to make a
        claim about.
        """
        if not self.is_known:
            return UNKNOWN_REVISION
        return f"{'dirty' if self.dirty else 'clean'}@{self.revision}"


def current_source() -> SourceIdentity:
    """The identity of the code in this process.

    ``_build_meta`` is imported inside the function rather than at module scope
    so that a test can replace the values on the module, and so that the
    generated file is not a hard import-order dependency of the whole service.
    """
    from .. import _build_meta

    return SourceIdentity(
        revision=_build_meta.SOURCE_REVISION or UNKNOWN_REVISION,
        dirty=_build_meta.SOURCE_DIRTY,
        built_at=_build_meta.BUILD_TIME or UNKNOWN_BUILD_TIME,
    )


class StaleDeploymentError(RuntimeError):
    """The running image is not the source that was meant to be running."""


def assert_deployment_fresh(
    expected: str,
    actual: SourceIdentity,
    *,
    allow_dirty: bool = False,
) -> SourceIdentity:
    """Return ``actual`` if it is ``expected``, otherwise explain why it is not.

    Raises rather than returning a bool, because the interesting case is the one
    a caller is tempted to ignore, and a bool is easy to log and move on from.
    Every refusal names the expected source, the actual source, and the
    difference between them, so the fix is obvious from the message alone.
    """
    if not actual.is_known:
        raise StaleDeploymentError(
            f"the running deployment reports its source as {actual.state}, so it "
            f"cannot be checked against {expected}: an image that does not know "
            f"what it was built from is the exact artifact this check exists to "
            f"refuse. Rebuild with SOURCE_REVISION set."
        )
    if not expected or expected == UNKNOWN_REVISION:
        raise StaleDeploymentError(
            f"the intended source is {expected!r}, which is not a commit: there "
            f"is nothing for the deployment to have been built from. Pass the "
            f"commit you meant to run."
        )
    if expected != actual.revision:
        raise StaleDeploymentError(
            f"the deployment is running {actual.state}, but {expected} is the "
            f"source that was meant to be running. The image predates the "
            f"intended commit, so results from it describe code that is not the "
            f"code under test."
        )
    if actual.dirty is not False and not allow_dirty:
        # Not a mismatch, so the SHA is right, and the image is still not the
        # commit: it is the commit plus whatever was uncommitted at build time.
        raise StaleDeploymentError(
            f"the deployment is running {actual.state}: it was built from "
            f"{actual.revision} with uncommitted changes, so the commit does not "
            f"name the code. Commit the changes and rebuild, or pass "
            f"allow_dirty if running a dirty tree is the intent."
        )
    return actual
