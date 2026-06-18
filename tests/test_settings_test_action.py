"""Generic console Test button — manifest `test: true` → schema test endpoint (ADR 0029)."""

from __future__ import annotations

from pathlib import Path

import yaml

from graph import settings_schema as ss
from graph.config import LangGraphConfig
from graph.plugins.manifest import load_manifest


def test_manifest_parses_test_flag(tmp_path):
    d = tmp_path / "demo"
    d.mkdir()
    (d / "protoagent.plugin.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "demo",
                "name": "Demo",
                "config_section": "demo",
                "test": True,
                "guide_url": "https://example.com/setup",
                "settings": [{"key": "bot_token", "type": "secret", "label": "Token"}],
            }
        )
    )
    m = load_manifest(d)
    assert m.test is True
    assert m.guide_url == "https://example.com/setup"


def test_comms_manifests_declare_test():
    # The generic Test button (ADR 0029): telegram declares it via the chat_surface
    # wirer. (Discord ships as an external plugin now — its manifest is tested there.)
    for p in ("telegram",):
        m = yaml.safe_load(Path(f"plugins/{p}/protoagent.plugin.yaml").read_text())
        assert m.get("test") is True, p


def test_build_schema_adds_test_endpoint(monkeypatch):
    class FakeSch:
        plugin_id = "telegram"
        section = "telegram"
        defaults = {"bot_token": ""}
        test = True

    spec = {"key": "bot_token", "type": "secret", "label": "Bot token"}
    monkeypatch.setattr(ss, "_plugin_field_specs", lambda: [(FakeSch(), "telegram.bot_token", "bot_token", spec)])
    groups = ss.build_schema(LangGraphConfig())
    g = next(g for g in groups if g["section"] == "Telegram")
    assert g.get("test") == {"endpoint": "/api/config/test-telegram"}


def test_build_schema_tags_plugin_group_with_plugin_id(monkeypatch):
    # ADR 0059 — plugin groups carry plugin_id so the Plugins surface can fold the
    # config into that plugin's Installed row.
    class FakeSch:
        plugin_id = "discord"
        section = "discord"
        defaults = {"admin_ids": []}
        test = False

    spec = {"key": "admin_ids", "type": "string_list", "label": "Admins"}
    monkeypatch.setattr(ss, "_plugin_field_specs", lambda: [(FakeSch(), "discord.admin_ids", "admin_ids", spec)])
    groups = ss.build_schema(LangGraphConfig())
    g = next(g for g in groups if g["section"] == "Discord")
    assert g.get("plugin_id") == "discord"


def test_build_schema_surfaces_guide_url(monkeypatch):
    # ADR 0059 — a manifest guide_url flows to the group so the console renders a
    # generic "Setup guide" link (no per-plugin frontend).
    class FakeSch:
        plugin_id = "discord"
        section = "discord"
        defaults = {"admin_ids": []}
        test = True
        guide_url = "https://example.com/guide"

    spec = {"key": "admin_ids", "type": "string_list", "label": "Admins"}
    monkeypatch.setattr(ss, "_plugin_field_specs", lambda: [(FakeSch(), "discord.admin_ids", "admin_ids", spec)])
    g = next(g for g in ss.build_schema(LangGraphConfig()) if g["section"] == "Discord")
    assert g.get("guide_url") == "https://example.com/guide"
    assert g.get("test") == {"endpoint": "/api/config/test-discord"}
