"""A project's declared model preferences (build.md section 31).

Section 31 asks for routing: a cheap local coder for ordinary work and a
stronger one for the hard tasks, with the choice declared by the project
rather than discovered at runtime. This module is that declaration, and
nothing more -- it names models, it does not resolve them. Resolution is
``providers.registry.select_for_role``, which never falls back (principle
10), so a policy naming a model nobody registered fails the run instead of
quietly routing it somewhere else.

The policy lives here rather than in ``domain.manifest`` because it is
project-owned configuration that outlives an import: it is persisted on the
project, read back by the repository and used by the workflow, none of which
may depend on manifest parsing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .enums import Complexity

#: The policy's fields, in the order a stored mapping lists them.
_FIELDS = ("default_coder", "high_complexity_coder", "reviewer")


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    """Which registered model each role should use.

    Every field is a model name (or provider id) as ``select_for_role``
    matches it, and ``None`` everywhere is the default: the project expressed
    no preference and the first provider enabled for the role is used, which
    is what every project did before this existed.
    """

    default_coder: str | None = None
    high_complexity_coder: str | None = None
    reviewer: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any((self.default_coder, self.high_complexity_coder, self.reviewer))

    def coder_for(self, complexity: Complexity) -> str | None:
        """The coder this task should run on, or ``None`` for no preference.

        A HIGH-complexity task gets ``high_complexity_coder`` when the project
        declared one and ``default_coder`` otherwise: the stronger model is an
        escalation on top of the default, so a project that names only a
        default still routes every task to it rather than to whichever coder
        happens to be registered first.
        """
        if complexity is Complexity.HIGH and self.high_complexity_coder:
            return self.high_complexity_coder
        return self.default_coder

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> ModelPolicy:
        """Read a stored policy back. The lenient direction, like a row read."""
        if not payload:
            return cls()
        return cls(**{field: _optional_name(payload.get(field)) for field in _FIELDS})

    def describe(self) -> dict[str, str]:
        """The stored form: only the roles the project actually named.

        Unset roles are omitted rather than written as ``null`` so an empty
        policy stores ``{}`` -- the same value the migration gives an existing
        row, and the same value a manifest with no ``model_policy`` block
        produces. A re-import can then tell an unchanged policy from a changed
        one without every project reporting a spurious update.
        """
        return {
            field: value
            for field in _FIELDS
            if isinstance(value := getattr(self, field), str)
        }


def _optional_name(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
