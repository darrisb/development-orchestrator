"""Measuring a model call (build.md section 35).

One definition of "how long did that take", shared by every agent that calls a
model, because the alternative is a ``duration_ms`` that means whatever the
nearest call site happened to pass -- and section 35's model comparison is
arithmetic over exactly that column.

``monotonic`` is the clock, deliberately: a wall clock can step backwards or
forwards under NTP and produce a negative duration, and an endpoint that
returns in 600 seconds and an endpoint that never returns are the two facts
this table exists to tell apart. The wall-clock *start* is derived from the
monotonic origin, so the row is still orderable and reportable by time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic, time

__all__ = ["elapsed_ms", "started_at"]


def elapsed_ms(started: float) -> int:
    """Milliseconds since a ``monotonic()`` origin. Never negative."""
    return max(0, int((monotonic() - started) * 1000))


def started_at(started: float) -> datetime:
    """The wall-clock instant a ``monotonic()`` origin refers to."""
    return datetime.fromtimestamp(time() - monotonic() + started, tz=UTC)
