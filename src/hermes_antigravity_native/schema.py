"""Tool-schema sanitising (port of the dereference / strip / normalize helpers in stream.ts)."""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 64
MAX_NODES = 10_000
MAP_KEYWORDS = {"properties", "patternProperties", "dependentSchemas", "dependencies"}
VALUE_KEYWORDS = {
    "additionalItems", "additionalProperties", "contains", "contentSchema", "else", "if", "items",
    "not", "propertyNames", "then", "unevaluatedItems", "unevaluatedProperties",
}
ARRAY_KEYWORDS = {"allOf", "anyOf", "oneOf", "prefixItems"}
META_KEYWORDS = {"$schema", "$id", "$anchor", "$dynamicAnchor", "$vocabulary", "$comment", "$defs", "definitions"}
CUSTOM_TOOL_ALLOW = {"type", "description", "properties", "required", "items", "enum"}


class SchemaRefError(Exception):
    pass


def resolve_pointer(ref: str, root: Any) -> Any:
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        raise SchemaRefError(f"{ref}: only local refs supported")
    cur = root
    for tok in ref[2:].split("/"):
        key = tok.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not key.isdigit() or int(key) >= len(cur):
                raise SchemaRefError(f"{ref}: bad index")
            cur = cur[int(key)]
        elif isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            raise SchemaRefError(f"{ref}: target is not present in the root schema")
    return cur


def dereference(schema: Any, root: Any = None, ref_stack: frozenset = frozenset(), obj_stack: frozenset = frozenset(), state: dict | None = None, depth: int = 0) -> Any:
    """Inline local ``$ref``s; raises SchemaRefError on cycles / unresolved / oversized schemas."""
    root = schema if root is None else root
    state = state if state is not None else {"nodes": 0}
    if depth > MAX_DEPTH:
        raise SchemaRefError("schema expansion exceeded depth limit")
    state["nodes"] += 1
    if state["nodes"] > MAX_NODES:
        raise SchemaRefError("schema expansion exceeded node limit")
    if not isinstance(schema, (dict, list)):
        return schema
    if isinstance(schema, list):
        return [dereference(i, root, ref_stack, obj_stack, state, depth + 1) for i in schema]
    if id(schema) in obj_stack:
        raise SchemaRefError("circular schema object")
    obj_stack = obj_stack | {id(schema)}
    if isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref in ref_stack:
            raise SchemaRefError(f"{ref}: circular local reference")
        resolved = dereference(resolve_pointer(ref, root), root, ref_stack | {ref}, obj_stack, state, depth + 1)
        siblings = dereference({k: v for k, v in schema.items() if k != "$ref"}, root, ref_stack, obj_stack, state, depth + 1)
        if isinstance(resolved, dict) and isinstance(siblings, dict):
            return {**resolved, **siblings}
        return resolved
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("$defs", "definitions"):
            continue
        if key in MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {k: dereference(v, root, ref_stack, obj_stack, state, depth + 1) for k, v in value.items()}
        elif key in VALUE_KEYWORDS or key in ARRAY_KEYWORDS or key in MAP_KEYWORDS:
            out[key] = dereference(value, root, ref_stack, obj_stack, state, depth + 1)
        else:
            out[key] = value
    return out


def ensure_root_object(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    if not schema.get("type"):
        return {**schema, "type": "object", "properties": schema.get("properties") or {}}
    return schema


def strip_meta(schema: Any) -> Any:
    if isinstance(schema, list):
        return [strip_meta(i) for i in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in META_KEYWORDS:
            continue
        if key in MAP_KEYWORDS and isinstance(value, dict):
            out[key] = {k: strip_meta(v) for k, v in value.items()}
        elif key in VALUE_KEYWORDS or key in ARRAY_KEYWORDS or key in MAP_KEYWORDS:
            out[key] = strip_meta(value)
        else:
            out[key] = value
    return out


def normalize_custom_tool_schema(schema: Any) -> Any:
    """Allowlist the protobuf Schema subset accepted by the Claude/GPT-OSS tool bridge."""
    if isinstance(schema, list):
        return [normalize_custom_tool_schema(i) for i in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in CUSTOM_TOOL_ALLOW:
            continue
        if key == "type":
            if isinstance(value, str):
                out["type"] = value
            elif isinstance(value, list):
                scalar = next((v for v in value if isinstance(v, str) and v != "null"), None)
                if scalar:
                    out["type"] = scalar
            continue
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {k: normalize_custom_tool_schema(v) for k, v in value.items()}
            continue
        if key == "enum" and isinstance(value, list) and not all(isinstance(v, str) for v in value):
            continue
        out[key] = normalize_custom_tool_schema(value)
    return out
