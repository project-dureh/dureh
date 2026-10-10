"""A small JSON Schema subset validator (type, const, enum, required, properties,
additionalProperties (false or a schema), items, minItems, minLength, pattern). Unknown keywords
other than annotations fail so a schema cannot silently weaken."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .org import HARNESS_ROOT
from .util import fail

_ANNOTATIONS = {"$schema", "$id", "title", "description"}
_KNOWN = {"type", "const", "enum", "required", "properties", "additionalProperties", "items",
          "minItems", "minLength", "pattern"} | _ANNOTATIONS
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def errors(value, schema: dict, where: str = "$") -> list[str]:
    out: list[str] = []
    unknown = set(schema) - _KNOWN
    if unknown:
        return [f"{where}: schema uses unsupported keywords {sorted(unknown)}"]
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](value) for t in types):
            return [f"{where}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{where}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            out.append(f"{where}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{where}: does not match {schema['pattern']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            out.append(f"{where}: needs at least {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                out.extend(errors(item, schema["items"], f"{where}[{i}]"))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}: missing required field {key!r}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                out.extend(errors(item, props[key], f"{where}.{key}"))
            elif schema.get("additionalProperties") is False:
                out.append(f"{where}: unexpected field {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                out.extend(errors(item, schema["additionalProperties"], f"{where}.{key}"))
    return out


@lru_cache(maxsize=None)
def schema_for(kind: str) -> dict:
    path = HARNESS_ROOT / "schemas" / f"{kind}.schema.json"
    if not path.is_file():
        fail(f"unknown record kind {kind!r}: no schema at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def check_record(record: dict) -> None:
    kind = record.get("kind")
    if not isinstance(kind, str):
        fail("record has no kind")
    problems = errors(record, schema_for(kind))
    if problems:
        fail(f"{kind} record fails schema: " + "; ".join(problems))
