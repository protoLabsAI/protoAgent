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
                "settings": [{"key": "bot_token", "type": "secret", "label": "Token"}],
            }
        )
    )
    m = load_manifest(d)
    assert m.test is True


def test_comms_manifests_declare_test():
    # telegram uses the generic Test button via the chat_surface wirer; Discord
    # keeps its bespoke button (with a guide link), so it doesn't set test.
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
