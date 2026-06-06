"""`python -m concierge --config path/to/gateway.yaml`

Also exposes a small token-admin CLI for the P1-1 per-tenant token store and
revocation list, so operators can mint / rotate / revoke without the HTTP path:

    python -m concierge token --config gw.yaml mint   --tenant acme
    python -m concierge token --config gw.yaml rotate --tenant acme --old tt_...
    python -m concierge token --config gw.yaml list
    python -m concierge token --config gw.yaml revoke --id tt_...
    python -m concierge token --config gw.yaml revocations

And a P1-3 approval-queue CLI mirroring the HTTP /admin/approvals surface:

    python -m concierge approval --config gw.yaml list   [--tenant acme]
    python -m concierge approval --config gw.yaml grant  --id ap_... --by ops@acme
    python -m concierge approval --config gw.yaml deny    --id ap_... --by ops@acme --reason "..."
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import sys
from pathlib import Path

import uvicorn

from .config import GatewayConfig, load_config
from .server.app import build_app


def _build_stores(cfg: GatewayConfig):  # type: ignore[no-untyped-def]
    """Construct the same revocation + tenant-token stores the app would use."""
    from .server.app import _build_revocation_store, _build_tenant_token_store

    return _build_revocation_store(cfg), _build_tenant_token_store(cfg)


async def _run_token_command(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    revocation, tenant_tokens = _build_stores(cfg)
    try:
        if args.action == "mint":
            minted = await tenant_tokens.mint(args.tenant)
            # The raw secret is printed exactly once. Capture it now.
            print(f"token_id: {minted.token_id}")
            print(f"tenant:   {minted.tenant_id}")
            print(f"token:    {minted.token}")
            return 0
        if args.action == "rotate":
            minted = await tenant_tokens.rotate(args.tenant, args.old)
            await revocation.revoke(args.old)
            print(f"token_id: {minted.token_id}")
            print(f"tenant:   {minted.tenant_id}")
            print(f"token:    {minted.token}")
            print(f"revoked old: {args.old}")
            return 0
        if args.action == "list":
            for rec in await tenant_tokens.list_records():
                print(f"{rec.token_id}\t{rec.tenant_id}")
            return 0
        if args.action == "revoke":
            await revocation.revoke(args.id, ttl_s=args.ttl)
            print(f"revoked: {args.id}")
            return 0
        if args.action == "revocations":
            for tid in await revocation.all():
                print(tid)
            return 0
        return 2
    finally:
        await revocation.aclose()
        await tenant_tokens.aclose()


async def _run_approval_command(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from .server.app import (
        _build_approval_store,
        _build_webhook_dispatcher,
    )
    from .util.audit import AuditLogger

    store = _build_approval_store(cfg)
    audit = AuditLogger()
    try:
        if args.action == "list":
            tenant = getattr(args, "tenant", None)
            for rec in await store.list_pending(tenant_id=tenant):
                print(f"{rec.approval_id}\t{rec.tenant_id}\t{rec.canonical_name}\t{rec.args_summary}")
            return 0
        # grant / deny go through a queue broker so webhooks + audit fire just like
        # the HTTP path. (A non-queue config still lets the operator inspect/decide.)
        webhooks = _build_webhook_dispatcher(cfg, audit)
        from .policy.approval import QueuedApprovalBroker

        broker = QueuedApprovalBroker(
            store,
            ttl_s=cfg.policy.approval.ttl_s,
            wait_timeout_s=cfg.policy.approval.wait_timeout_s,
            poll_interval_s=cfg.policy.approval.poll_interval_s,
            webhooks=webhooks,
            audit=audit,
        )
        granted = args.action == "grant"
        record = await broker.decide(
            args.id,
            granted=granted,
            decided_by=args.by,
            tenant_id=getattr(args, "tenant", None),
            reason=getattr(args, "reason", None),
        )
        if record is None:
            print(f"no such approval (or wrong tenant): {args.id}", file=sys.stderr)
            return 1
        print(f"{record.approval_id}\t{record.status.value}\tby={record.decided_by}")
        return 0
    finally:
        await store.aclose()


def _run_init_command(args: argparse.Namespace) -> int:
    """One-shot first-run helper: ensure .env exists and contains a GATEWAY_TOKEN."""
    env_path = Path(args.env_file)
    example_path = Path(args.env_example)

    if not env_path.exists():
        if example_path.exists():
            shutil.copy(example_path, env_path)
            print(f"Created {env_path} from {example_path}")
        else:
            print(f" neither {env_path} nor {example_path} found; creating empty {env_path}")
            env_path.write_text("\n")

    content = env_path.read_text()
    lines = content.splitlines(keepends=True)

    token_line_idx: int | None = None
    token_value = ""
    for i, line in enumerate(lines):
        if line.strip().startswith("GATEWAY_TOKEN="):
            token_line_idx = i
            token_value = line.split("=", 1)[1].strip()
            break
        if line.strip().startswith("# GATEWAY_TOKEN="):
            token_line_idx = i
            token_value = ""

    token_to_sync = token_value
    is_new = False

    if not token_value:
        token_to_sync = secrets.token_urlsafe(32)
        is_new = True
        new_line = f"GATEWAY_TOKEN={token_to_sync}\n"

        if token_line_idx is not None:
            lines[token_line_idx] = new_line
        else:
            lines.append("\n")
            lines.append("# Auto-generated on first run (concierge init)\n")
            lines.append(new_line)

        env_path.write_text("".join(lines))
        print(f"==> First-run admin token: {token_to_sync}   (also saved to {env_path})")

    # Sync GATEWAY_TOKEN to config/.gateway_token for container accessibility on first boot
    config_dir = env_path.parent / "config"
    if config_dir.exists() and config_dir.is_dir():
        token_file = config_dir / ".gateway_token"
        try:
            token_file.write_text(token_to_sync, encoding="utf-8")
            print(f"Synced GATEWAY_TOKEN to {token_file}")
        except Exception as e:
            print(f"Failed to sync token to {token_file}: {e}")

    if not is_new:
        print("GATEWAY_TOKEN already set — idempotent, env file not modified.")

    return 0


def _serve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not cfg.gateway.bind_public and cfg.gateway.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "refusing to bind to non-loopback host without gateway.bind_public=true",
            file=sys.stderr,
        )
        return 2
    app = build_app(cfg)
    uvicorn.run(app, host=cfg.gateway.host, port=cfg.gateway.port, log_config=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="concierge")
    # `--config` directly under the root keeps the historical `concierge --config`
    # invocation working (serve mode); subcommands take their own `--config`.
    parser.add_argument("--config", help="Path to gateway YAML config (serve mode).")
    sub = parser.add_subparsers(dest="command")

    token = sub.add_parser("token", help="Manage per-tenant tokens / revocations (P1-1).")
    token.add_argument("--config", required=True, help="Path to gateway YAML config.")
    token_sub = token.add_subparsers(dest="action", required=True)

    p_mint = token_sub.add_parser("mint", help="Mint a new token for a tenant.")
    p_mint.add_argument("--tenant", required=True)

    p_rot = token_sub.add_parser("rotate", help="Rotate a tenant token; revoke the old id.")
    p_rot.add_argument("--tenant", required=True)
    p_rot.add_argument("--old", required=True, help="Old token_id to retire + revoke.")

    token_sub.add_parser("list", help="List minted token ids (no secrets).")

    p_rev = token_sub.add_parser("revoke", help="Revoke a token id (static or OIDC jti).")
    p_rev.add_argument("--id", required=True)
    p_rev.add_argument("--ttl", type=int, default=None, help="Optional revocation TTL (s).")

    token_sub.add_parser("revocations", help="List currently revoked token ids.")

    approval = sub.add_parser("approval", help="Manage the P1-3 approval queue.")
    approval.add_argument("--config", required=True, help="Path to gateway YAML config.")
    approval_sub = approval.add_subparsers(dest="action", required=True)

    a_list = approval_sub.add_parser("list", help="List pending approvals.")
    a_list.add_argument("--tenant", default=None, help="Restrict to one tenant id.")

    a_grant = approval_sub.add_parser("grant", help="Grant a pending approval by id.")
    a_grant.add_argument("--id", required=True, help="approval_id to grant.")
    a_grant.add_argument("--by", required=True, help="Operator principal recorded on the decision.")
    a_grant.add_argument(
        "--tenant", default=None, help="Tenant scope (cross-tenant grants refused)."
    )
    a_grant.add_argument("--reason", default=None)

    a_deny = approval_sub.add_parser("deny", help="Deny a pending approval by id.")
    a_deny.add_argument("--id", required=True, help="approval_id to deny.")
    a_deny.add_argument("--by", required=True, help="Operator principal recorded on the decision.")
    a_deny.add_argument(
        "--tenant", default=None, help="Tenant scope (cross-tenant decisions refused)."
    )
    a_deny.add_argument("--reason", default=None)

    init = sub.add_parser("init", help="First-run helper: create .env and auto-generate GATEWAY_TOKEN.")
    init.add_argument("--env-file", default=".env", help="Path to the .env file to create/update.")
    init.add_argument("--env-example", default=".env.example", help="Template to copy when .env is missing.")

    args = parser.parse_args(argv)

    if args.command == "token":
        return asyncio.run(_run_token_command(args))

    if args.command == "approval":
        return asyncio.run(_run_approval_command(args))

    if args.command == "init":
        return _run_init_command(args)

    if not args.config:
        parser.error("--config is required to run the gateway")
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())
