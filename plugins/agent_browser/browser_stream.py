"""Interactive browser streaming — a CDP screencast + input bridge.

The `interactive` panel mode renders a live, *drivable* viewport instead of a
screenshot poll. It works by attaching a **second CDP client** to the same Chrome
that agent-browser drives (the CLI hands us the endpoint via `agent-browser get
cdp-url`), running **`Page.startScreencast`** for event-driven JPEG frames, and
forwarding operator input back with **`Input.dispatch*`**. A second CDP client
coexists with agent-browser's own session — the agent and the operator can both
touch the page.

Everything is bridged to the panel over a **gated same-origin WebSocket**
(`/api/plugins/agent_browser/stream`), so it inherits the operator bearer gate and
rides the fleet proxy — the interactive viewport works on a remote member, not just
the host (the thing the `full` dashboard embed never could).

This module is split so the CDP-facing brains are pure and host-free-testable:
``_http_base_from_ws`` / ``pick_page_target`` / ``input_to_cdp`` have no IO. The
``CDPStream`` async client and ``resolve_page_target`` do the talking; the WebSocket
route lives in ``browser_panel``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import subprocess
import time
import urllib.request

log = logging.getLogger("protoagent.plugins.agent_browser")


# ── WebSocket auth: single-use tickets ─────────────────────────────────────────
# The host's operator-bearer gate is HTTP-only middleware — it does NOT cover
# WebSocket handshakes (a browser `new WebSocket()` can't send an Authorization
# header anyway). So the stream WS gates itself: the panel first calls the *gated*
# `POST /stream-ticket` (bearer-checked by the host, so only an authenticated
# console gets one), then presents the ticket on the WS URL. This mirrors the host's
# own SSE-token escape hatch for `/api/events`. Short-lived + single-use so a ticket
# leaked into a proxy/access log is near-worthless.
_TICKET_TTL = 30.0
_tickets: dict[str, float] = {}


def _prune_tickets(now: float) -> None:
    for k in [k for k, exp in _tickets.items() if exp < now]:
        _tickets.pop(k, None)


def mint_ticket() -> str:
    """Issue a single-use ticket good for ~30s. Called only from the gated HTTP
    route, so possession of a ticket proves the caller cleared the operator gate."""
    now = time.monotonic()
    _prune_tickets(now)
    t = secrets.token_urlsafe(24)
    _tickets[t] = now + _TICKET_TTL
    return t


def consume_ticket(ticket: str) -> bool:
    """Validate + burn a ticket. False if unknown/expired (→ reject the WS)."""
    now = time.monotonic()
    _prune_tickets(now)
    exp = _tickets.pop(ticket, None) if ticket else None
    return exp is not None and exp >= now


# ── pure helpers (host-free-testable — no IO) ──────────────────────────────────

def _http_base_from_ws(ws_url: str) -> str:
    """`ws://127.0.0.1:52886/devtools/browser/…` → `http://127.0.0.1:52886`. The
    CDP HTTP endpoints (`/json/list`, `/json/version`) live at the same host:port."""
    rest = ws_url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    return "http://" + authority


def pick_page_target(targets: list[dict], current_url: str = "") -> str | None:
    """Choose which CDP target to stream from a `/json/list` array. Prefer the
    `page` matching the session's current URL (the active tab); else the first real
    `page` (skipping chrome:// / devtools surfaces). Returns its
    `webSocketDebuggerUrl`, or None if there's no page to stream."""
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    cur = (current_url or "").strip()
    if cur:
        for t in pages:
            if t.get("url", "") == cur:
                return t["webSocketDebuggerUrl"]
    real = [t for t in pages if not t.get("url", "").startswith(("chrome://", "devtools://"))]
    return (real or pages)[0]["webSocketDebuggerUrl"]


# Modifier bit-mask CDP expects on Input events (Alt=1, Ctrl=2, Meta=4, Shift=8).
def _modifiers(m: dict) -> int:
    return ((1 if m.get("alt") else 0) | (2 if m.get("ctrl") else 0)
            | (4 if m.get("meta") else 0) | (8 if m.get("shift") else 0))


_MOUSE_TYPE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}


def input_to_cdp(msg: dict) -> tuple[str, dict] | None:
    """Translate a panel input message → a `(cdp_method, params)` pair, or None if
    it isn't a drivable input. Coordinates arrive already in CSS pixels (the client
    maps canvas→page against the frame metadata), so we pass them straight through.

    Panel messages:
      {t:"mouse", action:"down|up|move", x, y, button?, clickCount?, buttons?, mods…}
      {t:"wheel", x, y, dx, dy, mods…}
      {t:"key",   action:"down|up", key, code?, text?, keyCode?, mods…}
    """
    t = msg.get("t")
    if t == "mouse":
        typ = _MOUSE_TYPE.get(msg.get("action", ""))
        if not typ:
            return None
        p = {"type": typ, "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0)),
             "modifiers": _modifiers(msg)}
        if typ != "mouseMoved":
            p["button"] = msg.get("button", "left")
            p["clickCount"] = int(msg.get("clickCount", 1))
        p["buttons"] = int(msg.get("buttons", 0))
        return "Input.dispatchMouseEvent", p
    if t == "wheel":
        return "Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": float(msg.get("x", 0)), "y": float(msg.get("y", 0)),
            "deltaX": float(msg.get("dx", 0)), "deltaY": float(msg.get("dy", 0)),
            "modifiers": _modifiers(msg)}
    if t == "key":
        typ = {"down": "keyDown", "up": "keyUp"}.get(msg.get("action", ""))
        if not typ:
            return None
        text = msg.get("text", "")
        # A keyDown that produces a character must be dispatched as "keyDown" with
        # text; CDP turns text-bearing keyDowns into the actual input.
        p = {"type": typ, "key": msg.get("key", ""), "code": msg.get("code", ""),
             "modifiers": _modifiers(msg)}
        if msg.get("keyCode"):
            p["windowsVirtualKeyCode"] = int(msg["keyCode"])
            p["nativeVirtualKeyCode"] = int(msg["keyCode"])
        if typ == "keyDown" and text:
            p["text"] = text
        return "Input.dispatchKeyEvent", p
    return None


def nav_state_from_history(result: dict | None) -> dict | None:
    """``Page.getNavigationHistory`` result → the panel's address-bar state:
    ``{url, title, canBack, canForward}``, or None when the reply is unusable. The
    history is the one source that answers all four at once — the URL the tab is
    actually on (after a redirect, a form POST, a link click or a back/forward), its
    title, and whether back/forward have anywhere to go."""
    if not isinstance(result, dict):
        return None
    entries = result.get("entries")
    idx = result.get("currentIndex")
    if not isinstance(entries, list) or not isinstance(idx, int) or not 0 <= idx < len(entries):
        return None
    cur = entries[idx] if isinstance(entries[idx], dict) else {}
    return {"url": str(cur.get("url") or ""), "title": str(cur.get("title") or ""),
            "canBack": idx > 0, "canForward": idx < len(entries) - 1}


def is_top_frame_nav(method: str, params: dict, main_frame_id: str | None) -> bool:
    """Does this CDP event move the TAB (not an iframe)? ``frameNavigated`` for a frame
    with no ``parentId`` is the top document (a load, a redirect's final hop, a form
    submit, a cross-document back/forward); ``navigatedWithinDocument`` is a same-document
    move (pushState, #fragment, a same-document back/forward) and counts only for the main
    frame — an ad iframe's pushState must not repaint the address bar. ``loadEventFired``
    is page-level, and is when the title is reliably set."""
    if method == "Page.frameNavigated":
        frame = params.get("frame") or {}
        return not frame.get("parentId")
    if method == "Page.navigatedWithinDocument":
        return main_frame_id is None or params.get("frameId") == main_frame_id
    return method == "Page.loadEventFired"


# ── the CDP client (IO) ────────────────────────────────────────────────────────

def resolve_page_target(binary: str, timeout: float = 10.0) -> tuple[str | None, str]:
    """Ask agent-browser for its Chrome CDP endpoint, then find the active page's
    per-target WebSocket. Returns ``(page_ws_url | None, note)`` — note carries a
    human-readable reason when there's nothing to stream (no session / no page)."""
    try:
        cdp = subprocess.run([binary, "get", "cdp-url"], capture_output=True,
                             text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"{binary!r} not on PATH"
    except subprocess.TimeoutExpired:
        return None, "agent-browser get cdp-url timed out"
    # Whitespace-only stdout (a bare newline) made `[0]` raise IndexError — outside the WS
    # route's try block, so the panel got a dropped socket instead of this function's
    # documented `(None, note)` (#3451 review).
    ws_lines = (cdp.stdout or "").strip().splitlines()
    browser_ws = ws_lines[0].strip() if ws_lines else ""
    if cdp.returncode != 0 or not browser_ws.startswith("ws"):
        return None, ((cdp.stderr or "").strip() or "no CDP url — is a session open?")
    base = _http_base_from_ws(browser_ws)
    cur = ""
    try:
        u = subprocess.run([binary, "get", "url"], capture_output=True, text=True, timeout=timeout)
        url_lines = (u.stdout or "").strip().splitlines() if u.returncode == 0 else []
        cur = url_lines[0].strip() if url_lines else ""
    except Exception:  # noqa: BLE001 — current url is a nicety for tab selection
        cur = ""
    try:
        with urllib.request.urlopen(base + "/json/list", timeout=timeout) as r:
            targets = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return None, f"CDP /json/list unreachable at {base}: {e}"
    page = pick_page_target(targets, cur)
    return (page, "" if page else "no page target to stream (open a URL first)")


def viewport_metrics(w, h, dpr) -> tuple[int, int, float, int, int]:
    """Clamp a panel size → ``(css_w, css_h, scale, frame_max_w, frame_max_h)``. The CSS
    size drives Chrome's layout viewport; scale (device-pixel-ratio, capped at 2) keeps
    frames crisp on hi-dpi; the frame max caps the screencast so a huge dock can't flood
    the socket."""
    cw = max(1, min(int(w or 0), 2048))
    ch = max(1, min(int(h or 0), 2048))
    scale = max(1.0, min(float(dpr or 1), 2.0))
    return cw, ch, scale, min(int(cw * scale), 2560), min(int(ch * scale), 2560)


# CDP events that mean "a new document is ready" → re-arm the screencast. The set that
# actually fires varies by navigation kind, so we listen for all of them (debounced).
_NAV_DONE = frozenset((
    "Page.loadEventFired", "Page.frameStoppedLoading",
    "Page.frameNavigated", "Page.navigatedWithinDocument",
))


class CDPStream:
    """A minimal async CDP client over one page target: start a screencast, ack frames,
    resize the viewport to the panel, and dispatch input. ``frame_cb(jpeg, metadata)``
    fires per ``Page.screencastFrame``; ``nav_cb(state)`` (optional) fires whenever the
    tab's ``{url, title, canBack, canForward}`` changes — the panel's address bar and
    back/forward buttons. Requires ``websockets`` (a uvicorn extra).

    Two robustness details the naive version missed:
    - **Re-arm on navigation.** A cross-process navigation swaps the page's render widget
      and Chrome silently stops the screencast — so we re-issue ``startScreencast`` on each
      top-frame ``Page.frameNavigated`` / ``Page.loadEventFired``. Without this you see the
      first page but nothing as the agent moves around or into sub-pages.
    - **One writer.** Frame acks + nav re-arms (reader task) and input/resize (request
      task) both write to the socket, so a lock serializes sends.
    - **Navigation → address bar.** The screencast carries only pixels, so the panel never
      learned where the tab went: its address bar sat on the placeholder while the agent
      opened, clicked, submitted and redirected. Each top-frame navigation (and each load,
      when the title lands) asks ``Page.getNavigationHistory``; the reply is picked up here
      in the reader (never awaited — the reader IS what would deliver it) and a changed
      state goes to ``nav_cb``."""

    def __init__(self, page_ws_url: str, frame_cb, quality: int = 80, nav_cb=None):
        self._url = page_ws_url
        self._frame_cb = frame_cb
        self._nav_cb = nav_cb
        self._history_ids: set[int] = set()   # in-flight Page.getNavigationHistory request ids
        self._main_frame: str | None = None   # top frame id, learned from frameNavigated
        self._last_nav: dict | None = None    # last state handed to nav_cb (dedupe)
        self._quality = max(1, min(int(quality or 80), 100))
        self._cast = (1280, 800)      # current screencast max frame size (device px)
        self._last_vp = None          # last applied (css_w, css_h, scale) — dedupe resize thrash
        self._ws = None
        self._id = 0
        self._last_arm = 0.0          # debounce re-arms (multi-frame pages fire many events)
        self._lock: asyncio.Lock | None = None
        self._reader: asyncio.Task | None = None

    async def __aenter__(self):
        import websockets
        self._lock = asyncio.Lock()
        self._ws = await websockets.connect(self._url, max_size=None, open_timeout=10)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc):
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    @property
    def reader_task(self):
        """The background CDP read loop; it completes when Chrome's page socket closes
        (tab gone / target replaced). The route races it so a dead stream reconnects fast
        instead of freezing until an idle-close."""
        return self._reader

    async def _send(self, method: str, params: dict | None = None, *, track: set | None = None) -> int:
        async with self._lock:  # serialize writes: reader (acks/re-arm) + request task (input/resize)
            self._id += 1
            if track is not None:
                track.add(self._id)   # registered BEFORE the send, so the reply can't beat it
            await self._ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
            return self._id

    async def _arm_cast(self):
        mw, mh = self._cast
        await self._send("Page.startScreencast", {"format": "jpeg", "quality": self._quality,
                         "maxWidth": mw, "maxHeight": mh, "everyNthFrame": 1})

    async def request_nav_state(self):
        """Ask where the tab is; the reply is handled in ``_read_loop`` → ``nav_cb``."""
        if self._nav_cb is not None:
            await self._send("Page.getNavigationHistory", track=self._history_ids)

    async def _on_history(self, result: dict | None):
        state = nav_state_from_history(result)
        if state is None or state == self._last_nav or self._nav_cb is None:
            return
        self._last_nav = state
        await self._nav_cb(state)

    async def start_screencast(self, max_w: int = 1280, max_h: int = 800):
        await self._send("Page.enable")   # also enables frameNavigated / loadEventFired for re-arm
        # Treat the page as permanently focused + visible. Otherwise Chrome throttles rendering
        # when the page isn't the focused surface, and the screencast stalls until the operator
        # clicks back into the panel ("only live-navigates when focused").
        await self._send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self._cast = (max_w, max_h)
        await self._arm_cast()
        await self.request_nav_state()   # the page already open when the panel attached

    async def set_viewport(self, w, h, dpr=1.0):
        """Resize Chrome's layout viewport to the panel and re-arm the screencast at the
        matching frame size — so the page reflows to fill, and stays crisp. Deduped: an
        unchanged size is a no-op (a drag fires many identical observations)."""
        cw, ch, scale, mw, mh = viewport_metrics(w, h, dpr)
        if (cw, ch, scale) == self._last_vp:
            return
        self._last_vp = (cw, ch, scale)
        await self._send("Emulation.setDeviceMetricsOverride",
                         {"width": cw, "height": ch, "deviceScaleFactor": scale, "mobile": False})
        self._cast = (mw, mh)
        await self._arm_cast()

    async def dispatch(self, msg: dict):
        t = msg.get("t")
        if t == "resize":
            await self.set_viewport(msg.get("w"), msg.get("h"), msg.get("dpr", 1))
            return
        if t == "refresh":                 # panel became visible again → force a fresh frame
            await self._arm_cast()
            return
        cmd = input_to_cdp(msg)
        if cmd:
            await self._send(*cmd)

    async def _read_loop(self):
        import base64
        async for raw in self._ws:
            try:
                m = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if m.get("id") in self._history_ids:
                self._history_ids.discard(m["id"])
                try:
                    await self._on_history(m.get("result"))
                except Exception:  # noqa: BLE001 — a closed panel socket must not kill the reader
                    log.debug("[agent_browser] nav state delivery failed", exc_info=True)
                continue
            method = m.get("method") or ""
            params = m.get("params") or {}
            if method == "Page.frameNavigated" and not (params.get("frame") or {}).get("parentId"):
                self._main_frame = (params.get("frame") or {}).get("id") or self._main_frame
            if is_top_frame_nav(method, params, self._main_frame):
                try:
                    await self.request_nav_state()   # where did the tab go? → address bar
                except Exception:  # noqa: BLE001 — transient during teardown
                    pass
            if method == "Page.screencastFrame":
                p = m["params"]
                try:
                    await self._frame_cb(base64.b64decode(p["data"]), p.get("metadata", {}))
                finally:
                    await self._send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
            elif method in _NAV_DONE:
                # A navigation swaps the render widget and Chrome drops the screencast.
                # These are the "new document is ready" signals — which one fires varies by
                # navigation kind (real cross-origin nav emits loadEventFired/frameNavigated;
                # data:/SPA emit frameStoppedLoading/navigatedWithinDocument), so we cover all
                # and debounce, since a multi-frame page fires several.
                now = time.monotonic()
                if now - self._last_arm > 0.2:
                    self._last_arm = now
                    try:
                        await self._arm_cast()   # bring the screencast back
                    except Exception:  # noqa: BLE001 — transient during teardown
                        pass
