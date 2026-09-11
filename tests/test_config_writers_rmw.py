"""The rest of the live-config writers don't lose each other's changes either (#2743 follow-up).

#3431 fixed the read-modify-write race for `plugins.enabled`: build the new value from the
committed config INSIDE `_CONFIG_WRITE_LOCK`, not from a copy read before it. Three more
writers had the same bug:

* the MCP routes (add / import / remove / promote / forget) built `mcp.servers` from a
  stale copy — and called the applier INLINE in an async handler, so adding an MCP
  server froze the whole server for the length of the reload;
* the provider routes (add / patch / delete) built the connection registry from a stale
  copy;
* the delegates store read-modified-wrote the live config without the lock at all, and
  its API routes called it on the event loop.

These drive the REAL `_apply_settings_changes` against tmp config files, the way the
plugin-state race tests do: the reload is patched only to commit STATE from the file and
to park inside the lock on its first call, pinning a second writer in the exact window.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

import graph.config_io as cio
import runtime.state as rs
from graph.config import LangGraphConfig

LEAF = """\
mcp:
  enabled: true
  servers:
    - {name: base, transport: stdio, command: base}
providers:
  - {id: gw, type: openai-compat, base_url: "https://gw.example/v1"}
  - {id: old, type: openai-compat, base_url: "https://old.example/v1"}
"""


@pytest.fixture
def live_config(monkeypatch, tmp_path: Path):
    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text(LEAF, encoding="utf-8")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    import infra.paths as paths

    monkeypatch.setattr(paths, "host_config_path", lambda: tmp_path / "host-config.yaml", raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig.from_yaml(str(leaf)), raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    return leaf


@pytest.fixture
def held_first_reload(monkeypatch, live_config):
    """The real applier; a reload that commits the file to STATE and parks inside the
    lock on its first call; and a sync point on the SECOND writer calling the applier —
    by which time anything it read before the lock has been read."""
    import server.agent_init as ai

    inside, release, second_calling = threading.Event(), threading.Event(), threading.Event()
    calls = {"reload": 0, "apply": 0}

    def _reload(*_a, **_k):
        calls["reload"] += 1
        if calls["reload"] == 1:
            inside.set()
            assert release.wait(10), "test never released the first writer"
        rs.STATE.graph_config = LangGraphConfig.from_yaml(str(live_config))
        return True, "reloaded"

    real_apply = ai._apply_settings_changes

    def _apply_spy(*a, **k):
        calls["apply"] += 1
        if calls["apply"] == 2:
            second_calling.set()
        return real_apply(*a, **k)

    monkeypatch.setattr(ai, "_reload_langgraph_agent", _reload)
    monkeypatch.setattr(ai, "_apply_settings_changes", _apply_spy)
    # The provider routes reach the applier through the host seam, as in the server.
    monkeypatch.setattr("graph.plugins.host.HOST.apply_settings", lambda patch: ai._apply_settings_changes(config=patch))
    return inside, release, second_calling


def _on_disk(leaf: Path) -> dict:
    return yaml.safe_load(leaf.read_text(encoding="utf-8")) or {}


async def _wait_for(event: threading.Event, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set():
        assert time.monotonic() < deadline, "a writer never reached its sync point"
        await asyncio.sleep(0.01)


def _app():
    from operator_api.mcp_routes import register_mcp_routes
    from operator_api.provider_routes import register_provider_routes

    app = FastAPI()
    register_mcp_routes(app)
    register_provider_routes(app)
    return app


async def _race(first, second, held):
    inside, release, second_calling = held
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as client:
        a = asyncio.create_task(first(client))
        await _wait_for(inside)
        b = asyncio.create_task(second(client))
        await _wait_for(second_calling)
        release.set()
        return await a, await b


# ── the lost updates ────────────────────────────────────────────────────────────────


async def test_two_concurrent_mcp_edits_both_survive(live_config, held_first_reload):
    r1, r2 = await _race(
        lambda c: c.post("/api/mcp/servers", json={"name": "x", "transport": "stdio", "command": "x"}),
        lambda c: c.post("/api/mcp/servers", json={"name": "y", "transport": "stdio", "command": "y"}),
        held_first_reload,
    )
    assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
    names = sorted(s["name"] for s in _on_disk(live_config)["mcp"]["servers"])
    assert names == ["base", "x", "y"], "one MCP server edit was lost"


async def test_an_mcp_remove_does_not_undo_a_concurrent_add(live_config, held_first_reload):
    await _race(
        lambda c: c.post("/api/mcp/servers", json={"name": "x", "transport": "stdio", "command": "x"}),
        lambda c: c.delete("/api/mcp/servers/base"),
        held_first_reload,
    )
    assert sorted(s["name"] for s in _on_disk(live_config)["mcp"]["servers"]) == ["x"]


async def test_a_connection_delete_does_not_undo_a_concurrent_add(live_config, held_first_reload):
    r1, r2 = await _race(
        lambda c: c.post(
            "/api/config/providers", json={"id": "new", "type": "openai-compat", "base_url": "https://new.example/v1"}
        ),
        lambda c: c.delete("/api/config/providers/old"),
        held_first_reload,
    )
    assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
    ids = sorted(p["id"] for p in _on_disk(live_config)["providers"])
    assert ids == ["gw", "new"], "the delete wrote a stale registry over the concurrent add"


async def test_a_duplicate_connection_added_in_two_tabs_is_refused_not_doubled(live_config, held_first_reload):
    body = {"id": "dup", "type": "openai-compat", "base_url": "https://dup.example/v1"}
    r1, r2 = await _race(
        lambda c: c.post("/api/config/providers", json=body),
        lambda c: c.post("/api/config/providers", json=body),
        held_first_reload,
    )
    assert r1.status_code == 200 and r2.status_code == 400, (r1.text, r2.text)
    assert [p["id"] for p in _on_disk(live_config)["providers"]].count("dup") == 1


def test_a_delegate_save_waits_for_an_in_flight_config_write(live_config):
    """The delegates store rewrites the live config itself. Interleaved with the applier's
    load→save, one side's change — a delegate, or anything else in the file — vanishes."""
    from graph.config_io import CONFIG_WRITE_LOCK
    from plugins.delegates import store

    done = threading.Event()

    def _save():
        store.upsert_delegate({"name": "coder", "type": "acp", "command": "echo"})
        done.set()

    with CONFIG_WRITE_LOCK:  # an applier between its read and its write
        t = threading.Thread(target=_save)
        t.start()
        assert not done.wait(0.3), "the delegate save rewrote the config while another write held the lock"
    t.join(5)
    assert done.is_set()
    assert [d["name"] for d in _on_disk(live_config)["delegates"]] == ["coder"]


# ── nothing here blocks the event loop ───────────────────────────────────────────────


async def _max_loop_stall(action) -> float:
    """Run `action` while a heartbeat ticks on the loop; return the longest gap."""
    stop = asyncio.Event()
    gaps: list[float] = []

    async def _beat():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(_beat())
    await asyncio.sleep(0.05)
    try:
        await action()
    finally:
        stop.set()
        await beat
    return max(gaps)


async def test_adding_an_mcp_server_does_not_freeze_the_server_during_the_reload(monkeypatch, live_config):
    # The route used to call the applier INLINE: every other request waited out the reload.
    import server.agent_init as ai

    def _slow_reload(*_a, **_k):
        time.sleep(0.6)  # a real rebuild takes seconds
        rs.STATE.graph_config = LangGraphConfig.from_yaml(str(live_config))
        return True, "reloaded"

    monkeypatch.setattr(ai, "_reload_langgraph_agent", _slow_reload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t") as client:

        async def _add():
            r = await client.post("/api/mcp/servers", json={"name": "x", "transport": "stdio", "command": "x"})
            assert r.status_code == 200, r.text

        stall = await _max_loop_stall(_add)
    assert stall < 0.3, f"the event loop stalled {stall:.2f}s during the reload"


async def test_saving_a_delegate_does_not_freeze_the_server(monkeypatch, live_config, tmp_path):
    # The store now waits on the config lock, which a reload can hold for seconds — so
    # the route must wait on it from a worker thread, not the event loop.
    import plugins.delegates.api as dapi
    from plugins.delegates import store

    async def _noreload():
        return True, "reloaded"

    def _slow_upsert(entry):
        time.sleep(0.6)  # waiting on a lock a reload holds
        return []

    monkeypatch.setattr(dapi, "_reload", _noreload)
    monkeypatch.setattr(store, "upsert_delegate", _slow_upsert)
    app = FastAPI()
    app.include_router(dapi.build_router())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:

        async def _create():
            r = await client.post(
                "/api/delegates", json={"name": "t", "type": "acp", "command": "echo", "workdir": str(tmp_path)}
            )
            assert r.status_code == 200, r.text

        stall = await _max_loop_stall(_create)
    assert stall < 0.3, f"the event loop stalled {stall:.2f}s while a delegate was saved"
