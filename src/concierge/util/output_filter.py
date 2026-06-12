"""
Output filtering for upstream tool results (response-side complement to input sanitize).

P1-8: redacts secrets/PII, caps lengths, filters content types before results reach
downstream clients or audit. Mirrors util/sanitize.py style: pure functions, no
side effects, composable chain, explicit and conservative.

Security: prevent secret leakage in tool outputs (OWASP sensitive data exposure);
length caps defend against token bloat / DoS from verbose upstreams.
"""

from __future__ import annotations

import re
from typing import Any

# Simple secret patterns (extend as needed; keep conservative to avoid false positives).
# Do NOT add a generic [A-Za-z0-9_-]{N,} catch-all: it matches ordinary words,
# filenames, UUIDs in URLs, and base64-ish identifiers (see issue #6).
#
# This redactor is a best-effort backstop, not the primary control against
# credential leakage. By design it only catches distinctively-prefixed secrets:
# an unprefixed random token (e.g. a gateway-minted tenant token, which is a bare
# secrets.token_urlsafe value) is indistinguishable from ordinary output, so it is
# intentionally NOT matched here. Redacting those reliably requires giving such
# tokens a stable issuer prefix at mint time — a separate change, not a regex.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(sk-[A-Za-z0-9_-]{6,})"), "[REDACTED_SECRET]"),
    (re.compile(r"(ghp_[A-Za-z0-9_-]{6,})"), "[REDACTED_SECRET]"),
    (re.compile(r"(Bearer\s+[A-Za-z0-9._-]{6,})", re.I), "Bearer [REDACTED]"),
    # Well-known credential formats anchored to their issuer prefix. Prefix
    # anchoring (not a generic length rule) keeps these from re-introducing the
    # issue #6 false positives on ordinary identifiers, UUIDs, and filenames.
    (re.compile(r"((?:AKIA|ASIA)[0-9A-Z]{16})"), "[REDACTED_SECRET]"),  # AWS access key id
    (re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED_SECRET]"),  # GitHub fine-grained PAT
    (re.compile(r"(gh[ousr]_[A-Za-z0-9_-]{20,})"), "[REDACTED_SECRET]"),  # GitHub OAuth token
    (re.compile(r"(xox[baprs]-[A-Za-z0-9-]{10,})"), "[REDACTED_SECRET]"),  # Slack token
    (re.compile(r"(AIza[0-9A-Za-z_-]{35})"), "[REDACTED_SECRET]"),  # Google API key
]


def _redact_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


class SecretRedactor:
    """Redacts common secret/token patterns from text content in results."""

    def apply(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        out = dict(result)
        if "content" in out and isinstance(out["content"], list):
            new_content = []
            for item in out["content"]:
                if isinstance(item, dict) and "text" in item:
                    item = dict(item)
                    item["text"] = _redact_text(item["text"])
                new_content.append(item)
            out["content"] = new_content
        return out


class LengthCapper:
    """Caps text fields in result content to prevent bloat (bytes)."""

    def __init__(self, max_bytes: int = 4096) -> None:
        self.max = max_bytes

    def apply(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict) or self.max <= 0:
            return result
        out = dict(result)
        if "content" in out and isinstance(out["content"], list):
            new_content = []
            for item in out["content"]:
                if isinstance(item, dict) and "text" in item and isinstance(item["text"], str):
                    item = dict(item)
                    txt = item["text"]
                    if len(txt.encode("utf-8")) > self.max:
                        # soft cap at boundary
                        cut = txt.encode("utf-8")[: self.max].decode("utf-8", errors="ignore")
                        item["text"] = cut + "…[TRUNCATED]"
                        if "_meta" not in item:
                            item["_meta"] = {}
                        item["_meta"]["gateway_truncated"] = True
                new_content.append(item)
            out["content"] = new_content
        return out


class ContentTypeFilter:
    """
    Drops disallowed content types (e.g. images, binary) from results.
    Conservative allow-list.
    """

    def __init__(self, allowed: set[str] | None = None) -> None:
        self.allowed = allowed or {"text", "json", "markdown"}

    def apply(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            return result
        out = dict(result)
        if "content" in out and isinstance(out["content"], list):
            new_content = []
            for item in out["content"]:
                if isinstance(item, dict):
                    ctype = item.get("type", "text")
                    if ctype in self.allowed:
                        new_content.append(item)
                    else:
                        # drop or replace with placeholder
                        new_content.append({
                            "type": "text",
                            "text": f"[filtered content-type: {ctype}]",
                        })
                else:
                    new_content.append(item)
            out["content"] = new_content
        return out


class OutputFilter:
    """Composable chain of output filters. Applied to tool result dicts before return to client."""

    def __init__(self, filters: list[Any] | None = None) -> None:
        self.filters = filters or []

    def apply(self, result: dict[str, Any]) -> dict[str, Any]:
        if not self.filters:
            return result  # conservative pass-through
        current = result
        for f in self.filters:
            if hasattr(f, "apply"):
                current = f.apply(current)
        return current
