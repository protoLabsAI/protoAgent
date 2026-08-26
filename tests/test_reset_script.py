"""Factory-reset CLI + compatibility wrapper (#3133, #3134, #3135)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.bashpath import real_bash

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "reset.sh"

pytestmark = pytest.mark.skipif(
    real_bash() is None, reason="reset.sh is a bash wrapper — nothing to exercise without a real bash"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(args: list[str], home: Path, *, box: Path | None = None, instance_home: Path | None = None):
    env = {**os.environ, "HOME": str(home), "PYTHON": sys.executable}
    for key in ("PROTOAGENT_BOX_ROOT", "PROTOAGENT_HOME", "PROTOAGENT_INSTANCE", "PROTOAGENT_HOST_CONFIG"):
        env.pop(key, None)
    env["PROTOAGENT_BOX_ROOT"] = str(box if box is not None else home / ".protoagent")
    if instance_home is not None:
        env["PROTOAGENT_HOME"] = str(instance_home)
    return subprocess.run([real_bash(), str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=str(REPO))


def _seed_instance(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "langgraph-config.yaml").write_text("model: {}\n", encoding="utf-8")
    (root / "checkpoints.db").write_text("x", encoding="utf-8")
    (root / "knowledge").mkdir()
    (root / "knowledge" / "agent.db").write_text("x", encoding="utf-8")


def test_dry_run_targets_current_instance_and_classifies_box_entries(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    _seed_instance(box / "default")
    _seed_instance(box / "dev")
    _seed_instance(box / "sibling")
    (box / "host-config.yaml").write_text("gateway: shared", encoding="utf-8")
    (box / "commons").mkdir()
    (box / "codex-oauth.json").write_text("{}", encoding="utf-8")
    (box / "legacy.db").write_text("x", encoding="utf-8")

    out = _run(["--dry-run", "--yes"], home)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert str(box / "default") in plan.split("Wipe:", 1)[1].split("Preserve (box-shared", 1)[0]
    assert str(box / "dev") in plan.split("Preserve (other instances):", 1)[1]
    assert str(box / "sibling") in plan.split("Preserve (other instances):", 1)[1]
    assert str(box / "legacy.db") in plan.split("Preserve (unrecognized box-root entries):", 1)[1]
    others_only = plan.split("Preserve (other instances):", 1)[1].split("Preserve (unrecognized", 1)[0]
    assert "legacy.db" not in others_only
    assert "Still signed in after reset" in plan and "ChatGPT" in plan
    assert (box / "default" / "checkpoints.db").exists()


def test_real_reset_wipes_only_the_current_instance(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    _seed_instance(box / "default")
    _seed_instance(box / "sibling")
    (box / "host-config.yaml").write_text("shared", encoding="utf-8")
    (box / "anthropic-oauth.json").write_text("{}", encoding="utf-8")

    out = _run(["--yes", "--port", str(_free_port())], home)
    assert out.returncode == 0, out.stderr
    assert not (box / "default").exists()
    assert (box / "sibling" / "checkpoints.db").exists()
    assert (box / "host-config.yaml").exists()
    assert (box / "anthropic-oauth.json").exists()
    assert "reset. Next boot runs the setup wizard" in out.stdout


def test_desktop_collapsed_layout_wipes_loose_state_not_box_shared(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "desktop-data"
    _seed_instance(root)
    (root / "audit").mkdir()
    (root / "host-config.yaml").write_text("shared", encoding="utf-8")
    (root / "commons").mkdir()
    (root / "codex-oauth.json").write_text("{}", encoding="utf-8")

    out = _run(["--yes", "--port", str(_free_port())], home, box=root, instance_home=root)
    assert out.returncode == 0, out.stderr
    assert not (root / "config").exists()
    assert not (root / "checkpoints.db").exists()
    assert not (root / "knowledge").exists()
    assert not (root / "audit").exists()
    assert (root / "host-config.yaml").exists()
    assert (root / "commons").exists()
    assert (root / "codex-oauth.json").exists()


def test_keep_secrets_restores_only_the_two_config_files(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    target = box / "default"
    _seed_instance(target)
    (target / "config" / "secrets.yaml").write_text("token: x\n", encoding="utf-8")
    (target / "config" / "theme.json").write_text("{}", encoding="utf-8")

    out = _run(["--yes", "--keep-secrets", "--port", str(_free_port())], home)
    assert out.returncode == 0, out.stderr
    assert (target / "config" / "secrets.yaml").read_text() == "token: x\n"
    assert (target / "config" / "langgraph-config.yaml").exists()
    assert not (target / "config" / "theme.json").exists()
    assert not (target / "checkpoints.db").exists()


def test_include_dev_wipes_dev_but_keeps_other_siblings(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    _seed_instance(box / "default")
    _seed_instance(box / "dev")
    _seed_instance(box / "sibling")

    out = _run(["--yes", "--include-dev", "--port", str(_free_port())], home)
    assert out.returncode == 0, out.stderr
    assert not (box / "default").exists()
    assert not (box / "dev").exists()
    assert (box / "sibling" / "checkpoints.db").exists()


def test_purge_box_removes_every_instance_and_machine_oauth(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    _seed_instance(box / "default")
    _seed_instance(box / "sibling")
    (box / "host-config.yaml").write_text("shared", encoding="utf-8")
    (box / "codex-oauth.json").write_text("{}", encoding="utf-8")

    dry = _run(["--dry-run", "--purge-box"], home)
    assert dry.returncode == 0
    assert "entire box" in dry.stdout and "Remove protoAgent's machine-wide OAuth copies" in dry.stdout
    assert box.exists()

    out = _run(["--yes", "--purge-box", "--port", str(_free_port())], home)
    assert out.returncode == 0, out.stderr
    assert not box.exists()


def test_purge_box_also_removes_explicit_instance_outside_box(tmp_path):
    home = tmp_path / "home"
    box = tmp_path / "machine-box"
    target = tmp_path / "explicit-instance"
    _seed_instance(target)
    (box / "commons").mkdir(parents=True)

    out = _run(
        ["--yes", "--purge-box", "--port", str(_free_port())],
        home,
        box=box,
        instance_home=target,
    )
    assert out.returncode == 0, out.stderr
    assert not box.exists()
    assert not target.exists()


def test_backup_contains_instance_and_host_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    box = home / ".protoagent"
    _seed_instance(box / "default")
    (box / "host-config.yaml").write_text("shared", encoding="utf-8")

    out = _run(["--yes", "--backup", "--port", str(_free_port())], home)
    assert out.returncode == 0, out.stderr
    archives = list(home.glob("protoagent-backup-default-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
    assert any(name.startswith("default/") for name in names)
    assert "host-config.yaml" in names


def test_missing_instance_is_nonzero_and_never_claims_success(tmp_path):
    home = tmp_path / "home"
    out = _run(["--yes", "--port", str(_free_port())], home)
    assert out.returncode == 1
    assert "Nothing to reset" in out.stdout
    assert "✓" not in out.stdout
