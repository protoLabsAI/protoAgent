"""Tests for graph.export_op — the chat-export gesture (#2158 P1).

Two layers, mirroring test_rewind_op:
- the pure halves (``redact`` / ``render_markdown``) exercised directly;
- ``export_thread`` against a fake graph, so no host or checkpointer is needed.

The redaction tests carry the weight here: an export is meant to LEAVE the machine
(and #2179 publishes the same bundle to a public URL), so a miss is a leaked
credential, not a cosmetic bug.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from graph.export_op import export_thread, redact, render_markdown


# ── redaction ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, kind",
    [
        ("here is sk-abcdefghijklmnopqrstuvwxyz123456 ok", "openai-key"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123 ok", "github-token"),
        ("key AKIAIOSFODNN7EXAMPLE ok", "aws-access-key"),
        ("slack xoxb-1234567890-abcdefghij ok", "slack-token"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "bearer-token"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u", "jwt"),
        ("clone https://user:hunter2@github.com/x/y.git", "url-credentials"),
        ("export DATABASE_PASSWORD=hunter2brown", "secret-assignment"),
        ('config api_key: "abc123def456"', "secret-assignment"),
        ("file at /Users/jsmith/dev/notes.md", "home-path"),
    ],
)
def test_redact_catches_secret_shapes(raw, kind):
    out, kinds = redact(raw)
    assert kind in kinds, f"{kind} not detected in {raw!r} (got {kinds})"
    assert f"[redacted:{kind}]" in out or "[redacted:user]" in out


def test_redact_removes_the_actual_secret_value():
    """The point isn't the label — it's that the credential is gone."""
    out, _ = redact("use sk-abcdefghijklmnopqrstuvwxyz123456 now")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
    out2, _ = redact("export STRIPE_SECRET=sk_live_totallyrealvalue")
    assert "sk_live_totallyrealvalue" not in out2
    # A private key block is taken whole, not just its header.
    body = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    out3, kinds3 = redact(body)
    assert "MIIEowIBAAKCAQEA" not in out3 and "private-key" in kinds3


def test_redact_keeps_the_key_name_but_drops_the_value():
    """Context is worth preserving: a reader should see WHICH setting was scrubbed."""
    out, _ = redact("DATABASE_PASSWORD=hunter2brown")
    assert "DATABASE_PASSWORD" in out and "hunter2brown" not in out


def test_redact_leaves_ordinary_prose_alone():
    clean = "Let's refactor the parser and ship it on Tuesday. See docs/guides/releasing.md."
    out, kinds = redact(clean)
    assert out == clean and kinds == []


def test_redact_is_empty_safe():
    assert redact("") == ("", [])


# ── rendering ────────────────────────────────────────────────────────────────
def test_render_excludes_system_prompt():
    """The system prompt is agent configuration, not conversation — never exported."""
    md, _ = render_markdown(
        [SystemMessage(content="SECRET SYSTEM PROMPT"), HumanMessage(content="hi")],
        thread_id="t1",
    )
    assert "SECRET SYSTEM PROMPT" not in md
    assert "## User" in md and "hi" in md


def test_render_labels_roles_and_summarizes_tool_calls():
    md, _ = render_markdown(
        [
            HumanMessage(content="what's the weather"),
            AIMessage(content="checking", tool_calls=[{"name": "get_weather", "args": {"city": "NYC"}, "id": "c1"}]),
            ToolMessage(content="72F and sunny", tool_call_id="c1"),
        ],
        thread_id="t1",
    )
    assert "## User" in md and "## Assistant" in md and "## Tool result" in md
    assert "**Tool call** `get_weather`" in md  # summarized, not a raw repr
    assert "72F and sunny" in md


def test_render_flattens_multipart_content():
    md, _ = render_markdown(
        [HumanMessage(content=[{"type": "text", "text": "look at this"}, {"type": "image_url"}])],
        thread_id="t1",
    )
    assert "look at this" in md and "_[image_url]_" in md
    assert "{'type'" not in md  # never a raw Python repr


def test_render_redacts_and_discloses_what_it_scrubbed():
    md, redactions = render_markdown(
        [HumanMessage(content="my key is sk-abcdefghijklmnopqrstuvwxyz123456")],
        thread_id="t1",
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in md
    assert "openai-key" in redactions
    assert "Redacted before export" in md  # the operator-facing review affordance
    assert "not a guarantee" in md  # honest about being a safety net


def test_render_can_disable_redaction():
    md, redactions = render_markdown(
        [HumanMessage(content="sk-abcdefghijklmnopqrstuvwxyz123456")], thread_id="t1", redact_secrets=False
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" in md and redactions == []
    assert "Redacted before export" not in md


def test_render_handles_a_thread_with_nothing_shareable():
    md, _ = render_markdown([SystemMessage(content="cfg")], thread_id="t1")
    assert "no shareable messages" in md


def test_render_includes_thread_id_and_title():
    md, _ = render_markdown([HumanMessage(content="hi")], thread_id="abc123", title="My Chat")
    assert md.startswith("# My Chat")
    assert "abc123" in md


# ── the op ───────────────────────────────────────────────────────────────────
class _FakeSnapshot:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages
        self.updated = False

    async def aget_state(self, _config):
        return _FakeSnapshot(self._messages)

    async def aupdate_state(self, *_a, **_k):  # pragma: no cover - must never run
        self.updated = True
        raise AssertionError("export must never mutate the checkpoint")


def test_export_thread_returns_markdown():
    graph = _FakeGraph([HumanMessage(content="hello"), AIMessage(content="hi there")])
    out = asyncio.run(export_thread(graph, object(), "t1"))
    assert out["found"] is True and out["reason"] == "ok"
    assert out["message_count"] == 2
    assert "hello" in out["markdown"] and "hi there" in out["markdown"]
    assert graph.updated is False  # read-only


def test_export_thread_is_read_only_even_with_secrets():
    graph = _FakeGraph([HumanMessage(content="key sk-abcdefghijklmnopqrstuvwxyz123456")])
    out = asyncio.run(export_thread(graph, object(), "t1"))
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out["markdown"]
    assert out["redactions"] == ["openai-key"]
    assert graph.updated is False


def test_export_thread_without_checkpointer():
    out = asyncio.run(export_thread(None, None, "t1"))
    assert out["found"] is False and out["reason"] == "no_checkpointer" and out["markdown"] == ""


def test_export_thread_empty_is_not_an_error():
    out = asyncio.run(export_thread(_FakeGraph([]), object(), "t1"))
    assert out["found"] is False and out["reason"] == "empty_thread"
    assert out["message_count"] == 0
