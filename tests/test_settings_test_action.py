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
        m = yaml.safe_load(Path(f"plugins/{p}/protoagent.plugin.yaml").read_text(encoding="utf-8"))
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


def test_build_schema_surfaces_ordered_settings_tab_metadata(monkeypatch):
    class FakeSch:
        plugin_id = "project_board"
        section = "project_board"
        defaults = {"coder": "", "auto_merge": False, "note": ""}
        settings_tabs = [
            {"id": "runtime", "label": "Runtime"},
            {"id": "review", "label": "Review & merge"},
        ]
        test = False

    specs = [
        {"key": "auto_merge", "label": "Auto merge", "type": "bool", "group": "Options", "tab": "review"},
        {"key": "coder", "label": "Coder", "type": "string", "group": "Options", "tab": "runtime"},
        {"key": "note", "label": "Note", "type": "text", "group": "Options"},
    ]
    monkeypatch.setattr(
        ss,
        "_plugin_field_specs",
        lambda: [(FakeSch(), f"project_board.{spec['key']}", spec["key"], spec) for spec in specs],
    )
    groups = [g for g in ss.build_schema(LangGraphConfig()) if g.get("plugin_id") == "project_board"]
    assert [g.get("settings_tab") for g in groups] == [
        {"id": "review", "label": "Review & merge", "order": 1},
        {"id": "runtime", "label": "Runtime", "order": 0},
        None,
    ]
    assert [g["section"] for g in groups] == ["Options", "Options", "Options"]
    assert {g["category"] for g in groups} == {"Plugins"}


def test_plugin_groups_with_same_display_name_never_cross_plugin_boundaries(monkeypatch):
    class First:
        plugin_id = "first"
        section = "first"
        defaults = {"enabled": False}
        settings_tabs = [{"id": "runtime", "label": "Runtime"}]
        test = False

    class Second:
        plugin_id = "second"
        section = "second"
        defaults = {"enabled": False}
        settings_tabs = [{"id": "runtime", "label": "Runtime"}]
        test = False

    spec = {"key": "enabled", "label": "Enabled", "type": "bool", "group": "Runtime", "tab": "runtime"}
    monkeypatch.setattr(
        ss,
        "_plugin_field_specs",
        lambda: [
            (First(), "first.enabled", "enabled", spec),
            (Second(), "second.enabled", "enabled", spec),
        ],
    )
    groups = [g for g in ss.build_schema(LangGraphConfig()) if g.get("plugin_id") in {"first", "second"}]
    assert len(groups) == 2
    assert {g["plugin_id"] for g in groups} == {"first", "second"}
    assert {g["fields"][0]["key"] for g in groups} == {"first.enabled", "second.enabled"}
    assert {g["category"] for g in groups} == {"Plugins"}


def test_duplicate_plugin_group_labels_keep_authored_insertion_order(monkeypatch):
    class FakeSch:
        plugin_id = "board"
        section = "board"
        defaults = {"first": "", "middle": "", "last": ""}
        settings_tabs = [
            {"id": "one", "label": "One"},
            {"id": "two", "label": "Two"},
        ]
        test = False

    specs = [
        {"key": "first", "label": "First", "group": "Runtime", "tab": "one"},
        {"key": "middle", "label": "Middle", "group": "Other"},
        {"key": "last", "label": "Last", "group": "Runtime", "tab": "two"},
    ]
    monkeypatch.setattr(
        ss,
        "_plugin_field_specs",
        lambda: [(FakeSch(), f"board.{spec['key']}", spec["key"], spec) for spec in specs],
    )
    groups = [g for g in ss.build_schema(LangGraphConfig()) if g.get("plugin_id") == "board"]
    assert [g["fields"][0]["key"] for g in groups] == ["board.first", "board.middle", "board.last"]
