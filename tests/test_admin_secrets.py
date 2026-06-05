"""Tests for admin secret handling and structured redaction (P2-ADMIN-8)."""
from __future__ import annotations

import pytest

from concierge.admin.redaction import (
    assert_no_leaked_secrets,
    redact_config_diff,
    redact_config_for_api,
    redact_config_for_yaml_export,
    redact_for_audit,
    redact_structured,
)
from concierge.admin.secrets import (
    WRITE_ONLY_MARKER,
    EncryptedCredentialStore,
    InMemoryCredentialStore,
    is_secret_ref,
    is_write_only_placeholder,
    make_secret_ref,
    merge_write_only_secrets,
    new_credential_id,
    parse_secret_ref,
    resolve_fernet_key,
    write_only_placeholder,
)


@pytest.fixture
def fernet_key() -> bytes:
    return resolve_fernet_key("unit-test-credential-key-material")


@pytest.fixture
def store(fernet_key: bytes) -> InMemoryCredentialStore:
    return InMemoryCredentialStore(key=fernet_key)


async def test_secret_ref_round_trip(store: InMemoryCredentialStore) -> None:
    cred_id = new_credential_id()
    ref = await store.store(
        credential_id=cred_id,
        payload={"access_token": "tok_super_secret_123", "refresh_token": "rt_abc"},
    )
    assert is_secret_ref(ref)
    assert parse_secret_ref(ref) == cred_id
    got = await store.retrieve(ref)
    assert got["access_token"] == "tok_super_secret_123"
    await store.delete(ref)
    with pytest.raises(KeyError):
        await store.retrieve(ref)


def test_write_only_merge_keeps_existing_ref() -> None:
    existing = {
        "headers": {"Authorization": make_secret_ref("cred_hdr")},
        "url": "https://upstream.example/mcp",
    }
    incoming = {
        "headers": {WRITE_ONLY_MARKER: True},
        "url": "https://upstream.example/v2",
    }
    merged = merge_write_only_secrets(incoming, existing)
    assert merged["headers"]["Authorization"] == existing["headers"]["Authorization"]
    assert merged["url"] == "https://upstream.example/v2"


def test_write_only_placeholder_detection() -> None:
    assert is_write_only_placeholder(WRITE_ONLY_MARKER)
    assert is_write_only_placeholder(write_only_placeholder())


def test_api_redaction_hides_raw_tokens() -> None:
    fake_token = "sk-1234567890abcdef"
    fake_bearer = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    config = {
        "auth": {"bearer_tokens": [fake_token]},
        "upstream_servers": [
            {
                "id": "gh",
                "headers": {"Authorization": fake_bearer, "X-Api-Key": "ghp_deadbeef0001"},
            }
        ],
    }
    redacted = redact_config_for_api(config)
    assert_no_leaked_secrets(redacted, needles=[fake_token, "ghp_deadbeef0001", "payload.sig"])
    assert redacted["auth"]["bearer_tokens"] == ["***"]


def test_yaml_export_strips_secret_refs_and_values() -> None:
    ref = make_secret_ref("cred_oauth")
    config = {
        "upstream_servers": [{"id": "x", "headers": {"Authorization": ref}}],
        "policy": {"webhook": {"default_secret": "whsec_plain"}},
    }
    exported = redact_config_for_yaml_export(config)
    assert_no_leaked_secrets(exported, needles=["whsec_plain", "cred_oauth"])
    assert exported["upstream_servers"][0]["headers"]["Authorization"] == "***"


def test_audit_redaction_masks_bearer_substrings() -> None:
    payload = redact_for_audit(
        {"operator": "admin", "header": "Authorization: Bearer secret-token-xyz"}
    )
    assert "secret-token-xyz" not in repr(payload)
    assert "Bearer ***" in repr(payload) or "***" in repr(payload)


def test_config_diff_redacts_changed_secrets() -> None:
    before = {"auth": {"bearer_tokens": ["old-secret-value"]}}
    after = {"auth": {"bearer_tokens": ["new-secret-value"]}}
    diff = redact_config_diff(before, after)
    token_paths = [k for k in diff if k.startswith("auth.bearer_tokens")]
    assert token_paths
    for path in token_paths:
        assert diff[path]["before"] == "***"
        assert diff[path]["after"] == "***"
    assert_no_leaked_secrets(diff, needles=["old-secret-value", "new-secret-value"])


def test_structured_redact_preserves_secret_ref_in_api_mode() -> None:
    ref = make_secret_ref("cred_keep")
    obj = {"token_ref": ref}
    out = redact_structured(obj, mode="api")
    assert out["token_ref"] == ref


def test_encrypted_store_requires_key_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONCIERGE_CREDENTIAL_KEY", raising=False)
    with pytest.raises(ValueError, match="CONCIERGE_CREDENTIAL_KEY"):
        EncryptedCredentialStore()


def test_encrypted_store_uses_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCIERGE_CREDENTIAL_KEY", "env-passphrase-for-tests")
    store = EncryptedCredentialStore()
    assert store is not None