"""Domain and service errors mapped to HTTP responses.

Registered once on the app so routers stay free of try/except noise and every
endpoint reports the same failure the same way (build.md section 49: no single
generic error path, but one consistent translation layer).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from ..domain.errors import (
    AbandonedRunError,
    InvalidStateTransition,
    LimitExceeded,
    ManifestError,
    RunOwnershipLostError,
)
from ..providers.errors import ModelProviderError, ProviderNotConfigured
from ..services.errors import (
    EntityConflict,
    EntityNotFound,
    LockWaitTimeout,
    NotInCapturableState,
)
from ..services.git_errors import (
    DirtyWorktree,
    GitCommandTimeout,
    ProtectedBranch,
    PushNotPermitted,
    WorktreeMissing,
)

#: Error type -> HTTP status. 422 for a bad manifest: the file is a request
#: payload the caller can fix, not a server fault. Provider failures are 5xx
#: because the caller did nothing wrong: the model endpoint is missing (503)
#: or answered badly (502). A lock wait that timed out is 503 as well: the
#: request was valid and the contention is usually transient, so a caller may
#: retry it -- which is also why it is not 409.
#:
#: The Git entries are the operational half of ``services.git_errors`` (concern
#: 59): a repository state or a policy that a person can do something about, and
#: which the taxonomy already calls out as such. A dirty managed repository, a
#: missing worktree and a refusal to write a protected branch are all 409 --
#: the request was well formed and the world is not in a state that allows it.
#:
#: Deliberately *not* listed: ``BranchAlreadyExists``, ``WorktreePathRejected``,
#: ``GitCommandFailed``, ``NotARepository``, ``NothingToCommit``. Each of those
#: now means an invariant is broken -- a run identity that collides, a path
#: outside the worktree root, a repository that is not one -- and a 500 with a
#: traceback is the honest answer to a bug. Mapping ``GitError`` wholesale would
#: have turned every one of them into a tidy 409 and hidden the next concern 59
#: instead of surfacing it.
#:
#: ``RunOwnershipLostError`` (concern 67) is 409 for the same reason: the
#: caller's request was well formed, and an operator recovery moved the run's
#: execution generation on before it could persist. It reaches this layer only
#: from a locked database predicate, so it is a report and not a guess.
#:
#: ``AbandonedRunError`` is here for the same reason ``EntityConflict`` is
#: (concern 64): a caller racing an operator's abandonment of the same run
#: asked for something well formed that the world no longer allows, which is
#: what 409 means. It reaches this layer only when the database refused a
#: terminal write, so a 409 is a truthful report rather than a policy choice.
_STATUS_BY_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (EntityNotFound, status.HTTP_404_NOT_FOUND),
    (EntityConflict, status.HTTP_409_CONFLICT),
    (AbandonedRunError, status.HTTP_409_CONFLICT),
    (RunOwnershipLostError, status.HTTP_409_CONFLICT),
    (InvalidStateTransition, status.HTTP_409_CONFLICT),
    (LimitExceeded, status.HTTP_409_CONFLICT),
    (NotInCapturableState, status.HTTP_409_CONFLICT),
    (DirtyWorktree, status.HTTP_409_CONFLICT),
    (WorktreeMissing, status.HTTP_409_CONFLICT),
    (ProtectedBranch, status.HTTP_409_CONFLICT),
    (PushNotPermitted, status.HTTP_409_CONFLICT),
    (GitCommandTimeout, status.HTTP_503_SERVICE_UNAVAILABLE),
    (ManifestError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (LockWaitTimeout, status.HTTP_503_SERVICE_UNAVAILABLE),
    (ProviderNotConfigured, status.HTTP_503_SERVICE_UNAVAILABLE),
    (ModelProviderError, status.HTTP_502_BAD_GATEWAY),
)


def _problem(exc: Exception, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


def register_exception_handlers(app: FastAPI) -> None:
    for error_type, http_status in _STATUS_BY_ERROR:

        def handler(
            _request: Request, exc: Exception, _status: int = http_status
        ) -> JSONResponse:
            return _problem(exc, _status)

        app.add_exception_handler(error_type, handler)
