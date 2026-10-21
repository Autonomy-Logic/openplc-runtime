# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Checks bodies against ``mqtt-api.v1.schema.json`` for the keywords that file uses.

The runtime has no jsonschema dependency; the full validator runs in the C0
spike. This covers type, const, enum, pattern, ranges, lengths, required,
additionalProperties, items, oneOf and if/then/else, which is every keyword the
API schema uses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "core/src/drivers/plugins/python/mqtt/contract/fixtures/schema/mqtt-api.v1.schema.json"
)
_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _resolve(ref: str) -> dict[str, object]:
    node: object = _SCHEMA
    for part in ref.removeprefix("#/").split("/"):
        assert isinstance(node, dict)
        node = node[part]
    assert isinstance(node, dict)
    return node


def _type_ok(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def errors(value: object, schema: dict[str, object], path: str = "") -> list[str]:
    if "$ref" in schema:
        return errors(value, _resolve(str(schema["$ref"])), path)
    found: list[str] = []
    if "type" in schema and not _type_ok(value, str(schema["type"])):
        return [f"{path}: type"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{path}: const")
    if "enum" in schema and value not in schema["enum"]:  # type: ignore[operator]
        found.append(f"{path}: enum")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(str(schema["pattern"]), value):
            found.append(f"{path}: pattern")
        if len(value) > int(schema.get("maxLength", len(value))):  # type: ignore[arg-type]
            found.append(f"{path}: maxLength")
        if len(value) < int(schema.get("minLength", 0)):  # type: ignore[arg-type]
            found.append(f"{path}: minLength")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < int(schema["minimum"]):  # type: ignore[arg-type]
            found.append(f"{path}: minimum")
        if "maximum" in schema and value > int(schema["maximum"]):  # type: ignore[arg-type]
            found.append(f"{path}: maximum")
    if isinstance(value, list):
        if len(value) > int(schema.get("maxItems", len(value))):  # type: ignore[arg-type]
            found.append(f"{path}: maxItems")
        if len(value) < int(schema.get("minItems", 0)):  # type: ignore[arg-type]
            found.append(f"{path}: minItems")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                found += errors(item, items, f"{path}/{i}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        for key in schema.get("required", []):  # type: ignore[union-attr]
            if key not in value:
                found.append(f"{path}: missing {key}")
        for key, item in value.items():
            if key in properties:
                found += errors(item, properties[key], f"{path}/{key}")
            elif schema.get("additionalProperties") is False:
                found.append(f"{path}: unexpected {key}")
    if "oneOf" in schema:
        matches = [s for s in schema["oneOf"] if not errors(value, s, path)]  # type: ignore[union-attr]
        if len(matches) != 1:
            found.append(f"{path}: oneOf")
    for clause in schema.get("allOf", []):  # type: ignore[union-attr]
        found += errors(value, clause, path)
    if "if" in schema:
        branch = "then" if not errors(value, schema["if"], path) else "else"  # type: ignore[arg-type]
        if branch in schema:
            found += errors(value, schema[branch], path)  # type: ignore[arg-type]
    if "not" in schema and not errors(value, schema["not"], path):  # type: ignore[arg-type]
        found.append(f"{path}: not")
    return found


def check(value: object, definition: str) -> list[str]:
    return errors(value, _resolve(f"#/$defs/{definition}"))
