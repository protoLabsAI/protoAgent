"""Text-only clients get honest artifact replies (#4144, ADR 0118 S21).

A plain API client — ``/v1/chat/completions`` or ``POST /api/chat`` — receives only the reply
text: no chat view, no chip, no panel. The artifact tools used to tell it the artifact was "now
showing inline in the chat", and ``check_artifact`` answered "check again shortly", which looped
(one ``/v1`` call burned ~222k prompt tokens on two checks).

The turn's origin now rides the graph state as ``renders_ui`` (stamped every turn by the server;
absent → True), and the tools read it from injected state, the same carrier as ``session_id``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from graph import components
from graph.components import extract_component, strip_component
from tests.test_artifact_plugin import _load

TEXT_ONLY = {"session_id": "s1", "renders_ui": False}
CONSOLE = {"session_id": "s1", "renders_ui": True}


@pytest.fixture(autouse=True)
def _clean_plugin_components():
    components.set_plugin_components(None)
    yield
    components.set_plugin_components(None)


def _live(art):
    components.set_plugin_components({art._ref.ARTIFACT_REF: art._ref.validate_artifact_ref})


# ── the origin → flag mapping ────────────────────────────────────────────────────────


def test_only_the_plain_api_and_plugin_relay_origins_are_text_only():
    from server.turn_control import renders_ui

    assert renders_ui("v1") is False
    assert renders_ui("api-chat") is False
    assert renders_ui(" API-Chat ") is False
    # HOST.invoke (Discord and the other chat-surface relays) hands back only the joined
    # assistant text, so no caller of it can receive a component.
    assert renders_ui("plugin") is False
    for origin in ("local", "", None, "a2a", "scheduler", "task", "delegate_to"):
        assert renders_ui(origin) is True


def test_sdk_reads_the_flag_from_injected_state():
    from graph import sdk

    assert sdk.renders_ui({"renders_ui": False}) is False
    assert sdk.renders_ui({"renders_ui": True}) is True
    # A host that never stamps it, or no state at all: today's behaviour.
    assert sdk.renders_ui({"session_id": "s1"}) is True
    assert sdk.renders_ui(None) is True


# ── the tools, text-only ─────────────────────────────────────────────────────────────


def test_show_on_a_text_only_client_says_saved_not_shown(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    code = "<h1>Hi</h1>"
    out = art.show_artifact.invoke({"kind": "html", "code": code, "title": "Calc", "state": TEXT_ONLY})
    aid = art._read_store()["artifacts"][0]["id"]
    assert out.startswith(f'Saved html artifact {aid} "Calc" ({len(code)} chars) — this client can\'t display it.')
    assert "renders text only" in out and "don't call check_artifact" in out and "get_artifact" in out
    # Nothing that points at a view this client doesn't have, and no chip it can't receive.
    assert "inline in the chat" not in out and "panel" not in out.lower()
    assert "check again" not in out.lower()
    assert extract_component(out) is None and strip_component(out) == out
    # The artifact is still saved — the operator's console can open it later.
    assert art._read_store()["artifacts"][0]["versions"][-1]["code"] == code


def test_edits_on_a_text_only_client_say_so_too(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": TEXT_ONLY})
    aid = art._read_store()["artifacts"][0]["id"]

    out = art.update_artifact.invoke({"old_string": "a", "new_string": "b", "state": TEXT_ONLY})
    assert out.startswith(f"Updated artifact {aid} → version 2.")
    assert "renders text only" in out and extract_component(out) is None

    out = art.rewrite_artifact.invoke({"code": "<p>c</p>", "state": TEXT_ONLY})
    assert out.startswith(f"Rewrote artifact {aid} → version 3.")
    assert "renders text only" in out and extract_component(out) is None


def test_a_text_only_write_never_waits_for_a_render_verdict(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise AssertionError("a text-only turn must not wait on a viewer")

    monkeypatch.setattr(art._render_status, "_render_suffix", boom)
    monkeypatch.setattr(art._render_status, "_await_render", boom)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": TEXT_ONLY})
    out = art.check_artifact.invoke({"state": TEXT_ONLY})
    assert "no viewer on this client" in out


def test_check_on_a_text_only_client_answers_at_once_without_a_loop(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": TEXT_ONLY})
    aid = art._read_store()["artifacts"][0]["id"]
    out = art.check_artifact.invoke({"artifact_id": aid, "state": TEXT_ONLY})
    assert out == (
        f"Artifact {aid} v1: no viewer on this client (it renders text only), so the render can't "
        "be checked. Don't re-check — answer in text."
    )
    assert "check again" not in out.lower()


# ── the artifact.show service (ADR 0116) honours the flag too ────────────────────────


def test_show_service_text_only_returns_the_honest_reply_and_no_chip(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)

    def boom(*_a, **_k):
        raise AssertionError("a text-only caller must not wait on a viewer")

    monkeypatch.setattr(art._render_status, "_render_suffix", boom)
    r = art.show_service(kind="html", code="<p>x</p>", title="Chart", renders_ui=False)
    aid = art._read_store()["artifacts"][0]["id"]
    assert r["ok"] is True and r["id"] == aid and r["version"] == 1
    assert r["ref"] == ""
    assert r["message"].startswith(f'Saved html artifact {aid} "Chart" (8 chars) — this client can\'t display it.')
    assert "renders text only" in r["message"] and "inline in the chat" not in r["message"]


def test_show_service_defaults_to_the_console_reply(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    r = art.show_service(kind="html", code="<p>x</p>", title="Chart")
    assert r["ok"] is True
    assert "now showing inline in the chat" in r["message"] and "renders text only" not in r["message"]
    assert extract_component(r["ref"])["props"]["artifact_id"] == r["id"]


def test_show_service_is_resolved_with_the_flag_through_the_sdk(monkeypatch, tmp_path):
    """The path a caller like the data plugin's ``data_chart`` takes: resolve the provider via
    ``sdk.service`` and pass ``sdk.renders_ui(state)`` from its own injected state."""
    from graph import plugin_services, sdk

    art = _load(monkeypatch, tmp_path)
    plugin_services.set_plugin_services({"artifact.show": art.show_service}, None)
    try:
        r = sdk.service("artifact.show")(
            kind="svg", code="<svg/>", session_id="s1", renders_ui=sdk.renders_ui(TEXT_ONLY)
        )
    finally:
        plugin_services.set_plugin_services(None, None)
    assert r["ok"] is True and r["ref"] == "" and "can't display it" in r["message"]


# ── the tools, console: unchanged except the non-looping inline wording ─────────────


def test_console_show_is_unchanged(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    _live(art)
    code = "<h1>Hi</h1>"
    for state in (CONSOLE, None):  # an explicit True, and a host that never stamps the flag
        out = art.show_artifact.invoke({"kind": "html", "code": code, "title": "Doc", "state": state})
        aid = art._read_store()["artifacts"][0]["id"]
        assert strip_component(out) == (
            f"Created html artifact {aid} ({len(code)} chars) — now showing inline in the chat. "
            "Edit it with update_artifact(old_string, new_string) or rewrite_artifact(code)."
        )
        assert extract_component(out)["props"]["artifact_id"] == aid
        assert "renders text only" not in out


def test_console_inline_check_without_a_verdict_does_not_invite_a_loop(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>a</p>", "state": CONSOLE})
    aid = art._read_store()["artifacts"][0]["id"]
    out = art.check_artifact.invoke({"artifact_id": aid, "state": CONSOLE})
    assert out == (
        f"Artifact {aid} v1: no render result yet — it's inline in the chat, but the operator's chat "
        "isn't showing it yet, so the render can't be confirmed. Don't keep re-checking; carry on, "
        "and fix it if the operator reports a problem."
    )


# ── end to end: the real graph, a fake model, through chat() ────────────────────────


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call(name: str, args: dict, cid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])


def _install_graph(monkeypatch, messages, tools):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    fake = _ToolFake(messages=iter(messages))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(
            LangGraphConfig(), extra_tools=tools, include_subagents=False, checkpointer=MemorySaver()
        )
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


async def _snapshot(graph, session_id: str):
    from server.chat import _resolve_thread_id

    return await graph.aget_state({"configurable": {"thread_id": _resolve_thread_id(None, session_id)}})


@pytest.mark.asyncio
async def test_api_chat_turn_reaches_the_tools_as_text_only_and_a_later_turn_resets(monkeypatch, tmp_path):
    """``/api/chat`` calls ``chat(..., origin="api-chat")`` (pinned in test_chat_routes). Through
    the REAL graph, that origin reaches the artifact tools' injected state; and since the channel
    persists in the checkpointer, the next turn from a rendering origin is stamped back to True."""
    from server.chat import chat

    art = _load(monkeypatch, tmp_path)
    show = {"kind": "html", "code": "<p>calc</p>", "title": "Calc"}
    g = _install_graph(
        monkeypatch,
        [
            _call("show_artifact", show, "c1"),
            _call("check_artifact", {}, "c2"),
            AIMessage(content="Saved it as an artifact; here's the code if you want it."),
            _call("check_artifact", {}, "c3"),
            AIMessage(content="ok"),
        ],
        [art.show_artifact, art.check_artifact],
    )

    out = await chat("make me a calculator", "sessT", origin="api-chat")
    assert out[0]["content"].startswith("Saved it as an artifact")
    snap = await _snapshot(g, "sessT")
    assert snap.values.get("renders_ui") is False
    tool_out = [m.content for m in snap.values["messages"] if isinstance(m, ToolMessage)]
    assert tool_out[0].startswith("Saved html artifact") and "can't display it" in tool_out[0]
    assert "no viewer on this client" in tool_out[1]
    assert not any("check again" in t.lower() or "inline in the chat" in t for t in tool_out)

    await chat("check it", "sessT")  # the default origin renders UI
    snap = await _snapshot(g, "sessT")
    assert snap.values.get("renders_ui") is True
    last = [m.content for m in snap.values["messages"] if isinstance(m, ToolMessage)][-1]
    assert "no viewer" not in last and "inline in the chat" in last


@pytest.mark.asyncio
async def test_a_relay_plugin_turn_reaches_the_tools_as_text_only(monkeypatch, tmp_path):
    """The ADR 0018 ``HOST.invoke`` seam (Discord and the other chat-surface relays) runs
    ``chat(..., origin="plugin")`` and returns only the joined assistant text — so its turn is
    text-only, and the artifact tools say so."""
    import server.agent_init as ai

    art = _load(monkeypatch, tmp_path)
    g = _install_graph(
        monkeypatch,
        [_call("show_artifact", {"kind": "html", "code": "<p>x</p>"}, "c1"), AIMessage(content="done")],
        [art.show_artifact, art.check_artifact],
    )
    assert await ai._plugin_agent_invoke("make a thing", "discord:42") == "done"
    snap = await _snapshot(g, "discord:42")
    assert snap.values.get("renders_ui") is False
    (tool_out,) = [m.content for m in snap.values["messages"] if isinstance(m, ToolMessage)]
    assert tool_out.startswith("Saved html artifact") and "can't display it" in tool_out
