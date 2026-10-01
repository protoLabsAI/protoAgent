"""Plugin lifecycle against a REAL server process — what actually needs a restart.

Boots ``python -m server`` (lean ``--ui none`` tier, the live-smoke recipe) with an isolated
instance root, then drives the operator API exactly as the console does: install a plugin
from a local git repo, update it to new code, force re-install it, disable / enable it, and
uninstall it. The plugin contributes a console view, a router and a background surface, and
each generation stamps its version into the router's responses and the surface's start/stop
log. So every step asserts what is LIVE, not what a mock was told:

- the router serves the NEW generation's content (or 404s once removed);
- the previous generation's surface task really stopped, and the new one started;
- ``restart_recommended`` matches: false whenever the above held.

A second plugin's surface ignores its stop and swallows cancellation, so a reload can't
end it. That is the one case a restart is still needed for, and the response must say so.
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

_MANIFEST = """\
id: {pid}
name: {pid}
version: 0.1.0
description: Real-process restart probe.
views:
  - {{ id: {pid}, label: "Probe", icon: "Box", path: "/plugins/{pid}/view" }}
"""

# A well-behaved surface: stop() sets an event, the task ends and logs it.
_GOOD = """\
import asyncio, os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

V = "{version}"
_LOG = os.environ["PROBE_SURFACE_LOG"]
_evt = None


def _log(msg):
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write("{pid} " + msg + "\\n")


async def _run(evt):
    _log("start " + V)
    try:
        while not evt.is_set():
            await asyncio.sleep(0.05)
    finally:
        _log("stop " + V)


async def _start():
    global _evt
    _evt = asyncio.Event()
    return asyncio.ensure_future(_run(_evt))


def _stop():
    if _evt is not None:
        _evt.set()


def register(registry):
    r = APIRouter()

    @r.get("/view")
    async def view():
        return HTMLResponse("<p>view " + V + "</p>")

    @r.get("/version")
    async def version():
        return {{"v": V}}

    registry.register_router(r)
    registry.register_surface(_start, stop=_stop, name="{pid}-surface")
"""

# The same surface, but it opts into reconfiguring in place with a ``reload(cfg)`` hook, so
# a reload keeps the RUNNING instance (and its code) instead of replacing it.
_RELOADING = _GOOD.replace(
    'registry.register_surface(_start, stop=_stop, name="{pid}-surface")',
    'registry.register_surface(_start, stop=_stop, name="{pid}-surface", reload=lambda cfg: _log("reload " + V))',
)

# A surface that won't end: stop() is a no-op and the task swallows every cancel.
_STUCK = """\
import asyncio, os
from fastapi import APIRouter

V = "{version}"
_LOG = os.environ["PROBE_SURFACE_LOG"]


def _log(msg):
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write("{pid} " + msg + "\\n")


async def _run():
    _log("start " + V)
    while True:
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            continue


async def _start():
    return asyncio.ensure_future(_run())


def _stop():
    pass


def register(registry):
    r = APIRouter()

    @r.get("/version")
    async def version():
        return {{"v": V}}

    registry.register_router(r)
    registry.register_surface(_start, stop=_stop, name="{pid}-surface")
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=probe@example.com", "-c", "user.name=probe", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write_generation(repo: Path, pid: str, version: str, template: str) -> None:
    (repo / "protoagent.plugin.yaml").write_text(_MANIFEST.format(pid=pid), encoding="utf-8")
    (repo / "__init__.py").write_text(template.format(pid=pid, version=version), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", version)


def _make_repo(base: Path, pid: str, template: str) -> Path:
    repo = base / pid
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write_generation(repo, pid, "v1", template)
    return repo


class _Server:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 120.0) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                try:
                    return r.status, (json.loads(raw) if raw else {})
                except ValueError:  # the HTML view
                    return r.status, {"raw": raw.decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw.decode("utf-8", "replace")}
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:  # not up yet / reset
            return 0, {"error": str(e)}

    def install(self, url: str, *, force: bool = False) -> dict:
        status, body = self.call("POST", "/api/plugins/install", {"url": url, "force": force})
        if body.get("needs_ack"):
            assert self.call("POST", "/api/plugins/ack", {"url": url})[0] == 200
            status, body = self.call("POST", "/api/plugins/install", {"url": url, "force": force})
        assert status == 200, body
        assert not body.get("enable_error") and not body.get("load_errors"), body
        return body


def _wait_for(pred, what: str, timeout: float = 30.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = pred()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what} (last={last!r})")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    base = tmp_path_factory.mktemp("lifecycle")
    home, box, repos = base / "home", base / "box", base / "repos"
    for d in (home / "config", box, repos):
        d.mkdir(parents=True)
    surface_log = base / "surfaces.log"
    surface_log.write_text("", encoding="utf-8")
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
        "PROTOAGENT_INSTANCE": "lifecycletest",
        "PROTOAGENT_HEADLESS_SETUP": "1",
        "PROTOAGENT_DISCOVERY_DISABLE": "1",
        "PROBE_SURFACE_LOG": str(surface_log),
        "PYTHONPATH": str(ROOT),
    }
    for k in ("PROTOAGENT_CONFIG_DIR", "PROTOAGENT_PLUGINS_DIR", "A2A_AUTH_TOKEN"):
        env.pop(k, None)
    server_log = open(base / "server.log", "w", encoding="utf-8")  # noqa: SIM115 — closed in teardown
    fake = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fake_openai_server.py"), str(fake_port)])
    agent = subprocess.Popen(
        [sys.executable, "-m", "server", "--ui", "none", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    srv = _Server(port)
    try:
        _wait_for(
            lambda: srv.call("GET", "/healthz", timeout=2)[0] == 200 if agent.poll() is None else "exited",
            "/healthz",
            timeout=120,
        )
        assert agent.poll() is None, (base / "server.log").read_text(encoding="utf-8")[-4000:]
        yield srv, repos, surface_log
    finally:
        for p in (agent, fake):
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        server_log.close()
        if os.environ.get("PROBE_KEEP_LOGS"):
            print((base / "server.log").read_text(encoding="utf-8")[-6000:])


def _lines(log: Path, pid: str) -> list[str]:
    return [ln[len(pid) + 1 :] for ln in log.read_text(encoding="utf-8").splitlines() if ln.startswith(pid + " ")]


def _serves(srv: _Server, pid: str, version: str):
    return lambda: srv.call("GET", f"/plugins/{pid}/version", timeout=5) == (200, {"v": version})


def test_install_update_reinstall_disable_enable_uninstall_are_live(live):
    srv, repos, log = live
    pid = "probegood"
    repo = _make_repo(repos, pid, _GOOD)
    url = repo.as_uri()

    # Fresh install: router + view serve, the surface started — no restart.
    body = srv.install(url)
    assert body["restart_recommended"] is False
    _wait_for(_serves(srv, pid, "v1"), "v1 route")
    assert srv.call("GET", f"/plugins/{pid}/view")[0] == 200
    _wait_for(lambda: "start v1" in _lines(log, pid), "v1 surface start")

    # Update to new code: the route serves v2, the v1 surface task ended, v2's started.
    _write_generation(repo, pid, "v2", _GOOD)
    status, body = srv.call("POST", f"/api/plugins/{pid}/update")
    assert status == 200, body
    _wait_for(_serves(srv, pid, "v2"), "v2 route after update")
    _wait_for(lambda: _lines(log, pid)[-2:] == ["stop v1", "start v2"], "v1 stop then v2 start")
    assert body["restart_recommended"] is False, body

    # Force re-install over the live plugin: same story for v3.
    _write_generation(repo, pid, "v3", _GOOD)
    body = srv.install(url, force=True)
    _wait_for(_serves(srv, pid, "v3"), "v3 route after force re-install")
    _wait_for(lambda: _lines(log, pid)[-2:] == ["stop v2", "start v3"], "v2 stop then v3 start")
    assert body["restart_recommended"] is False, body

    # Disable: routes gone, surface stopped. Enable: back, surface restarted.
    status, body = srv.call("POST", f"/api/plugins/{pid}/enabled", {"enabled": False})
    assert status == 200 and body["restart_recommended"] is False, body
    _wait_for(lambda: srv.call("GET", f"/plugins/{pid}/version", timeout=5)[0] == 404, "404 after disable")
    _wait_for(lambda: _lines(log, pid)[-1:] == ["stop v3"], "surface stop on disable")
    status, body = srv.call("POST", f"/api/plugins/{pid}/enabled", {"enabled": True})
    assert status == 200 and body["restart_recommended"] is False, body
    _wait_for(_serves(srv, pid, "v3"), "route after re-enable")
    _wait_for(lambda: _lines(log, pid)[-1:] == ["start v3"], "surface start on enable")

    # Uninstall: routes gone, surface stopped.
    status, body = srv.call("DELETE", f"/api/plugins/{pid}")
    assert status == 200, body
    _wait_for(lambda: srv.call("GET", f"/plugins/{pid}/version", timeout=5)[0] == 404, "404 after uninstall")
    assert srv.call("GET", f"/plugins/{pid}/view", timeout=5)[0] == 404
    _wait_for(lambda: _lines(log, pid)[-1:] == ["stop v3"], "surface stop on uninstall")
    assert body["restart_recommended"] is False, body
    # Every generation that started also stopped, exactly once.
    assert _lines(log, pid) == [
        "start v1",
        "stop v1",
        "start v2",
        "stop v2",
        "start v3",
        "stop v3",
        "start v3",
        "stop v3",
    ]


def test_a_surface_that_wont_stop_still_recommends_a_restart(live):
    srv, repos, log = live
    pid = "probestuck"
    repo = _make_repo(repos, pid, _STUCK)
    url = repo.as_uri()

    body = srv.install(url)
    assert body["restart_recommended"] is False
    _wait_for(lambda: "start v1" in _lines(log, pid), "stuck v1 surface start")

    # The update's router half is live; the surface can't be replaced (v1 never ends),
    # so v2's surface is NOT started and the response must ask for a restart.
    _write_generation(repo, pid, "v2", _STUCK)
    status, body = srv.call("POST", f"/api/plugins/{pid}/update")
    assert status == 200, body
    _wait_for(_serves(srv, pid, "v2"), "v2 route after update")
    assert "start v2" not in _lines(log, pid)
    assert body["restart_recommended"] is True, body

    # Uninstall: the routes leave, but the surface task can't be ended → restart.
    status, body = srv.call("DELETE", f"/api/plugins/{pid}")
    assert status == 200, body
    _wait_for(lambda: srv.call("GET", f"/plugins/{pid}/version", timeout=5)[0] == 404, "404 after uninstall")
    assert body["restart_recommended"] is True, body


def test_a_surface_kept_on_its_reload_hook_across_an_update_recommends_a_restart(live):
    srv, repos, log = live
    pid = "probereload"
    repo = _make_repo(repos, pid, _RELOADING)

    body = srv.install(repo.as_uri())
    assert body["restart_recommended"] is False
    _wait_for(lambda: "start v1" in _lines(log, pid), "v1 surface start")

    # The router serves v2, but the reconcile keeps the running v1 surface and only calls
    # its (old) reload hook — v2's surface code isn't running, so a restart is needed.
    _write_generation(repo, pid, "v2", _RELOADING)
    status, body = srv.call("POST", f"/api/plugins/{pid}/update")
    assert status == 200, body
    _wait_for(_serves(srv, pid, "v2"), "v2 route after update")
    lines = _lines(log, pid)
    assert "stop v1" not in lines and "start v2" not in lines and "reload v1" in lines, lines
    assert body["restart_recommended"] is True, body

    # Uninstall still ends it cleanly — nothing left to restart for.
    status, body = srv.call("DELETE", f"/api/plugins/{pid}")
    assert status == 200, body
    _wait_for(lambda: _lines(log, pid)[-1:] == ["stop v1"], "surface stop on uninstall")
    assert body["restart_recommended"] is False, body


def test_a_stuck_surface_on_a_disabled_plugin_still_recommends_a_restart(live):
    # Disable can't end the surface, so the plugin is out of plugins.enabled with nothing
    # loaded or mounted — yet its task is still running. Update and uninstall run no
    # reload for a plugin that isn't enabled, so the stuck record is the only evidence
    # it's live; both must still ask for a restart.
    srv, repos, log = live
    pid = "probestuckoff"
    repo = _make_repo(repos, pid, _STUCK)

    body = srv.install(repo.as_uri())
    assert body["restart_recommended"] is False
    _wait_for(lambda: "start v1" in _lines(log, pid), "stuck v1 surface start")

    status, body = srv.call("POST", f"/api/plugins/{pid}/enabled", {"enabled": False})
    assert status == 200 and body["restart_recommended"] is True, body
    _wait_for(lambda: srv.call("GET", f"/plugins/{pid}/version", timeout=5)[0] == 404, "404 after disable")

    _write_generation(repo, pid, "v2", _STUCK)
    status, body = srv.call("POST", f"/api/plugins/{pid}/update")
    assert status == 200, body
    assert body["reloaded"] is False, body  # not enabled → no reload
    assert body["restart_recommended"] is True, body

    status, body = srv.call("DELETE", f"/api/plugins/{pid}")
    assert status == 200, body
    assert body["reloaded"] is False, body
    assert body["restart_recommended"] is True, body


def test_updating_or_uninstalling_a_cleanly_disabled_plugin_needs_no_restart(live):
    # The control for the test above: disabled the normal way, nothing of it runs (the
    # runtime roster still LISTS it, unloaded), so its update and uninstall are clean.
    srv, repos, log = live
    pid = "probeoff"
    repo = _make_repo(repos, pid, _GOOD)

    srv.install(repo.as_uri())
    _wait_for(lambda: "start v1" in _lines(log, pid), "v1 surface start")
    status, body = srv.call("POST", f"/api/plugins/{pid}/enabled", {"enabled": False})
    assert status == 200 and body["restart_recommended"] is False, body
    _wait_for(lambda: _lines(log, pid)[-1:] == ["stop v1"], "surface stop on disable")

    _write_generation(repo, pid, "v2", _GOOD)
    status, body = srv.call("POST", f"/api/plugins/{pid}/update")
    assert status == 200 and body["reloaded"] is False, body
    assert body["restart_recommended"] is False, body
    assert "start v2" not in _lines(log, pid)  # a disabled plugin's update runs nothing

    status, body = srv.call("DELETE", f"/api/plugins/{pid}")
    assert status == 200 and body["reloaded"] is False, body
    assert body["restart_recommended"] is False, body
