"""Default instance scoping (#706) — never silently share the root."""

from __future__ import annotations

from infra import paths


def test_explicit_instance_always_wins(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    monkeypatch.setenv("PROTOAGENT_AUTO_SCOPE", "1")
    assert paths.instance_id() == "alice"


def test_default_is_unscoped_without_auto(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.delenv("PROTOAGENT_AUTO_SCOPE", raising=False)
    assert paths.instance_id() == ""  # legacy behavior preserved (no breakage)


def test_auto_scope_derives_stable_per_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.setenv("PROTOAGENT_AUTO_SCOPE", "1")
    monkeypatch.chdir(tmp_path)
    iid = paths.instance_id()
    assert iid and iid == paths.instance_id()  # non-empty + stable across calls
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert paths.instance_id() != iid  # a different dir → its own scope


def test_unscoped_warning_only_when_root_has_state(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.delenv("PROTOAGENT_AUTO_SCOPE", raising=False)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert paths.unscoped_warning() is None  # empty home → quiet
    (tmp_path / "checkpoints.db").write_text("x")
    assert "UNSCOPED" in (paths.unscoped_warning() or "")
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    assert paths.unscoped_warning() is None  # scoped → quiet


def test_shared_skills_resolve_to_commons_not_scoped(monkeypatch, tmp_path):
    """ADR 0041 — a `shared` skills store resolves to the COMMONS (un-scoped),
    identical for every instance; a scoped store stays per-instance."""
    from server.agent_init import _resolve_skills_db

    commons = tmp_path / "commons"
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path / "box"))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "agentA")
    paths.reset_instance_paths()
    shared_a = _resolve_skills_db("/x/skills.db", shared=True, commons=commons)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "agentB")
    paths.reset_instance_paths()
    shared_b = _resolve_skills_db("/x/skills.db", shared=True, commons=commons)
    scoped_b = _resolve_skills_db("/x/skills.db", shared=False)  # per-instance: instance_root/skills.db

    assert shared_a == shared_b == str(commons / "skills.db")  # one commons for all agents
    assert "agentB" in scoped_b and scoped_b != shared_a  # scoped is per-instance
    assert scoped_b == str(tmp_path / "box" / "agentB" / "skills.db")


def test_skills_tier_config_parses(tmp_path):
    """`skills.shared` + `commons.path` parse into the config (ADR 0041)."""
    from graph.config import LangGraphConfig

    cfg = tmp_path / "c.yaml"
    cfg.write_text("skills: { shared: true }\ncommons: { path: /tmp/commons }\n")
    c = LangGraphConfig.from_yaml(str(cfg))
    assert c.skills_shared is True
    assert c.commons_path == "/tmp/commons"


# ── co-location heartbeats (#706) ─────────────────────────────────────────────
def _home(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.delenv("PROTOAGENT_AUTO_SCOPE", raising=False)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    return tmp_path


def test_register_unregister_heartbeat(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    paths.register_instance(7871, "protoagent")
    import os

    f = home / ".instances" / f"{os.getpid()}.json"
    assert f.exists()
    assert paths.colocated_instances() == []  # self doesn't count as a sibling
    paths.unregister_instance()
    assert not f.exists()


def test_heartbeats_live_at_box_root(monkeypatch, tmp_path):
    """Heartbeats are BOX-tier — ``box_root/.instances`` regardless of the instance id,
    so a live sibling anywhere on the machine is detectable (#813)."""
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    paths.reset_instance_paths()
    paths.register_instance(7874, "roxy")
    import os

    assert (tmp_path / ".instances" / f"{os.getpid()}.json").exists()  # box-tier, NOT under roxy
    assert not (tmp_path / "roxy" / ".instances").exists()
    paths.unregister_instance()


def test_colocated_sibling_detected_and_warned(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    d = home / ".instances"
    d.mkdir()
    (d / "12345.json").write_text('{"pid": 12345, "port": 7871, "identity": "roxy"}')
    monkeypatch.setattr(paths, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(paths, "_is_protoagent_pid", lambda pid: True)
    sibs = paths.colocated_instances()
    assert sibs == [{"pid": 12345, "port": 7871, "identity": "roxy"}]
    w = paths.colocation_warning()
    assert "roxy" in w and "PROTOAGENT_INSTANCE" in w and str(home) in w


def test_stale_heartbeats_pruned(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    d = home / ".instances"
    d.mkdir()
    (d / "12345.json").write_text('{"pid": 12345}')  # dead pid
    (d / "23456.json").write_text('{"pid": 23456}')  # recycled pid (not a server)
    (d / "not-a-pid.json").write_text("{}")  # garbage name → ignored
    monkeypatch.setattr(paths, "_pid_alive", lambda pid: pid == 23456)
    monkeypatch.setattr(paths, "_is_protoagent_pid", lambda pid: False)
    assert paths.colocated_instances() == []
    assert not (d / "12345.json").exists() and not (d / "23456.json").exists()
    assert paths.colocation_warning() is None


def test_runtime_status_carries_warnings():
    from operator_api.runtime import build_runtime_status

    s = build_runtime_status(config=None, setup_complete=False, graph_loaded=False, warnings=["sibling alert", ""])
    assert s["warnings"] == ["sibling alert"]  # empties filtered
    s2 = build_runtime_status(config=None, setup_complete=False, graph_loaded=False)
    assert s2["warnings"] == []


def test_instance_uid_stable_and_scoped(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    paths.reset_instance_paths()
    uid = paths.instance_uid()
    assert uid and paths.instance_uid() == uid  # created once, then stable
    # default instance → instance_root is box_root/default
    assert (tmp_path / "default" / ".instance-uid").read_text().strip() == uid

    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    paths.reset_instance_paths()
    scoped = paths.instance_uid()
    assert scoped and scoped != uid  # different instance root → different uid
    assert (tmp_path / "roxy" / ".instance-uid").exists()


def test_runtime_status_carries_instance_uid():
    from operator_api.runtime import build_runtime_status

    s = build_runtime_status(config=None, setup_complete=False, graph_loaded=False, instance_uid="abc123")
    assert s["instance_uid"] == "abc123"
