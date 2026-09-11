"""ADR 0104's durable turns must carry what the operator SAID, readably (QA 2026-09-11).

Found on the released v0.164.0 desktop app. ``GET /api/chat/sessions/<id>/turns``
returned turns whose ``history`` held only the agent's status frames — six
``ROLE_AGENT`` tool frames for a two-tool turn and NO ``ROLE_USER`` message — so a
console rebuilding the chat (new device, cleared storage) drew answers with no
questions and titled the tab "New chat". The route's own tests seeded hand-written
rows that already contained a prompt, which is why nothing noticed: no test looked at
what the executor actually persists.

And a turn that narrated around a tool call came back as "…first.I'll now check…
zone.The workspace…" — the canonical text joined each model call's narration bare.

Every test here drives the REAL pipeline — ``ProtoAgentExecutor`` under the a2a-sdk
request handler (hardened registry, as production mounts it) into the durable SQLite
task store — and reads the result back through the REAL turns route.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI

from tests.test_a2a_handler import A2A_HEADERS, _build_app, _poll_terminal


@pytest.fixture(autouse=True)
async def _a2a_hygiene(monkeypatch):
    """The per-test hygiene tests/test_a2a_handler.py applies to its own apps: no
    leftover executor hooks, fast producer retirement, and each handler's cleanup
    tasks drained inside this test's loop."""
    from a2a_impl.executor import set_progress_hook, set_terminal_hook
    from tests import test_a2a_handler as h

    set_terminal_hook(None)
    set_progress_hook(None)
    monkeypatch.setattr("a2a_impl.registry.FLUSH_GRACE_S", 0.02)
    yield
    for handler in h._HANDLERS:
        tasks = set(getattr(getattr(handler, "_active_task_registry", None), "_cleanup_tasks", ()) or ())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    h._HANDLERS.clear()
    set_terminal_hook(None)
    set_progress_hook(None)


async def _durable_store(tmp_path):
    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine

    store = ReasoningCoalescingTaskStore(make_sqlite_engine(str(tmp_path / "a2a-tasks.db")))
    await store.initialize()
    return store


def _turns_app(monkeypatch, engine) -> FastAPI:
    """The operator chat routes over the given task-store engine — the reader the console
    calls to rebuild a chat."""
    import operator_api.chat_routes as cr
    import runtime.state as rs

    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return app


async def _read_turns(monkeypatch, engine, session_id: str) -> list[dict]:
    app = _turns_app(monkeypatch, engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get(f"/api/chat/sessions/{session_id}/turns")).json()
    return body["turns"]


def _message(text: str, session_id: str, *, parts: list[dict] | None = None, metadata: dict | None = None) -> dict:
    msg: dict = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "contextId": session_id,
        "parts": [{"text": text}, *(parts or [])],
    }
    if metadata:
        msg["metadata"] = metadata
    return msg


async def _send(client, message: dict) -> dict:
    r = await client.post(
        "/a2a",
        headers=A2A_HEADERS,
        json={"jsonrpc": "2.0", "id": "r1", "method": "SendMessage", "params": {"message": message}},
    )
    assert r.status_code == 200, r.text
    return await _poll_terminal(client, r.json()["result"]["task"]["id"])


def _texts(message: dict) -> list[str]:
    return [p["text"] for p in message.get("parts") or [] if p.get("text")]


@pytest.mark.asyncio
async def test_durable_turn_history_opens_with_the_operator_prompt(monkeypatch, tmp_path):
    """The live shape that lost its prompt: a turn with tool calls. The durable history
    must OPEN with the operator's message — text and metadata intact (the console
    recovers a tab's incognito flag from it) — followed by the agent's frames."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("tool_start", {"id": "call-1", "name": "list_dir", "input": '{"path": "."}'})
        yield ("tool_end", {"id": "call-1", "name": "list_dir", "output": "AGENTS.md\nCLAUDE.md"})
        yield ("text", "The workspace has two entries.")
        yield ("done", "The workspace has two entries.")

    store = await _durable_store(tmp_path)
    app = _build_app(stream, task_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        final = await _send(c, _message("List the workspace", "chat-qa", metadata={"incognito": True}))
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"

    [turn] = await _read_turns(monkeypatch, store.engine, "chat-qa")
    history = turn["history"]
    users = [m for m in history if m.get("role") == "ROLE_USER"]
    assert len(users) == 1, f"expected exactly one operator message, got roles {[m.get('role') for m in history]}"
    assert history[0] is users[0], "the prompt must OPEN the turn's history (chronology)"
    assert _texts(users[0]) == ["List the workspace"]
    assert users[0].get("metadata", {}).get("incognito") is True
    # The agent's own frames still follow — the prompt was added, nothing displaced.
    assert any(m.get("role") == "ROLE_AGENT" for m in history[1:])
    assert turn["text"] == "The workspace has two entries."
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_durable_prompt_keeps_attachment_names_but_not_their_bytes(monkeypatch, tmp_path):
    """An inline image rides the operator's message as ``raw`` bytes. The turn itself must
    still see it, but the durable copy keeps only its name and type: the SDK re-saves the
    WHOLE task on every frame, so persisted bytes would be rewritten hundreds of times a
    turn and then retained for the store's lifetime."""
    seen: dict = {}
    image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096

    async def stream(text, ctx, *, resume=False, caller_trace=None, images=None, **kwargs):
        seen["text"] = text
        seen["images"] = images
        yield ("done", "Looks like a PNG.")

    store = await _durable_store(tmp_path)
    app = _build_app(stream, task_store=store)
    attachment = {"raw": base64.b64encode(image).decode(), "mediaType": "image/png", "filename": "shot.png"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        await _send(c, _message("What is this?", "chat-img", parts=[attachment]))

    # The live turn is untouched: the model still gets the image.
    assert seen["text"] == "What is this?"
    assert [mt for mt, _ in seen["images"]] == ["image/png"]
    assert seen["images"][0][1] == "data:image/png;base64," + base64.b64encode(image).decode()

    [turn] = await _read_turns(monkeypatch, store.engine, "chat-img")
    prompt = turn["history"][0]
    assert prompt["role"] == "ROLE_USER"
    assert _texts(prompt) == ["What is this?"]
    stored = prompt["parts"][1]
    assert stored["filename"] == "shot.png" and stored["mediaType"] == "image/png"
    assert "raw" not in stored and "url" not in stored
    assert stored["metadata"]["omittedBytes"] == len(image)
    # And nowhere else in the row either.
    assert base64.b64encode(image).decode()[:64] not in json.dumps(turn)
    await store.engine.dispose()


# ── The transcript keeps what the OPERATOR saw, not what the model was handed ───────


async def _one_turn(monkeypatch, tmp_path, message: dict, **executor_kwargs) -> tuple[dict, dict]:
    """Run one real turn for ``message``; return (the durable turn, what the model got)."""
    seen: dict = {}

    async def stream(text, ctx, *, resume=False, caller_trace=None, images=None, **kwargs):
        seen["text"] = text
        seen["images"] = images
        yield ("done", "ok")

    store = await _durable_store(tmp_path)
    app = _build_app(stream, task_store=store, **executor_kwargs)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        await _send(c, message)
    [turn] = await _read_turns(monkeypatch, store.engine, message["contextId"])
    await store.engine.dispose()
    return turn, seen


@pytest.mark.asyncio
async def test_an_attachment_send_stores_the_bubble_not_the_document_dump(monkeypatch, tmp_path):
    """The console prepends each attachment's extracted text for the MODEL (up to the
    inline budget per file, any number of files) but the bubble shows the typed text + a
    📎 list, and records that bubble as ``display``. Storing the model-facing text had the
    SDK rewrite ~32KB of it on every save of a long turn (60% of everything written)."""
    dumps = [f"[Attached file: f{i}.md]\n{'lorem ipsum ' * 700}\n[end of f{i}.md]" for i in range(4)]
    sent = "\n\n".join([*dumps, "Summarize these."])
    bubble = "Summarize these.\n\nAttached: f0.md, f1.md, f2.md, f3.md"
    image = {"raw": base64.b64encode(b"\x89PNG" + b"\x00" * 512).decode(), "mediaType": "image/png", "filename": "a.png"}
    turn, seen = await _one_turn(
        monkeypatch, tmp_path, _message(sent, "chat-att", parts=[image], metadata={"display": bubble, "incognito": True})
    )

    assert seen["text"] == sent  # the model still gets every document
    prompt = turn["history"][0]
    assert _texts(prompt) == [bubble]
    assert prompt["parts"][1]["filename"] == "a.png" and "raw" not in prompt["parts"][1]
    assert prompt["metadata"] == {"incognito": True}  # the text IS the display — not stored twice
    assert len(json.dumps(prompt)) < 1024


@pytest.mark.asyncio
async def test_a_hidden_send_keeps_its_metadata_but_no_text(monkeypatch, tmp_path):
    """A dismissal/approval resume, a regenerate or a goal kickoff drew no bubble, so the
    transcript keeps no text for it — but keeps the message, because its metadata carries
    the per-message incognito stamp a rebuilt tab recovers."""
    dismissal = "[dismissed] The operator dismissed this request without providing input."
    turn, seen = await _one_turn(
        monkeypatch,
        tmp_path,
        _message(dismissal, "chat-hidden", metadata={"hidden": True, "hitl_resume": True, "incognito": True}),
    )
    assert seen["text"] == dismissal
    [prompt] = [m for m in turn["history"] if m.get("role") == "ROLE_USER"]
    assert not prompt.get("parts")
    assert prompt["metadata"] == {"hidden": True, "hitl_resume": True, "incognito": True}


@pytest.mark.parametrize(
    ("origin", "kept"),
    [
        ("scheduler", False),  # server-fired: machine text, never a bubble
        ("background-resume", False),
        ("a2a", True),  # a peer agent delegating: its request IS the conversation
    ],
)
@pytest.mark.asyncio
async def test_a_server_fired_turn_stores_no_prompt(monkeypatch, tmp_path, origin, kept):
    from server.chat import is_autonomous_origin  # the predicate production wires in

    prompt = "[Autonomous wake — scheduled run. Orient from <working_state>, then:]\n\ncheck the deploy"
    turn, seen = await _one_turn(
        monkeypatch,
        tmp_path,
        _message(prompt, "chat-fired", metadata={"origin": origin}),
        server_fired_origin=is_autonomous_origin,
    )
    assert seen["text"] == prompt
    users = [m for m in turn["history"] if m.get("role") == "ROLE_USER"]
    assert [_texts(m) for m in users] == ([[prompt]] if kept else [])


@pytest.mark.asyncio
async def test_a_data_url_attachment_is_elided_but_a_link_is_kept(monkeypatch, tmp_path):
    """An image can also ride inline as a ``data:`` URL — the same payload, just spelled as
    a URL — while an ``http(s)`` URL is only a reference."""
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"\x01" * 2048).decode()
    link = "https://example.com/diagram.png"
    turn, seen = await _one_turn(
        monkeypatch,
        tmp_path,
        _message(
            "Compare these",
            "chat-urls",
            parts=[
                {"url": data_url, "mediaType": "image/png", "filename": "inline.png"},
                {"url": link, "mediaType": "image/png", "filename": "linked.png"},
            ],
        ),
    )
    assert [uri for _, uri in seen["images"]] == [data_url, link]  # the turn sees both
    inline, linked = turn["history"][0]["parts"][1:]
    assert "url" not in inline and inline["metadata"]["omittedBytes"] == len(data_url)
    assert inline["filename"] == "inline.png"
    assert linked["url"] == link and "omittedBytes" not in (linked.get("metadata") or {})


@pytest.mark.asyncio
async def test_a_giant_paste_is_capped_in_the_transcript_not_in_the_turn(monkeypatch, tmp_path):
    from a2a_impl.executor import _TRANSCRIPT_PROMPT_MAX_CHARS as cap

    pasted = "".join(f"line {i:05d} of a pasted log\n" for i in range(1500))
    assert len(pasted) > cap
    turn, seen = await _one_turn(monkeypatch, tmp_path, _message(pasted, "chat-paste"))
    assert seen["text"] == pasted
    [stored] = _texts(turn["history"][0])
    assert stored.startswith(pasted[:cap])
    assert stored.endswith(f"… [{len(pasted) - cap:,} more characters not kept]")


# ── Paragraphs between model calls: the real native turn, end to end ──────────────


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("a.", "b", "\n\n"),
        ("a.\n", "b", "\n"),
        ("a.", "\nb", "\n"),
        ("a.\n\n", "b", ""),
        ("a.\n", "\nb", ""),
    ],
)
def test_paragraph_break_counts_the_newlines_already_there(before, after, expected):
    from server.chat import _paragraph_break

    assert _paragraph_break(before, after) == expected


class _ScriptedModel:
    """Built lazily so the langchain imports stay out of module import time."""

    @staticmethod
    def build(messages):
        import itertools

        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage, AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        class _Fake(GenericFakeChatModel):
            """Replays scripted AIMessages over the STREAMING path the way a real provider
            delivers them: the narration first, then the tool call as its own chunk."""

            def bind_tools(self, tools, **kwargs):
                return self

            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                msg = next(self.messages)
                await asyncio.sleep(0)
                if msg.content:
                    yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))
                calls = getattr(msg, "tool_calls", None) or []
                if calls:
                    yield ChatGenerationChunk(
                        message=AIMessageChunk(
                            content="",
                            tool_call_chunks=[
                                {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                                for i, tc in enumerate(calls)
                            ],
                        )
                    )

        tail = itertools.repeat(AIMessage(content="(extra step)"))
        return _Fake(messages=itertools.chain(iter(messages), tail))


def _install_native_graph(monkeypatch, messages):
    """A real agent graph (real middleware, real ToolNode) over a scripted model."""
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    fake = _ScriptedModel.build(messages)
    monkeypatch.setattr("graph.agent.create_llm", lambda *a, **k: fake)
    from graph.agent import create_agent_graph

    graph = create_agent_graph(LangGraphConfig(), include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)


async def _stream_turn(client, message: dict) -> list[dict]:
    """POST SendStreamingMessage and return every result frame, in order."""
    frames: list[dict] = []
    async with client.stream(
        "POST",
        "/a2a",
        headers=A2A_HEADERS,
        json={"jsonrpc": "2.0", "id": "s1", "method": "SendStreamingMessage", "params": {"message": message}},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[5:].strip()).get("result") or {})
    return frames


def _console_text(frames: list[dict]) -> tuple[str, str]:
    """(streamed, canonical) exactly as the console sees them: ``streamed`` accumulates
    the answer frames that arrive before the terminal one (``append`` true extends, an
    absent flag starts over); ``canonical`` is the terminal replace's full text. The
    console keeps a turn's text↔tool interleaving only while the two agree
    (``apps/web/src/chat/parts.ts`` replaceText) — so a fix that separated only the
    canonical copy would collapse every live tool turn at its end."""
    streamed, canonical = "", None
    for frame in frames:
        update = frame.get("artifactUpdate")
        if not update:
            continue
        text = "".join(p.get("text", "") for p in update["artifact"].get("parts") or [])
        if update.get("lastChunk"):
            canonical = text
        elif update.get("append") is True:
            streamed += text
        else:
            streamed = text
    assert canonical is not None, "no terminal answer frame"
    return streamed, canonical


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("I'll check the time first.", "It is noon."),
        # A model that already ends or opens its narration with a newline must still get
        # exactly ONE blank line — not a second break stacked on its own.
        ("I'll check the time first.\n", "It is noon."),
        ("I'll check the time first.", "\nIt is noon."),
        ("I'll check the time first.", "\n\nIt is noon."),
    ],
)
@pytest.mark.asyncio
async def test_narration_around_a_tool_call_stays_separate_paragraphs(monkeypatch, tmp_path, before, after):
    """The model says one thing, calls a tool, then says the next thing — two model calls.
    Durable and live text must read "first.\\n\\nIt is", never "first.It is", and the
    live stream must carry the SAME break the stored text does."""
    from langchain_core.messages import AIMessage

    from server.chat import _chat_langgraph_stream

    _install_native_graph(
        monkeypatch,
        [
            AIMessage(
                content=before,
                tool_calls=[{"name": "current_time", "args": {"timezone": "UTC"}, "id": "c1", "type": "tool_call"}],
            ),
            AIMessage(content=after),
        ],
    )
    store = await _durable_store(tmp_path)
    app = _build_app(_chat_langgraph_stream, task_store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30) as c:
        frames = await _stream_turn(c, _message("What time is it?", "chat-sep"))

    streamed, canonical = _console_text(frames)
    assert canonical == "I'll check the time first.\n\nIt is noon."
    assert streamed == canonical, f"live stream {streamed!r} diverges from the terminal text {canonical!r}"

    [turn] = await _read_turns(monkeypatch, store.engine, "chat-sep")
    assert turn["state"] == "TASK_STATE_COMPLETED"
    assert turn["text"] == "I'll check the time first.\n\nIt is noon."
    assert _texts(turn["history"][0]) == ["What time is it?"]
    await store.engine.dispose()
