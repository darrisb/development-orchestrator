"""Turning model text into a validated object (phase E item 5).

A local model's answer is text, even when a JSON schema was requested. Three
things routinely sit between that text and a usable object:

1.  a reasoning block (``<think>...</think>``) that Qwen3-class models emit
    before the answer,
2.  a Markdown fence around the JSON,
3.  a sentence of commentary either side of it.

Recovering from those is deterministic parsing, not leniency about
correctness: anything that is still not valid JSON of the required shape is
an ``InvalidModelResponse``, which the failure policy sends back to the coder
as evidence rather than retrying blindly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .errors import InvalidModelResponse

#: Reasoning wrappers seen from local coder models. Content inside is never
#: part of the answer and is dropped before parsing or diffing.
_REASONING_BLOCK = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE
)
#: An unclosed opener means the answer was cut off mid-reasoning; everything
#: from it onwards is reasoning, so there is no answer to keep.
_UNCLOSED_REASONING = re.compile(r"<(think|thinking|reasoning)>.*\Z", re.DOTALL | re.IGNORECASE)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)

_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def strip_reasoning(text: str) -> str:
    """Remove reasoning blocks from an answer."""
    without_blocks = _REASONING_BLOCK.sub("", text)
    return _UNCLOSED_REASONING.sub("", without_blocks).strip()


def extract_json_object(text: str) -> str:
    """Return the JSON document embedded in ``text``.

    Prefers a fenced block, then falls back to the first balanced ``{...}`` or
    ``[...]`` span, so commentary around the JSON does not lose the answer.
    Whichever delimiter appears first wins: an object plucked out of the
    middle of an array would silently discard the rest of the array.

    Raises:
        InvalidModelResponse: no JSON document could be located.
    """
    candidate = text.strip()
    if not candidate:
        raise InvalidModelResponse("Model returned an empty response")

    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        body = fenced.group("body").strip()
        if body:
            return body

    spans = [
        (candidate.find(opening), _balanced_span(candidate, opening, closing))
        for opening, closing in (("{", "}"), ("[", "]"))
    ]
    located = sorted((start, span) for start, span in spans if start != -1 and span)
    if located:
        return located[0][1]

    raise InvalidModelResponse("Model response contains no JSON document")


def _balanced_span(text: str, opening: str, closing: str) -> str | None:
    """First balanced ``opening``/``closing`` span, ignoring braces in strings."""
    start = text.find(opening)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_structured(text: str, schema: Mapping[str, object], *, schema_name: str) -> dict:
    """Parse ``text`` into an object satisfying ``schema``.

    Only the top-level ``type`` and ``required`` are checked. Deeper
    validation belongs to the caller's own model, which knows what the fields
    mean; duplicating it here would mean two places to keep in step.

    Raises:
        InvalidModelResponse: the text is not JSON, or is the wrong shape.
    """
    document = extract_json_object(text)
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError as exc:
        raise InvalidModelResponse(
            f"Model response for schema '{schema_name}' is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc

    expected_type = schema.get("type", "object")
    if isinstance(expected_type, str):
        python_type = _TYPE_CHECKS.get(expected_type)
        # bool is a subclass of int; an integer field must not accept `true`.
        wrong_type = python_type is not None and (
            not isinstance(parsed, python_type)
            or (expected_type in {"integer", "number"} and isinstance(parsed, bool))
        )
        if wrong_type:
            raise InvalidModelResponse(
                f"Model response for schema '{schema_name}' should be a JSON "
                f"{expected_type}, got {type(parsed).__name__}"
            )

    if not isinstance(parsed, dict):
        raise InvalidModelResponse(
            f"Model response for schema '{schema_name}' must be a JSON object "
            f"to be used as structured output, got {type(parsed).__name__}"
        )

    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [key for key in required if key not in parsed]
        if missing:
            raise InvalidModelResponse(
                f"Model response for schema '{schema_name}' is missing required "
                f"field(s): {', '.join(str(key) for key in missing)}"
            )
    return parsed
