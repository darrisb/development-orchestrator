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
