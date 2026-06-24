"""Tests for the plugin chat-command seam (register_chat_command).

A plugin can own a user-only ``/<name>`` control command that short-circuits the
turn with a reply — the generalized form of the core ``/goal`` (and the soon
plugin-owned ``/issue``). The seam spans the registry (collect), the loader
(``PluginLoadResult.chat_commands`` + first-wins de-dupe), ``runtime.state`` and
``graph.slash_commands`` (resolve + precedence + palette).
"""

from __future__ import annotations

from pathlib import Path

from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins
from graph.plugins.registry import PluginRegistry

# A plugin whose register() owns a chat command. The token is mixed-case to prove
# it is slugified+lowercased ("Issue" -> "issue"). The handler passes through
# (returns None) on an empty rest, else replies — mirroring a real control command.
_CMD_PLUGIN = '''
def register(registry):
    async def _handler(rest, session_id):
        """File a GitHub issue."""
        if not rest.strip():
            return None
        return f"handled:{rest}:{session_id}"
    registry.register_chat_command("Issue", _handler)
'''


def _make_plugin(root: Path, pid: str, *, body: str, enabled: bool = True) -> Path:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: {'true' if enabled else 'false'}\n",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(body, encoding="utf-8")
    return d


def _cfg(**kw):
    return LangGraphConfig(**kw)


# --- registry-level surface --------------------------------------------------


def test_register_chat_command_slugifies_reserves_goal_and_dedupes() -> None:
    reg = PluginRegistry("p", Path("/tmp"))

    async def h(rest, session_id):
        return "first"

    async def h2(rest, session_id):
        return "second"

    reg.register_chat_command("My Cmd", h)
    assert "my-cmd" in reg.chat_commands  # slugified + lowercased

    reg.register_chat_command("goal", h)  # reserved core token — refused
    assert "goal" not in reg.chat_commands

    reg.register_chat_command("my-cmd", h2)  # same token again — keep the first
    assert reg.chat_commands["my-cmd"] is h

    reg.register_chat_command("", h)  # empty / no token — ignored
    reg.register_chat_command("bad", None)  # non-callable — ignored
    assert set(reg.chat_commands) == {"my-cmd"}


# --- loader collection -------------------------------------------------------


def test_loader_collects_chat_command(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "cmdp", body=_CMD_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])

    res = load_plugins(_cfg())
    assert set(res.chat_commands) == {"issue"}  # slugified token landed
    assert res.meta[0]["chat_commands"] == ["/issue"]  # surfaced for the console


async def test_loader_first_wins_on_collision(tmp_path, monkeypatch) -> None:
    root = tmp_path / "plugins"
    # Discovery is sorted by dir name, so "ap" is collected before "bp".
    _make_plugin(root, "ap", body=_dup_plugin("A"))
    _make_plugin(root, "bp", body=_dup_plugin("B"))
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])

    res = load_plugins(_cfg())
    assert set(res.chat_commands) == {"dup"}
    # The first plugin's handler wins; the second is dropped.
    assert await res.chat_commands["dup"]("x", "s") == "A:x"


def _dup_plugin(tag: str) -> str:
    return (
        "def register(registry):\n"
        "    async def _h(rest, session_id):\n"
        f"        return '{tag}:' + rest\n"
        "    registry.register_chat_command('dup', _h)\n"
    )


# --- resolution + precedence + palette (graph.slash_commands) ----------------


async def test_resolve_run_and_passthrough(monkeypatch) -> None:
    from graph import slash_commands as sc
    from runtime.state import STATE

    async def handler(rest, session_id):
        """File a GitHub issue."""
        return None if rest == "skip" else f"ok:{rest}:{session_id}"

    monkeypatch.setattr(STATE, "plugin_chat_commands", {"issue": handler})

    assert sc.find_plugin_chat_command("issue") is handler
    assert sc.find_plugin_chat_command("Issue") is handler  # case-insensitive
    assert sc.slash_kind("issue") == "plugin_command"

    assert await sc.run_plugin_chat_command("issue", "hello", "sess1") == "ok:hello:sess1"
    assert await sc.run_plugin_chat_command("issue", "skip", "s") is None  # handler passes through
    assert await sc.run_plugin_chat_command("nope", "x", "s") is None  # no such command


async def test_raising_handler_is_swallowed(monkeypatch) -> None:
    from graph import slash_commands as sc
    from runtime.state import STATE

    async def boom(rest, session_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(STATE, "plugin_chat_commands", {"boom": boom})
    out = await sc.run_plugin_chat_command("boom", "", "s")
    assert out.startswith("⚠️") and "boom" in out  # turn still short-circuits, no 500


def test_precedence_over_workflow_and_palette(monkeypatch) -> None:
    from graph import slash_commands as sc
    from runtime.state import STATE

    async def handler(rest, session_id):
        """File a GitHub issue."""
        return "ok"

    class _WF:
        def get(self, name):
            return {"name": name} if name in ("issue", "deploy") else None

        def list(self):
            return [{"name": "issue", "description": "wf"}, {"name": "deploy", "description": "Deploy it"}]

    monkeypatch.setattr(STATE, "plugin_chat_commands", {"issue": handler})
    monkeypatch.setattr(STATE, "workflow_registry", _WF())
    monkeypatch.setattr(STATE, "skills_index", None)

    # A plugin command outranks a same-named workflow; an unclaimed token stays a workflow.
    assert sc.slash_kind("issue") == "plugin_command"
    assert sc.slash_kind("deploy") == "workflow"

    inv = {c["name"]: c for c in sc.resolve_slash_commands()}
    assert inv["issue"]["kind"] == "plugin_command"  # not double-listed as a workflow
    assert inv["deploy"]["kind"] == "workflow"
    assert inv["issue"]["description"] == "File a GitHub issue."  # from the handler docstring
