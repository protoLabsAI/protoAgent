"""Unit seams for reset's destructive-action and process guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from infra.paths import InstancePaths
from ops import reset


def _paths(tmp_path: Path) -> InstancePaths:
    box = tmp_path / "box"
    target = box / "default"
    target.mkdir(parents=True)
    return InstancePaths("default", box, target, tmp_path / "app")


def test_force_stops_only_a_verified_instance_process(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(reset, "_tracked_pid", lambda _paths: 42)
    monkeypatch.setattr(reset, "_port_open", lambda _port: True)
    stopped = []
    monkeypatch.setattr(reset, "terminate_tree", lambda pid: stopped.append(pid) or True)

    assert not reset._guard_running_server(paths, port=7870, force=False)
    assert stopped == []
    assert reset._guard_running_server(paths, port=7870, force=True)
    assert stopped == [42]


def test_force_refuses_to_kill_an_unknown_port_listener(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(reset, "_tracked_pid", lambda _paths: None)
    monkeypatch.setattr(reset, "_port_open", lambda _port: True)
    monkeypatch.setattr(
        reset,
        "terminate_tree",
        lambda _pid: (_ for _ in ()).throw(AssertionError("an unverified process must never be killed")),
    )
    assert not reset._guard_running_server(paths, port=7870, force=True)


def test_heartbeat_finds_a_running_desktop_process_without_a_pidfile(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(reset, "colocated_instances", lambda: [{"pid": os.getpid(), "instance_root": str(paths.instance_root)}])
    assert reset._tracked_pid(paths) == os.getpid()


def test_delete_guard_rejects_root_and_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ValueError, match="unsafe reset target"):
        reset._assert_safe_delete(tmp_path)
    with pytest.raises(ValueError, match="unsafe reset target"):
        reset._assert_safe_delete(Path("/"))
