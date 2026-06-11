#!/usr/bin/env python3
"""CI live-smoke: boot the REAL server (lean `--ui none` tier) against a fake
OpenAI endpoint and drive a real A2A turn end-to-end.

This catches the green-but-wire-broken class that unit/mock tests miss — CRLF SSE
framing, A2A routing + version negotiation, the agent-card build, and lean-image
import gaps — by exercising the actual transport, not a mock. The fake model
(scripts/fake_openai_server.py) returns a canned completion so the turn reaches a
terminal state without a real gateway.

`--bin <path>` smokes a PyInstaller-frozen sidecar (the desktop build) instead of
`python -m server` — same wire checks, but against the actual frozen binary, so
per-platform under-collection fails CI instead of the first desktop user.

Exit 0 on success, non-zero (with a diagnostic) on any failure.
"""

from __future__ import annotations

import argparse
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

# Windows consoles/pipes default to cp1252, which can't encode "✓" — the smoke
# then dies at its own success banner. Force UTF-8 (lossy on truly odd terminals).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="protoAgent live-smoke")
    ap.add_argument(
        "--bin",
        dest="bin_path",
        default=None,
        help="smoke a frozen server binary (the desktop sidecar) instead of `python -m server`",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="seconds to wait for /healthz (frozen onefile binaries self-extract on first boot)",
    )
    return ap.parse_args()


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
    args = _parse_args()
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

    if args.bin_path:
        # Frozen sidecar: run from a neutral cwd with no PYTHONPATH so the repo
        # checkout can't paper over PyInstaller under-collection — the desktop
        # app won't have the repo on disk either.
        env.pop("PYTHONPATH", None)
        agent_cmd = [str(Path(args.bin_path).resolve()), "--ui", "none", "--port", str(agent_port)]
        agent_cwd = str(cfg_dir)
    else:
        agent_cmd = [sys.executable, "-m", "server", "--ui", "none", "--port", str(agent_port)]
        agent_cwd = str(ROOT)

    fake = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fake_openai_server.py"), str(fake_port)])
    agent = subprocess.Popen(agent_cmd, cwd=agent_cwd, env=env)
    try:
        if not _wait_healthz(agent_port, timeout=args.timeout):
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
