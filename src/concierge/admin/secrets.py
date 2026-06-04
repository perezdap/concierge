"""Admin secret handling: secret_ref, write-only fields, encrypted credential storage."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

SECRET_REF_PREFIX = "secret_ref:"
REDACTED_VALUE = "***"
WRITE_ONLY_MARKER = "__write_only__"


def new_credential_id() -> str:
    return "cred_" + secrets.token_hex(12)


def make_secret_ref(credential_id: str) -> str:
    if not credential_id or credential_id.startswith(SECRET_REF_PREFIX):
        raise ValueError("invalid credential_id for secret_ref")
    return f"{SECRET_REF_PREFIX}{credential_id}"


def is_secret_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_REF_PREFIX)


def parse_secret_ref(ref: str) -> str:
    if not is_secret_ref(ref):
        raise ValueError(f"not a secret_ref: {ref!r}")
    return ref[len(SECRET_REF_PREFIX) :]


def is_write_only_placeholder(value: Any) -> bool:
    """True when an inbound admin payload omits a secret (keep existing ref)."""
    if value is WRITE_ONLY_MARKER:
        return True
    return isinstance(value, dict) and value.get(WRITE_ONLY_MARKER) is True


def write_only_placeholder() -> dict[str, bool]:
    return {WRITE_ONLY_MARKER: True}


def resolve_fernet_key(key_material: str | bytes | None = None) -> bytes:
    """Derive or parse a Fernet key from explicit material or env."""
    raw: bytes
    if key_material is None:
        env = os.environ.get("CONCIERGE_CREDENTIAL_KEY", "")
        if not env:
            raise ValueError(
                "CONCIERGE_CREDENTIAL_KEY is required for EncryptedCredentialStore"
            )
        key_material = env
    if isinstance(key_material, str):
        raw = key_material.encode()
    else:
        raw = key_material
    try:
        Fernet(raw)
        return raw
    except Exception:
        digest = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(digest)


class CredentialStore(Protocol):
    async def store(self, *, credential_id: str, payload: dict[str, Any]) -> str: ...

    async def retrieve(self, ref: str) -> dict[str, Any]: ...

    async def delete(self, ref: str) -> None: ...


class EncryptedCredentialStore:
    """Fernet-encrypted credential blobs keyed by credential_id (dev/local default)."""

    def __init__(self, *, key: bytes | None = None) -> None:
        self._fernet = Fernet(key or resolve_fernet_key())
        self._ciphertext: dict[str, bytes] = {}

    async def store(self, *, credential_id: str, payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True).encode()
        self._ciphertext[credential_id] = self._fernet.encrypt(blob)
        return make_secret_ref(credential_id)

    async def retrieve(self, ref: str) -> dict[str, Any]:
        credential_id = parse_secret_ref(ref)
        token = self._ciphertext.get(credential_id)
        if token is None:
            raise KeyError(f"unknown credential: {credential_id}")
        try:
            raw = self._fernet.decrypt(token)
        except InvalidToken as e:
            raise ValueError("credential blob failed decryption") from e
        data = json.loads(raw.decode())
        if not isinstance(data, dict):
            raise ValueError("credential payload must be a JSON object")
        return data

    async def delete(self, ref: str) -> None:
        credential_id = parse_secret_ref(ref)
        self._ciphertext.pop(credential_id, None)


class InMemoryCredentialStore(EncryptedCredentialStore):
    """Alias for tests and local admin flows."""


def merge_write_only_secrets(
    incoming: Any,
    existing: Any,
    *,
    secret_paths: set[str] | None = None,
) -> Any:
    """Apply write-only semantics: placeholders keep prior secret_ref values."""
    if is_write_only_placeholder(incoming):
        return existing
    if isinstance(incoming, dict) and isinstance(existing, dict):
        out: dict[str, Any] = {}
        for key, val in incoming.items():
            prior = existing.get(key)
            if is_write_only_placeholder(val):
                out[key] = prior
            elif isinstance(val, dict) and isinstance(prior, dict):
                out[key] = merge_write_only_secrets(val, prior, secret_paths=secret_paths)
            else:
                out[key] = val
        for key, val in existing.items():
            if key not in out and is_secret_ref(val):
                out[key] = val
        return out
    return incoming


