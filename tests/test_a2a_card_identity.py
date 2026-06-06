"""A2A card identity is config/plugin-driven (#570) — a fork declares its
advertised skills + card description in langgraph-config.yaml or via a plugin's
register_a2a_skill, with ZERO edits to server/a2a.py. The template default
(one free-text "chat" skill) holds when nothing is provided."""

import server
import server.a2a as a2a
from graph.config import LangGraphConfig


# ── config parse ────────────────────────────────────────────────────────────

def test_config_parses_a2a_section(tmp_path):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text(
        "a2a:\n"
        "  description: Acme triage bot\n"
        "  skills:\n"
        "    - id: triage\n"
        "      name: Triage\n"
        "      description: d\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.a2a_description == "Acme triage bot"
    assert [s["id"] for s in cfg.a2a_skills] == ["triage"]


def test_config_a2a_defaults_empty(tmp_path):
    p = tmp_path / "langgraph-config.yaml"
    p.write_text("model:\n  provider: openai\n")
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.a2a_skills == [] and cfg.a2a_description == ""


# ── plugin hook ─────────────────────────────────────────────────────────────

def test_register_a2a_skill_accumulates_and_validates():
    from graph.plugins.registry import PluginRegistry

    reg = PluginRegistry.__new__(PluginRegistry)  # avoid HOST import in __init__
    reg.plugin_id = "demo"
    reg.a2a_skills = []
    reg.register_a2a_skill({"id": "s1", "name": "S1", "description": "d"})
    reg.register_a2a_skill({"name": "no-id"})          # rejected: no id
    reg.register_a2a_skill("not-a-dict")               # rejected: not a dict
    assert [s["id"] for s in reg.a2a_skills] == ["s1"]


# ── resolver precedence: config + plugin, else default ──────────────────────

def _cfg(skills=None, description=""):
    c = LangGraphConfig()
    c.a2a_skills = skills or []
    c.a2a_description = description
    return c


def test_resolver_falls_back_to_template_default(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    assert [s["id"] for s in a2a._resolved_skill_specs()] == ["chat"]  # the placeholder


def test_resolver_uses_config_then_plugin(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config",
                        _cfg(skills=[{"id": "cfg1", "name": "Cfg1", "description": "d"}]), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills",
                        [{"id": "plug1", "name": "Plug1", "description": "d"}], raising=False)
    ids = [s["id"] for s in a2a._resolved_skill_specs()]
    assert ids == ["cfg1", "plug1"]  # config first, then plugins; default dropped


def test_agent_skills_built_from_resolver(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config",
                        _cfg(skills=[{"id": "triage", "name": "Triage", "description": "d",
                                      "result_mime": "application/vnd.x-v1+json"}]), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    skills = a2a._agent_skills()
    assert skills[0].id == "triage"
    assert list(skills[0].output_modes) == ["application/vnd.x-v1+json"]
    # structured schema resolves through the same source
    monkeypatch.setattr(server.STATE, "graph_config",
                        _cfg(skills=[{"id": "triage", "name": "T", "description": "d",
                                      "result_mime": "application/vnd.x-v1+json",
                                      "output_schema": {"type": "object"}}]), raising=False)
    assert a2a.structured_skill_schema("triage") == {
        "schema": {"type": "object"}, "mime": "application/vnd.x-v1+json"}


# ── card description from config, default otherwise ─────────────────────────

def test_card_description_from_config(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config", _cfg(description="Acme triage bot"), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(server.STATE, "active_port", 7870, raising=False)
    card = a2a._build_agent_card_proto()
    assert card.description == "Acme triage bot"


def test_card_description_default(monkeypatch):
    monkeypatch.setattr(server.STATE, "graph_config", _cfg(description=""), raising=False)
    monkeypatch.setattr(server.STATE, "plugin_a2a_skills", [], raising=False)
    monkeypatch.setattr(server.STATE, "active_port", 7870, raising=False)
    card = a2a._build_agent_card_proto()
    assert card.description == a2a._DEFAULT_CARD_DESCRIPTION
