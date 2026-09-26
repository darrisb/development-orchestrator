"""Secret redaction (build.md sections 12 and 36).

Command output is stored as a run artifact, sent to a reviewer and read by a
human. Any of those three is a way for a credential to leave the machine, so
output passes through here first.

Two mechanisms, deliberately:

* **Known values.** The secrets this installation actually holds -- a reviewer
  API key, whatever a worker was given -- are matched literally. This is the
  reliable half: it cannot miss, because it knows what it is looking for.
* **Shapes.** Patterns for things that look like credentials whoever owns them:
  an ``Authorization`` header, a PEM private key block, a ``TOKEN=...``
  assignment, a provider-prefixed key. This half exists because a build log can
  print a secret this process was never told about.

Neither is trusted to be complete, which is why ``.env`` files are a protected
path (``domain.scope``) and are never part of a context package: redaction is
the last line, not the first.

Pure functions over strings. No settings, no environment, no I/O -- the caller
supplies the values, so this module can never be the thing that reads a secret
out of the environment and forgets to mask it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

PLACEHOLDER = "[redacted]"

#: A known value shorter than this is not redacted. A two-character secret
#: would match constantly and turn a build log into noise, and a value that
#: short is not protecting anything.
MIN_REDACTABLE_LENGTH = 8

#: Variable-name fragments that mean a value is a credential. Used for the
#: ``NAME=value`` shape below and, by the worker service, to decide which of
#: its own environment it will not pass on.
SECRET_NAME_HINTS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "PRIVATE",
    "SIGNATURE",
    "SESSION_ID",
)

_NAME_HINT_GROUP = "|".join(SECRET_NAME_HINTS)


def _compile_shapes() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The credential shapes, with the group to mask named ``secret`` in each."""
    return (
        # -----BEGIN RSA PRIVATE KEY----- ... -----END ...-----
        (
            "private_key",
            re.compile(
                r"(?P<secret>-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
                r"-----END [A-Z ]*PRIVATE KEY-----)",
                re.DOTALL,
            ),
        ),
        # Authorization: Bearer xxx / Authorization: Basic xxx
        (
            "auth_header",
            re.compile(
                r"(?i)(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic|token)?\s*(?P<secret>[A-Za-z0-9._~+/=-]{8,})"
            ),
        ),
        # NAME=value / "NAME": "value" for a credential-shaped name
        (
            "named_value",
            re.compile(
                # The lookahead keeps an auth *scheme* and an already-masked
                # value from being masked a second time: "Authorization" is a
                # credential-shaped name, but "Bearer" is not the credential.
                # The optional quotes either side of the separator are what
                # makes this work on a JSON body as well as on NAME=value.
                rf"(?i)\b[A-Z0-9_]*(?:{_NAME_HINT_GROUP})[A-Z0-9_]*\b[\"']?\s*[:=]\s*"
                rf"[\"']?(?!bearer\b|basic\b|token\b|{re.escape(PLACEHOLDER)})"
                rf"(?P<secret>[^\s\"',;)]{{6,}})"
            ),
        ),
        # Provider-prefixed keys, which are recognisable on their own.
        (
            "prefixed_key",
            re.compile(
                r"(?P<secret>(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"
                r"|gh[pousr]_[A-Za-z0-9]{16,}"
                r"|xox[abposr]-[A-Za-z0-9-]{10,}"
                r"|AKIA[0-9A-Z]{16}"
                r"|AIza[0-9A-Za-z_-]{20,}"
                r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
            ),
        ),
    )


SHAPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = _compile_shapes()


@dataclass(frozen=True, slots=True)
class Redactor:
    """Masks known secret values and credential-shaped text.

    Attributes:
        values: literal secrets this installation holds. Never logged, never
            serialised -- ``repr`` is suppressed for exactly that reason.
        mask_shapes: whether to apply ``SHAPE_PATTERNS`` as well. Off makes a
            test deterministic; on is the default everywhere else.
    """

    values: frozenset[str] = field(default_factory=frozenset, repr=False)
    mask_shapes: bool = True

    @classmethod
    def for_values(cls, values: Iterable[str | None], *, mask_shapes: bool = True) -> Redactor:
        """Build a redactor, dropping blanks and values too short to mask."""
        return cls(
            values=frozenset(
                value.strip()
                for value in values
                if value and len(value.strip()) >= MIN_REDACTABLE_LENGTH
            ),
            mask_shapes=mask_shapes,
        )

    def redact(self, text: str) -> str:
        """Return ``text`` with every secret it can recognise masked."""
        if not text:
            return text
        result = text
        # Longest first: masking a prefix of a longer secret would leave its
        # tail in the output.
        for value in sorted(self.values, key=len, reverse=True):
            result = result.replace(value, PLACEHOLDER)
        if not self.mask_shapes:
            return result
        for _, pattern in SHAPE_PATTERNS:
            result = pattern.sub(_mask_group, result)
        return result

    def redact_mapping(self, values: dict[str, str]) -> dict[str, str]:
        """A mapping safe to log: credential-named keys are masked by name.

        Used for a worker's environment, where the *name* is the reliable
        signal and the value may be anything at all.
        """
        return {
            key: PLACEHOLDER if is_secret_name(key) else self.redact(value)
            for key, value in values.items()
        }


def _mask_group(match: re.Match[str]) -> str:
    """Replace only the ``secret`` group, keeping the text around it.

    The surrounding text is what makes a redacted log readable: an operator
    needs to see that an ``Authorization`` header was present, not just that
    something was removed.
    """
    whole = match.group(0)
    if not match.group("secret"):
        return whole
    offset = match.start()
    start, end = match.span("secret")
    return whole[: start - offset] + PLACEHOLDER + whole[end - offset :]


def is_secret_name(name: str) -> bool:
    """Whether a variable name says its value is a credential."""
    upper = name.upper()
    return any(hint in upper for hint in SECRET_NAME_HINTS)


__all__ = [
    "MIN_REDACTABLE_LENGTH",
    "PLACEHOLDER",
    "SECRET_NAME_HINTS",
    "SHAPE_PATTERNS",
    "Redactor",
    "is_secret_name",
]
