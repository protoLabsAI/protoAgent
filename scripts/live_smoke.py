#!/usr/bin/env python3
"""CI live-smoke: boot the REAL server (lean `--ui none` tier) against a fake
OpenAI endpoint and drive a real A2A turn end-to-end.

This catches the green-but-wire-broken class that unit/mock tests miss — CRLF SSE
framing, A2A routing + version negotiation, the agent-card build, and lean-image
import gaps — by exercising the actual transport, not a mock. The fake model
(scripts/fake_openai_server.py) returns a canned completion so the turn reaches a
terminal state without a real gateway.

Exit 0 on success, non-zero (with a diagnostic) on any failure.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthz(port: int, timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    fake_port, agent_port = _free_port(), _free_port()
    cfg_dir = Path(tempfile.mkdtemp(prefix="smoke-cfg-"))
    (cfg_dir / "langgraph-config.yaml").write_text(
        "model:\n"
        "  name: protolabs/reasoning\n"
        f"  api_base: http://127.0.0.1:{fake_port}/v1\n"
        "middleware:\n  knowledge: false\n  scheduler: false\n"
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "fake-smoke-key",
        "PROTOAGENT_CONFIG_DIR": str(cfg_dir),
        "PROTOAGENT_INSTANCE": "cismoke",
        "PROTOAGENT_HEADLESS_SETUP": "1",
        "PYTHONPATH": str(ROOT),
    }

    fake = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fake_openai_server.py"), str(fake_port)])
    agent = subprocess.Popen(
        [sys.executable, "-m", "server", "--ui", "none", "--port", str(agent_port)],
        cwd=str(ROOT), env=env,
    )
    try:
        if not _wait_healthz(agent_port):
            print("FAIL: /healthz never returned 200 (server did not become ready)")
            return 1
        print("ok: /healthz 200 (lean server booted + graph compiled)")

        # Agent card serves + has identity.
        with urllib.request.urlopen(f"http://127.0.0.1:{agent_port}/.well-known/agent-card.json", timeout=5) as r:
            card = json.loads(r.read())
        assert card.get("name"), "agent card has no name"
        assert card.get("skills"), "agent card has no skills"
        print(f"ok: agent card serves (name={card['name']}, skills={[s.get('id') for s in card['skills']]})")

        # Real A2A streaming turn over the actual transport.
        body = json.dumps({
            "jsonrpc": "2.0", "id": "smoke", "method": "SendStreamingMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "ping"}],
                                   "messageId": "m1", "contextId": "smoke"}},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{agent_port}/a2a", data=body,
            headers={"A2A-Version": "1.0", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")

        assert "data:" in raw, f"no SSE data frames in response: {raw[:300]!r}"
        terminal = ("COMPLETED" in raw or "live smoke ok" in raw or '"artifact' in raw.lower())
        assert terminal, f"no terminal/answer frame; first 600 chars: {raw[:600]!r}"
        print("ok: A2A SendStreamingMessage turn decoded + reached a terminal frame")
        print("\nLIVE SMOKE PASSED ✓")
        return 0
    except Exception as e:  # noqa: BLE001 — smoke must report, not traceback-crash
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1
    finally:
        for p in (agent, fake):
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
