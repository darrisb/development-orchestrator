"""Service-layer errors.

Distinct from ``domain.errors``: these describe application-level outcomes
(missing or conflicting records) that the API maps to HTTP status codes, and
they carry no workflow policy of their own.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer failures."""


class EntityNotFound(ServiceError):
    def __init__(self, entity: str, identifier: object) -> None:
        super().__init__(f"{entity} {identifier} not found")
        self.entity = entity
        self.identifier = identifier


class EntityConflict(ServiceError):
    """The request conflicts with an existing record or its current state."""


class NotInCapturableState(ServiceError):
    """The entity exists but is not in a state this operation accepts.

    Distinct from :class:`EntityConflict` because the conflict is with the
    entity's *state* rather than with another record, and distinct from a bare
    ``ValueError`` because a caller -- an HTTP handler in particular -- has to be
    able to recognise it and answer 409 instead of letting it surface as a 500.
    """


class LockWaitTimeout(ServiceError):
    """A statement gave up waiting for a database lock (concern 57).

    Infrastructure contention rather than anything about the request: the rows
    are held by another transaction that has not ended. Distinct from
    :class:`EntityConflict`, which is a conflict with a record's content or
    state and will still be there on a retry -- this one usually will not,
    which is why it classifies as ``RESOURCE_UNAVAILABLE`` and why a run that
    meets it can be resumed rather than restarted.
    """
