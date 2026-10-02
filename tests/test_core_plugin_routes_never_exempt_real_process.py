"""Core plugin-lifecycle routes can't be made public by a plugin — against a REAL server.

A plugin's manifest ``public_paths`` / ``federation_paths`` may exempt (or lower the
operator ceiling on) its OWN namespace, ``/plugins/<id>/…`` and ``/api/plugins/<id>/…``.
Core mounts its per-plugin operator routes INSIDE that second namespace —
``POST /api/plugins/<id>/update`` and ``POST /api/plugins/<id>/enabled`` — and the
bundle routes under ``/api/plugins/bundles/…``. So a manifest declaring
``public_paths: [/api/plugins/<id>/]`` (or a plugin whose id is ``bundles``) used to
make those core routes callable with NO credential, and ``federation_paths`` handed them
to a federation-token holder.

This boots ``python -m server`` with an operator bearer AND a federation token set,
installs such plugins with the bearer, then calls every core lifecycle route without a
credential and with the federation token: each must be refused (401 / 403), while the
plugin's own route under the same exempted prefix stays public.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform_sensitive  # spawns the server + git processes

ROOT = Path(__file__).resolve().parent.parent
BEARER = "operator-secret-token"
FEDERATION = "federation-secret-token"

_PLUGIN = """\
from fastapi import APIRouter


def register(registry):
    r = APIRouter()

    @r.get("/hook")
    async def hook():
        return {{"plugin": "{pid}"}}

    registry.register_router(r, prefix="/api/plugins/{pid}")
"""

_MANIFEST = """\
id: {pid}
name: {pid}
version: 0.1.0
description: Tries to exempt core lifecycle routes.
public_paths: ["/api/plugins/{pid}/"]
federation_paths: ["/api/plugins/{pid}/"]
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=p@example.com", "-c", "user.name=p", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_repo(base: Path, pid: str) -> Path:
    repo = base / pid
    repo.mkdir()
    (repo / "protoagent.plugin.yaml").write_text(_MANIFEST.format(pid=pid), encoding="utf-8")
    (repo / "__init__.py").write_text(_PLUGIN.format(pid=pid), encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    return repo


def _call(base: str, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode("utf-8", "replace")}
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, {"error": str(e)}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    base = tmp_path_factory.mktemp("authcore")
    home, box, repos = base / "home", base / "box", base / "repos"
    for d in (home / "config", box, repos):
        d.mkdir(parents=True)
    fake_port, port = _free_port(), _free_port()
    (home / "config" / "langgraph-config.yaml").write_text(
        "model:\n"
        "  name: protolabs/reasoning\n"
        f"  api_base: http://127.0.0.1:{fake_port}/v1\n"
        "middleware:\n  knowledge: false\n  scheduler: false\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "fake-key",
        "PROTOAGENT_HOME": str(home),
        "PROTOAGENT_BOX_ROOT": str(box),
        "PROTOAGENT_INSTANCE": "authcoretest",
        "PROTOAGENT_HEADLESS_SETUP": "1",
        "PROTOAGENT_DISCOVERY_DISABLE": "1",
        "A2A_AUTH_TOKEN": BEARER,
        "A2A_FEDERATION_TOKEN": FEDERATION,
        "PYTHONPATH": str(ROOT),
    }
    for k in ("PROTOAGENT_CONFIG_DIR", "PROTOAGENT_PLUGINS_DIR"):
        env.pop(k, None)
    log = open(base / "server.log", "w", encoding="utf-8")  # noqa: SIM115 — closed in teardown
    fake = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fake_openai_server.py"), str(fake_port)])
    agent = subprocess.Popen(
        [sys.executable, "-m", "server", "--ui", "none", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        end = time.time() + 120
        while time.time() < end and agent.poll() is None:
            if _call(url, "GET", "/healthz")[0] == 200:
                break
            time.sleep(0.2)
        assert agent.poll() is None and _call(url, "GET", "/healthz")[0] == 200, (base / "server.log").read_text(
            encoding="utf-8"
        )[-4000:]
        yield url, repos
    finally:
        for p in (agent, fake):
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        log.close()


def _install(url: str, repo: Path) -> dict:
    src = repo.as_uri()
    status, body = _call(url, "POST", "/api/plugins/install", {"url": src}, token=BEARER)
    if body.get("needs_ack"):
        assert _call(url, "POST", "/api/plugins/ack", {"url": src}, token=BEARER)[0] == 200
        status, body = _call(url, "POST", "/api/plugins/install", {"url": src}, token=BEARER)
    assert status == 200 and not body.get("load_errors"), body
    return body


def test_a_plugin_cannot_exempt_its_own_core_lifecycle_routes(server):
    url, repos = server
    pid = "probepub"
    _install(url, _make_repo(repos, pid))

    # The plugin's OWN route under the exempted prefix is public, as designed.
    assert _call(url, "GET", f"/api/plugins/{pid}/hook") == (200, {"plugin": pid})

    core = [
        ("POST", f"/api/plugins/{pid}/update", None),
        ("POST", f"/api/plugins/{pid}/enabled", {"enabled": False}),
        ("POST", f"/api/plugins/{pid}/enabled/", {"enabled": False}),  # trailing-slash variant
        ("DELETE", f"/api/plugins/{pid}", None),
        ("POST", "/api/plugins/install-deps", {"id": pid}),
    ]
    for method, path, body in core:
        status, resp = _call(url, method, path, body)
        assert status == 401, (method, path, status, resp)
        status, resp = _call(url, method, path, body, token=FEDERATION)
        assert status == 403, (method, path, status, resp)

    # Still enabled + installed: nothing above ran.
    status, body = _call(url, "GET", "/api/plugins/installed", token=BEARER)
    row = next(p for p in body["plugins"] if p["id"] == pid)
    assert row["enabled"] is True, row


def test_a_plugin_named_bundles_cannot_exempt_the_core_bundle_routes(server):
    url, repos = server
    pid = "bundles"
    # The id that would shadow /api/plugins/bundles/… is refused outright, so its
    # public_paths never reach the auth gate.
    src = _make_repo(repos, pid).as_uri()
    _call(url, "POST", "/api/plugins/ack", {"url": src}, token=BEARER)
    status, body = _call(url, "POST", "/api/plugins/install", {"url": src}, token=BEARER)
    assert status != 200 or body.get("load_errors") or pid not in (body.get("enabled") or []), body

    for method, path in (("POST", "/api/plugins/bundles/x/update"), ("DELETE", "/api/plugins/bundles/x")):
        assert _call(url, method, path)[0] == 401, path
        assert _call(url, method, path, token=FEDERATION)[0] == 403, path
