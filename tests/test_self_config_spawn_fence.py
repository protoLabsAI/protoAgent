"""`set_config`'s fence over plugin settings that name a program to SPAWN.

`delegates[].command` was fenced by section and `*.command` by leaf name, but a plugin key
like `ffmpeg_path` names a binary the plugin spawns with no leaf word to give it away. The
fence now closes that class three ways — tokenized key names, a manifest `spawns: true`
marker read from every INSTALLED plugin, and seeing through nested dict/list values — and
this suite is mostly bypass attempts against it.
"""

from __future__ import annotations

import asyncio

import pytest

from tools import lg_tools, self_edit_tools


def _tool():
    (t,) = lg_tools._build_config_editor_tool()
    return t


def _call(updates):
    return asyncio.run(_tool().ainvoke({"updates": updates}))


@pytest.fixture
def applied(monkeypatch):
    """Records every patch that reaches the write path — a refusal must leave it empty."""
    from graph.plugins import host

    seen: list = []
    monkeypatch.setattr(host.HOST, "apply_settings", lambda p: (seen.append(p), (True, []))[1], raising=False)
    return seen


@pytest.fixture
def no_markers(monkeypatch):
    monkeypatch.setattr(self_edit_tools, "_spawn_marked_keys", lambda: set())


# ── layer 1 + 2: names ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "project_board.local_gate_cmd",  # a shell command the board runs
        "palmier_pro.proxy_command",
        "learning_wiki.rh_bin",
        "pr_reviewer.clawpatch_bin",
        "cua.binary_path",
        "agent_browser.browser_args",
        "x.ffmpegExe",  # camelCase
        "x.python-interpreter",  # hyphenated
        "x.tool.command.path",  # a denied word anywhere below the section
    ],
)
def test_refuses_spawn_names_by_token(key, applied, no_markers):
    out = _call({key: "/bin/sh"})
    assert out.startswith("Refused:") and "program to run" in out, out
    assert applied == []


@pytest.mark.parametrize(
    "key",
    [
        "campaign.brand_kit_path",  # names DATA — a blanket *_path rule would break these
        "social.data_dir",
        "project_board.coder",  # choosing a provisioned executable by name
        "x.combine_mode",  # 'bin' / 'cmd' as substrings are not tokens
        "x.cabinet",
        "x.commander_name",
    ],
)
def test_data_and_selection_keys_still_apply(key, applied, no_markers):
    assert "Applied:" in _call({key: "/some/where"})
    assert len(applied) == 1


# ── layer 3: the manifest marker, read from disk ──────────────────────────────────────


def _install_campaign(tmp_path, monkeypatch, *, spawns: bool):
    """A real manifest on disk, and the fence's discovery pointed at it. The plugin is NOT
    in any enabled set: the marker must hold for an installed-but-disabled plugin too, or a
    value planted while it's off spawns the moment the operator turns it on."""
    from graph.plugins import pconfig

    root = tmp_path / "plugins"
    d = root / "campaign"
    d.mkdir(parents=True)
    marker = ", spawns: true" if spawns else ""
    (d / "protoagent.plugin.yaml").write_text(
        "id: campaign\nname: Campaign\n"
        "config: {ffmpeg_path: '', brand_kit_path: ''}\n"
        "settings:\n"
        f"  - {{key: ffmpeg_path, label: ffmpeg, type: string{marker}}}\n"
        "  - {key: brand_kit_path, label: Brand kit, type: string}\n",
        encoding="utf-8",
    )

    def _installed(*, strict=False):
        from graph.plugins.loader import discover_plugins

        every = {m.id for m in discover_plugins([root])}
        return pconfig.discover_plugin_config([root], every, set(), strict=strict)

    monkeypatch.setattr(pconfig, "installed_plugin_config_schemas", _installed)


def test_manifest_marker_fences_a_conventional_name(tmp_path, monkeypatch, applied):
    _install_campaign(tmp_path, monkeypatch, spawns=True)

    out = _call({"campaign.ffmpeg_path": "/tmp/evil"})
    assert out.startswith("Refused:") and "program to run" in out
    assert applied == []

    # the sibling DATA path in the same plugin is untouched by the marker
    assert "Applied:" in _call({"campaign.brand_kit_path": "/tmp/kit.yaml"})


def test_unmarked_conventional_name_applies(tmp_path, monkeypatch, applied):
    """Control: without the marker the same key applies — the marker is doing the work."""
    _install_campaign(tmp_path, monkeypatch, spawns=False)
    assert "Applied:" in _call({"campaign.ffmpeg_path": "/usr/bin/ffmpeg"})


@pytest.mark.parametrize(
    "updates",
    [
        {"campaign": {"ffmpeg_path": "/tmp/evil"}},  # the whole section as a dict value
        {"Campaign.FFMPEG_PATH": "/tmp/evil"},  # case games
        {" campaign . ffmpeg_path ": "/tmp/evil"},  # whitespace games
        {"campaign.ffmpeg_path.0": "/tmp/evil"},  # writing UNDER the marked key
        {"campaign.brand_kit_path": "ok", "campaign": {"ffmpeg_path": "/tmp/evil"}},  # smuggled
    ],
)
def test_bypass_attempts_on_a_marked_key_are_refused(updates, tmp_path, monkeypatch, applied):
    _install_campaign(tmp_path, monkeypatch, spawns=True)
    out = _call(updates)
    assert out.startswith("Refused:"), out
    assert applied == []


# ── nested values ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "updates",
    [
        {"some_plugin": {"command": "/bin/sh"}},  # leaf hidden in a dict value
        {"some_plugin.tool": {"nested": {"local_gate_cmd": "x"}}},  # ...at any depth
        {"some_plugin.hooks": [{"name": "a", "command": "x"}]},  # ...or inside a list of dicts
        {"tools": {"self_config_enabled": True}},  # denied sections still deny
    ],
)
def test_nested_values_cannot_smuggle_a_denied_key(updates, applied, no_markers):
    """The earlier fence only looked at the dotted keys, but `nest_updates` writes a dict
    value verbatim — `{"some_plugin": {"command": ...}}` defined an executable unopposed."""
    out = _call(updates)
    assert out.startswith("Refused:"), out
    assert applied == []


def test_nested_secret_is_refused(applied):
    out = _call({"model": {"api_key": "sk-live-123"}})
    assert out.startswith("Refused:") and "sk-live-123" not in out
    assert applied == []


def test_absurdly_deep_payload_is_refused(applied, no_markers):
    deep: dict = {}
    cur = deep
    for i in range(40):
        cur[f"k{i}"] = {}
        cur = cur[f"k{i}"]
    assert _call({"some_plugin": deep}).startswith("Refused:")
    assert applied == []


# ── fail closed ───────────────────────────────────────────────────────────────────────


def test_discovery_failure_fails_closed_for_plugin_sections(monkeypatch, applied):
    """If the fence can't tell which plugin settings spawn, a plugin-section write is refused
    — while a core write, which no plugin can mark, still goes through."""

    def _boom():
        raise RuntimeError("manifest scan failed")

    monkeypatch.setattr(self_edit_tools, "_spawn_marked_keys", _boom)

    assert _call({"campaign.ffmpeg_path": "/x"}).startswith("Refused:")
    assert applied == []
    assert "Applied:" in _call({"routing.aux_model": "claude-opus-4-6"})
