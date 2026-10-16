"""A turn delivers a plugin-produced file to a non-console client (#4252).

A tool saves a file to the media store and calls ``graph.sdk.attach_file(ref)``; the A2A
executor then emits it as an A2A 1.0 file part (``{"url", "mediaType", "filename"}``) on the
answer artifact, and ``/v1`` appends an absolute link plus an ``attachments`` field. Media
links are made absolute against the agent's public base (``A2A_PUBLIC_URL``, else the bound
loopback port — the same address the A2A card advertises).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from a2a_impl.executor import set_progress_hook, set_terminal_hook
from graph import sdk
from infra import media
from tests._a2a_app import build_app, drain_handlers, poll_terminal, send_msg

BASE = "https://agent.example"


def _access(url: str, now: float | None = None) -> str:
    """The media gate's verdict on a (possibly absolute) link this store minted."""
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    return media.check_access(parts.path, q.get("sig", [""])[0], q.get("exp", [""])[0], now=now)


def _exp(url: str) -> int:
    return int(parse_qs(urlsplit(url).query)["exp"][0])


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
    # ttl 0: links only made absolute (the pre-#4258 shape).
    out = media.absolutize_media_links(text, ttl_s=0)
    assert f"[it]({BASE}{ref.url})" in out
    assert out.count(BASE + ref.url) == 2  # the already-absolute one isn't doubled
    assert "/docs/media/x?sig=ab" in out  # not a media-store link
    assert media.absolutize_media_links(text, base="") == text
    # Default: the relative link leaves the box as an EXPIRING one (#4258); the absolute one
    # (not ours to rewrite) is untouched.
    out = media.absolutize_media_links(text)
    name = ref.path.name
    (expiring,) = re.findall(rf"\[it\]\(({re.escape(BASE)}/media/{name}\?exp=\d+&sig=[0-9a-f]{{64}})\)", out)
    assert _access(expiring) == "ok"
    assert out.count(BASE + ref.url) == 1


def test_a_forged_relative_link_is_made_absolute_but_never_re_signed():
    text = "see /media/abc.png?sig=" + "0" * 64
    assert media.absolutize_media_links(text) == "see " + BASE + "/media/abc.png?sig=" + "0" * 64


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
        (f,) = media.turn_files()
        assert f["mime"] == "image/png" and f["name"] == "sheet.png"
        assert f["url"].startswith(f"{BASE}/media/{ref.path.name}?exp=") and _access(f["url"]) == "ok"
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
    (file_part,) = [p for p in parts if "url" in p]
    assert file_part["mediaType"] == "video/mp4" and file_part["filename"] == "overview.mp4"
    name = seen["url"].split("?")[0].removeprefix("/media/")
    assert file_part["url"].startswith(f"{BASE}/media/{name}?exp=")
    # The absolute URL really serves the file: its signature verifies for that name, and it
    # carries the remote-link lifetime (default 7 days).
    assert _access(file_part["url"]) == "ok"
    assert abs(_exp(file_part["url"]) - (time.time() + media.DEFAULT_REMOTE_LINK_TTL_S)) < 60


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


# ── #4258: expiring links ───────────────────────────────────────────────────────────────


def test_expiring_signature_carries_exp_inside_the_hmac():
    import hashlib
    import hmac

    name = media.save_media(b"x", "image/png").path.name
    url = media.media_url(name, ttl_s=60, now=1_000_000)
    assert url.startswith(f"/media/{name}?exp=1000060&sig=")
    sig = parse_qs(urlsplit(url).query)["sig"][0]
    key = (media.media_dir() / ".signing-key").read_text().strip().encode()
    assert sig == hmac.new(key, f"{name}|exp=1000060".encode(), hashlib.sha256).hexdigest()
    path = f"/media/{name}"
    assert media.check_access(path, sig, "1000060", now=1_000_059) == "ok"
    assert media.check_access(path, sig, "1000060", now=1_000_060) == "expired"
    # The expiry can't be stretched, moved to another file, or dropped.
    assert media.check_access(path, sig, "1000061", now=0) == "deny"
    assert media.check_access(f"/media/other{name}", sig, "1000060", now=0) == "deny"
    assert media.check_access(path, sig, "", now=0) == "deny"
    # Non-canonical spellings of an instant never verify.
    for bad in ("01000060", "+1000060", "-1", "1e6", " 1000060"):
        assert media.check_access(path, sig, bad, now=0) == "deny", bad
    # A permanent (pre-#4258) link stays valid, and its signature isn't an expiring one.
    legacy = media.sign_name(name)
    assert media.check_access(path, legacy, "", now=10**12) == "ok"
    assert media.check_access(path, legacy, "1000060", now=0) == "deny"


def test_ttl_comes_from_config_and_zero_means_permanent(monkeypatch):
    import runtime.state as rs
    from types import SimpleNamespace

    ref = media.save_media(b"x", "image/png")
    monkeypatch.setattr(
        rs.STATE,
        "graph_config",
        SimpleNamespace(media_public=False, media_retention_days=0, media_remote_link_ttl_s=90),
    )
    token = media.begin_turn_files()
    try:
        assert sdk.attach_file(ref) is True
        (f,) = media.turn_files()
        assert abs(_exp(f["url"]) - (time.time() + 90)) < 5
    finally:
        media.end_turn_files(token)
    monkeypatch.setattr(
        rs.STATE, "graph_config", SimpleNamespace(media_public=False, media_retention_days=0, media_remote_link_ttl_s=0)
    )
    token = media.begin_turn_files()
    try:
        assert sdk.attach_file(ref) is True
        assert media.turn_files()[0]["url"] == BASE + ref.url  # permanent: the console's link
    finally:
        media.end_turn_files(token)
    token = media.begin_turn_files()
    try:
        assert sdk.attach_file(ref, ttl_s=30) is True  # a tool's own ttl wins
        assert abs(_exp(media.turn_files()[0]["url"]) - (time.time() + 30)) < 5
    finally:
        media.end_turn_files(token)


def test_config_parses_remote_link_ttl():
    from graph.config import LangGraphConfig

    assert LangGraphConfig().media_remote_link_ttl_s == 7 * 86400
    assert LangGraphConfig.from_dict({"media": {"remote_link_ttl_s": 60}}).media_remote_link_ttl_s == 60
    assert LangGraphConfig.from_dict({"media": {"remote_link_ttl_s": 0}}).media_remote_link_ttl_s == 0
    # Only an explicit 0 means never: null / junk / negative is the DEFAULT, past a year clamps.
    for bad in (None, -5, "soon", True, [1]):
        assert LangGraphConfig.from_dict({"media": {"remote_link_ttl_s": bad}}).media_remote_link_ttl_s == 604800, bad
    assert LangGraphConfig.from_dict({"media": {"remote_link_ttl_s": 10**9}}).media_remote_link_ttl_s == 365 * 86400
    assert LangGraphConfig.from_dict({"media": {"remote_link_ttl_s": "120"}}).media_remote_link_ttl_s == 120


def test_an_expired_link_cannot_be_attached():
    name = media.save_media(b"x", "image/png").path.name
    token = media.begin_turn_files()
    try:
        assert sdk.attach_file(media.media_url(name, ttl_s=10, now=time.time() - 100)) is False
        assert sdk.attach_file(media.media_url(name, ttl_s=600)) is True
    finally:
        media.end_turn_files(token)


@pytest.fixture()
def _gate():
    """The real default-deny middleware with a bearer, over the real media route."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from a2a_impl import auth
    from server.media import register_media_routes

    app = FastAPI()
    register_media_routes(app)
    auth.install(app, bearer_token="secret-token", api_key="", allowed_origins_raw="")
    yield TestClient(app)
    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None


def test_the_gate_serves_a_live_expiring_link_and_403s_an_expired_one(_gate):
    ref = media.save_media(b"payload", "image/png")
    name = ref.path.name
    live = _gate.get(media.media_url(name, ttl_s=600))
    assert live.status_code == 200 and live.content == b"payload"
    expired = _gate.get(media.media_url(name, ttl_s=10, now=time.time() - 100))
    assert expired.status_code == 403
    assert "expired" in expired.json()["detail"]
    # Forged / unsigned stay the default-deny 401; the permanent link still works.
    exp = str(int(time.time()) + 600)
    assert _gate.get(f"/media/{name}?exp={exp}&sig={'0' * 64}").status_code == 401
    assert _gate.get(f"/media/{name}").status_code == 401
    assert _gate.get(ref.url).status_code == 200
    # HEAD is real on the media route: same status and headers, no body.
    head = _gate.head(media.media_url(name, ttl_s=600))
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == str(len(b"payload"))
    head = _gate.head(media.media_url(name, ttl_s=10, now=time.time() - 100))
    assert head.status_code == 403 and head.content == b""
    assert _gate.head(f"/media/{name}").status_code == 401
    # A credential still admits a request whose link expired.
    r = _gate.get(
        media.media_url(name, ttl_s=10, now=time.time() - 100), headers={"Authorization": "Bearer secret-token"}
    )
    assert r.status_code == 200


# ── #4258: attachment edge cases ────────────────────────────────────────────────────────


def test_a_late_attach_after_the_answer_was_built_is_dropped():
    import contextvars

    token = media.begin_turn_files()
    background = contextvars.copy_context()  # a job spawned during the turn copies its context
    try:
        assert sdk.attach_file(media.save_media(b"a", "image/png")) is True
        delivered = media.close_turn_files()  # the answer is built
        late = media.save_media(b"b", "image/png")
        assert background.run(sdk.attach_file, late) is False
        assert sdk.attach_file(late) is False
        assert media.turn_files() == delivered and len(delivered) == 1
    finally:
        media.end_turn_files(token)
    # ...and after the turn ended outright, from the copied context too.
    assert background.run(sdk.attach_file, media.save_media(b"c", "image/png")) is False


def test_attachments_dedupe_by_content_without_a_name():
    """A re-run tool's fresh copy of the same bytes (an overflow retry) is the same attachment
    even with no ``name=`` — the default name is the per-copy filename, so only content can
    tell them apart. The latest copy's link wins."""
    token = media.begin_turn_files()
    try:
        ref = media.save_media(b"same-bytes", "image/png")
        assert sdk.attach_file(ref) is True
        assert sdk.attach_file(ref.url) is True  # same file, another link shape
        again = media.save_media(b"same-bytes", "image/png")
        assert again.path.name != ref.path.name
        assert sdk.attach_file(again) is True
        (f,) = media.turn_files()
        assert f"/media/{again.path.name}?" in f["url"]
        # Different bytes, no name: a second attachment.
        assert sdk.attach_file(media.save_media(b"other", "image/png")) is True
        assert len(media.turn_files()) == 2
    finally:
        media.end_turn_files(token)


def test_a_same_name_copy_replaces_and_entries_collapse_to_one():
    token = media.begin_turn_files()
    try:
        first = media.save_media(b"v1", "image/png")
        assert sdk.attach_file(first, name="chart.png") is True
        # A re-render with different bytes under the same explicit name replaces it.
        second = media.save_media(b"v2", "image/png")
        assert sdk.attach_file(second, name="chart.png") is True
        (f,) = media.turn_files()
        assert f["name"] == "chart.png" and f"/media/{second.path.name}?" in f["url"]
        # An entry matching TWO existing ones (same content as one, same name as another)
        # collapses them into ONE — never two entries for one file.
        other = media.save_media(b"v3", "image/png")
        assert sdk.attach_file(other) is True
        assert len(media.turn_files()) == 2
        copy = media.save_media(b"v3", "image/png")
        assert sdk.attach_file(copy, name="chart.png") is True
        (f,) = media.turn_files()
        assert f["name"] == "chart.png" and f"/media/{copy.path.name}?" in f["url"]
        # An unnamed re-attach of the same content keeps the name it was given.
        assert sdk.attach_file(media.save_media(b"v3", "image/png")) is True
        assert media.turn_files()[0]["name"] == "chart.png"
    finally:
        media.end_turn_files(token)


def test_park_closes_the_turn_and_binds_the_stash_to_its_principal():
    ctx = "ctx-" + str(time.time_ns())
    token = media.begin_turn_files()
    try:
        assert sdk.attach_file(media.save_media(b"a", "image/png")) is True
        media.park_turn_files(ctx, "principal-A")
        # An attach racing the park is refused, never reported attached and lost.
        assert sdk.attach_file(media.save_media(b"b", "image/png")) is False
    finally:
        media.end_turn_files(token)
    # Another principal naming the same conversation carries nothing — and leaves it parked.
    token = media.begin_turn_files(carry=ctx, principal="principal-B")
    try:
        assert media.turn_files() == []
    finally:
        media.end_turn_files(token)
    token = media.begin_turn_files(carry=ctx, principal="principal-A")
    try:
        assert len(media.turn_files()) == 1
    finally:
        media.end_turn_files(token)
    # Taken once: a second resume gets nothing.
    token = media.begin_turn_files(carry=ctx, principal="principal-A")
    try:
        assert media.turn_files() == []
    finally:
        media.end_turn_files(token)


def test_a_canceled_or_aged_out_park_delivers_nothing(monkeypatch):
    for how in ("cancel", "age"):
        ctx = f"ctx-{how}-{time.time_ns()}"
        token = media.begin_turn_files()
        try:
            sdk.attach_file(media.save_media(how.encode(), "image/png"))
            media.park_turn_files(ctx, "p")
        finally:
            media.end_turn_files(token)
        with monkeypatch.context() as m:
            if how == "cancel":
                media.drop_parked_turn_files(ctx)
            else:
                real = time.time
                m.setattr(media.time, "time", lambda: real() + media._PARKED_TTL_S + 1)
            token = media.begin_turn_files(carry=ctx, principal="p")
            try:
                assert media.turn_files() == [], how
            finally:
                media.end_turn_files(token)


def test_the_cap_holds_under_concurrent_attaches():
    token = media.begin_turn_files()
    try:
        refs = [media.save_media(bytes([i]), "image/png") for i in range(40)]
        barrier = threading.Barrier(len(refs))
        results: list[bool] = []
        lock = threading.Lock()
        import contextvars

        def _attach(ref):
            barrier.wait()
            ok = sdk.attach_file(ref)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=contextvars.copy_context().run, args=(_attach, r)) for r in refs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 16
        assert len(media.turn_files()) == 16
    finally:
        media.end_turn_files(token)


async def _resume(c, task, text="yes"):
    return await c.post(
        "/a2a",
        headers={"A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": "r2",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "m2",
                    "role": "ROLE_USER",
                    "taskId": task["id"],
                    "contextId": task["contextId"],
                    "parts": [{"text": text}],
                }
            },
        },
    )


@pytest.mark.asyncio
async def test_attachments_survive_a_hitl_pause_and_resume():
    seen: dict = {}

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        if resume:
            yield ("done", "Approved — here is the video.")
        else:
            seen["url"] = await asyncio.to_thread(_tool_that_delivers_a_video)
            yield ("input_required", {"question": "Publish it?"})

    app = build_app(stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await send_msg(c)).json()["result"]["task"]
        parked = await poll_terminal(c, task["id"])
        assert parked["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert (await _resume(c, task)).status_code == 200
        for _ in range(60):
            final = await poll_terminal(c, task["id"])
            if final["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            await asyncio.sleep(0.03)
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    files = [p for art in final["artifacts"] for p in art["parts"] if "url" in p]
    name = seen["url"].split("?")[0].removeprefix("/media/")
    assert len(files) == 1 and f"/media/{name}?exp=" in files[0]["url"] and _access(files[0]["url"]) == "ok"
    assert files[0]["filename"] == "overview.mp4"


@pytest.mark.asyncio
async def test_v03_wire_shape_through_the_sdk_converter():
    """A classic (no ``A2A-Version``) ``tasks/get`` renders the file part through a2a-sdk's own
    v0.3 converter: ``{kind: file, file: {uri, mimeType, name}}``."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        await asyncio.to_thread(_tool_that_delivers_a_video)
        yield ("done", "Your video is ready.")

    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from fastapi import FastAPI

    from a2a_impl.v03_compat import create_a2a_jsonrpc_routes

    # The production route builder (v0.3 compat ON) over the same handler build_app wires.
    handler = build_app(stream).state.a2a_handler
    app = FastAPI()
    add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_a2a_jsonrpc_routes(handler, rpc_url="/a2a"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
        task = (await send_msg(c)).json()["result"]["task"]
        await poll_terminal(c, task["id"])
        v03 = (
            await c.post(
                "/a2a", json={"jsonrpc": "2.0", "id": "g3", "method": "tasks/get", "params": {"id": task["id"]}}
            )
        ).json()["result"]
    parts = v03["artifacts"][0]["parts"]
    assert parts[0] == {"kind": "text", "text": "Your video is ready."}
    (fp,) = [p for p in parts if p.get("kind") == "file"]
    assert set(fp["file"]) == {"uri", "mimeType", "name"}
    assert fp["file"]["mimeType"] == "video/mp4" and fp["file"]["name"] == "overview.mp4"
    assert fp["file"]["uri"].startswith(BASE + "/media/") and _access(fp["file"]["uri"]) == "ok"


# ── #4258: the REAL /v1 turn path (real graph, fake model) ───────────────────────────────


class _Overflow(Exception):
    """Stands in for a provider's context-window overflow."""


def _real_v1_client(monkeypatch, script):
    """``/v1`` over the real ``server.chat.chat`` → ``turn_sync`` → a real LangGraph agent whose
    model replays ``script`` (an item that is an exception is raised instead) and whose one
    tool, ``render_chart``, saves a file and attaches it — run by LangGraph in a worker thread."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver
    from unittest.mock import patch

    import operator_api.chat_routes as cr
    import runtime.state as rs
    from graph.config import LangGraphConfig

    saved: list = []

    @tool
    def render_chart(title: str) -> str:
        """Render a chart and attach it to the answer."""
        ref = media.save_media(b"\x89PNG" + title.encode(), "image/png")
        saved.append(ref)
        sdk.attach_file(ref)  # no name= — dedupe must not depend on one
        return f"Rendered ![{title}]({ref.url})"

    items = iter(script)

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            from langchain_core.outputs import ChatGeneration, ChatResult

            item = next(items)
            if isinstance(item, BaseException):
                raise item
            return ChatResult(generations=[ChatGeneration(message=item)])

    fake = _Fake(messages=iter(()))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        cfg = LangGraphConfig()
        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver(), extra_tools=[render_chart])
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")

    async def _compacted(exc, thread_id, session_id):
        return isinstance(exc, _Overflow) and thread_id is not None

    import importlib

    monkeypatch.setattr(importlib.import_module("server.chat"), "_overflow_compacted", _compacted)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app), saved


def _call(name: str, cid: str, **args):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])


def test_real_v1_turn_delivers_the_attachment(monkeypatch):
    from langchain_core.messages import AIMessage

    c, saved = _real_v1_client(
        monkeypatch, [_call("render_chart", "c1", title="Q3"), AIMessage(content="Chart is ready.")]
    )
    body = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "chart Q3"}]}).json()
    msg = body["choices"][0]["message"]
    (ref,) = saved
    (att,) = msg["attachments"]
    assert att["name"] == ref.path.name and att["mime_type"] == "image/png"
    assert att["url"].startswith(f"{BASE}/media/{ref.path.name}?exp=") and _access(att["url"]) == "ok"
    assert msg["content"].startswith("Chart is ready.") and msg["content"].count(att["url"]) == 1
    assert media.turn_files() == []  # disarmed after the request


def test_real_v1_streaming_turn_delivers_the_attachment(monkeypatch):
    import json

    from langchain_core.messages import AIMessage

    c, saved = _real_v1_client(
        monkeypatch, [_call("render_chart", "c1", title="Q3"), AIMessage(content="Chart is ready.")]
    )
    streamed = c.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "chart Q3"}], "stream": True}
    ).text
    chunks = [json.loads(line[6:]) for line in streamed.splitlines() if line.startswith("data: {")]
    deltas = [ch["choices"][0]["delta"] for ch in chunks if ch.get("choices")]
    (att,) = [a for d in deltas for a in d.get("attachments", [])]
    assert f"/media/{saved[0].path.name}?exp=" in att["url"] and _access(att["url"]) == "ok"
    assert any(att["url"] in (d.get("content") or "") for d in deltas)


def test_real_v1_overflow_retry_does_not_duplicate_the_attachment(monkeypatch):
    """The tool runs, the next model call overflows, the thread is compacted and the turn retried
    (turn_sync's overflow retry): the retry re-runs the tool and saves a fresh copy — the answer
    still carries ONE attachment, the copy the retry made."""
    from langchain_core.messages import AIMessage

    c, saved = _real_v1_client(
        monkeypatch,
        [
            _call("render_chart", "c1", title="Q3"),
            _Overflow("context window exceeded"),
            _call("render_chart", "c2", title="Q3"),
            AIMessage(content="Chart is ready."),
        ],
    )
    msg = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "chart Q3"}]}).json()[
        "choices"
    ][0]["message"]
    assert len(saved) == 2, msg  # the tool really ran twice
    (att,) = msg["attachments"]
    assert f"/media/{saved[1].path.name}?" in att["url"]


async def _resume_with(c, task, headers, text="yes"):
    return await c.post(
        "/a2a",
        headers={"A2A-Version": "1.0", **headers},
        json={
            "jsonrpc": "2.0",
            "id": "r2",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "m2",
                    "role": "ROLE_USER",
                    "taskId": task["id"],
                    "contextId": task["contextId"],
                    "parts": [{"text": text}],
                }
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_principal", [True, False])
async def test_parked_attachments_go_only_to_the_principal_that_parked_them(same_principal):
    """Bearer and X-API-Key are both accepted (alternatives, #2620) — a resume presented with the
    OTHER credential is another principal, and the parked files don't follow it."""
    from a2a_impl import auth

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        if resume:
            yield ("done", "resumed")
        else:
            await asyncio.to_thread(_tool_that_delivers_a_video)
            yield ("input_required", {"question": "Publish it?"})

    app = build_app(stream, bearer="bear", api_key="key")
    bearer = {"Authorization": "Bearer bear"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10, headers=bearer
        ) as c:
            task = (await send_msg(c)).json()["result"]["task"]
            assert (await poll_terminal(c, task["id"]))["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
            other = bearer if same_principal else {"Authorization": "", "X-API-Key": "key"}
            assert (await _resume_with(c, task, other)).status_code == 200
            for _ in range(60):
                final = await poll_terminal(c, task["id"])
                if final["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                await asyncio.sleep(0.03)
    finally:
        auth._BEARER[0] = None
        auth._FEDERATION[0] = None
        auth._API_KEY[0] = ""
        auth._ALLOWED_ORIGINS[0] = None
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    files = [p for art in final["artifacts"] for p in art["parts"] if "url" in p]
    assert len(files) == (1 if same_principal else 0)


@pytest.mark.asyncio
async def test_a_fleet_members_answer_text_carries_expiring_absolute_links(monkeypatch):
    """Behind a hub (public base ``{hub}/agents/<slug>``) the text's signed links become absolute
    and EXPIRING — the only kind the hub forwards anonymously. A standalone agent's text keeps
    its permanent relative link (its console resolves that itself)."""
    ref = media.save_media(b"png", "image/png")

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("done", f"See ![chart]({ref.url})")

    async def answer_text() -> str:
        app = build_app(stream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
            task = (await send_msg(c)).json()["result"]["task"]
            final = await poll_terminal(c, task["id"])
        return final["artifacts"][0]["parts"][0]["text"]

    assert await answer_text() == f"See ![chart]({ref.url})"  # standalone: unchanged
    hub = "https://hub.example/agents/vera"
    monkeypatch.setenv("A2A_PUBLIC_URL", hub)
    text = await answer_text()
    (link,) = re.findall(r"\((https://[^)]+)\)", text)
    assert link.startswith(f"{hub}/media/{ref.path.name}?exp=")
    assert _access(link.replace("/agents/vera", "", 1)) == "ok"  # what the member's gate sees
