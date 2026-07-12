"""Non-streaming chat (/api/chat + OpenAI-compat) robustness — bd-2qy.

A turn can end with no assistant text: at an ask_human interrupt, after a `wait`
yield, or on a scratch-only turn. The non-streaming path used to return a silent
empty 200 in all three cases (the streaming/A2A path handled them). These drive
the REAL graph (a fake model emitting the relevant tool call / output) through
``server.chat.chat`` and assert it never returns a blank reply.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class _ToolFake(GenericFakeChatModel):
    """Fake chat model that supports bind_tools (returns itself) so it can drop
    into create_agent and replay preset AIMessages, including tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


class _FakeJob:
    def __init__(self, next_fire: str):
        self.id = "job-1"
        self.next_fire = next_fire


class _FakeScheduler:
    def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
        return _FakeJob(schedule)

    def list_jobs(self):
        return []

    def cancel_job(self, job_id):
        return True


def _install_graph(monkeypatch, messages, scheduler=None):
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    fake = _ToolFake(messages=iter(messages))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(
            LangGraphConfig(),
            scheduler=scheduler,
            include_subagents=False,
            checkpointer=MemorySaver(),
        )
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    return g


@pytest.mark.asyncio
async def test_ask_human_interrupt_surfaces_the_question(monkeypatch):
    from server.chat import chat

    _install_graph(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ask_human",
                        "args": {"question": "What timezone are you in?"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
        ],
    )
    out = await chat("ask me my timezone", "sessA")
    content = out[0]["content"]
    assert content, "interrupt must not return an empty reply"
    assert "Input needed" in content and "timezone" in content.lower()


def test_sum_usage_folds_models_to_openai_shape():
    from server.chat import _sum_usage

    per_model = {
        "lead": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        "aux": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    }
    assert _sum_usage(per_model) == {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18}


def test_sum_usage_empty_and_total_fallback():
    from server.chat import _sum_usage

    assert _sum_usage({}) == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    # gateway omitted total_tokens → derived from prompt + completion
    assert _sum_usage({"m": {"input_tokens": 5, "output_tokens": 2}})["total_tokens"] == 7


@pytest.mark.asyncio
async def test_usage_metadata_is_summed_and_attached(monkeypatch):
    """A model turn attaches the OpenAI-shaped `usage` to the assistant dict (ADR 0075 D4).
    The fake model reports usage_metadata + model_name, which reaches the turn's
    UsageMetadataCallbackHandler through the real graph; _sum_usage folds it."""
    from server.chat import chat

    _install_graph(
        monkeypatch,
        [
            AIMessage(
                content="the answer",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                response_metadata={"model_name": "test-model"},
            ),
        ],
    )
    out = await chat("hello", "sessU")
    assert out[0]["content"] == "the answer"
    assert out[0]["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


@pytest.mark.asyncio
async def test_wait_yield_turn_falls_back_to_tool_text(monkeypatch):
    from server.chat import chat

    _install_graph(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "wait", "args": {"seconds": 30, "then": "resume"}, "id": "c1", "type": "tool_call"}
                ],
            ),
            AIMessage(content="unused"),
        ],
        scheduler=_FakeScheduler(),
    )
    out = await chat("wait a bit then resume", "sessC")
    content = out[0]["content"]
    assert content and "Wait scheduled" in content  # not a blank reply


def test_vision_human_message_gates_on_model_vision(monkeypatch, tmp_path):
    # #1943: image parts become multimodal content blocks only on a vision-capable
    # model; otherwise the image BLOCKS are dropped and the message stays plain
    # text — the same gating the streaming path applies. (config=None — e.g.
    # tests/boot — is non-vision.) Since #1969 every data: attachment is also
    # bridged into the media store and named by id in a marker note, so the note
    # rides both shapes.
    import runtime.state as rs
    from server.chat import _ATTACHMENT_REFS_MARKER, _vision_human_message

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    images = [("image/png", "data:image/png;base64,AAA=")]

    class _Cfg:
        model_vision = True

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)
    human = _vision_human_message("look", images)
    assert human.content[:2] == [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
    ]
    assert human.content[2]["type"] == "text" and _ATTACHMENT_REFS_MARKER in human.content[2]["text"]
    # Image-only turn (no text) → no empty text block; the note block still rides.
    blocks = _vision_human_message("", images).content
    assert blocks[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}}
    assert _ATTACHMENT_REFS_MARKER in blocks[1]["text"]

    _Cfg.model_vision = False
    content = _vision_human_message("look", images).content
    assert content.startswith("look") and _ATTACHMENT_REFS_MARKER in content and "image_url" not in content
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    assert _ATTACHMENT_REFS_MARKER in _vision_human_message("look", images).content
    assert _vision_human_message("look", None).content == "look"


def test_attachment_bridge_saves_to_media_store(monkeypatch, tmp_path):
    # #1969: each data: image attachment is persisted once at turn entry with
    # user_attachment provenance; the note names ids in attachment order.
    from infra.media import media_dir
    from server.chat import _bridge_attachment_ids

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    images = [
        ("image/png", "data:image/png;base64,AAA="),
        ("image/jpeg", "data:image/jpeg;base64,AAA="),
    ]
    note = _bridge_attachment_ids(images, "sess-bridge")
    assert "image 1 = `" in note and "image 2 = `" in note
    saved = sorted(p.suffix for p in media_dir().iterdir() if not p.name.startswith("."))
    assert saved == [".jpg", ".png"]
    # Provenance sidecar carries source + session.
    import json

    sidecars = [json.loads(p.read_text()) for p in media_dir().glob(".*.json")]
    assert all(s["meta"] == {"source": "user_attachment", "session_id": "sess-bridge"} for s in sidecars)


def test_attachment_bridge_skips_incognito_remote_and_garbage(monkeypatch, tmp_path):
    # Incognito turns persist nothing (ADR 0069); remote http URLs are never
    # fetched (SSRF); undecodable payloads degrade to no-note, never a crash.
    import runtime.state as rs
    from infra.media import media_dir
    from server.chat import _ATTACHMENT_REFS_MARKER, _bridge_attachment_ids, _vision_human_message

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)

    data_img = [("image/png", "data:image/png;base64,AAA=")]
    human = _vision_human_message("look", data_img, incognito=True)
    assert human.content == "look"  # no note …
    assert not list(media_dir(create=True).iterdir())  # … and nothing written

    assert _bridge_attachment_ids([("image/png", "https://example.com/x.png")]) == ""
    assert _bridge_attachment_ids([("image/png", "data:image/png;base64,%%%not-b64%%%")]) == ""
    assert _ATTACHMENT_REFS_MARKER not in _vision_human_message("look", None).content
