"""Persistence hardening (2026-06-10 prod-readiness audit).

Three guarantees pinned here:

1. ``paths.atomic_write`` — temp-in-same-dir + ``os.replace`` so a crash
   mid-write can never leave a truncated registry; optional ``mode`` lands
   BEFORE the swap (a credentials file never exists world-readable).
2. The fleet registries (``fleet.json`` / ``remotes.json``) write atomically,
   ``remotes.json`` is 0600 (it carries remote bearer tokens), and a corrupt
   registry loads as empty WITH a warning instead of silently forgetting
   every record.
3. ``_apply_settings_changes`` / ``_reload_langgraph_agent`` are serialized by
   ``_CONFIG_WRITE_LOCK`` — concurrent saves can't interleave the YAML
   read-modify-write or the graph build-then-commit choreography.
"""

from __future__ import annotations

import json
import logging
import stat
import threading
import time


from infra.paths import atomic_write

# ── 1. atomic_write ──────────────────────────────────────────────────────────


def test_atomic_write_creates_parents_and_content(tmp_path):
    p = tmp_path / "a" / "b" / "f.json"
    atomic_write(p, '{"x": 1}')
    assert json.loads(p.read_text()) == {"x": 1}


def test_atomic_write_replaces_existing(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("old")
    atomic_write(p, "new")
    assert p.read_text() == "new"


def test_atomic_write_mode_0600(tmp_path):
    p = tmp_path / "secret.json"
    atomic_write(p, "{}", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_atomic_write_leaves_no_temp_droppings(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write(p, "data")
    assert [f.name for f in tmp_path.iterdir()] == ["f.txt"]


# ── 2. fleet registries ──────────────────────────────────────────────────────


def test_corrupt_fleet_state_warns_and_loads_empty(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    from graph.fleet import supervisor

    f = supervisor._state_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"truncated": ')  # crash-mid-write artifact
    with caplog.at_level(logging.WARNING):
        assert supervisor._load_state() == {}
    assert any("unreadable" in r.message for r in caplog.records)


def test_corrupt_remotes_warns_and_loads_empty(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    from graph.fleet import supervisor

    p = supervisor._remotes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json")
    with caplog.at_level(logging.WARNING):
        assert supervisor._load_remotes() == {}
    assert any("unreadable" in r.message for r in caplog.records)


def test_remotes_written_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    from graph.fleet import supervisor

    supervisor._save_remotes({"r1": {"url": "http://h:7870", "token": "tok"}})
    p = supervisor._remotes_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert supervisor._load_remotes()["r1"]["token"] == "tok"


def test_save_yaml_doc_is_atomic_no_droppings(tmp_path):
    from graph.config_io import load_yaml_doc, save_yaml_doc

    p = tmp_path / "cfg.yaml"
    save_yaml_doc({"model": {"name": "x"}}, p)
    assert load_yaml_doc(p)["model"]["name"] == "x"
    assert [f.name for f in tmp_path.iterdir()] == ["cfg.yaml"]


# ── 3. config write serialization ────────────────────────────────────────────


def test_apply_settings_changes_serialized(monkeypatch):
    """Two threads in _apply_settings_changes never overlap (lost-update guard)."""
    from server import agent_init

    in_flight = []
    overlaps = []

    def fake_reload():
        in_flight.append(1)
        if len(in_flight) - len(overlaps) > 1:  # someone else is inside too
            overlaps.append(1)
        time.sleep(0.05)
        in_flight.pop()
        return True, "reloaded (fake)"

    # Patch the reload + autostart sync; with config/soul None this makes
    # _apply_settings_changes a pure (locked) reload.
    monkeypatch.setattr(agent_init, "_reload_langgraph_agent", fake_reload)
    monkeypatch.setattr(agent_init, "_sync_autostart_with_config", lambda c: "")

    threads = [threading.Thread(target=agent_init._apply_settings_changes) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlaps, "concurrent _apply_settings_changes bodies interleaved"


def test_reload_is_directly_lockable():
    """Plugin routes call _reload_langgraph_agent directly — it must hold the
    same lock (RLock: nested acquisition from _apply_settings_changes is fine)."""
    from server import agent_init

    assert agent_init._reload_langgraph_agent.__wrapped__ is not None
    # The decorator wraps with _CONFIG_WRITE_LOCK; prove re-entrancy works.
    with agent_init._CONFIG_WRITE_LOCK:
        acquired = agent_init._CONFIG_WRITE_LOCK.acquire(blocking=False)
        assert acquired
        agent_init._CONFIG_WRITE_LOCK.release()
