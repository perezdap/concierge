"""ADMIN-10 E2E harness tests."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from pathlib import Path

from scripts.admin_e2e import (
    DEFAULT_WORK_ROOT,
    run_admin_release_gate,
    run_fake_oauth_idp_smoke,
)


def _work_dir() -> Path:
    path = DEFAULT_WORK_ROOT / ("pytest-" + uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_admin_release_gate() -> None:
    result = run_admin_release_gate(_work_dir())
    assert result["spa"]["asset"].startswith("assets/")
    assert result["auth"] == {"localhost": True, "bearer": True}
    assert result["upstream"]["id"] == "admin-e2e-echo"
    assert result["upstream"]["tools_discovered"] >= 1
    assert result["upstream"]["tool_available_without_restart"] == "admin-e2e-echo__echo"
    assert result["profile"]["matched"] >= 1
    assert result["rollback_after_failed_reload"] is True
    assert result["redaction"]["api_diff_export"] is True


def test_fake_oauth_idp_smoke() -> None:
    result = asyncio.run(run_fake_oauth_idp_smoke())
    assert result == {"oauth_revoke_calls": 1, "connected": False}


def test_admin_e2e_cli() -> None:
    script = Path("scripts/admin_e2e.py")
    proc = subprocess.run(
        [sys.executable, str(script), "--work-dir", str(_work_dir())],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout
    assert '"tool_available_without_restart": "admin-e2e-echo__echo"' in proc.stdout
    assert '"oauth_revoke_calls": 1' in proc.stdout
