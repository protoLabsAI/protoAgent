"""Demo-agent settings (#4609 part 3) — the three ``tools.fleet_demo.*`` config keys, their
Settings-schema fields, and the wiring that carries ``max_live`` / ``ttl_hours`` from config
into the demo-create core.

Part 1 (``graph.fleet.demo``) owns the cap + TTL as params; part 2 (``tools.fleet_demo_tools``)
owns the model-facing wrappers. This part adds the config surface that feeds them:

* r1 — the three keys parse from YAML with the stated defaults (and honor explicit values).
* r2 — with ``max_live=1`` a second create is refused, and ``ttl_hours`` reaches ``reap_expired``.
* r3 — the keys render in the Settings schema, and the ``enabled`` help names the demo-only guard.

The create path's leaves (``list_demos`` / ``reap_expired``) are faked so the cap + reap run
without a real workspace or fleet.
"""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.settings_schema import FIELDS
import graph.fleet.demo as demo
import tools.fleet_demo_tools as fdt


def _write_yaml(dir_path, body: str) -> str:
    cfg = dir_path / "langgraph-config.yaml"
    cfg.write_text(body)
    return str(cfg)


def _config(**overrides) -> LangGraphConfig:
    cfg = LangGraphConfig(identity_name="pm")
    cfg.tools_fleet_demo_enabled = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _tools(config) -> dict:
    return {t.name: t for t in fdt._build_fleet_demo_tools(config)}


# ── r1: the three keys parse from YAML with the stated defaults ──────────────────


def test_keys_default_off_and_to_the_stated_limits(tmp_path):
    cfg = LangGraphConfig.from_yaml(_write_yaml(tmp_path, "tools: {}\n"))
    assert cfg.tools_fleet_demo_enabled is False
    assert cfg.tools_fleet_demo_max_live == 3
    assert cfg.tools_fleet_demo_ttl_hours == 24


def test_defaults_hold_when_the_tools_section_is_absent_entirely(tmp_path):
    cfg = LangGraphConfig.from_yaml(_write_yaml(tmp_path, "model:\n  name: openai:gpt-4o\n"))
    assert cfg.tools_fleet_demo_enabled is False
    assert cfg.tools_fleet_demo_max_live == 3
    assert cfg.tools_fleet_demo_ttl_hours == 24


def test_keys_parse_explicit_values_from_yaml(tmp_path):
    body = (
        "tools:\n"
        "  fleet_demo:\n"
        "    enabled: true\n"
        "    max_live: 5\n"
        "    ttl_hours: 48\n"
    )
    cfg = LangGraphConfig.from_yaml(_write_yaml(tmp_path, body))
    assert cfg.tools_fleet_demo_enabled is True
    assert cfg.tools_fleet_demo_max_live == 5
    assert cfg.tools_fleet_demo_ttl_hours == 48


def test_from_dict_parses_the_fleet_demo_block():
    cfg = LangGraphConfig.from_dict({"tools": {"fleet_demo": {"enabled": True, "max_live": 2, "ttl_hours": 6}}})
    assert (cfg.tools_fleet_demo_enabled, cfg.tools_fleet_demo_max_live, cfg.tools_fleet_demo_ttl_hours) == (
        True,
        2,
        6,
    )


# ── r2: max_live=1 refuses a second create; ttl_hours flows into reap_expired ────


async def test_config_max_live_and_ttl_hours_reach_create_demo(monkeypatch):
    """The pure part-3 wiring: whatever the config says is forwarded into ``create_demo``."""
    captured: dict = {}

    def _fake_create(archetype, model_connection, model, name=None, *, created_by, max_live, ttl_hours):
        captured.update(max_live=max_live, ttl_hours=ttl_hours)
        return {"slug": "demo-x", "port": 7872, "token": "t", "archetype_ref": "basic",
                "plugin_versions": {}, "created_at": "now"}

    monkeypatch.setattr(demo, "create_demo", _fake_create)
    tools = _tools(_config(tools_fleet_demo_max_live=7, tools_fleet_demo_ttl_hours=12))
    out = await tools["fleet_demo_create"].ainvoke(
        {"archetype": "basic", "model_connection": "openai", "model": "gpt-4o"}
    )
    assert out["ok"] is True
    assert captured == {"max_live": 7, "ttl_hours": 12}


async def test_second_create_is_refused_at_max_live_1_and_ttl_reaches_reap(monkeypatch):
    """Through the REAL ``create_demo`` core: a demo already live + ``max_live=1`` → the cap
    refuses before any creation, and the config ``ttl_hours`` reached ``reap_expired`` first."""
    reaped_with: list[int] = []
    monkeypatch.setattr(demo, "reap_expired", lambda ttl_hours: reaped_with.append(ttl_hours) or [])
    # One demo is already running, so the next create is the "second" one.
    monkeypatch.setattr(demo, "list_demos", lambda: [{"id": "demo-live", "demo": True}])

    tools = _tools(_config(tools_fleet_demo_max_live=1, tools_fleet_demo_ttl_hours=9))
    out = await tools["fleet_demo_create"].ainvoke(
        {"archetype": "basic", "model_connection": "openai", "model": "gpt-4o"}
    )
    assert out["ok"] is False
    assert "demo cap" in out["error"]  # DemoCapReached message surfaced as a refusal string
    assert reaped_with == [9]  # the config TTL flowed into the reap that runs first


# ── r3: the keys render in the Settings schema with help text ────────────────────


def _field(key: str):
    return next((f for f in FIELDS if f.key == key), None)


def test_all_three_keys_are_in_the_settings_schema():
    enabled = _field("tools.fleet_demo.enabled")
    max_live = _field("tools.fleet_demo.max_live")
    ttl = _field("tools.fleet_demo.ttl_hours")
    assert enabled is not None and max_live is not None and ttl is not None

    assert enabled.attr == "tools_fleet_demo_enabled" and enabled.type == "bool"
    assert max_live.attr == "tools_fleet_demo_max_live" and max_live.type == "number"
    assert ttl.attr == "tools_fleet_demo_ttl_hours" and ttl.type == "number"
    for f in (enabled, max_live, ttl):
        assert f.section == "Tools"
        assert f.description.strip(), f"{f.key} must carry help text"


def test_enabled_help_names_the_demo_only_guard():
    enabled = _field("tools.fleet_demo.enabled")
    help_text = enabled.description.lower()
    # The help must make the safety story explicit: created-only, demo-stamped, never a real agent.
    assert "created" in help_text
    assert "demo: true" in help_text
    assert "never a real" in help_text
