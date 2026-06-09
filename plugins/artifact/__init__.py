"""Artifact plugin (ADR 0038) — generative UI on demand.

The agent calls ``show_artifact(kind, code)`` to render HTML / SVG / Mermaid / React into the
console's Artifact panel. The panel is a plugin-served shell page (iframed by the console, ADR
0026) that renders the agent's generated code in a **nested sandboxed iframe**
(``sandbox="allow-scripts"``, no same-origin) — the same isolation model as Claude Artifacts and
Open WebUI: generated code runs, but can't touch the console, its cookies, or its APIs.
"""

from __future__ import annotations

import logging
import time

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.artifact")

_KINDS = {"html", "svg", "mermaid", "react"}
# The latest artifact the agent rendered (transient — "what's on screen now").
_current: dict = {"kind": "", "code": "", "title": "", "ts": 0}


@tool
def show_artifact(kind: str, code: str, title: str = "") -> str:
    """Render a generative-UI artifact into the console's Artifact panel.

    ``kind`` is one of: "html" (a full or partial HTML document), "svg" (inline SVG markup),
    "mermaid" (a Mermaid diagram definition), or "react" (a self-contained React component script
    that renders into ``#root``; React, ReactDOM and Babel are provided). ``code`` is the source;
    ``title`` is an optional label. The artifact runs sandboxed — it cannot access the console.
    Use this to show charts, diagrams, mock-ups, or interactive widgets you generate.
    """
    k = (kind or "").strip().lower()
    if k not in _KINDS:
        return f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
    _current.update(kind=k, code=code or "", title=title or "", ts=int(time.time() * 1000))
    return f"Rendered a {k} artifact ({len(code or '')} chars) to the Artifact panel."


def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/current")
    async def _current_artifact() -> dict:
        return dict(_current)

    @router.get("/view")
    async def _view():
        return HTMLResponse(_SHELL_HTML)

    return router


def register(registry) -> None:
    registry.register_tool(show_artifact)
    registry.register_router(_build_router(), prefix="/api/plugins/artifact")


# The shell page (ADR 0026 iframe). It takes the operator bearer via the console's postMessage
# handshake, polls /current, and renders each new artifact into a NESTED sandboxed iframe. The
# nested frame is sandbox="allow-scripts" with NO allow-same-origin — generated code is isolated.
_SHELL_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:var(--bg,#0a0a0c);color:#9aa0aa;
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif}
  #empty{display:flex;align-items:center;justify-content:center;height:100%;text-align:center;padding:24px;font-size:14px}
  #frame{border:0;width:100%;height:100%;display:none;background:#fff}
</style></head><body>
<div id="empty">No artifact yet. Ask the agent to render one — a chart, diagram, or widget.</div>
<iframe id="frame" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
<script>
  var token = null, lastTs = 0;
  window.addEventListener("message", function (e) {
    var m = e.data || {}; if (m.type === "protoagent:init") token = m.token || null;
  });
  function srcdoc(kind, code) {
    if (kind === "html") return code;
    if (kind === "svg") return '<!doctype html><body style="margin:0;display:grid;place-items:center;min-height:100vh">' + code + '</body>';
    if (kind === "mermaid") return '<!doctype html><body style="margin:0;background:#fff">' +
      '<script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js"><\/script>' +
      '<pre class="mermaid">' + code.replace(/</g, "&lt;") + '<\/pre>' +
      '<script>mermaid.initialize({startOnLoad:true});<\/script></body>';
    if (kind === "react") return '<!doctype html><body style="margin:0"><div id="root"></div>' +
      '<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"><\/script>' +
      '<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"><\/script>' +
      '<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.24.7/babel.min.js"><\/script>' +
      '<script type="text/babel">' + code + '<\/script></body>';
    return "<body>unsupported artifact kind</body>";
  }
  async function poll() {
    try {
      var r = await fetch("/api/plugins/artifact/current", { headers: token ? { Authorization: "Bearer " + token } : {} });
      var a = await r.json();
      if (a && a.ts && a.ts !== lastTs && a.code) {
        lastTs = a.ts;
        document.getElementById("empty").style.display = "none";
        var f = document.getElementById("frame");
        f.srcdoc = srcdoc(a.kind, a.code);
        f.style.display = "block";
      }
    } catch (e) { /* transient */ }
  }
  setInterval(poll, 1500); poll();
</script></body></html>"""
