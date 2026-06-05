"""Structured field-path redaction for admin config API, diffs, YAML export, and audit."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Literal

from ..util.redact import redact as redact_audit_keys
from .secrets import REDACTED_VALUE, is_secret_ref

RedactionMode = Literal["api", "yaml", "audit", "diff"]

_SECRET_KEY_RE = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|passwd|authorization|bearer|cookie|client_secret|access_token|refresh_token)"
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_JWTISH_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9._\-]+\.[A-Za-z0-9._\-]+")

# Path patterns use ``*`` for any list index or dict key segment.
DEFAULT_SENSITIVE_PATHS: tuple[str, ...] = (
    "auth.bearer_tokens",
    "auth.bearer_tokens.*",
    "upstream_servers.*.headers",
    "upstream_servers.*.headers.*",
    "upstream_servers.*.env",
    "upstream_servers.*.env.*",
    "policy.webhook.default_secret",
    "policy.webhook.tenant_secrets",
    "policy.webhook.tenant_secrets.*",
    "observability.audit_http_sink_url",
)


def _segment_match(pattern_seg: str, path_seg: str) -> bool:
    return pattern_seg == "*" or pattern_seg == path_seg


def _path_matches(pattern: str, path: str) -> bool:
    p_parts = pattern.split(".")
    path_parts = path.split(".")
    if len(p_parts) != len(path_parts):
        return False
    return all(_segment_match(ps, pp) for ps, pp in zip(p_parts, path_parts, strict=True))


def _collect_paths(
    obj: Any,
    *,
    prefix: str = "",
    patterns: tuple[str, ...],
) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if any(_path_matches(pat, path) for pat in patterns):
                found.add(path)
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                found.add(path)
            found |= _collect_paths(val, prefix=path, patterns=patterns)
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            path = f"{prefix}.{idx}" if prefix else str(idx)
            list_path = f"{prefix}.*" if prefix else "*"
            if any(_path_matches(pat, list_path) for pat in patterns):
                found.add(path)
            found |= _collect_paths(item, prefix=path, patterns=patterns)
    return found


def _looks_like_secret_value(value: Any) -> bool:
    if is_secret_ref(value):
        return True
    if not isinstance(value, str):
        return False
    if _BEARER_RE.search(value):
        return True
    if _JWTISH_RE.search(value):
        return True
    if len(value) >= 24 and _SECRET_KEY_RE.search(value):
        return True
    return bool(_SECRET_KEY_RE.search(value) and len(value) >= 8)


def _redact_subtree(value: Any, *, mode: RedactionMode, force: bool = False) -> Any:
    if is_secret_ref(value):
        return value if mode == "api" else REDACTED_VALUE
    if isinstance(value, dict):
        return {
            k: _redact_subtree(
                v,
                mode=mode,
                force=force or (isinstance(k, str) and bool(_SECRET_KEY_RE.search(k))),
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_subtree(item, mode=mode, force=force) for item in value]
    if force or _looks_like_secret_value(value):
        return REDACTED_VALUE
    if isinstance(value, str):
        return _BEARER_RE.sub("Bearer ***", value)
    return value


def _redact_value(value: Any, *, mode: RedactionMode, force: bool = False) -> Any:
    return _redact_subtree(value, mode=mode, force=force)


def redact_at_path(obj: Any, path: str, *, mode: RedactionMode = "api") -> Any:
    """Return a deep copy with one dotted path redacted (raises KeyError if missing)."""
    out = deepcopy(obj)
    parts = path.split(".")
    cur: Any = out
    for part in parts[:-1]:
        if isinstance(cur, dict):
            cur = cur[part]
        elif isinstance(cur, list):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    last = parts[-1]
    if isinstance(cur, dict):
        cur[last] = _redact_value(cur[last], mode=mode, force=True)
    elif isinstance(cur, list):
        cur[int(last)] = _redact_value(cur[int(last)], mode=mode)
    else:
        raise KeyError(path)
    return out


def redact_structured(
    obj: Any,
    *,
    mode: RedactionMode = "api",
    extra_paths: tuple[str, ...] = (),
) -> Any:
    """Redact sensitive paths and key names across a config-shaped object."""
    patterns = DEFAULT_SENSITIVE_PATHS + extra_paths
    sensitive = _collect_paths(obj, patterns=patterns)
    if not sensitive:
        return _redact_tree(obj, mode=mode, current_path="")
    out = deepcopy(obj)
    for path in sorted(sensitive, key=len, reverse=True):
        try:
            out = redact_at_path(out, path, mode=mode)
        except KeyError:
            continue
    return _redact_tree(out, mode=mode, current_path="")


def _redact_tree(obj: Any, *, mode: RedactionMode, current_path: str) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, val in obj.items():
            path = f"{current_path}.{key}" if current_path else key
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = _redact_subtree(val, mode=mode, force=True)
            else:
                out[key] = _redact_tree(val, mode=mode, current_path=path)
        return out
    if isinstance(obj, list):
        return [
            _redact_tree(item, mode=mode, current_path=f"{current_path}.{idx}")
            for idx, item in enumerate(obj)
        ]
    return _redact_value(obj, mode=mode)


def redact_for_audit(fields: dict[str, Any]) -> dict[str, Any]:
    """Audit-safe payload: structured paths + legacy key-name redaction."""
    return redact_audit_keys(redact_structured(fields, mode="audit"))


def redact_config_for_api(obj: Any) -> Any:
    return redact_structured(obj, mode="api")


def redact_config_for_yaml_export(obj: Any) -> Any:
    """YAML export must never contain raw secrets — refs become placeholders."""
    redacted = redact_structured(obj, mode="yaml")
    return _strip_secret_refs(redacted)


def _strip_secret_refs(obj: Any) -> Any:
    if is_secret_ref(obj):
        return REDACTED_VALUE
    if isinstance(obj, dict):
        return {k: _strip_secret_refs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_secret_refs(x) for x in obj]
    return obj


def _flatten_leaves(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, val in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(val, (dict, list)):
                out.update(_flatten_leaves(val, path))
            else:
                out[path] = val
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            path = f"{prefix}.{idx}" if prefix else str(idx)
            if isinstance(item, (dict, list)):
                out.update(_flatten_leaves(item, path))
            else:
                out[path] = item
    elif prefix:
        out[prefix] = obj
    return out


def redact_config_diff(before: Any, after: Any) -> dict[str, dict[str, Any]]:
    """Return changed leaf paths with redacted before/after values."""
    b_flat = _flatten_leaves(before)
    a_flat = _flatten_leaves(after)
    changes: dict[str, dict[str, Any]] = {}
    for path in sorted(set(b_flat) | set(a_flat)):
        old = b_flat.get(path)
        new = a_flat.get(path)
        if old != new:
            changes[path] = {
                "before": _redact_value(old, mode="diff", force=True) if old is not None else None,
                "after": _redact_value(new, mode="diff", force=True) if new is not None else None,
            }
    return changes


def assert_no_leaked_secrets(payload: Any, *, needles: list[str]) -> None:
    """Test helper: raise AssertionError if any needle appears in a serialized tree."""
    text = repr(payload)
    for needle in needles:
        if needle in text:
            raise AssertionError(f"leaked secret needle {needle!r} in payload")