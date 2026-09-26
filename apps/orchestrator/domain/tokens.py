"""Token estimation (build.md sections 13 and 15).

One ratio, used by both the context budget and the provider's pre-flight
prompt guard. Two different estimates would let the context builder fill a
window the guard then rejects, and the resulting failure would look like a
model defect rather than the arithmetic disagreement it is.
"""

from __future__ import annotations

#: Characters per token. Deliberately pessimistic for source code (real
#: tokenizers average nearer 3.5 on prose and lower on dense code), so the
#: budget runs out before the endpoint truncates silently. It is an estimate
#: and never an accounting record: real usage comes back from the endpoint.
CHARS_PER_TOKEN = 3.5


def estimate_tokens_from_characters(characters: int) -> int:
    return int(characters / CHARS_PER_TOKEN) + 1


def estimate_tokens(text: str) -> int:
    return estimate_tokens_from_characters(len(text))


def characters_for_tokens(tokens: int) -> int:
    """The inverse: how many characters fit in ``tokens``."""
    return max(0, int(tokens * CHARS_PER_TOKEN))
