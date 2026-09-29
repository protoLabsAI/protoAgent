"""Seam guard for the ``server/chat.py`` pre-turn dispatch extraction (#3861).

The shared pre-turn dispatch chain (``_PreTurn`` / ``_pre_turn_dispatch`` /
``_short_circuit_reply`` + the dispatch-only helpers) moved to
``server/chat_dispatch.py``; ``server.chat`` re-exports every name, so
``server.chat.<name>`` still RESOLVES — but it is a copy of the binding. A monkeypatch
of it there intercepts nothing (the drivers call ``_chat_dispatch.<name>`` and the moved
chain reads its own module's globals). This scans the suite so a stale target fails
loudly.

The moved chain also reaches its collaborators through the modules that OWN them, at
call time: the workflow / subagent / skill parsing + runners through
``server.chat_commands`` (a patch of ``server.chat._run_parsed_subagent`` — the old
spelling — would now intercept nothing, so it is guarded too), the @-mention parse +
exchange through ``server.chat_rooms``, and the tool-input coercion through
``server.chat``. Each is pinned below by patching the owner and watching the chain use
the fake.
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import pytest

import server.chat_commands as chat_commands
import server.chat_dispatch as chat_dispatch
import server.chat_rooms as chat_rooms
from runtime.state import STATE
from tests._seam_scan import stale_patches

# Every name that moved — ``server.chat`` re-exports each as the same object.
_MOVED = (
    "_FENCED_ACP_REFUSAL",
    "_PreTurn",
    "_SLASH_TOKEN_RE",
    "_lifecycle_command_reply",
    "_pre_turn_dispatch",
    "_short_circuit_reply",
    "_unknown_slash_command_reply",
)
# Collaborators the moved chain now reaches through their OWNER (not through
# ``server.chat``): a patch of these on ``server.chat`` is a patch of a copy.
_VIA_OWNER = (
    "_parse_at_delegates",
    "_parse_skill_command",
    "_parse_slash_command",
    "_parse_subagent_command",
    "_parse_workflow_command",
    "_run_parsed_subagent",
    "_run_parsed_workflow",
    "_skill_directive",
)

_SELF = Path(__file__).resolve()


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_moved_dispatch_names_on_server_chat():
    stale = stale_patches("server.chat", _MOVED + _VIA_OWNER, exclude=[_SELF])
    assert not stale, (
        "patch these on server.chat_dispatch (or the collaborator's owner, server.chat_commands / "
        "server.chat_rooms), not server.chat (#3861): " + ", ".join(stale)
    )


def test_re_exports_are_the_same_objects():
    chat = _chat()
    for name in _MOVED:
        assert getattr(chat, name) is getattr(chat_dispatch, name), name


@pytest.mark.platform_sensitive
def test_the_dispatch_module_imports_without_server_chat():
    """No import-time edge back into ``server.chat`` (it is reached at call time only)."""
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", "import server.chat_dispatch"], check=True, cwd=str(_SELF.parent.parent))
    import ast

    tree = ast.parse(Path(chat_dispatch.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "server.chat" for n in top)
    assert not any(isinstance(n, ast.Import) and any(a.name == "server.chat" for a in n.names) for n in top)
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module == "server" and any(a.name == "chat" for a in n.names) for n in top
    )


class _NoGraph:
    async def ainvoke(self, *a, **k):  # pragma: no cover — a short-circuit never reaches it
        raise AssertionError("the native turn ran")


@pytest.fixture
def quiet_state(monkeypatch):
    """Just enough STATE for the chain: no goals, no plugin commands, native runtime."""
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "plugin_chat_commands", {}, raising=False)
    monkeypatch.setattr(STATE, "graph", _NoGraph(), raising=False)
    monkeypatch.setattr(
        STATE,
        "graph_config",
        types.SimpleNamespace(agent_runtime="native", max_iterations=5, operator_mcp_tools=[], acp_agents={}),
        raising=False,
    )


# ── both drivers call the dispatch through server.chat_dispatch ───────────────


def _fake_dispatch(seen: list):
    async def _dispatch(pre, session_id, request_metadata):
        seen.append((pre.message, session_id))
        pre.handled = True
        yield ("done", "via-chat-dispatch")

    return _dispatch


async def test_streaming_driver_calls_the_dispatch_through_chat_dispatch(quiet_state, monkeypatch):
    seen: list = []
    monkeypatch.setattr(chat_dispatch, "_pre_turn_dispatch", _fake_dispatch(seen))

    frames = [f async for f in _chat()._chat_langgraph_stream_impl("hello", "s-seam")]

    assert seen == [("hello", "s-seam")]
    assert frames[-1] == ("done", "via-chat-dispatch")


async def test_nonstreaming_driver_calls_the_dispatch_and_shaper_through_chat_dispatch(quiet_state, monkeypatch):
    seen: list = []
    shaped: list = []
    monkeypatch.setattr(chat_dispatch, "_pre_turn_dispatch", _fake_dispatch(seen))

    def _shape(frame):
        shaped.append(frame)
        return [{"role": "assistant", "content": "shaped-by-chat-dispatch"}]

    monkeypatch.setattr(chat_dispatch, "_short_circuit_reply", _shape)

    out = await _chat()._chat_langgraph_impl("hello", "s-seam")

    assert seen == [("hello", "s-seam")]
    assert shaped == [("done", "via-chat-dispatch")]
    assert out == [{"role": "assistant", "content": "shaped-by-chat-dispatch"}]


async def test_nonstreaming_driver_builds_the_pre_turn_through_chat_dispatch(quiet_state, monkeypatch):
    built: list = []
    real = chat_dispatch._PreTurn

    def _pre(*a, **k):
        built.append(k.get("fence"))
        return real(*a, **k)

    monkeypatch.setattr(chat_dispatch, "_PreTurn", _pre)
    monkeypatch.setattr(chat_dispatch, "_pre_turn_dispatch", _fake_dispatch([]))

    await _chat()._chat_langgraph_impl("hello", "s-seam", tool_fence=["x"])
    [f async for f in _chat()._chat_langgraph_stream_impl("hello", "s-seam")]

    assert built == [["x"], None]


# ── the moved chain reaches its collaborators through their owners ────────────


async def _drain(message: str, **pre_kw):
    pre = chat_dispatch._PreTurn(message, **pre_kw)
    frames = [f async for f in chat_dispatch._pre_turn_dispatch(pre, "s-seam", None)]
    return pre, frames


async def test_subagent_runner_is_reached_through_chat_commands(quiet_state, monkeypatch):
    ran: list = []

    async def _run(sub_type, prompt, *, session_id=""):
        ran.append((sub_type, prompt, session_id))
        return "via-chat-commands"

    monkeypatch.setattr(chat_commands, "_parse_subagent_command", lambda m: ("researcher", "dig"))
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: None)
    monkeypatch.setattr(chat_commands, "_run_parsed_subagent", _run)

    pre, frames = await _drain("/researcher dig")

    assert ran == [("researcher", "dig", "s-seam")]
    assert pre.handled and frames[-1] == ("done", "via-chat-commands")


async def test_workflow_runner_and_tool_coercion_are_reached_through_their_owners(quiet_state, monkeypatch):
    async def _run(name, inputs, *, on_step=None):
        return f"wf:{name}"

    monkeypatch.setattr(chat_commands, "_parse_slash_command", lambda m: ("", ""))
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: ("brief", {"topic": "x"}))
    monkeypatch.setattr(chat_commands, "_run_parsed_workflow", _run)
    monkeypatch.setattr(_chat(), "_coerce_tool_value", lambda v: "COERCED")

    pre, frames = await _drain("/brief x")

    assert frames[0] == ("tool_start", {"id": "workflow:brief", "name": "workflow:brief", "input": "COERCED"})
    assert pre.handled and frames[-1] == ("done", "wf:brief")


async def test_skill_rewrite_is_reached_through_chat_commands(quiet_state, monkeypatch):
    monkeypatch.setattr(chat_commands, "_parse_slash_command", lambda m: ("", ""))
    monkeypatch.setattr(chat_commands, "_parse_workflow_command", lambda m: None)
    monkeypatch.setattr(chat_commands, "_parse_subagent_command", lambda m: None)
    monkeypatch.setattr(chat_commands, "_parse_skill_command", lambda m: ({"name": "s"}, "args"))
    monkeypatch.setattr(chat_commands, "_skill_directive", lambda skill, args: "DIRECTIVE")

    pre, frames = await _drain("/s args")

    assert frames == [] and not pre.handled
    assert pre.message == "DIRECTIVE"


async def test_mention_parse_is_reached_through_chat_rooms(quiet_state, monkeypatch):
    parsed: list = []

    def _parse(message):
        parsed.append(message)
        return None

    monkeypatch.setattr(chat_rooms, "_parse_at_delegates", _parse)

    pre, _frames = await _drain("hello there")

    assert parsed == ["hello there"]
    assert not pre.handled and pre.message == "hello there"
