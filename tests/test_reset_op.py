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


def test_heartbeat_ignores_a_stale_registry_process(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    monkeypatch.setattr(reset, "colocated_instances", lambda: [{"pid": 424242, "instance_root": str(paths.instance_root)}])
    monkeypatch.setattr(reset, "pid_alive", lambda _pid: False)

    assert reset._tracked_pid(paths) is None


@pytest.mark.parametrize(("env_port", "expected"), [("8123", 8123), ("", 7870)])
def test_reset_uses_port_environment_for_the_process_guard(monkeypatch, tmp_path, env_port, expected):
    paths = _paths(tmp_path)
    seen = {}
    monkeypatch.setenv("PORT", env_port)
    monkeypatch.setattr(reset, "instance_paths", lambda: paths)
    monkeypatch.setattr(reset, "render_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        reset,
        "_guard_running_server",
        lambda _paths, *, port, force: seen.update(port=port, force=force) or False,
    )

    assert reset.run_reset_cli(["--yes"]) == 1
    assert seen == {"port": expected, "force": False}


def test_dry_run_warns_when_the_real_reset_would_be_blocked(monkeypatch, tmp_path, capsys):
    paths = _paths(tmp_path)
    (paths.instance_root / "checkpoints.db").write_text("state", encoding="utf-8")
    monkeypatch.setattr(reset, "instance_paths", lambda: paths)
    monkeypatch.setattr(reset, "_tracked_pid", lambda _paths: None)
    monkeypatch.setattr(reset, "_port_open", lambda port: port == 8123)

    assert reset.run_reset_cli(["--dry-run", "--port", "8123"]) == 0
    output = capsys.readouterr().out
    assert "NOTE: something is listening on :8123" in output
    assert "stop it before a real run" in output


def test_delete_guard_rejects_root_and_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ValueError, match="unsafe reset target"):
        reset._assert_safe_delete(tmp_path)
    with pytest.raises(ValueError, match="unsafe reset target"):
        reset._assert_safe_delete(Path("/"))
