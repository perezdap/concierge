"""Redaction helpers for audit logging and error data."""
from __future__ import annotations

import re
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|passwd|authorization|bearer|cookie)"
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def redact_value(v: Any) -> Any:
    if isinstance(v, str):
        return _BEARER_RE.sub("Bearer ***", v)
    return v


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return redact_value(obj)
