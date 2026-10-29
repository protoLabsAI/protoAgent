"""Demo-agent core (#4609 part 1) — stamp, gate, cap, TTL reap.

Exercises :mod:`graph.fleet.demo` against the REAL workspace manager on a temp
``PROTOAGENT_WORKSPACES_DIR``, with the supervisor (process spawn/stop) and the
host-model inheritance faked so nothing is launched and no host config is read. The
archetype resolver is pinned to a code-free "basic" so no bundle is installed over the
network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from graph.fleet import demo
from graph.workspaces import manager

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    # Machine-independent port selection (mirrors tests/test_workspaces.py).
    monkeypatch.setattr(manager, "_port_is_free", lambda port: True)

    # No host-model inheritance in the test — a demo overwrites model.name explicitly, and
    # reading the real host config would make the test depend on the ambient instance.
    import ops.fleet as fleet_ops

    monkeypatch.setattr(fleet_ops, "_inherit_model_source", lambda inherit_config: None)

    # Fake the supervisor: creating/stopping a member must not spawn or signal a process.
    from graph.fleet import supervisor

    def _fake_start(ident, **_kw):
        w = manager._find(ident)
        return {"name": w["name"], "id": w["id"], "port": w["port"], "running": True}

    monkeypatch.setattr(supervisor, "start", _fake_start)
    monkeypatch.setattr(supervisor, "stop", lambda ident, **_kw: {"name": str(ident), "stopped": True})

    # Pin archetype resolution to a code-free persona (no bundle → no install).
    monkeypatch.setattr(
        demo,
        "resolve_archetype",
        lambda a: {"id": a, "label": a, "bundle": None, "ref": None, "soul": "", "requires_tools": []},
    )
    return tmp_path / "ws"


def _record(root, slug: str) -> dict:
    return yaml.safe_load((root / slug / "workspace.yaml").read_text())


# ── r1: stamp + return shape ────────────────────────────────────────────────


def test_create_demo_stamps_and_returns(root):
    out = demo.create_demo("basic", "openai", "gpt-4o", created_by="jobCoach")

    # Return shape.
    assert set(out) == {"slug", "port", "token", "archetype_ref", "plugin_versions", "created_at"}
    assert out["slug"].startswith("demo-basic-")
    assert out["port"] == 7872  # :7871 is the dev sandbox's, never a member's
    assert out["token"]  # the member's scoped bearer
    assert out["archetype_ref"] == "basic"  # code-free → the archetype id
    assert out["plugin_versions"] == {}  # nothing installed
    assert out["created_at"]

    # The stamp is persisted in workspace.yaml.
    rec = _record(root, out["slug"])
    assert rec["demo"] is True
    assert rec["created_by"] == "jobCoach"
    assert rec["created_at"] == out["created_at"]

    # The model was pointed at <connection>:<model> before the member started.
    cfg = yaml.safe_load((root / out["slug"] / "config" / "langgraph-config.yaml").read_text())
    assert cfg["model"]["name"] == "openai:gpt-4o"

    # It shows up as a live demo.
    assert [d["slug"] if "slug" in d else d["id"] for d in demo.list_demos()] == [out["slug"]]


def test_default_name_can_be_overridden(root):
    out = demo.create_demo("basic", "openai", "gpt-4o", name="pitch", created_by="pm")
    assert out["slug"].startswith("pitch-")
    assert _record(root, out["slug"])["demo"] is True


# ── r2: the stamp gate refuses unstamped members ────────────────────────────


def test_require_demo_refuses_a_real_member(root):
    real = manager.create("jobCoach")  # a normal, unstamped member
    with pytest.raises(demo.NotDemoMember):
        demo.require_demo(real["id"])


def test_configure_and_delete_refuse_unstamped_member_and_change_nothing(root):
    real = manager.create("jobCoach")
    ws = root / real["id"]
    before_cfg = (ws / "config" / "langgraph-config.yaml").read_text()
    before_rec = (ws / "workspace.yaml").read_text()

    with pytest.raises(demo.NotDemoMember):
        demo.configure_demo(real["id"], model_connection="openai", model="gpt-4o")
    with pytest.raises(demo.NotDemoMember):
        demo.delete_demo(real["id"])

    # Nothing was touched, nothing was deleted.
    assert ws.exists()
    assert (ws / "config" / "langgraph-config.yaml").read_text() == before_cfg
    assert (ws / "workspace.yaml").read_text() == before_rec


def test_require_demo_fails_closed_on_unknown_slug(root):
    with pytest.raises(demo.NotDemoMember):
        demo.require_demo("ghost-does-not-exist")


def test_configure_demo_updates_a_stamped_member(root):
    out = demo.create_demo("basic", "openai", "gpt-4o", created_by="pm")
    demo.configure_demo(
        out["slug"],
        model_connection="anthropic",
        model="claude",
        plugin_settings={"notes": {"max": 5}},
    )
    cfg = yaml.safe_load((root / out["slug"] / "config" / "langgraph-config.yaml").read_text())
    assert cfg["model"]["name"] == "anthropic:claude"
    assert cfg["notes"]["max"] == 5


def test_delete_demo_purges_a_stamped_member(root):
    out = demo.create_demo("basic", "openai", "gpt-4o", created_by="pm")
    assert (root / out["slug"]).exists()
    demo.delete_demo(out["slug"])
    assert not (root / out["slug"]).exists()


# ── r3: live cap + TTL reap ──────────────────────────────────────────────────


def test_fourth_live_demo_is_refused_at_cap_three(root):
    for _ in range(3):
        demo.create_demo("basic", "openai", "gpt-4o", created_by="pm", max_live=3)
    assert len(demo.list_demos()) == 3
    with pytest.raises(demo.DemoCapReached):
        demo.create_demo("basic", "openai", "gpt-4o", created_by="pm", max_live=3)


def test_reap_expired_deletes_old_demos_only(root):
    real = manager.create("jobCoach")  # a real member — must survive regardless of age
    old = demo.create_demo("basic", "openai", "gpt-4o", created_by="pm")
    fresh = demo.create_demo("basic", "openai", "gpt-4o", created_by="pm")

    # Backdate the old demo's stamp past the TTL.
    old_ws = root / old["slug"] / "workspace.yaml"
    rec = yaml.safe_load(old_ws.read_text())
    rec["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    old_ws.write_text(yaml.safe_dump(rec))

    reaped = demo.reap_expired(ttl_hours=24)

    assert old["slug"] in reaped
    assert fresh["slug"] not in reaped
    assert not (root / old["slug"]).exists()  # old demo purged
    assert (root / fresh["slug"]).exists()  # fresh demo kept
    assert (root / real["id"]).exists()  # real member untouched


def test_reap_skips_demo_with_unparseable_stamp(root):
    out = demo.create_demo("basic", "openai", "gpt-4o", created_by="pm")
    ws = root / out["slug"] / "workspace.yaml"
    rec = yaml.safe_load(ws.read_text())
    rec["created_at"] = "not-a-date"
    ws.write_text(yaml.safe_dump(rec))
    assert demo.reap_expired(ttl_hours=0) == []  # can't age it → leave it alone
    assert (root / out["slug"]).exists()


def test_create_demo_reaps_before_enforcing_the_cap(root):
    # Fill the cap, then expire one; the next create reaps it first and succeeds.
    outs = [demo.create_demo("basic", "openai", "gpt-4o", created_by="pm", max_live=3) for _ in range(3)]
    stale = outs[0]
    ws = root / stale["slug"] / "workspace.yaml"
    rec = yaml.safe_load(ws.read_text())
    rec["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    ws.write_text(yaml.safe_dump(rec))

    fresh = demo.create_demo("basic", "openai", "gpt-4o", created_by="pm", max_live=3)
    assert not (root / stale["slug"]).exists()  # the stale one was reaped
    assert (root / fresh["slug"]).exists()
    assert len(demo.list_demos()) == 3


# ── r4: manager.create is unchanged without the new param ────────────────────


def test_manager_create_without_extra_meta_is_unstamped(root):
    plain = manager.create("plain")
    rec = _record(root, plain["id"])
    assert "demo" not in rec
    assert "created_by" not in rec
    assert "created_at" not in rec


def test_extra_meta_cannot_overwrite_core_record_keys(root):
    # A caller can ADD metadata but never rewrite identity/routing keys.
    created = manager.create("fenced", extra_meta={"id": "evil", "port": 1, "demo": True})
    rec = _record(root, created["id"])
    assert rec["id"] == created["id"] != "evil"
    assert rec["port"] == created["port"] != 1
    assert rec["demo"] is True  # the non-core stamp still lands
