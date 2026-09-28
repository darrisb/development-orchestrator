"""Concern 66 cleanup behavior for invalidated SQLAlchemy sessions."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError

from apps.orchestrator.db import session as session_module
from apps.orchestrator.providers.errors import ModelTimeout


class _BrokenSession:
    def __init__(self, *, invalidated: bool) -> None:
        self.invalidated_error = invalidated
        self.invalidations = 0
        self.closes = 0

    def rollback(self) -> None:
        raise DBAPIError(
            "ROLLBACK",
            {},
            RuntimeError("connection was terminated"),
            connection_invalidated=self.invalidated_error,
        )

    def invalidate(self) -> None:
        self.invalidations += 1

    def close(self) -> None:
        self.closes += 1


def _factory_for(session: _BrokenSession):
    return lambda: session


def test_invalidated_rollback_does_not_mask_the_model_failure(monkeypatch):
    broken = _BrokenSession(invalidated=True)
    timeout = ModelTimeout("provider waited too long", timeout_seconds=600)
    monkeypatch.setattr(
        session_module, "get_session_factory", lambda: _factory_for(broken)
    )

    with pytest.raises(ModelTimeout) as caught, session_module.session_scope():
        raise timeout

    assert caught.value is timeout
    assert broken.invalidations == 1
    assert broken.closes == 1


def test_an_unrelated_rollback_error_is_not_suppressed(monkeypatch):
    broken = _BrokenSession(invalidated=False)
    timeout = ModelTimeout("provider waited too long", timeout_seconds=600)
    monkeypatch.setattr(
        session_module, "get_session_factory", lambda: _factory_for(broken)
    )

    with pytest.raises(DBAPIError) as caught, session_module.session_scope():
        raise timeout

    assert caught.value.connection_invalidated is False
    assert broken.invalidations == 0
    assert broken.closes == 1
