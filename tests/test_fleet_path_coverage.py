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


def test_run_exec_wires_home_and_instance(ws_root):
    """A member launches with ``PROTOAGENT_HOME=<ws>`` (so config at ``<ws>/config``,
    plugins at ``<ws>/plugins``, lock at ``<ws>/plugins.lock`` all derive from one
    instance root) + ``PROTOAGENT_INSTANCE=<id>`` (data-store scope). The single root
    is what deletes the old double-scope bug class."""
    s = manager.create("alpha")
    ws = ws_root / s["id"]
    env, _argv = manager.run_exec("alpha", [])
    assert env["PROTOAGENT_HOME"] == str(ws)
    assert env["PROTOAGENT_INSTANCE"] == s["id"]
    # The member's config + plugins both derive from <ws> (config/ + plugins/ siblings).
    assert (ws / "config" / "langgraph-config.yaml").exists()


# ── Path: plugin_roots_from uses the instance plugins root ───────────────────────


def test_plugin_roots_from_uses_plugins_root(tmp_path):
    """``plugin_roots_from(plugins_root)`` returns the given live plugins root (e.g.
    ``instance_paths().plugins_dir``) as the live root; a dir override wins over it.
    The bundle root is the in-tree ``app_root/plugins``."""
    from graph.plugins.pconfig import plugin_roots_from

    roots = plugin_roots_from(tmp_path / "plugins")
    assert roots[-1] == tmp_path / "plugins"
    overridden = plugin_roots_from(tmp_path / "plugins", str(tmp_path / "elsewhere"))
    assert overridden[-1] == tmp_path / "elsewhere"


# ── Path: host_config_path is BOX-tier, shared by every instance on the machine ──


def test_host_config_path_is_box_shared(monkeypatch, tmp_path):
    """``host_config_path()`` is the BOX-tier Host layer (``box_root/host-config.yaml``),
    NOT under the instance root — every instance the machine owns reads the one
    machine-wide Host config (that's the point of the layer). The settings-cascade tests
    always pin an explicit ``PROTOAGENT_HOST_CONFIG``, so this branch was never exercised."""
    import infra.paths as paths

    monkeypatch.delenv("PROTOAGENT_HOST_CONFIG", raising=False)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "hub-9")
    paths.reset_instance_paths()
    p = paths.host_config_path()
    assert p == tmp_path / "host-config.yaml"  # box-shared — NOT under the hub-9 instance root
    assert "hub-9" not in p.parts


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
    a.add_skill(
        types.SimpleNamespace(
            name="warp_jump",
            description="navigate via a warp gate",
            prompt_template="do warp",
            tools_used=(),
            source_session_id="",
        )
    )
    if hasattr(a, "close"):
        a.close()

    b = SkillsIndex(db_path=path_b)  # a DIFFERENT agent opening the same commons db
    b.initialize_db()
    assert "warp_jump" in {r["name"] for r in b.skill_summaries()}


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
