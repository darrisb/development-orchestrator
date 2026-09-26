"""Domain-level errors.

Deliberately granular: build.md section 49 forbids a single generic
exception path for every failure.
"""

from __future__ import annotations

from .enums import FailureReason, TaskStatus


class DomainError(Exception):
    """Base class for all domain errors."""


class InvalidStateTransition(DomainError):
    def __init__(self, current: TaskStatus, requested: TaskStatus) -> None:
        super().__init__(f"Cannot transition task from {current} to {requested}")
        self.current = current
        self.requested = requested


class LimitExceeded(DomainError):
    """A configured limit (attempts, review cycles, diff size) was exceeded."""

    def __init__(self, message: str, reason: FailureReason) -> None:
        super().__init__(message)
        self.reason = reason


class ManifestError(DomainError):
    """The project manifest is structurally invalid."""


class DependencyCycleError(ManifestError):
    def __init__(self, cycle: list[str]) -> None:
        super().__init__(f"Dependency cycle detected: {' -> '.join(cycle)}")
        self.cycle = cycle


class UnknownDependencyError(ManifestError):
    def __init__(self, task_id: str, missing: str) -> None:
        super().__init__(f"Task {task_id} depends on unknown task {missing}")
        self.task_id = task_id
        self.missing = missing
