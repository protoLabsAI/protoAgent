"""`scripts/reset.sh` dry-run safety (#1159).

The factory-reset script is destructive, so its dangerous logic — "target prod's
unscoped data, preserve EVERY sibling instance + every scoped <store>/<instance>
leaf" — is pinned here by running it in `--dry-run` against a synthetic data tree
and asserting (a) the plan targets the right things and (b) it deletes NOTHING.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "reset.sh"

# data_home() prefers /sandbox over $HOME/.protoagent; if it exists the script
# would target it instead of our test HOME, so skip there (CI has no /sandbox).
_SANDBOX = pytest.mark.skipif(Path("/sandbox").is_dir(), reason="data_home() would target /sandbox")


def _run(args: list[str], home: Path, cfg: Path):
    env = {**os.environ, "HOME": str(home), "PROTOAGENT_CONFIG_DIR": str(cfg)}
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=str(REPO)
    )


@_SANDBOX
def test_dry_run_targets_prod_only_and_preserves_siblings(tmp_path):
    home = tmp_path / "home"
    data = home / ".protoagent"
    data.mkdir(parents=True)
    # prod (unscoped) state
    (data / "checkpoints.db").write_text("x")
    (data / "telemetry.db").write_text("x")
    (data / ".instance-uid").write_text("x")
    # a shared store dir: prod's direct file + two scoped instance leaves
    (data / "knowledge" / "dev").mkdir(parents=True)
    (data / "knowledge" / "roxy").mkdir(parents=True)
    (data / "knowledge" / "agent.db").write_text("prod")
    (data / "knowledge" / "dev" / "agent.db").write_text("dev")
    (data / "knowledge" / "roxy" / "agent.db").write_text("roxy")
    # a sibling instance ROOT (must be preserved wholesale)
    (data / "sib").mkdir()
    (data / "sib" / ".instance-uid").write_text("sib")

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "langgraph-config.yaml").write_text("x")
    (cfg / "secrets.yaml").write_text("x")
    (cfg / "plugins").mkdir()

    out = _run(["--dry-run", "--yes"], home, cfg)
    assert out.returncode == 0, out.stderr
    plan = out.stdout

    assert "delete prod file:  checkpoints.db" in plan
    assert "delete prod file:  telemetry.db" in plan
    assert "delete prod file:  .instance-uid" in plan
    # the shared store dir: drop prod's 1 file, KEEP the 2 instance leaves
    assert "store dir knowledge/: drop 1 prod file(s); keep 2 instance leaf(s)" in plan
    # sibling instance root preserved + dev (default keeps dev too)
    assert "PRESERVE other instances:" in plan and "sib" in plan
    assert "delete local:  langgraph-config.yaml" in plan
    assert "delete local:  plugins" in plan
    assert "restore tracked: config/SOUL.md" in plan

    # CRITICAL: a dry run deletes NOTHING.
    assert (data / "checkpoints.db").exists()
    assert (data / "knowledge" / "agent.db").exists()
    assert (data / "knowledge" / "dev" / "agent.db").exists()
    assert (data / "sib" / ".instance-uid").exists()
    assert (cfg / "langgraph-config.yaml").exists()
    assert (cfg / "plugins").is_dir()


@_SANDBOX
def test_dry_run_keep_secrets_preserves_creds(tmp_path):
    home = tmp_path / "home"
    (home / ".protoagent").mkdir(parents=True)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "secrets.yaml").write_text("x")
    (cfg / "langgraph-config.yaml").write_text("x")
    (cfg / "plugins").mkdir()

    out = _run(["--dry-run", "--yes", "--keep-secrets"], home, cfg)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "keep (--keep-secrets): secrets.yaml" in plan
    assert "keep (--keep-secrets): langgraph-config.yaml" in plan
    assert "delete local:  langgraph-config.yaml" not in plan
    assert "delete local:  plugins" in plan  # plugins still wiped


@_SANDBOX
def test_dry_run_include_dev_wipes_dev(tmp_path):
    home = tmp_path / "home"
    data = home / ".protoagent"
    (data / "dev").mkdir(parents=True)
    (data / "dev" / ".instance-uid").write_text("dev")
    cfg = tmp_path / "config"
    (cfg / "dev").mkdir(parents=True)

    out = _run(["--dry-run", "--yes", "--include-dev"], home, cfg)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    # default keeps dev; --include-dev drops it from the preserve set + wipes its config
    assert "PRESERVE other instances:" not in plan or "dev" not in plan.split("PRESERVE")[-1]
    assert "delete dev config:  dev/" in plan
