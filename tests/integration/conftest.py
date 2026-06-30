"""Real-subprocess multi-instance fleet harness.

Boots actual ``python -m server`` processes (a hub, and members the hub spawns)
against a fake OpenAI gateway, on temp roots + free ports, and tears everything
down. This is the integration layer the fleet had no coverage for — every prior
fleet test mocked ``subprocess`` or ran single-instance.

Isolation: each server gets its own tmp ``HOME`` so the box tier (host-config,
commons, heartbeats) lands under ``<tmp>/.protoagent`` instead of the real one, and
``PROTOAGENT_HOME`` puts its config AND every per-instance data store under
``<tmp-home>`` (the new instance_root layout). Members the hub spawns inherit that
tmp ``HOME`` and get their own ``PROTOAGENT_HOME=<ws>``, so the whole fleet stays in tmp.

These tests are SLOW (real boots) and opt-in: set ``PA_RUN_INTEGRATION=1`` to run
them; the default ``pytest tests/`` skips them.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

RUN_INTEGRATION = bool(os.environ.get("PA_RUN_INTEGRATION"))
requires_integration = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="real-subprocess fleet integration — set PA_RUN_INTEGRATION=1 to run",
)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_healthz(port: int, timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def http_get(url: str, timeout: float = 10.0, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def http_post(url: str, body: dict, timeout: float = 30.0, headers: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def poll(fn, *, timeout: float = 60.0, interval: float = 1.0):
    """Call ``fn`` until it returns a truthy value or the timeout elapses; returns
    the last value (falsy on timeout)."""
    end = time.time() + timeout
    val = None
    while time.time() < end:
        try:
            val = fn()
            if val:
                return val
        except Exception:
            val = None
        time.sleep(interval)
    return val


@dataclass
class Server:
    name: str
    port: int
    home: Path
    data_root: Path
    proc: subprocess.Popen

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="module")
def fake_gateway():
    """A fake OpenAI-compatible endpoint (canned completions) so booted agents can
    take a real turn without a live gateway. Referenced by every booted config."""
    port = free_port()
    proc = subprocess.Popen([PY, str(ROOT / "scripts" / "fake_openai_server.py"), str(port)])
    # wait for it to bind
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


@pytest.fixture
def fleet(tmp_path_factory, fake_gateway):
    """Factory that boots isolated real servers and tears them (and any members
    they spawned) down. Returns ``boot(name=..., instance=..., ui=...)`` → ``Server``."""
    servers: list[Server] = []

    def boot(*, name: str = "hub", instance: str | None = None, ui: str = "none", timeout: float = 120.0) -> Server:
        data_root = tmp_path_factory.mktemp(f"{name}-data")
        home = tmp_path_factory.mktemp(f"{name}-home")
        (home / "config").mkdir(parents=True, exist_ok=True)
        (home / "config" / "langgraph-config.yaml").write_text(
            "model:\n"
            "  name: protolabs/reasoning\n"
            f"  api_base: http://127.0.0.1:{fake_gateway}/v1\n"
            "middleware:\n  knowledge: false\n  scheduler: false\n"
        )
        port = free_port()
        env = {
            **os.environ,
            "HOME": str(data_root),  # isolate data_home() → <data_root>/.protoagent (box tier: host/commons/heartbeats)
            "PROTOAGENT_HOME": str(home),  # instance_root → config + every per-instance store under <home>
            "PROTOAGENT_HEADLESS_SETUP": "1",
            "OPENAI_API_KEY": "fake-integration-key",
            "PYTHONPATH": str(ROOT),
        }
        env.pop("PROTOAGENT_BOX_ROOT", None)
        env.pop("PROTOAGENT_CONFIG_DIR", None)
        env.pop("PROTOAGENT_INSTANCE", None)
        if instance:
            env["PROTOAGENT_INSTANCE"] = instance
        proc = subprocess.Popen([PY, "-m", "server", "--ui", ui, "--port", str(port)], cwd=str(ROOT), env=env)
        s = Server(name=name, port=port, home=home, data_root=data_root, proc=proc)
        servers.append(s)
        if not wait_healthz(port, timeout):
            proc.terminate()
            raise RuntimeError(f"server {name!r} on :{port} never became healthy (timeout {timeout}s)")
        return s

    yield boot

    # Teardown: collect member pids from each hub's fleet status, then stop hubs + members.
    member_pids: set[int] = set()
    for s in servers:
        try:
            st, raw = http_get(f"{s.base}/api/fleet", timeout=5)
            if st == 200:
                for a in json.loads(raw).get("agents", []) or []:
                    if a.get("pid") and not a.get("remote"):
                        member_pids.add(int(a["pid"]))
        except Exception:
            pass
    for s in servers:
        s.proc.terminate()
    for s in servers:
        try:
            s.proc.wait(timeout=10)
        except Exception:
            s.proc.kill()
    for pid in member_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
