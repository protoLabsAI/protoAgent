"""Artifact plugin (ADR 0038) — generative UI on demand.

The agent calls ``show_artifact(kind, code)`` to render HTML / SVG / Mermaid / React into the
console's Artifact panel. The panel is a plugin-served shell page (iframed by the console, ADR
0026) that renders the agent's generated code in a **nested sandboxed iframe**
(``sandbox="allow-scripts"``, no same-origin) — the same isolation model as Claude Artifacts and
Open WebUI: generated code runs, but can't touch the console, its cookies, or its APIs.

The current artifact is persisted to a **file** (instance-scoped), not module memory — under the
ACP runtime the tool executes in the operator-MCP process while the route is served by the main
process, so the two only share state through disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.artifact")

_KINDS = {"html", "svg", "mermaid", "react"}


def _artifact_path() -> Path:
    base = Path(os.environ.get("ARTIFACT_DIR") or (Path.home() / ".protoagent" / "artifact"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "current.json"


def _read_current() -> dict:
    try:
        return json.loads(_artifact_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"kind": "", "code": "", "title": "", "ts": 0}


def _write_current(payload: dict) -> None:
    path = _artifact_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@tool
def show_artifact(kind: str, code: str, title: str = "") -> str:
    """Render a generative-UI artifact into the console's Artifact panel.

    ``kind`` is one of: "html" (a full or partial HTML document), "svg" (inline SVG markup),
    "mermaid" (a Mermaid diagram definition), or "react" (a self-contained React component script
    that renders into ``#root``; React, ReactDOM and Babel are provided). ``code`` is the source;
    ``title`` is an optional label. The artifact runs sandboxed — it cannot access the console.
    Use this to SHOW the user a chart, diagram, mock-up, or interactive widget you generate —
    prefer it over writing files when the user just wants to see something rendered.
    """
    k = (kind or "").strip().lower()
    if k not in _KINDS:
        return f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
    _write_current({"kind": k, "code": code or "", "title": title or "", "ts": int(time.time() * 1000)})
    return f"Rendered a {k} artifact ({len(code or '')} chars) to the Artifact panel."


def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/current")
    async def _current_artifact() -> dict:
        return _read_current()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_SHELL_HTML)

    return router


def register(registry) -> None:
    registry.register_tool(show_artifact)
    registry.register_skill_dir("skills")  # teaches: render with show_artifact, don't write files
    registry.register_router(_build_router(), prefix="/api/plugins/artifact")


# The shell page (ADR 0026 iframe). It takes the operator bearer via the console's postMessage
# handshake, polls /current, and renders each new artifact into a NESTED sandboxed iframe. The
# nested frame is sandbox="allow-scripts" with NO allow-same-origin — generated code is isolated.
_SHELL_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
  :root{ --bg:#0a0a0c; --fg:#ededed; --fg-muted:#9aa0aa; }
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg-muted);
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif}
  #empty{display:flex;align-items:center;justify-content:center;height:100%;text-align:center;padding:24px;font-size:14px}
  /* No white flash — the artifact frame defaults to the console's dark ground (ADR 0038). */
  #frame{border:0;width:100%;height:100%;display:none;background:var(--bg)}
</style></head><body>
<div id="empty">No artifact yet. Ask the agent to render one — a chart, diagram, or widget.</div>
<iframe id="frame" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
<script>
  var token = null, lastTs = 0;
  // Theme follows the console (ADR 0026 bridge). Dark fallbacks so we never flash white.
  var theme = { bg: "#0a0a0c", fg: "#ededed", fgMuted: "#9aa0aa" };
  window.addEventListener("message", function (e) {
    var m = e.data || {}; if (m.type !== "protoagent:init") return;
    token = m.token || null;
    if (m.theme) {
      theme = { bg: m.theme.bg || theme.bg, fg: m.theme.fg || theme.fg, fgMuted: m.theme.fgMuted || theme.fgMuted };
      document.documentElement.style.setProperty("--bg", theme.bg);
      document.documentElement.style.setProperty("--fg", theme.fg);
      document.documentElement.style.setProperty("--fg-muted", theme.fgMuted);
    }
  });
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  // Base style injected into every artifact so unstyled content inherits the console's dark theme
  // by default (generated code can still set its own background to override).
  function base(){ return '<style>html,body{margin:0;background:' + theme.bg + ';color:' + theme.fg + '}</style>'; }
  function srcdoc(kind, code) {
    if (kind === "html") return base() + code;
    if (kind === "svg") return '<!doctype html>' + base() + '<body style="display:grid;place-items:center;min-height:100vh">' + code + '</body>';
    if (kind === "mermaid") return '<!doctype html>' + base() + '<body><pre class="mermaid">' + esc(code) + '</pre>' +
      '<script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js"><\/script>' +
      '<script>mermaid.initialize({startOnLoad:false,theme:"dark"});mermaid.run();<\/script></body>';
    if (kind === "react") return '<!doctype html>' + base() + '<body><div id="root"></div>' +
      '<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"><\/script>' +
      '<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"><\/script>' +
      '<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.24.7/babel.min.js"><\/script>' +
      '<script type="text/babel" data-presets="react">' + code + '<\/script></body>';
    return '<!doctype html>' + base() + '<body style="font-family:sans-serif;padding:16px">unsupported artifact kind</body>';
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
