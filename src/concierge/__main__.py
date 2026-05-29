"""`python -m concierge --config path/to/gateway.yaml`"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from .config import load_config
from .server.app import build_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="concierge")
    parser.add_argument("--config", required=True, help="Path to gateway YAML config.")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
