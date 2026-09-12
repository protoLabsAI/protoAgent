"""Browser panel console view (ADR 0026) — a **fully drivable** browser viewport.

A live **CDP screencast** (event-driven JPEG frames, not a screenshot poll) is bridged
from the agent-browser Chrome to a ``<canvas>`` over a **gated same-origin WebSocket**,
and operator mouse / keyboard / scroll are forwarded back via ``Input.dispatch*`` — so
you can actually click, type, and scroll the page, alongside the agent. It rides the
fleet proxy (all bytes are same-origin), so it works on the host AND a remote member.
Streaming/input lives in ``browser_stream``; this file is the page + the routes.

Self-contained vanilla JS (no build step). Every fetch / link is derived from
``base = location.pathname.split("/plugins/")[0]`` (="" on the host, ``/agents/<slug>``
when proxied) so the page is same-origin + slug-aware (the token/theme handshake).

**Where the auth actually is.** The page route is public *chrome* by design: the host
auto-exempts every manifest ``views[].path`` from the operator bearer gate, because the
console iframes it with a plain navigation that cannot carry a header. So the page is
deliberately not the boundary — the CAPABILITY is:

* ``POST /nav`` and ``POST /stream-ticket`` live under ``/api/plugins/agent_browser`` and
  ride the host's operator-bearer gate, so only an authenticated console can drive the
  browser or mint a ticket;
* ``WS /stream`` self-gates with a single-use, short-lived ticket from that gated route,
  because the host's auth middleware is HTTP-only and does not cover WS handshakes.

The standalone repo's last unreleased change (#21) added a THIRD thing here — a
signed-cookie gate on a ``/panel/dash`` alias — and #3451 removed it rather than vendor
it. It keyed off ``cfg["require_auth"]``, which neither the manifest nor the host ever
set, so it could never fire; and had it fired it would have guarded nothing, since the
public ``/panel`` sibling serves byte-identical HTML and the host's public-path match is
a PREFIX match (``/plugins/agent_browser/panel`` exempts ``/panel/dash`` too). ``dash``
was itself a leftover name from the retired ``full`` dashboard-embed mode. A gate that
cannot fire — and that an operator could reasonably read as "the panel is protected" —
is worse than none.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess

# Module-scope fastapi imports (the host always provides fastapi; __init__.py catches
# ImportError so the tools still serve if the panel can't import).
from fastapi import APIRouter, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from . import browser_stream, preflight
from .runtime import bad_operand, launch_flags, number

log = logging.getLogger("protoagent.plugins.agent_browser")


def build_panel_router(cfg: dict | None):
    cfg = cfg or {}
    home = str(cfg.get("home_url") or "").strip()
    router = APIRouter()

    # A safe JS string literal for `const HOME=__HOME_URL__;`. json.dumps handles quote/
    # backslash escaping; the `<` → < step stops a `</script>` in the value from
    # closing the inline script tag in the HTML parser (still the same string in JS).
    home_literal = json.dumps(home).replace("<", "\\u003c")

    @router.get("/panel")
    async def _panel():
        return HTMLResponse(_INTERACTIVE_PAGE.replace("__HOME_URL__", home_literal))

    return router


def build_panel_data_router(cfg: dict | None):
    """The panel DATA/ACTION routes — mounted under ``/api/plugins/agent_browser``.
    The HTTP routes (``POST /nav``, ``POST /stream-ticket``) inherit the operator bearer
    gate (plugin-view rule 2), so nobody who lacks the bearer can drive the browser or
    mint a stream ticket.

    ``WS /stream`` is the exception the host forces on us: its auth middleware is
    HTTP-only and does NOT cover WebSocket handshakes, so the WS gates itself with the
    single-use ticket that ``POST /stream-ticket`` (which IS gated) hands out."""
    cfg = cfg or {}
    binary = str(cfg.get("binary") or "agent-browser")
    # A hand-written config reaches here raw (`type: number` only validates Settings
    # edits): a non-numeric value used to raise and leave the panel unmounted.
    timeout = number(cfg, "timeout_s", 60.0, positive=True)
    quality = number(cfg, "stream_quality", 80, cast=int)

    router = APIRouter()

    def _run(*args: str) -> tuple[int, str]:
        try:
            p = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stderr or p.stdout or "").strip()
        except OSError:
            # Missing OR unstartable (FileNotFoundError covers both, PermissionError the
            # latter). Say what to DO about it — the same sentence the operator's setup
            # banner carries — instead of a bare "not on PATH" in a toolbar toast.
            return 127, preflight.hint(preflight.probe(cfg)) or f"{binary!r} could not be started"
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    # ── interactive stream: a single-use ticket (gated) + the WS bridge (self-gated) ──
    @router.post("/stream-ticket")
    async def _stream_ticket():
        """Mint a single-use ticket for the interactive WS. HTTP, so it rides the
        host's operator-bearer gate — only an authenticated console reaches it."""
        return JSONResponse({"ticket": browser_stream.mint_ticket()})

    @router.websocket("/stream")
    async def _stream(ws: WebSocket):
        """Interactive viewport: CDP screencast frames out (binary JPEG), operator
        input in (JSON → ``Input.dispatch*``). Ticket-gated (the host doesn't gate
        WS). One sender only — ``on_frame`` — while the loop just receives, so the two
        directions never race on the socket."""
        if not browser_stream.consume_ticket(ws.query_params.get("ticket", "")):
            await ws.close(code=1008)  # policy violation: missing/replayed/expired ticket
            return
        await ws.accept()
        page_ws, note = await asyncio.to_thread(browser_stream.resolve_page_target, binary, timeout)
        if not page_ws:
            # A setup problem gets the setup sentence (install the CLI / install Chrome);
            # "no session" / "no page open" pass through as the normal states they are.
            await ws.send_json({"t": "error", "msg": await asyncio.to_thread(preflight.explain_note, note, cfg)})
            await ws.close()
            return
        # Coalescing delivery: on_frame just stashes the NEWEST frame and signals; a sender
        # task drains it. Under load this DROPS stale frames instead of letting ws.send_bytes
        # block — the backpressure buildup was what stalled the socket (frozen frames → the
        # proxy idle-closes it → the "live" dot flaps offline).
        latest: dict = {"jpeg": None, "wh": None}
        pending = asyncio.Event()

        async def on_frame(jpeg: bytes, md: dict):
            latest["jpeg"] = jpeg
            latest["wh"] = (md.get("deviceWidth"), md.get("deviceHeight"))
            pending.set()

        async def sender():
            sent_wh = None
            try:
                while True:
                    await pending.wait()
                    pending.clear()
                    jpeg, wh = latest["jpeg"], latest["wh"]
                    if jpeg is None:
                        continue
                    if wh != sent_wh:
                        sent_wh = wh
                        await ws.send_json({"t": "meta", "w": wh[0], "h": wh[1]})
                    await ws.send_bytes(jpeg)
            except Exception:  # noqa: BLE001 — client gone / cancelled on teardown
                pass

        send_task = None
        try:
            async with browser_stream.CDPStream(page_ws, on_frame, quality=quality) as cdp:
                await cdp.start_screencast()
                send_task = asyncio.create_task(sender())
                while True:
                    # race operator input against the CDP socket dying — if the page target
                    # goes away, break so the client reconnects to the live page fast.
                    recv = asyncio.create_task(ws.receive_json())
                    done, _ = await asyncio.wait({recv, cdp.reader_task},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if recv not in done:
                        recv.cancel()
                        break
                    await cdp.dispatch(recv.result())
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 — a CDP/stream fault must not take down the worker
            log.exception("[agent_browser] interactive stream failed")
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if send_task:
                send_task.cancel()

    @router.post("/nav")
    async def _nav(body: dict = Body(...)):
        """Drive the viewport from the toolbar: open <url> / back / forward / reload."""
        action = str(body.get("action", "")).strip().lower()
        url = str(body.get("url", "")).strip()
        if action == "open":
            if not url:
                return JSONResponse({"ok": False, "error": "url required"})
            # The SAME argv guard the tools use (CWE-88): this url lands in the CLI's argv,
            # and the CLI reads options anywhere in it — `--profile=/tmp/x` or `--headed`
            # would set a launch flag. The bearer gate limits WHO can call /nav; it doesn't
            # make the value safe.
            if (bad := bad_operand(url=url)):
                return JSONResponse({"ok": False, "error": bad})
            # launch flags (headed/profile/stealth/…) so a session started from the panel
            # matches one the agent opens.
            rc, err = await asyncio.to_thread(lambda: _run(*launch_flags(cfg), "open", url))
        elif action in ("back", "forward", "reload"):
            rc, err = await asyncio.to_thread(lambda: _run(action))
        else:
            return JSONResponse({"ok": False, "error": f"bad action {action!r}"})
        return JSONResponse({"ok": rc == 0, "error": "" if rc == 0 else err[:300]})

    return router


# ── the interactive browser panel: a live, drivable CDP-screencast viewport ─────
# A <canvas> fed JPEG frames over the gated WS (browser_stream bridges CDP screencast),
# with mouse/keyboard/scroll forwarded back as Input.dispatch*. Chrome is the protoLabs
# design system (plugin-kit CSS + .pl-* components); the viewport canvas itself is a
# bespoke domain surface (theme the frame around it, not the pixels).
_INTERACTIVE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Browser</title>
<link id="dskit" rel="stylesheet" href="">
<script>
// Same-origin base: "" on the host, "/agents/<slug>" through the fleet proxy. Prefix
// EVERY fetch / asset with it (the kit does this for us via apiUrl/apiFetch).
const BASE=location.pathname.split("/plugins/")[0];
document.getElementById("dskit").href=BASE+"/_ds/plugin-kit.css";
</script>
<style>
  html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans);font-size:13px}
  .bar{height:38px;display:flex;align-items:center;gap:6px;padding:0 10px;
    border-bottom:var(--pl-border-width) solid var(--pl-color-border)}
  .bar .pl-input{flex:1;min-width:0}
  .conn{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--pl-color-fg-muted);white-space:nowrap}
  .dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex:none;
    background:var(--pl-color-fg-muted,#9aa0aa)}
  .dot.ok{background:#22c55e}.dot.err{background:#ef4444}
  .stage{height:calc(100% - 38px);position:relative;background:var(--pl-color-bg-inset);overflow:hidden}
  canvas{width:100%;height:100%;display:block;outline:none;touch-action:none;object-fit:contain}
  #msg{display:none;position:absolute;inset:0;align-items:center;justify-content:center;
    padding:24px;box-sizing:border-box}
  .card{max-width:460px;text-align:center}
  .card .t{font-size:15px;font-weight:600;margin-bottom:8px}
  .card .d{color:var(--pl-color-fg-muted);line-height:1.6}
  .card code{background:var(--pl-color-bg-subtle,rgba(127,127,127,.16));padding:.1em .35em;border-radius:4px}
  .toast{position:absolute;left:12px;right:12px;bottom:12px;padding:8px 10px;border-radius:6px;
    font-size:12px;line-height:1.5;background:var(--pl-color-bg,#111);color:var(--pl-color-fg,#eee);
    border:var(--pl-border-width,1px) solid var(--pl-color-border,#444);box-shadow:0 4px 16px rgba(0,0,0,.25)}
</style></head><body>
  <div class="bar">
    <button class="pl-btn pl-btn--ghost pl-btn--icon pl-btn--sm" title="Back" onclick="nav('back')">◀</button>
    <button class="pl-btn pl-btn--ghost pl-btn--icon pl-btn--sm" title="Forward" onclick="nav('forward')">▶</button>
    <button class="pl-btn pl-btn--ghost pl-btn--icon pl-btn--sm" title="Reload" onclick="nav('reload')">⟳</button>
    <input id="url" class="pl-input" placeholder="example.com — Enter to open" autocomplete="off">
    <button class="pl-btn pl-btn--primary pl-btn--sm" onclick="go()">Go</button>
    <span class="conn"><span id="dot" class="dot"></span><span id="cs">connecting…</span></span>
  </div>
  <div class="stage">
    <canvas id="cv" width="1280" height="800" tabindex="0"></canvas>
    <div id="msg"><div class="card"><div class="t" id="mt"></div><div class="d" id="md"></div></div></div>
    <div id="toast" class="toast" hidden></div>
  </div>
<script type="module">
const BASE=location.pathname.split("/plugins/")[0];
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(cb){ if(cb) cb(); }, apiFetch:(p,i)=>fetch(BASE+p,i), apiUrl:(p)=>BASE+p }; }
const $=(id)=>document.getElementById(id);
const cv=$("cv"), ctx=cv.getContext("2d");
const HOME=__HOME_URL__;   // configured homepage (blank ⇒ Start opens about:blank, no auto-open)

// ── nav toolbar — reuses the gated HTTP /nav route (agent-browser open/back/…) ──
// The route answers 200 with {ok:false,error} for a failed command (a missing CLI, a
// closed session). SHOW it: swallowing the body left the operator clicking Go against a
// viewport that never changed, with the explanation sitting unread in the response.
async function nav(action,url){
  try{
    const r=await kit.apiFetch("/api/plugins/agent_browser/nav",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({action,url})});
    const b=await r.json().catch(()=>({}));
    if(b && b.ok===false && b.error){
      // A LIVE page is still on screen (e.g. `back` with no history exits non-zero), so
      // toast it — covering the page with "No page open" would be a lie. Only a panel with
      // no stream gets the empty-state card.
      if(connected){ toast(b.error); } else { setStatus("err","error"); showStart(b.error); }
      return;
    }
  }catch(_){ setStatus("err","offline"); }
  if(!connected) connect();   // a just-created session now has a page to stream
}
function go(){ let u=$("url").value.trim(); if(!u)return; if(!/^https?:\/\//.test(u))u="https://"+u; nav("open",u); }
window.nav=nav; window.go=go;
$("url").addEventListener("keydown",(e)=>{ if(e.key==="Enter") go(); });

function setStatus(s,label){ $("dot").className="dot"+(s?(" "+s):""); $("cs").textContent=label; }
function live(){ return (devW&&devH) ? ("live · "+devW+"×"+devH) : "live"; }  // show the real viewport size
function showMsg(t,html){ $("mt").textContent=t; $("md").innerHTML=html||""; $("msg").style.display="flex"; }
function hideMsg(){ $("msg").style.display="none"; }
let toastTimer=null;
function toast(msg){ const t=$("toast"); t.textContent=String(msg==null?"":msg); t.hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>{ t.hidden=true; },6000); }
// Server-supplied notes (a setup hint, a CLI error) carry filesystem paths and command
// text — escape before they touch innerHTML.
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,(c)=>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

// ── the empty state: a Start button (+ a one-shot auto-open of the homepage) ───
let autoStarted=false;
function homeHost(){ try{ return new URL(HOME).host || HOME; }catch(_){ return HOME; } }
function startBrowser(){ nav("open", HOME || "about:blank"); }
window.startBrowser=startBrowser;
function showStart(note){
  const label = HOME ? ("Open " + homeHost()) : "Start browser";
  showMsg("No page open",
    (note ? esc(note) + "<br><br>" : "")
    + '<button class="pl-btn pl-btn--primary pl-btn--sm" onclick="startBrowser()">' + label + '</button>'
    + '<div style="margin-top:12px">or type a URL above, or let the agent drive — <code>browser_open</code>.</div>');
  if(HOME && !autoStarted){ autoStarted=true; startBrowser(); }   // open the configured homepage once
}

// ── the interactive stream: mint a ticket (gated) → open the WS → paint frames ──
let ws=null, connected=false, devW=1280, devH=800, retry=null;
function wsUrl(ticket){
  const u=new URL(kit.apiUrl("/api/plugins/agent_browser/stream"), location.href);
  u.protocol = u.protocol==="https:" ? "wss:" : "ws:";      // http→ws, https→wss
  u.searchParams.set("ticket", ticket);
  return u.toString();
}
async function connect(){
  clearTimeout(retry);
  try{
    const r=await kit.apiFetch("/api/plugins/agent_browser/stream-ticket",{method:"POST"});
    const ticket=(await r.json()).ticket;
    ws=new WebSocket(wsUrl(ticket));
    ws.binaryType="arraybuffer";
    ws.onopen=()=>{ connected=true; setStatus("ok",live()); sendResize(); setTimeout(sendResize,500); };
    ws.onmessage=onMsg;
    ws.onclose=()=>{ connected=false; setStatus("err","offline"); scheduleRetry(); };
    ws.onerror=()=>{ try{ ws.close(); }catch(_){} };
  }catch(_){ setStatus("err","offline"); scheduleRetry(); }
}
function scheduleRetry(){ clearTimeout(retry); retry=setTimeout(connect,1200); }
async function onMsg(ev){
  if(typeof ev.data==="string"){
    let m; try{ m=JSON.parse(ev.data); }catch(_){ return; }
    if(m.t==="meta"){ if(m.w) devW=m.w; if(m.h) devH=m.h; setStatus("ok",live()); }
    else if(m.t==="error"){ setStatus("err","no page"); showStart(m.msg); }
    return;
  }
  try{                                   // binary → a JPEG screencast frame
    const bmp=await createImageBitmap(new Blob([ev.data]));
    if(cv.width!==bmp.width||cv.height!==bmp.height){ cv.width=bmp.width; cv.height=bmp.height; }
    ctx.drawImage(bmp,0,0); bmp.close(); hideMsg(); setStatus("ok",live());
  }catch(_){}
}

// ── input forwarding: canvas events → CDP Input.dispatch* (coords in CSS px) ────
function send(o){ if(ws&&ws.readyState===1) ws.send(JSON.stringify(o)); }
function mods(e){ return {shift:e.shiftKey,ctrl:e.ctrlKey,alt:e.altKey,meta:e.metaKey}; }
// map a client point → page CSS px, accounting for object-fit:contain letterboxing
// (the frame's aspect may differ from the canvas box for a moment after a resize).
function pos(e){ const r=cv.getBoundingClientRect();
  const bw=cv.width||devW, bh=cv.height||devH;              // backing store = frame pixels
  const s=Math.min(r.width/bw, r.height/bh);                // contain scale
  const dw=bw*s, dh=bh*s, ox=(r.width-dw)/2, oy=(r.height-dh)/2;
  return { x:(e.clientX-r.left-ox)/dw*devW, y:(e.clientY-r.top-oy)/dh*devH }; }
const BTN={0:"left",1:"middle",2:"right"};
cv.addEventListener("mousedown",(e)=>{ e.preventDefault(); cv.focus(); const p=pos(e);
  send({t:"mouse",action:"down",x:p.x,y:p.y,button:BTN[e.button]||"left",clickCount:e.detail||1,buttons:e.buttons,...mods(e)}); });
window.addEventListener("mouseup",(e)=>{ const p=pos(e);
  send({t:"mouse",action:"up",x:p.x,y:p.y,button:BTN[e.button]||"left",clickCount:1,buttons:e.buttons,...mods(e)}); });
let moveQueued=false,lastMove=null;
cv.addEventListener("mousemove",(e)=>{ lastMove=e; if(moveQueued)return; moveQueued=true;
  requestAnimationFrame(()=>{ moveQueued=false; if(!lastMove)return; const p=pos(lastMove);
    send({t:"mouse",action:"move",x:p.x,y:p.y,buttons:lastMove.buttons,...mods(lastMove)}); }); });
cv.addEventListener("contextmenu",(e)=>e.preventDefault());
cv.addEventListener("wheel",(e)=>{ e.preventDefault(); const p=pos(e);
  send({t:"wheel",x:p.x,y:p.y,dx:e.deltaX,dy:e.deltaY,...mods(e)}); },{passive:false});
function printable(e){ return e.key && e.key.length===1 && !e.ctrlKey && !e.metaKey; }
cv.addEventListener("keydown",(e)=>{ e.preventDefault();
  send({t:"key",action:"down",key:e.key,code:e.code,keyCode:e.keyCode,text:printable(e)?e.key:"",...mods(e)}); });
cv.addEventListener("keyup",(e)=>{ e.preventDefault();
  send({t:"key",action:"up",key:e.key,code:e.code,keyCode:e.keyCode,...mods(e)}); });

// ── keep the browser viewport matched to the panel (full stretch + responsive) ──
// The server resizes Chrome's layout viewport to these dims (CDP), so the page reflows
// to fill the dock and frames come at its shape/DPI — no letterboxed "standard viewport".
let rzTimer=null;
function panelSize(){ const r=cv.parentElement.getBoundingClientRect();
  return { w:Math.round(r.width), h:Math.round(r.height), dpr:Math.min(window.devicePixelRatio||1, 2) }; }
function sendResize(){ const s=panelSize(); if(s.w>10 && s.h>10) send({t:"resize",w:s.w,h:s.h,dpr:s.dpr}); }
new ResizeObserver(()=>{ clearTimeout(rzTimer); rzTimer=setTimeout(sendResize, 220); }).observe(cv.parentElement);

// When the panel becomes visible/focused again, the console may have throttled it while
// hidden — reconnect if the socket dropped, else force a fresh frame so it snaps to current.
function onVisible(){ if(document.hidden) return; if(!connected) connect(); else { send({t:"refresh"}); sendResize(); } }
document.addEventListener("visibilitychange", onVisible);
window.addEventListener("focus", onVisible);

// ── boot ONCE — on the handshake (so apiFetch has the bearer for the gated ticket)
// or an 800ms fallback for a standalone/older host that posts no init. ──────────
let booted=false;
function boot(){ if(booted)return; booted=true; connect(); }
setStatus("", "connecting…");
kit.initPluginView(boot);
setTimeout(boot, 800);
</script></body></html>"""
