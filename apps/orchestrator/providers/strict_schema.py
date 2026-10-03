"""OpenAI Responses API strict-schema transport (concern 77).

OpenAI's ``json_schema`` structured output in strict mode is narrower than
JSON Schema: every object must list *every* one of its properties in
``required``, and must set ``additionalProperties: false``. A schema with a
genuinely optional property is a 400 before a single token is generated --
which is what the real smoke test hit on ``code_edits``::

    In context=('properties', 'edits', 'items'), 'required' is required to be
    supplied and to be an array including every key in properties.
    Missing 'oldText'...

The canonical ``domain.edits.EDIT_SCHEMA`` is not wrong; it is the contract
local endpoints decode against, and concern 70 depends on ``oldText``/
``newText`` being absent on every operation except ``replace``. Making them
globally required to please one vendor would make the canonical protocol a
function of that vendor's validator.

So the adaptation lives here, at the transport boundary, and has two halves
that must stay inverses of each other:

*   ``to_strict_schema`` derives a *transport* schema -- every property
    required, optional ones widened to accept ``null`` -- without mutating
    the canonical one.
*   ``normalise_strict_payload`` deletes the ``null`` placeholders the model
    sends back for those optional properties, so what reaches
    ``CodeChangeSet.from_payload`` is indistinguishable from what a local
    provider returns. A ``replace``'s real ``oldText`` is a string, not
    ``null``, and survives untouched; a ``create``'s ``oldText: null``
    disappears rather than arriving as a key that concern 70 would (rightly)
    refuse.

Canonical validation stays authoritative either way: nothing here decides an
edit is acceptable, only that a vendor-shaped absence is spelled the way the
domain spells absence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: Types that must not be widened with ``"null"``: a schema that is already
#: nullable, or whose type is absent, has nothing to add.
_NULL = "null"


def to_strict_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """A deep copy of ``schema`` that satisfies OpenAI strict structured output.

    Applied recursively to every nested object and array item schema:

    *   every key of ``properties`` is listed in ``required``;
    *   a property that the canonical schema did *not* require accepts
        ``null`` in addition to its own type;
    *   ``additionalProperties: false`` is set on any object that declares
        ``properties`` (and an explicit ``false`` already present is kept).

    An object schema with no ``properties`` is structural rather than a
    record -- there is nothing to require and nothing to close -- and is
    copied through unchanged apart from its nested subschemas.

    The input is never mutated.
    """
    return _transform(schema, nullable=False)


def _transform(node: object, *, nullable: bool) -> object:
    if isinstance(node, Mapping):
        return _transform_mapping(node, nullable=nullable)
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return [_transform(item, nullable=False) for item in node]
    return node


def _transform_mapping(node: Mapping[str, object], *, nullable: bool) -> dict[str, object]:
    result: dict[str, object] = {}
    raw_properties = node.get("properties")
    properties: Mapping[str, object] | None = (
        raw_properties if isinstance(raw_properties, Mapping) else None
    )
    canonical_required = _required_names(node.get("required"))

    for key, value in node.items():
        if properties is not None and key == "properties":
            result[key] = {
                name: _transform(subschema, nullable=name not in canonical_required)
                for name, subschema in properties.items()
            }
        elif properties is not None and key == "required":
            # Replaced below from the property order, so that the strict
            # requirement is derived rather than hand-maintained.
            continue
        else:
            result[key] = _transform(value, nullable=False)

    if properties is not None:
        result["required"] = list(properties.keys())
        result.setdefault("additionalProperties", False)

    if nullable:
        result["type"] = _nullable_type(result.get("type"))
    return result


def _nullable_type(declared: object) -> object:
    if isinstance(declared, str):
        return [declared, _NULL] if declared != _NULL else declared
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        names = [str(name) for name in declared]
        return names if _NULL in names else [*names, _NULL]
    # No declared type: the property already accepts anything, including null.
    return declared


def normalise_strict_payload(payload: object, schema: Mapping[str, object]) -> object:
    """Undo ``to_strict_schema``'s null placeholders against ``schema``.

    Walks the *canonical* schema alongside the parsed response and drops any
    property whose value is ``null`` and which the canonical schema did not
    require. Everything else -- including a ``null`` under a canonically
    required key, which is a model error the domain must see rather than a
    placeholder -- is left exactly as it arrived.

    The input is never mutated.
    """
    return _normalise(payload, schema)


def _normalise(payload: object, schema: object) -> object:
    if not isinstance(schema, Mapping):
        return payload

    if isinstance(payload, Mapping):
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return dict(payload)
        required = _required_names(schema.get("required"))
        result: dict[str, object] = {}
        for key, value in payload.items():
            if value is None and key in properties and key not in required:
                continue
            result[key] = _normalise(value, properties.get(key))
        return result

    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        items = schema.get("items")
        return [_normalise(entry, items) for entry in payload]

    return payload


def _required_names(required: object) -> frozenset[str]:
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        return frozenset(str(name) for name in required)
    return frozenset()


__all__ = ["normalise_strict_payload", "to_strict_schema"]
