"""A turn delivers a plugin-produced file to a non-console client (#4252).

A tool saves a file to the media store and calls ``graph.sdk.attach_file(ref)``; the A2A
executor then emits it as an A2A 1.0 file part (``{"url", "mediaType", "filename"}``) on the
answer artifact, and ``/v1`` appends an absolute link plus an ``attachments`` field. Media
links are made absolute against the agent's public base (``A2A_PUBLIC_URL``, else the bound
loopback port — the same address the A2A card advertises).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from a2a_impl.executor import set_progress_hook, set_terminal_hook
from graph import sdk
from infra import media
from tests._a2a_app import build_app, drain_handlers, poll_terminal, send_msg

BASE = "https://agent.example"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import runtime.state as rs

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path / "instance"))
    monkeypatch.setenv("A2A_PUBLIC_URL", BASE + "/")
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    monkeypatch.setattr("a2a_impl.registry.FLUSH_GRACE_S", 0.02)
    set_terminal_hook(None)
    set_progress_hook(None)
    yield
    set_terminal_hook(None)
    set_progress_hook(None)


@pytest.fixture(autouse=True)
async def _drain():
    yield
    await drain_handlers()


# ── the public base + absolute links ────────────────────────────────────────────────────


def test_public_base_prefers_a2a_public_url_then_the_bound_port(monkeypatch):
    import runtime.state as rs

    assert media.public_base_url() == BASE  # trailing slash trimmed
    monkeypatch.delenv("A2A_PUBLIC_URL")
    monkeypatch.setattr(rs.STATE, "active_port", 7871, raising=False)
    assert media.public_base_url() == "http://127.0.0.1:7871"
    monkeypatch.setattr(rs.STATE, "active_port", None, raising=False)
    assert media.public_base_url() == ""


def test_the_card_and_the_media_links_share_one_base(monkeypatch):
    from server.a2a import _a2a_card_url

    assert _a2a_card_url() == f"{BASE}/a2a"


def test_media_ref_absolute_url_and_link_rewriting():
    ref = media.save_media(b"\x00" * 16, "video/mp4")
    assert ref.url.startswith("/media/") and "?sig=" in ref.url
    assert ref.absolute_url() == BASE + ref.url
    assert ref.absolute_url("http://other:1") == "http://other:1" + ref.url
    assert media.absolute_media_url("https://x.test/a.png") == "https://x.test/a.png"
    text = f"Watch [it]({ref.url}) or {BASE}{ref.url} — see /docs/media/x?sig=ab too"
    out = media.absolutize_media_links(text)
    assert f"[it]({BASE}{ref.url})" in out
    assert out.count(BASE + ref.url) == 2  # the already-absolute one isn't doubled
    assert "/docs/media/x?sig=ab" in out  # not a media-store link
    assert media.absolutize_media_links(text, base="") == text


# ── attach_file ─────────────────────────────────────────────────────────────────────────


def test_attach_outside_a_turn_is_a_no_op():
    ref = media.save_media(b"x", "image/png")
    assert sdk.attach_file(ref) is False
    assert media.turn_files() == []


def test_attach_only_takes_signed_media_store_urls():
    token = media.begin_turn_files()
    try:
        ref = media.save_media(b"x", "image/png")
        assert sdk.attach_file("https://evil.example/x.png") is False
        assert sdk.attach_file("/media/../secret") is False
        assert sdk.attach_file(ref.url.split("?")[0] + "?sig=" + "0" * 64) is False  # forged signature
        assert sdk.attach_file(ref.url, name="sheet.png") is True  # a signed URL string
        assert sdk.attach_file(ref) is True  # same file again: deduped
        files = media.turn_files()
        assert files == [{"url": BASE + ref.url, "mime": "image/png", "name": "sheet.png"}]
    finally:
        media.end_turn_files(token)
    assert media.turn_files() == []


def test_attach_caps_files_per_turn():
    token = media.begin_turn_files()
    try:
        refs = [media.save_media(bytes([i]), "image/png") for i in range(17)]
        results = [sdk.attach_file(r) for r in refs]
        assert results[:16] == [True] * 16 and results[16] is False
    finally:
        media.end_turn_files(token)


# ── the A2A round trip ──────────────────────────────────────────────────────────────────


def _tool_that_delivers_a_video() -> str:
    """A sync tool body, run in a worker thread with the turn's context copied in — the way
    LangGraph runs a sync tool."""
    ref = media.save_media(b"\x00\x00\x00\x18ftypmp42" + b"v" * 64, "video/mp4", {"title": "Overview"})
    assert sdk.attach_file(ref, name="overview.mp4") is True
    return ref.url


@pytest.mark.asyncio
async def test_a2a_answer_carries_the_file_part():
    seen: dict = {}

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        seen["url"] = await asyncio.to_thread(_tool_that_delivers_a_video)
        yield ("text", "Your video is ready.")
        yield ("done", "Your video is ready.")

    app = build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await send_msg(c)).json()["result"]["task"]
        final = await poll_terminal(c, task["id"])
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    parts = final["artifacts"][0]["parts"]
    assert parts[0]["text"] == "Your video is ready."
    files = [p for p in parts if "url" in p]
    assert files == [{"url": BASE + seen["url"], "mediaType": "video/mp4", "filename": "overview.mp4"}]
    # The absolute URL really serves the file: its signature verifies for that name.
    name = seen["url"].split("?")[0].removeprefix("/media/")
    assert media.request_allowed(f"/media/{name}", seen["url"].split("sig=")[1])


@pytest.mark.asyncio
async def test_a_turn_without_attachments_has_no_file_part_and_turns_dont_leak():
    async def with_file(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        await asyncio.to_thread(_tool_that_delivers_a_video)
        yield ("done", "one")

    async def plain(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", "two")

    for stream, expect in ((with_file, 1), (plain, 0)):
        app = build_app(stream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
            task = (await send_msg(c)).json()["result"]["task"]
            final = await poll_terminal(c, task["id"])
        assert len([p for p in final["artifacts"][0]["parts"] if "url" in p]) == expect
    assert media.turn_files() == []  # disarmed after each turn


# ── /v1 ─────────────────────────────────────────────────────────────────────────────────


def _v1_client(monkeypatch, reply_for):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.chat_routes as cr
    import runtime.state as rs

    class _Graph:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": []})

    async def _fake_chat(message, session_id, **_kw):
        return [{"role": "assistant", "content": await asyncio.to_thread(reply_for)}]

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", _Graph(), raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def test_v1_gets_absolute_links_and_an_attachments_field(monkeypatch):
    def reply():
        url = _tool_that_delivers_a_video()
        sheet = media.save_media(b"png", "image/png")
        return f"Done — [the cut]({url}); sheet at {sheet.url}"

    c = _v1_client(monkeypatch, reply)
    body = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "go"}]}).json()
    msg = body["choices"][0]["message"]
    assert "](/media/" not in msg["content"] and " /media/" not in msg["content"]
    assert msg["content"].count(BASE + "/media/") == 2
    (att,) = msg["attachments"]
    assert att["url"].startswith(BASE + "/media/") and att["mime_type"] == "video/mp4" and att["name"] == "overview.mp4"
    # The attached file's link was already in the text — no duplicate link line.
    assert msg["content"].count(att["url"]) == 1


def test_v1_appends_a_link_for_an_attachment_the_text_omits(monkeypatch):
    def reply():
        _tool_that_delivers_a_video()
        return "Your video is ready."

    c = _v1_client(monkeypatch, reply)
    body = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "go"}]}).json()
    msg = body["choices"][0]["message"]
    assert msg["content"].startswith("Your video is ready.\n\n- [overview.mp4](" + BASE + "/media/")
    streamed = c.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "go"}], "stream": True}
    ).text
    assert '"attachments"' in streamed and BASE + "/media/" in streamed


def test_v1_without_attachments_is_unchanged(monkeypatch):
    c = _v1_client(monkeypatch, lambda: "plain answer")
    msg = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "go"}]}).json()["choices"][0][
        "message"
    ]
    assert msg == {"role": "assistant", "content": "plain answer"}
