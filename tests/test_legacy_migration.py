"""Auto-on-boot migration from the pre-redesign on-disk layout into the new
``instance_root/config`` — ``graph.config_io.migrate_legacy_layout``.

Idempotent, non-destructive (copy, not move), and a no-op once migrated or on a
fresh install. Covers the two legacy shapes that together span all four
deployments: flat-under-instance-root (desktop ``PROTOAGENT_HOME`` / fleet member
``<ws>``) and the bundle/repo config dir (local default / dev sandbox / container).
"""

from __future__ import annotations

import graph.config_io as cio
import infra.paths as paths


def _setup(monkeypatch, tmp_path, **env):
    """Pin box_root + app_root to clean tmp dirs (so we never read the real repo
    config), clear the root env vars, apply ``env``, and re-resolve."""
    box = tmp_path / "box"
    box.mkdir()
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    monkeypatch.setattr(paths, "data_home", lambda: box)
    monkeypatch.setattr(paths, "_app_root", lambda: app)
    for v in (
        "PROTOAGENT_HOME",
        "PROTOAGENT_INSTANCE",
        "PROTOAGENT_BOX_ROOT",
        "PROTOAGENT_CONFIG_DIR",
        "PROTOAGENT_SEED_CONFIG",
    ):
        monkeypatch.delenv(v, raising=False)
    for k, val in env.items():
        monkeypatch.setenv(k, val)
    paths.reset_instance_paths()
    return box, app


def test_migrates_flat_home_layout(monkeypatch, tmp_path):
    """Desktop/fleet shape: old config sat flat under the instance root (=PROTOAGENT_HOME)."""
    home = tmp_path / "appdata"
    home.mkdir()
    (home / "langgraph-config.yaml").write_text("model: {}\n")
    (home / "secrets.yaml").write_text("model:\n  api_key: sek\n")
    (home / ".setup-complete").write_text("")
    (home / "theme.json").write_text("{}")
    _setup(monkeypatch, tmp_path, PROTOAGENT_HOME=str(home))

    assert cio.migrate_legacy_layout() is True
    p = paths.instance_paths()
    assert p.config_yaml.read_text() == "model: {}\n"
    assert p.secrets_yaml.read_text() == "model:\n  api_key: sek\n"
    assert p.setup_marker.exists()
    assert p.theme_json.exists()
    # originals are left in place (non-destructive)
    assert (home / "langgraph-config.yaml").exists()
    # idempotent: second run copies nothing
    assert cio.migrate_legacy_layout() is False


def test_migrates_repo_bundle_scoped_layout(monkeypatch, tmp_path):
    """Dev sandbox shape: old config was at ``<repo>/config/dev/langgraph-config.yaml``."""
    _, app = _setup(monkeypatch, tmp_path, PROTOAGENT_INSTANCE="dev")
    legacy = app / "config" / "dev"
    legacy.mkdir(parents=True)
    (legacy / "langgraph-config.yaml").write_text("x: 1\n")
    (legacy / "secrets.yaml").write_text("k: v\n")

    assert cio.migrate_legacy_layout() is True
    p = paths.instance_paths()
    assert p.config_yaml.read_text() == "x: 1\n"
    assert p.secrets_yaml.read_text() == "k: v\n"


def test_default_unscoped_repo_layout(monkeypatch, tmp_path):
    """Local default shape: old live config at ``<repo>/config/langgraph-config.yaml``."""
    _, app = _setup(monkeypatch, tmp_path)  # no env → "default"
    (app / "config" / "langgraph-config.yaml").write_text("d: 1\n")

    assert cio.migrate_legacy_layout() is True
    p = paths.instance_paths()
    assert p.instance_id == "default"
    assert p.config_yaml.read_text() == "d: 1\n"


def test_no_migration_when_new_config_present(monkeypatch, tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "langgraph-config.yaml").write_text("old\n")
    _setup(monkeypatch, tmp_path, PROTOAGENT_HOME=str(home))
    p = paths.instance_paths()
    p.config_dir.mkdir(parents=True)
    p.config_yaml.write_text("new\n")

    assert cio.migrate_legacy_layout() is False
    assert p.config_yaml.read_text() == "new\n"  # untouched


def test_fresh_install_nothing_to_migrate(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)  # default, empty app/config (only would-be .example)
    assert cio.migrate_legacy_layout() is False
    assert not paths.instance_paths().config_yaml.exists()


def test_ensure_live_config_runs_migration(monkeypatch, tmp_path):
    """ensure_live_config bridges the old layout before falling back to the .example seed."""
    home = tmp_path / "h"
    home.mkdir()
    (home / "langgraph-config.yaml").write_text("carried: over\n")
    _setup(monkeypatch, tmp_path, PROTOAGENT_HOME=str(home))

    # No .example exists in the tmp app dir, so a non-migrating ensure_live_config would
    # seed nothing; migration must carry the old config across.
    cio.ensure_live_config()
    assert paths.instance_paths().config_yaml.read_text() == "carried: over\n"


# ── store-tier migration (box_root/<store> → box_root/default/<store>) ───────────


def _seed_legacy_stores(box):
    """Drop pre-redesign data stores flat under the box root + box-tier shared state."""
    # Per-instance stores (these MOVE into instance_root):
    (box / "checkpoints.db").write_text("ckpt")
    (box / "skills.db").write_text("skills")
    (box / "knowledge").mkdir()
    (box / "knowledge" / "agent.db").write_text("kb")
    (box / "memory").mkdir()
    (box / "memory" / "s1.json").write_text("{}")
    (box / "goals").mkdir()
    (box / "goals" / "g.json").write_text("{}")
    # Box-tier shared state (these STAY at the box root — never copied):
    (box / "host-config.yaml").write_text("host: cfg")
    (box / "commons").mkdir()
    (box / "commons" / "skills.db").write_text("shared")
    (box / ".instances").mkdir()
    (box / ".instances" / "123.json").write_text("{}")
    (box / ".data-version").write_text("{}")
    (box / "workspaces").mkdir()
    (box / "workspaces" / "fleet.json").write_text("{}")


def test_migrates_default_instance_stores(monkeypatch, tmp_path):
    """First boot of the default instance carries the flat box-root data stores into
    ``box_root/default/<store>``; box-tier shared state is left alone."""
    box, _ = _setup(monkeypatch, tmp_path)  # default instance
    _seed_legacy_stores(box)

    assert cio.migrate_legacy_layout() is True
    inst = box / "default"
    # per-instance stores carried (files + dirs):
    assert (inst / "checkpoints.db").read_text() == "ckpt"
    assert (inst / "skills.db").read_text() == "skills"
    assert (inst / "knowledge" / "agent.db").read_text() == "kb"
    assert (inst / "memory" / "s1.json").exists()
    assert (inst / "goals" / "g.json").exists()
    # originals untouched (copy, not move):
    assert (box / "checkpoints.db").exists()
    # box-tier shared state NOT copied under the instance root:
    assert not (inst / "host-config.yaml").exists()
    assert not (inst / "commons").exists()
    assert not (inst / ".instances").exists()
    assert not (inst / ".data-version").exists()
    assert not (inst / "workspaces").exists()


def test_store_migration_is_idempotent(monkeypatch, tmp_path):
    """Second pass copies nothing (destinations already exist)."""
    box, _ = _setup(monkeypatch, tmp_path)
    _seed_legacy_stores(box)
    assert cio.migrate_legacy_layout() is True
    assert cio.migrate_legacy_layout() is False  # no-op once carried


def test_store_migration_skipped_for_scoped_instance(monkeypatch, tmp_path):
    """Only the DEFAULT instance auto-migrates — a named/dev instance re-inits."""
    box, _ = _setup(monkeypatch, tmp_path, PROTOAGENT_INSTANCE="dev")
    _seed_legacy_stores(box)
    assert cio.migrate_legacy_layout() is False
    assert not (box / "dev" / "checkpoints.db").exists()


def test_store_migration_not_resurrected_after_config_present(monkeypatch, tmp_path):
    """Once the live config exists (post-first-boot), the bridge is skipped entirely —
    so a store the operator later clears is never re-copied from a legacy orphan."""
    box, _ = _setup(monkeypatch, tmp_path)
    p = paths.instance_paths()
    p.config_dir.mkdir(parents=True)
    p.config_yaml.write_text("already: migrated\n")
    _seed_legacy_stores(box)  # legacy orphans still sitting at the box root

    assert cio.migrate_legacy_layout() is False
    assert not (box / "default" / "checkpoints.db").exists()  # not resurrected
