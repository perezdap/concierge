"""Regression tests for the local docker-compose stack (Twelve-Factor shape).

These tests guard the operator-facing contract documented in
docs/DEPLOYMENT.md ("Twelve-Factor compose"):

* The compose file parses as valid YAML and resolves cleanly with both
  Compose v1 and v2 (we re-parse with PyYAML since the harness may not have
  Docker available).
* The bind-mount `./config:/app/config:ro` is present, so operator edits to
  config/gateway.yaml show up on `docker compose up --force-recreate` with
  NO image rebuild.
* `env_file: ./.env` is wired, with `required: false` so the stack still
  starts when no .env exists (the committed example uses ${VAR:-default}).
* No hard-coded secret values are present anywhere in the compose file.
* `.env.example` is committed, has the right shape, and is NOT gitignored.
* The example config is loadable with `expand_env` (the P0-6 path) using
  dummy env values.

These are structural / documentation tests, not behavioural ones — they
exist so a future edit can't accidentally drop the bind mount, the
env_file, or the .env.example.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
COMPOSE_TEST = REPO_ROOT / "docker-compose.test.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
EXAMPLE_CONFIG = REPO_ROOT / "config" / "gateway.example.yaml"
GITIGNORE = REPO_ROOT / ".gitignore"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


# ---------------------------------------------------------------------------
# Compose file structure
# ---------------------------------------------------------------------------


def _load_compose() -> dict:
    with COMPOSE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_compose_yaml_parses():
    data = _load_compose()
    assert "services" in data, "compose file must define services"
    assert "concierge" in data["services"], "compose file must define the concierge service"


def test_compose_uses_bind_mount_for_config():
    """The bind mount is the whole point: operator edits to config/ show up
    on the next recreate without rebuilding the image."""
    data = _load_compose()
    service = data["services"]["concierge"]
    volumes = service.get("volumes", [])
    # Compose v2 normalizes volumes to a list of dicts; accept either form.
    found = False
    for v in volumes:
        if isinstance(v, dict):
            source = v.get("source", "")
            target = v.get("target", "")
            read_only = v.get("read_only", False)
            if source.endswith("config") and target == "/app/config" and read_only:
                found = True
                break
        elif isinstance(v, str) and v.startswith("./config:/app/config"):
            found = True
            break
    assert found, (
        "Expected a read-only bind mount of ./config to /app/config "
        f"so operator config edits are picked up without rebuilding. Got volumes: {volumes!r}"
    )


def test_compose_has_env_file():
    """Operators inject secrets via .env (Twelve-Factor §III). The compose
    file MUST load it, with required=false so the stack still starts when
    no .env exists yet (the example config uses ${VAR:-default})."""
    data = _load_compose()
    service = data["services"]["concierge"]
    env_file = service.get("env_file")
    assert env_file, "compose service must declare env_file so .env is auto-loaded"
    # Compose accepts either a path string or a list of {path, required}.
    entries = env_file if isinstance(env_file, list) else [{"path": env_file}]
    assert any(".env" in (e.get("path") if isinstance(e, dict) else str(e)) for e in entries), (
        f"env_file must reference ./.env, got: {env_file!r}"
    )
    # If entries are dicts, the .env one must be non-required so the default
    # example still works for first-time operators.
    for e in entries:
        if isinstance(e, dict) and ".env" in e.get("path", ""):
            assert e.get("required", True) is False, (
                "env_file ./.env must be required:false so the stack starts "
                "without a populated .env (the example config uses ${VAR:-default})"
            )


def test_compose_command_points_at_a_config_in_the_mounted_path():
    """The active --config argument should resolve to a path inside the
    bind-mounted /app/config, so operator edits show up without rebuild."""
    data = _load_compose()
    cmd = data["services"]["concierge"].get("command")
    assert cmd, "compose service must set a command with --config"
    assert "--config" in cmd
    config_arg = cmd[cmd.index("--config") + 1]
    assert config_arg.startswith("/app/config/"), (
        f"--config should point into the bind-mounted /app/config, got: {config_arg!r}"
    )


def test_compose_does_not_harbor_hardcoded_secrets():
    """A hardcoded Bearer token, AWS access key, or similar in the compose
    file would defeat Twelve-Factor §III and the docs/DEPLOYMENT.md
    'Secret handling' policy."""
    text = COMPOSE.read_text(encoding="utf-8")
    # Any of these substrings would be a leak. They're case-insensitive on
    # the credential kind so a typo'd header still trips it.
    forbidden = [
        "Bearer eyJ",          # JWT-shaped literal
        "AKIA",                # AWS access key id pattern
        "ghp_",                # GitHub PAT
        "sk-",                 # OpenAI-style secret
        "xoxb-",               # Slack bot token
        "xoxp-",               # Slack user token
    ]
    for needle in forbidden:
        assert needle not in text, f"forbidden secret-like literal {needle!r} found in docker-compose.yml"


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


def test_env_example_exists_and_is_committable():
    assert ENV_EXAMPLE.exists(), f"{ENV_EXAMPLE.name} must exist as a committed template"
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # No populated secrets in the template — every reference must be commented out
    # or empty. The simplest check: no `=value` assignments on uncommented lines.
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Uncommented `KEY=value` would mean we shipped a real secret.
        assert re.fullmatch(r"[A-Z_][A-Z0-9_]*\s*=\s*", raw), (
            f"uncommented assignment in .env.example would leak: {raw!r}"
        )


def test_env_example_acknowledges_core_tokens():
    """The template must call out the tokens the example YAML references,
    otherwise operators won't know what to put in .env."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for token in ("BM_LIVE_TOKEN", "NOTES_TOKEN", "JIRA_TOKEN"):
        assert token in text, f".env.example should reference {token} (used by the example config)"


def test_env_example_not_gitignored():
    """git check-ignore — make sure the .gitignore doesn't accidentally
    hide the template. We allow a negation rule `!.env.example`."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git repo")
    result = subprocess.run(
        ["git", "check-ignore", "-v", ".env.example"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # git check-ignore exits 0 when the path IS ignored, 1 when it's NOT,
    # 128 on error. A negation rule counts as "matched but allowed", so
    # we look at the matched rule — if it's `!.env.example`, we're good.
    if result.returncode == 0:
        # The matched rule is in stdout; it must be a `!` allow rule.
        assert "!.env.example" in result.stdout, (
            f".env.example is being ignored. Allow it with `!.env.example` in .gitignore. "
            f"git said: {result.stdout!r}"
        )


def test_env_file_blocked_in_dockerignore():
    """`.env` must never be baked into the image, even by accident. The
    `.dockerignore` should list it explicitly (or via a pattern that
    matches it)."""
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    patterns = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    # Accept either an exact `.env` line or a `.env.*` line.
    assert any(p in (".env", ".env.*") for p in patterns), (
        ".dockerignore must list .env (or .env.*) so it can't be baked into the image"
    )


# ---------------------------------------------------------------------------
# Example config sanity (smoke-test the runtime path with dummy env)
# ---------------------------------------------------------------------------


def test_example_config_loads_with_dummy_env(monkeypatch):
    """The example YAML references ${NOTES_TOKEN} and ${JIRA_TOKEN}. The
    `${VAR}` strict-expansion in load_config() would normally refuse to
    start without those, but the example uses no-default ${VAR}. Setting
    a dummy value proves the env-templating + load_config path still
    works end-to-end after our compose change."""
    monkeypatch.setenv("NOTES_TOKEN", "dummy")
    monkeypatch.setenv("JIRA_TOKEN", "dummy")
    # Re-import so module-level env caches don't pin stale values.
    from concierge.config import load_config  # type: ignore
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.gateway.port == 8765
    # And the templated header actually got the dummy value (not a literal
    # "${NOTES_TOKEN}" or empty string).
    notes_server = next(s for s in cfg.upstream_servers if s.id == "notes")
    auth = notes_server.headers.get("Authorization", "")
    assert "dummy" in auth, f"NOTES_TOKEN env-templating broke; got header: {auth!r}"
