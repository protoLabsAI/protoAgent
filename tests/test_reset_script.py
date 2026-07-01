"""`scripts/reset.sh` dry-run safety (#1159, ADR 0065).

The factory-reset script is destructive, so its dangerous logic — "wipe the default
instance's single subtree (box_root/default), preserve EVERY box-shared item and
EVERY other instance subtree" — is pinned here by running it in `--dry-run` against a
synthetic two-tier data tree and asserting (a) the plan targets the right things and
(b) it deletes NOTHING.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "reset.sh"

# box_root prefers /sandbox over $HOME/.protoagent; if it exists the script would
# target it instead of our test HOME, so skip there (CI has no /sandbox).
_SANDBOX = pytest.mark.skipif(Path("/sandbox").is_dir(), reason="box_root would target /sandbox")


def _run(args: list[str], home: Path):
    env = {**os.environ, "HOME": str(home)}
    env.pop("PROTOAGENT_BOX_ROOT", None)  # let the script derive box_root from HOME
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=str(REPO)
    )


def _seed_instance(root: Path) -> None:
    """A minimal two-tier instance subtree: config leaf + a couple of data stores,
    all directly under the instance root (ADR 0065 — one subtree per instance)."""
    (root / "config").mkdir(parents=True)
    (root / "config" / "langgraph-config.yaml").write_text("x")
    (root / "checkpoints.db").write_text("x")
    (root / "knowledge").mkdir()
    (root / "knowledge" / "agent.db").write_text("x")


@_SANDBOX
def test_dry_run_targets_default_only_and_preserves_the_rest(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    # the default instance (the wipe target) + two other instance subtrees
    _seed_instance(box / "default")
    _seed_instance(box / "dev")
    _seed_instance(box / "sib")
    # box-tier shared state (machine-wide, must be preserved)
    (box / "host-config.yaml").write_text("gateway: shared")
    (box / "commons").mkdir()
    (box / "commons" / "skills.db").write_text("x")

    out = _run(["--dry-run", "--yes"], home)
    assert out.returncode == 0, out.stderr
    plan = out.stdout

    # (a) the plan targets box_root/default
    assert f"delete instance: {box / 'default'}" in plan

    # box-shared items are preserved (machine-wide)
    shared = plan.split("Preserve (box-shared, machine-wide):")[1].split("Preserve (other instances):")[0]
    assert "host-config.yaml" in shared
    assert "commons" in shared

    # every OTHER instance subtree is preserved
    others = plan.split("Preserve (other instances):")[1]
    assert "dev" in others
    assert "sib" in others

    # CRITICAL: a dry run deletes NOTHING.
    assert (box / "default" / "checkpoints.db").exists()
    assert (box / "default" / "config" / "langgraph-config.yaml").exists()
    assert (box / "dev" / "checkpoints.db").exists()
    assert (box / "sib" / "checkpoints.db").exists()
    assert (box / "host-config.yaml").exists()
    assert (box / "commons" / "skills.db").exists()


@_SANDBOX
def test_dry_run_keep_secrets_preserves_creds(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    _seed_instance(box / "default")
    (box / "default" / "config" / "secrets.yaml").write_text("token: x")

    out = _run(["--dry-run", "--yes", "--keep-secrets"], home)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    assert "keep (--keep-secrets): config/secrets.yaml" in plan
    assert "keep (--keep-secrets): config/langgraph-config.yaml" in plan
    # still targets the default subtree for the wipe
    assert f"delete instance: {box / 'default'}" in plan
    # dry run changed nothing
    assert (box / "default" / "config" / "secrets.yaml").exists()


@_SANDBOX
def test_dry_run_include_dev_wipes_dev(tmp_path):
    home = tmp_path / "home"
    box = home / ".protoagent"
    _seed_instance(box / "default")
    _seed_instance(box / "dev")

    out = _run(["--dry-run", "--yes", "--include-dev"], home)
    assert out.returncode == 0, out.stderr
    plan = out.stdout
    # dev moves from "preserved" to a wipe target
    assert f"delete instance: {box / 'dev'}  (--include-dev)" in plan
    others = plan.split("Preserve (other instances):")[1]
    assert "dev" not in others
    # dry run still deletes nothing
    assert (box / "dev" / "checkpoints.db").exists()
