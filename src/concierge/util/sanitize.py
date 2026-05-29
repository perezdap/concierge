"""
Metadata sanitization helpers.

Upstream MCP servers are not trusted. Their tool descriptions and titles can be
attacker-controlled (prompt injection / tool poisoning). Before any upstream
metadata is cataloged or surfaced to the downstream model, run it through here.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

# Names allowed for canonical / display purposes after sanitization.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")
_NAME_REPLACE = re.compile(r"[^a-zA-Z0-9_\-]+")

# Strip control chars except \n \t. Leave printable text alone.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Hard caps. Any larger field is suspicious and is truncated.
MAX_DESC_LEN = 600
MAX_LABEL_LEN = 80
MAX_NAME_LEN = 128


def _normalize(s: str) -> str:
    """NFKC normalize so confusables collapse, strip control bytes."""
    s = unicodedata.normalize("NFKC", s)
    return _CTRL_RE.sub("", s)


def sanitize_text(value: Any, *, max_len: int, fallback: str = "") -> str:
    """Coerce arbitrary upstream input to a safe, capped, plain string."""
    if value is None:
        return fallback
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return fallback
    value = _normalize(value).strip()
    if not value:
        return fallback
    if len(value) > max_len:
        # Soft-trim at a word boundary if possible.
        cut = value[:max_len]
        sp = cut.rfind(" ")
        if sp > max_len * 0.6:
            cut = cut[:sp]
        value = cut + "…"
    return value


def sanitize_description(value: Any, fallback: str = "") -> str:
    return sanitize_text(value, max_len=MAX_DESC_LEN, fallback=fallback)


def sanitize_label(value: Any, fallback: str = "") -> str:
    return sanitize_text(value, max_len=MAX_LABEL_LEN, fallback=fallback)


def sanitize_primitive_name(value: str) -> str:
    """
    Convert an arbitrary upstream tool name into a safe identifier segment.
    Result still must be combined with a server_id to form the canonical name.
    """
    if not isinstance(value, str):
        raise ValueError("upstream primitive name must be a string")
    s = _normalize(value).strip()
    if not s:
        raise ValueError("empty upstream primitive name")
    s = _NAME_REPLACE.sub("_", s)
    s = s.strip("_")
    if not s:
        raise ValueError("upstream primitive name reduced to empty after sanitize")
    if len(s) > MAX_NAME_LEN:
        s = s[:MAX_NAME_LEN]
    if not _NAME_RE.match(s):
        raise ValueError(f"unsafe primitive name: {value!r}")
    return s


def sanitize_server_id(value: str) -> str:
    """server_id is operator-provided in config so we keep it strict."""
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ValueError(f"invalid server_id: {value!r}")
    return value


def make_canonical_name(server_id: str, upstream_name: str) -> str:
    return f"{sanitize_server_id(server_id)}.{sanitize_primitive_name(upstream_name)}"


# ---------------------------------------------------------------------------
# JSON Schema validation — accept only known-good structural shapes.
# ---------------------------------------------------------------------------

_ALLOWED_SCHEMA_TYPES = {"object", "string", "number", "integer", "boolean", "array", "null"}


def validate_input_schema(schema: Any) -> dict[str, Any]:
    """
    Structural validation of an upstream JSON Schema. We do not run a full
    JSON Schema validator; we just refuse anything that isn't a plain dict of
    known-safe shape. Anything weirder is dropped to {} (tool still callable,
    but the model will get a permissive shape).
    """
    if schema is None:
        return {}
    if not isinstance(schema, dict):
        return {}
    out: dict[str, Any] = {}
    t = schema.get("type")
    if isinstance(t, str) and t in _ALLOWED_SCHEMA_TYPES:
        out["type"] = t
    if "properties" in schema and isinstance(schema["properties"], dict):
        clean_props = {}
        for k, v in schema["properties"].items():
            if not isinstance(k, str) or not _NAME_RE.match(k):
                continue
            if isinstance(v, dict):
                clean_props[k] = validate_input_schema(v) or {"type": "string"}
        if clean_props:
            out["properties"] = clean_props
    if "required" in schema and isinstance(schema["required"], list):
        out["required"] = [r for r in schema["required"] if isinstance(r, str)]
    if "items" in schema and isinstance(schema["items"], dict):
        out["items"] = validate_input_schema(schema["items"])
    if "description" in schema:
        d = sanitize_description(schema.get("description"))
        if d:
            out["description"] = d
    if "enum" in schema and isinstance(schema["enum"], list):
        out["enum"] = [x for x in schema["enum"] if isinstance(x, (str, int, float, bool))]
    return out


def summarize_arguments(schema: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Compact arg list for the discovery primitive output."""
    if not schema:
        return []
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: list[dict[str, Any]] = []
    for name, sub in props.items():
        if not isinstance(sub, dict):
            continue
        out.append({
            "name": name,
            "type": sub.get("type", "string"),
            "required": name in required,
            "description": sanitize_description(sub.get("description"))[:160] or None,
        })
    return out


def schema_hash(schema: dict[str, Any] | None) -> str:
    return hashlib.sha256(
        json.dumps(schema or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
