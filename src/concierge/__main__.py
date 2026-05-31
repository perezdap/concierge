"""`python -m concierge --config path/to/gateway.yaml`

Also exposes a small token-admin CLI for the P1-1 per-tenant token store and
revocation list, so operators can mint / rotate / revoke without the HTTP path:

    python -m concierge token --config gw.yaml mint   --tenant acme
    python -m concierge token --config gw.yaml rotate --tenant acme --old tt_...
    python -m concierge token --config gw.yaml list
    python -m concierge token --config gw.yaml revoke --id tt_...
    python -m concierge token --config gw.yaml revocations
"""
from __future__ import annotations

import argparse
import asyncio
import sys

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

    args = parser.parse_args(argv)

    if args.command == "token":
        return asyncio.run(_run_token_command(args))

    if not args.config:
        parser.error("--config is required to run the gateway")
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())
