"""The Browser panel's address bar follows the tab.

Seen on e4ab6775: the agent opened httpbin's form, filled it, submitted it and landed on
the /post echo — and the panel's address bar sat on its placeholder the whole time, with
back/forward always enabled. The screencast bridge watched ``Page.frameNavigated`` only to
re-arm the screencast; nothing told the panel WHERE the tab went.

Now each top-frame navigation asks ``Page.getNavigationHistory`` and the bridge forwards
``{t:"nav", url, title, canBack, canForward}`` to the view, which paints the bar — unless
the operator is mid-edit in it. Covered here at each seam: the pure CDP brains, the
reader loop against a scripted CDP socket, the WS route, and the view's JS (run in node).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "agent_browser"
_PKG = load_plugin(ROOT, "agent_browser")
bs = importlib.import_module(f"{_PKG.__name__}.browser_stream")
bp = importlib.import_module(f"{_PKG.__name__}.browser_panel")
NODE = shutil.which("node")


def _history(idx, *entries):
    return {"currentIndex": idx, "entries": [{"id": i, "url": u, "title": t} for i, (u, t) in enumerate(entries)]}


# ── pure brains ────────────────────────────────────────────────────────────────


def test_history_reply_becomes_the_address_bar_state():
    h = _history(1, ("about:blank", ""), ("https://httpbin.org/post", ""), ("https://httpbin.org/get", "get"))
    assert bs.nav_state_from_history(h) == {
        "url": "https://httpbin.org/post", "title": "", "canBack": True, "canForward": True}
    assert bs.nav_state_from_history(_history(0, ("https://a.test/", "A"))) == {
        "url": "https://a.test/", "title": "A", "canBack": False, "canForward": False}
    last = bs.nav_state_from_history(_history(1, ("https://a.test/", "A"), ("https://b.test/", "B")))
    assert last["canBack"] is True and last["canForward"] is False


@pytest.mark.parametrize("bad", [None, {}, {"entries": []}, {"currentIndex": 3, "entries": [{}]},
                                 {"currentIndex": "0", "entries": [{}]}, {"currentIndex": -1, "entries": [{}]}])
def test_an_unusable_history_reply_is_ignored(bad):
    assert bs.nav_state_from_history(bad) is None


def test_only_top_frame_moves_count_as_tab_navigation():
    top = {"frame": {"id": "MAIN", "url": "https://a.test/"}}
    child = {"frame": {"id": "AD", "parentId": "MAIN", "url": "https://ads.test/"}}
    assert bs.is_top_frame_nav("Page.frameNavigated", top, None)
    assert not bs.is_top_frame_nav("Page.frameNavigated", child, "MAIN")          # an iframe loading
    assert bs.is_top_frame_nav("Page.navigatedWithinDocument", {"frameId": "MAIN"}, "MAIN")  # pushState / #hash
    assert not bs.is_top_frame_nav("Page.navigatedWithinDocument", {"frameId": "AD"}, "MAIN")
    assert bs.is_top_frame_nav("Page.loadEventFired", {}, "MAIN")                 # the title has landed
    assert not bs.is_top_frame_nav("Page.frameStoppedLoading", {"frameId": "MAIN"}, "MAIN")
    assert not bs.is_top_frame_nav("Page.screencastFrame", {}, "MAIN")


# ── the reader loop against a scripted CDP socket ──────────────────────────────


class _ScriptedCDP:
    """A page-target socket: ``feed`` pushes CDP messages to the reader; ``sent`` records
    what the client wrote. Iteration ends when ``close`` is fed."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def feed(self, msg):
        self.q.put_nowait(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return json.dumps(m)

    def history_requests(self):
        return [m["id"] for m in self.sent if m["method"] == "Page.getNavigationHistory"]


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


def _stream(nav_seen):
    async def frame_cb(jpeg, md):
        pass

    async def nav_cb(state):
        nav_seen.append(state)

    s = bs.CDPStream("ws://fake", frame_cb, nav_cb=nav_cb)
    s._ws = _ScriptedCDP()
    s._lock = asyncio.Lock()
    return s


async def test_agent_navigations_reach_nav_cb_with_url_title_and_history_state():
    seen: list[dict] = []
    s = _stream(seen)
    sock = s._ws
    reader = asyncio.create_task(s._read_loop())

    # the agent's browser_open → a top-frame commit → the bridge asks where the tab is
    sock.feed({"method": "Page.frameNavigated", "params": {"frame": {"id": "MAIN", "url": "https://httpbin.org/forms/post"}}})
    await _settle()
    (rid,) = sock.history_requests()
    sock.feed({"id": rid, "result": _history(1, ("about:blank", ""), ("https://httpbin.org/forms/post", ""))})
    await _settle()
    assert seen == [{"url": "https://httpbin.org/forms/post", "title": "", "canBack": True, "canForward": False}]

    # the form submit lands on /post (a redirect's final hop looks the same)
    sock.feed({"method": "Page.frameNavigated", "params": {"frame": {"id": "MAIN", "url": "https://httpbin.org/post"}}})
    await _settle()
    rid = sock.history_requests()[-1]
    sock.feed({"id": rid, "result": _history(2, ("about:blank", ""), ("https://httpbin.org/forms/post", ""),
                                             ("https://httpbin.org/post", ""))})
    await _settle()
    assert seen[-1]["url"] == "https://httpbin.org/post"

    # an iframe navigating, or a sub-frame pushState, never asks — nor moves the bar
    n = len(sock.history_requests())
    sock.feed({"method": "Page.frameNavigated", "params": {"frame": {"id": "AD", "parentId": "MAIN", "url": "https://ads.test/"}}})
    sock.feed({"method": "Page.navigatedWithinDocument", "params": {"frameId": "AD", "url": "https://ads.test/#x"}})
    await _settle()
    assert len(sock.history_requests()) == n

    # history back → load fires with an UNCHANGED state twice: delivered once, not re-sent
    sock.feed({"method": "Page.loadEventFired", "params": {}})
    await _settle()
    rid = sock.history_requests()[-1]
    back = _history(1, ("about:blank", ""), ("https://httpbin.org/forms/post", "Form"), ("https://httpbin.org/post", ""))
    sock.feed({"id": rid, "result": back})
    sock.feed({"method": "Page.navigatedWithinDocument", "params": {"frameId": "MAIN", "url": "x"}})
    await _settle()
    sock.feed({"id": sock.history_requests()[-1], "result": back})
    await _settle()
    assert seen[-1] == {"url": "https://httpbin.org/forms/post", "title": "Form", "canBack": True, "canForward": True}
    assert len(seen) == 3

    sock.feed(None)
    await asyncio.wait_for(reader, 2)


async def test_the_screencast_still_re_arms_on_navigation():
    """The nav forwarding rides the same events the screencast re-arm uses — it must not
    swallow them (`continue` only for history replies)."""
    s = _stream([])
    sock = s._ws
    reader = asyncio.create_task(s._read_loop())
    sock.feed({"method": "Page.frameNavigated", "params": {"frame": {"id": "MAIN", "url": "https://a.test/"}}})
    await _settle()
    assert any(m["method"] == "Page.startScreencast" for m in sock.sent)
    sock.feed(None)
    await asyncio.wait_for(reader, 2)


async def test_attaching_asks_for_the_page_already_open():
    """The panel attached mid-session must show the URL right away, not after the next nav."""
    s = _stream([])
    await s.start_screencast()
    assert s._ws.history_requests()


async def test_without_a_nav_cb_no_history_is_requested():
    async def frame_cb(jpeg, md):
        pass

    s = bs.CDPStream("ws://fake", frame_cb)
    s._ws, s._lock = _ScriptedCDP(), asyncio.Lock()
    await s.start_screencast()
    assert s._ws.history_requests() == []


# ── the WS route forwards nav state to the view ────────────────────────────────


def test_stream_route_forwards_nav_state_as_a_nav_message(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class FakeCDP:
        def __init__(self, page_ws, on_frame, quality=80, nav_cb=None):
            self.nav_cb = nav_cb

        async def __aenter__(self):
            self.reader_task = asyncio.get_running_loop().create_future()   # never dies
            return self

        async def __aexit__(self, *exc):
            pass

        async def start_screencast(self):
            await self.nav_cb({"url": "https://httpbin.org/post", "title": "", "canBack": True, "canForward": False})

        async def dispatch(self, msg):
            pass

    monkeypatch.setattr(bp.browser_stream, "resolve_page_target", lambda *a, **k: ("ws://page", ""))
    monkeypatch.setattr(bp.browser_stream, "CDPStream", FakeCDP)
    app = FastAPI()
    app.include_router(bp.build_panel_data_router({}), prefix="/api/plugins/agent_browser")
    c = TestClient(app)
    ticket = c.post("/api/plugins/agent_browser/stream-ticket").json()["ticket"]
    with c.websocket_connect(f"/api/plugins/agent_browser/stream?ticket={ticket}") as ws:
        assert ws.receive_json() == {"t": "nav", "url": "https://httpbin.org/post", "title": "",
                                     "canBack": True, "canForward": False}


# ── the view: paint the bar, never clobber an operator mid-edit (run in node) ────


def _page() -> str:
    return bp._INTERACTIVE_PAGE


def _js_function(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    depth, i = 0, js.index("{", start)
    while True:
        depth += js[i] == "{"
        depth -= js[i] == "}"
        i += 1
        if depth == 0:
            return js[start:i]


def _run_view(script: str):
    if not NODE:
        pytest.skip("node not on PATH")
    page = _page()
    harness = (
        "const els={url:{value:'',title:'',id:'url'},back:{disabled:true},fwd:{disabled:true}};"
        "const $=(id)=>els[id]; const document={activeElement:null,title:'Browser'};"
        "let navState={url:'',title:'',canBack:false,canForward:false}, urlDirty=false;"
        + _js_function(page, "navView") + _js_function(page, "applyNav")
        + "const out=[]; const snap=(label)=>out.push({label,value:els.url.value,tip:els.url.title,"
          "doc:document.title,back:els.back.disabled,fwd:els.fwd.disabled,dirty:urlDirty});"
        + script + ";console.log(JSON.stringify(out));"
    )
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return {r["label"]: r for r in json.loads(out.stdout)}


@pytest.mark.platform_sensitive
def test_view_paints_url_title_and_back_forward_state():
    r = _run_view(
        "applyNav({url:'https://httpbin.org/forms/post',title:'',canBack:true,canForward:false}); snap('open');"
        "applyNav({url:'https://httpbin.org/post',title:'post',canBack:true,canForward:true}); snap('submit');"
        "applyNav({url:'about:blank',title:'',canBack:false,canForward:false}); snap('blank');"
    )
    assert r["open"]["value"] == "https://httpbin.org/forms/post"
    assert r["open"]["back"] is False and r["open"]["fwd"] is True       # .disabled
    assert r["submit"]["value"] == "https://httpbin.org/post"
    assert r["submit"]["tip"] == "post — https://httpbin.org/post" and r["submit"]["doc"] == "post · Browser"
    assert r["submit"]["fwd"] is False
    assert r["blank"]["value"] == ""             # about:blank shows the placeholder, like a real bar
    assert r["blank"]["back"] is True and r["blank"]["fwd"] is True


@pytest.mark.platform_sensitive
def test_view_never_clobbers_a_focused_edited_bar():
    r = _run_view(
        "applyNav({url:'https://a.test/',title:'A',canBack:false,canForward:false});"
        # operator clicks into the bar and types → the agent navigates meanwhile
        "document.activeElement=els.url; els.url.value='my-half-typed'; urlDirty=true;"
        "applyNav({url:'https://b.test/',title:'B',canBack:true,canForward:false}); snap('typing');"
        # focused but NOT edited (just clicked in): a nav still paints
        "urlDirty=false; applyNav({url:'https://c.test/',title:'C',canBack:true,canForward:false}); snap('focused-clean');"
        # edited, then left the bar without Enter: the next nav repaints it
        "els.url.value='abandoned'; urlDirty=true; document.activeElement=null;"
        "applyNav({url:'https://d.test/',title:'D',canBack:true,canForward:false}); snap('blurred');"
    )
    assert r["typing"]["value"] == "my-half-typed" and r["typing"]["dirty"] is True
    assert r["typing"]["back"] is False          # the buttons + tooltip still track the tab
    assert r["typing"]["tip"] == "B — https://b.test/"
    assert r["focused-clean"]["value"] == "https://c.test/"
    assert r["blurred"]["value"] == "https://d.test/" and r["blurred"]["dirty"] is False


def test_view_wires_nav_messages_and_edit_tracking():
    page = _page()
    assert 'm.t==="nav"){ applyNav(m); }' in page
    assert '$("url").addEventListener("input",()=>{ urlDirty=true; });' in page
    go = _js_function(page, "go")
    assert "urlDirty=false" in go                # Enter/Go hands the bar back to the tab
    assert 'e.key==="Escape"' in page            # Escape reverts to the current URL
    assert 'id="back"' in page and 'id="fwd"' in page
