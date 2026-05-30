"""Reusable Concierge end-to-end verification harness.

Runs the high-risk P0 regression tests, a focused resource-safety soak, and the
real example server/client flow against ``config/gateway.example.yaml``.
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import socket
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TARGETED_PYTEST = [
    "tests/test_facade_integration.py",
    "tests/test_origin.py",
    "tests/test_server_app.py",
    "tests/test_policy_engine.py",
    "tests/test_adapters_framing.py",
    "tests/test_resource_safety.py",
    "tests/test_resilience.py",
    "tests/test_config.py",
]


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # The example config intentionally fails fast when secret env vars are
    # missing. E2E uses harmless dummy values so local verification is one-shot.
    env.setdefault("NOTES_TOKEN", "e2e-dummy")
    env.setdefault("JIRA_TOKEN", "e2e-dummy")
    return env


def _run(label: str, cmd: list[str], *, timeout: float = 180.0) -> None:
    print(f"\n== {label} ==")
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    output = proc.stdout.strip()
    if output:
        lines = output.splitlines()
        print("\n".join(lines[-80:]))
    if proc.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {proc.returncode}")


class _FakeSseResponse:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def aiter_text(self) -> Any:
        for chunk in self._chunks:
            await asyncio.sleep(0)
            yield chunk


async def _resource_safety_soak() -> None:
    from concierge.adapters.sse_legacy import LegacySseAdapter
    from concierge.adapters.stdio import StdioAdapter
    from concierge.adapters.streamable_http import StreamableHttpAdapter
    from concierge.core.notifications import NotificationBus
    from concierge.core.session import SessionManager

    tracemalloc.start()
    evicted: list[str] = []

    async def on_evict(session_id: str) -> None:
        evicted.append(session_id)

    sessions = SessionManager(idle_ttl_seconds=-1, on_evict=on_evict)
    bus = NotificationBus(coalesce_window_s=0.0, max_queue_size=8)
    session_ids: list[str] = []

    for idx in range(1000):
        session = await sessions.create(auth_subject=f"subject-{idx}")
        session_ids.append(session.session_id)
        for notification_idx in range(20):
            bus.publish(session.session_id, f"notifications/test/{notification_idx}")

    depths = bus.queue_depths()
    assert len(depths) == 1000
    assert max(depths.values()) == 8
    assert min(depths.values()) == 8
    assert await sessions.gc() == 1000
    assert len(evicted) == 1000
    for session_id in session_ids:
        bus.drop(session_id)
    gc.collect()
    current, peak = tracemalloc.get_traced_memory()

    code = (
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    sys.stderr.write('x' * 500000); sys.stderr.flush()\n"
        "    sys.stdout.write(json.dumps({"
        "'jsonrpc': '2.0', 'id': req['id'], 'result': {'ok': True}"
        "}) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )
    adapter = StdioAdapter("e2e-chatty", [PYTHON, "-c", code], request_timeout_s=5.0)
    await adapter.connect()
    try:
        assert await adapter._request("ping", {}) == {"ok": True}
    finally:
        await adapter.close()

    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    message = await StreamableHttpAdapter._read_first_sse_message(
        _FakeSseResponse(["data: " + payload[:12], payload[12:30], payload[30:] + "\n"])
    )
    assert message["result"]["ok"] is True

    events: list[tuple[str, str]] = []
    chunks = ["event: end", "point\n", "data: /m", "cp\n\n"]
    async for event, data in LegacySseAdapter._iter_sse_events(_FakeSseResponse(chunks)):
        events.append((event, data))
    assert events == [("endpoint", "/mcp")]

    print(
        "resource soak passed: "
        f"queues=1000 max_depth=8 evicted=1000 current_kib={current // 1024} "
        f"peak_kib={peak // 1024}"
    )


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
            return True
    except OSError:
        return False


def _run_real_server_flow() -> None:
    print("\n== real server session flow ==")
    if _port_open():
        raise SystemExit("127.0.0.1:8765 is already in use; stop the existing server first")

    server = subprocess.Popen(
        [PYTHON, "-m", "concierge", "--config", "config/gateway.example.yaml"],
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        started = False
        for _ in range(120):
            if server.poll() is not None:
                break
            if _port_open():
                started = True
                break
            time.sleep(0.1)
        if not started:
            output = server.stdout.read() if server.stdout is not None else ""
            raise SystemExit("server did not start\n" + "\n".join(output.splitlines()[-80:]))

        _run("examples/session_flow.py", [PYTHON, "examples/session_flow.py"], timeout=45)
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def main() -> None:
    _run("targeted P0 pytest suite", [PYTHON, "-m", "pytest", "-q", *TARGETED_PYTEST])
    print("\n== focused resource-safety soak ==")
    asyncio.run(_resource_safety_soak())
    _run_real_server_flow()
    print("\nE2E verification passed")


if __name__ == "__main__":
    main()
