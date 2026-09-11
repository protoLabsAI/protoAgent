"""Concurrent plugin-state writes don't lose each other's `plugins.enabled` (#2743 item 3).

Every writer used to read ``plugins.enabled`` from the live config, merge its change,
and only THEN take ``_CONFIG_WRITE_LOCK`` for the write. So two concurrent writers both
read ``[base]``, one wrote ``[base, x]``, the other ``[base, y]`` — and ``x`` was
installed but silently never enabled.

These drive the REAL ``_apply_settings_changes`` through the real route and op against
tmp config files. The reload is patched only to do what the real one does to the
config (commit ``STATE.graph_config`` from the file) and to hold the lock on its first
call — which pins the second writer inside the exact window where the lost update
happened. Nothing asserts on a captured dict: the assertion is the file on disk.
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


@pytest.fixture
def live_config(monkeypatch, tmp_path: Path):
    leaf = tmp_path / "langgraph-config.yaml"
    leaf.write_text("plugins:\n  enabled: [base]\n  disabled: []\n", encoding="utf-8")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "secrets.yaml")
    import infra.paths as paths

    monkeypatch.setattr(paths, "host_config_path", lambda: tmp_path / "host-config.yaml", raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig.from_yaml(str(leaf)), raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_meta", [], raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_router_keys", set(), raising=False)
    return leaf


@pytest.fixture
def held_first_reload(monkeypatch, live_config):
    """The real applier, with a reload that commits the file to STATE (like the real
    one) and PARKS inside the lock on its first call until the test releases it."""
    import server.agent_init as ai

    inside = threading.Event()
    release = threading.Event()
    second_calling = threading.Event()
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
        # The sync point: the second writer is about to call the applier. Everything a
        # writer does BEFORE that call — where the old code read the stale list — has
        # happened, so releasing the first writer now can't let a stale read slip in
        # after the commit and pass by luck.
        calls["apply"] += 1
        if calls["apply"] == 2:
            second_calling.set()
        return real_apply(*a, **k)

    monkeypatch.setattr(ai, "_reload_langgraph_agent", _reload)
    monkeypatch.setattr(ai, "_apply_settings_changes", _apply_spy)
    return inside, release, second_calling


def _enabled_on_disk(leaf: Path) -> list[str]:
    return list((yaml.safe_load(leaf.read_text(encoding="utf-8")) or {}).get("plugins", {}).get("enabled") or [])


async def _wait_for(event: threading.Event, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set():
        assert time.monotonic() < deadline, "a writer never reached its sync point"
        await asyncio.sleep(0.01)




async def test_two_concurrent_enables_through_the_route_both_survive(live_config, held_first_reload):
    from operator_api.plugin_routes import register_plugin_routes

    inside, release, second_calling = held_first_reload
    app = FastAPI()
    register_plugin_routes(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        first = asyncio.create_task(client.post("/api/plugins/x/enabled", json={"enabled": True}))
        await _wait_for(inside)  # x's write is on disk; its reload holds the lock
        second = asyncio.create_task(client.post("/api/plugins/y/enabled", json={"enabled": True}))
        await _wait_for(second_calling)
        release.set()
        r1, r2 = await first, await second

    assert r1.status_code == r2.status_code == 200
    assert sorted(_enabled_on_disk(live_config)) == ["base", "x", "y"], "one enable was lost"


@pytest.mark.parametrize(
    ("label", "second_request"),
    [
        # The three routes that write plugins.enabled back out as a SIDE effect — the lists
        # unchanged (sync/update: the write is just the reload trigger) or minus one id
        # (uninstall). Each rewrote a copy read before the lock over the first writer's x.
        ("sync", lambda c: c.post("/api/plugins/sync")),
        ("update", lambda c: c.post("/api/plugins/base/update")),
        ("uninstall", lambda c: c.delete("/api/plugins/base")),
    ],
)
async def test_a_side_effect_rewrite_does_not_undo_a_concurrent_enable(
    monkeypatch, live_config, held_first_reload, label, second_request
):
    from graph.plugins import installer

    from operator_api.plugin_routes import register_plugin_routes

    monkeypatch.setattr(installer, "sync", lambda **_k: [{"id": "base", "status": "installed"}])
    monkeypatch.setattr(installer, "list_installed", lambda: [{"id": "base", "source_url": "https://example.test/base"}])
    monkeypatch.setattr(installer, "install", lambda *a, **_k: {"id": "base", "version": "1", "resolved_sha": "abc"})
    monkeypatch.setattr(installer, "uninstall", lambda pid, purge=False: {"id": pid, "removed": ["dir"]})

    inside, release, second_calling = held_first_reload
    app = FastAPI()
    register_plugin_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = asyncio.create_task(client.post("/api/plugins/x/enabled", json={"enabled": True}))
        await _wait_for(inside)
        second = asyncio.create_task(second_request(client))
        await _wait_for(second_calling)
        release.set()
        r1, r2 = await first, await second

    assert r1.status_code == 200 and r2.status_code == 200, (label, r2.text)
    on_disk = _enabled_on_disk(live_config)
    assert "x" in on_disk, f"{label} wrote a stale plugins.enabled over the concurrent enable of x: {on_disk}"
    assert ("base" in on_disk) is (label != "uninstall")


async def test_two_concurrent_installs_both_end_up_enabled(monkeypatch, live_config, held_first_reload):
    """The issue's literal case: two plugin installs at once, each auto-enabling."""
    from graph.plugins import installer, loader
    from ops import OpContext
    import server.agent_init as ai
    from ops.plugins import install_and_activate

    inside, release, second_calling = held_first_reload
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **_k: {"id": url.rsplit("/", 1)[-1]})
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda _pid: None)

    def _install(name):
        return install_and_activate(
            f"https://example.test/{name}",
            ctx=OpContext.from_state(),
            apply_settings=lambda updates: ai._apply_settings_changes(config=updates),
        )

    first = asyncio.create_task(_install("x"))
    await _wait_for(inside)
    second = asyncio.create_task(_install("y"))
    await _wait_for(second_calling)
    release.set()
    a, b = await first, await second

    assert a.enabled == ["x"] and b.enabled == ["y"]
    assert sorted(_enabled_on_disk(live_config)) == ["base", "x", "y"], "an install's enable was lost"


def test_the_uninstall_scrub_waits_for_an_in_flight_write(live_config):
    """`installer._clean_config_refs` rewrites the same file from the graph layer. It must
    hold the same lock across its read and write, or a scrub interleaved with the
    applier's load→save either resurrects the uninstalled id or drops an enable."""
    from graph.config_io import CONFIG_WRITE_LOCK
    from graph.plugins.installer import _clean_config_refs

    done = threading.Event()

    def _scrub():
        _clean_config_refs("base", "base", False)
        done.set()

    with CONFIG_WRITE_LOCK:  # an applier mid-write
        t = threading.Thread(target=_scrub)
        t.start()
        assert not done.wait(0.3), "the scrub rewrote the file while another write held the lock"
    t.join(5)
    assert done.is_set()
    assert _enabled_on_disk(live_config) == []


def test_the_server_lock_is_the_config_layer_lock():
    # One lock, not two that merely share a name: the graph layer's writers take
    # config_io's, the server's applier takes agent_init's.
    import server.agent_init as ai
    from graph.config_io import CONFIG_WRITE_LOCK

    assert ai._CONFIG_WRITE_LOCK is CONFIG_WRITE_LOCK


def test_the_purge_secrets_scrub_waits_for_an_in_flight_write(live_config):
    """`_clean_secrets` (uninstall --purge) rewrites secrets.yaml, which the applier's
    `save_secrets` also read-modify-writes under the lock. Interleaved, one drops the
    other's secret update or brings the purged section back."""
    from graph.config_io import CONFIG_WRITE_LOCK, secrets_yaml_path
    from graph.plugins.installer import _clean_secrets

    secrets_yaml_path().write_text("doomed:\n  token: x\nkept:\n  token: y\n", encoding="utf-8")
    done = threading.Event()

    def _scrub():
        _clean_secrets("doomed")
        done.set()

    with CONFIG_WRITE_LOCK:
        t = threading.Thread(target=_scrub)
        t.start()
        assert not done.wait(0.3), "the secrets scrub rewrote the file while another write held the lock"
    t.join(5)
    assert done.is_set()
    assert yaml.safe_load(secrets_yaml_path().read_text(encoding="utf-8")) == {"kept": {"token": "y"}}


def test_a_failing_update_callable_is_a_failed_apply_not_an_exception(monkeypatch, live_config):
    # The (ok, messages) contract: an install whose code is already on disk must come back
    # as "installed; enabling failed: <why>", not a bare 500 from an escaping exception.
    import server.agent_init as ai

    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda *a, **k: pytest.fail("nothing to reload"))
    before = live_config.read_text(encoding="utf-8")

    def _boom(_current):
        raise RuntimeError("the live YAML is unreadable")

    ok, messages = ai._apply_settings_changes(config=_boom)

    assert ok is False
    assert any("the live YAML is unreadable" in m for m in messages)
    assert live_config.read_text(encoding="utf-8") == before, "nothing may be written"
