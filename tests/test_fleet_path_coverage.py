"""Coverage true-up for the fleet / path-scoping / shared-skills seams that let the
member-config double-scope bug ship (ADR 0042 / 0004 / 0041).

These guard the *interactions* the existing suite missed: `run_exec`'s plugin-dir
wiring vs where the member reads plugins, `plugin_roots_from`'s config-dir join, the
`host_config_path` scope_leaf branch, the shared-skills commons behaviorally (not just
its path string), and the deliberate ABSENCE of shared-knowledge.
"""

from __future__ import annotations

import types

import pytest

from graph.workspaces import manager


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    return tmp_path / "ws"


# ── Fleet: the create-side / run-side plugin-dir alignment (the bug's seam) ──────


def test_run_exec_wires_plugin_dirs_unscoped(ws_root):
    """A member's PROTOAGENT_PLUGINS_DIR is ``<ws>/plugins`` — UN-scoped, co-located
    with its config dir (``<ws>``). The double-scope bug nested the config under
    ``<ws>/<id>/`` while the plugins stayed at ``<ws>/plugins``, so the member's
    plugin-config resolver looked in the wrong dir. Pin them to the same root."""
    s = manager.create("alpha")
    ws = ws_root / s["id"]
    env, _argv = manager.run_exec("alpha", [])
    assert env["PROTOAGENT_CONFIG_DIR"] == str(ws)
    assert env["PROTOAGENT_PLUGINS_DIR"] == str(ws / "plugins")
    assert env["PROTOAGENT_PLUGINS_LOCK"] == str(ws / "plugins.lock")


# ── Path: plugin_roots_from joins config_dir (the wrong-dir computation) ─────────


def test_plugin_roots_from_joins_config_dir(tmp_path):
    """``plugin_roots_from(config_dir)`` resolves the live plugins root as
    ``config_dir/plugins`` — the join a nested config_dir poisoned. A dir override
    wins over the default."""
    from graph.plugins.pconfig import plugin_roots_from

    roots = plugin_roots_from(tmp_path)
    assert roots[-1] == tmp_path / "plugins"
    overridden = plugin_roots_from(tmp_path, str(tmp_path / "elsewhere"))
    assert overridden[-1] == tmp_path / "elsewhere"


# ── Path: host_config_path's scope_leaf branch (cascade tests always override) ───


def test_host_config_path_scoped_per_instance(monkeypatch):
    """``host_config_path()`` scope_leafs per instance when ``PROTOAGENT_HOST_CONFIG``
    isn't set, so co-located hubs stay isolated (#813). The settings-cascade tests
    always pin an explicit path, so this branch was never exercised."""
    import infra.paths as paths

    monkeypatch.delenv("PROTOAGENT_HOST_CONFIG", raising=False)
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "hub-9")
    p = paths.host_config_path()
    assert p.name == "host-config.yaml" and "hub-9" in p.parts


# ── Shared skills (ADR 0041): behavioral commons, not just the path string ───────


def test_commons_dir_honors_override(tmp_path):
    """``_commons_dir`` uses ``commons.path`` when set; else ~/.protoagent/commons."""
    from server.agent_init import _commons_dir

    assert _commons_dir(types.SimpleNamespace(commons_path=str(tmp_path / "shared"))) == tmp_path / "shared"
    assert _commons_dir(types.SimpleNamespace(commons_path="")).name == "commons"


def test_shared_commons_skill_visible_across_agents(tmp_path):
    """Two agents with ``skills.shared`` resolve to the SAME commons DB, so a skill one
    learns is visible to the other. Only the resolved path-STRING was tested before —
    this is the behavioral proof the commons actually works fleet-wide."""
    from server.agent_init import _resolve_skills_db
    from graph.skills.index import SkillsIndex

    commons = tmp_path / "commons"
    path_a = _resolve_skills_db("/x/skills.db", shared=True, commons=commons)
    path_b = _resolve_skills_db("/x/skills.db", shared=True, commons=commons)
    assert path_a == path_b  # one commons for the whole fleet

    a = SkillsIndex(db_path=path_a)
    a.initialize_db()
    a.add_skill(types.SimpleNamespace(
        name="warp_jump", description="navigate via a warp gate",
        prompt_template="do warp", tools_used=(), source_session_id="",
    ))
    if hasattr(a, "close"):
        a.close()

    b = SkillsIndex(db_path=path_b)  # a DIFFERENT agent opening the same commons db
    b.initialize_db()
    assert "warp_jump" in {r.name for r in b.load_skills("warp", k=10)}


# ── Shared knowledge: deliberately NOT implemented (guard against silent drift) ──


def test_shared_knowledge_is_not_implemented(tmp_path):
    """ADR 0041 mentions shared knowledge, but only shared SKILLS is built — knowledge
    stays a single per-agent DB. Pin the absence so a future shared-knowledge change is
    a deliberate addition, not an accident."""
    from graph.config import LangGraphConfig

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("skills: { shared: true }\n")
    cfg = LangGraphConfig.from_yaml(str(cfg_file))
    assert cfg.skills_shared is True
    assert not hasattr(cfg, "knowledge_shared")
    assert not hasattr(cfg, "knowledge_commons")
