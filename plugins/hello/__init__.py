"""Example protoAgent plugin.

A plugin is a directory with a ``protoagent.plugin.yaml`` manifest and a module
exposing ``register(registry)``. The registry collects what the plugin
contributes. This example shows **all five** contribution types (ADR 0001 + 0018):

- a **tool** (``hello``),
- a bundled **SKILL.md** directory (``skills/``),
- an HTTP **route** (``GET /plugins/hello/ping``),
- a lifecycle **surface** (logs on startup/shutdown),
- a **subagent** (``hello_helper``).

Enable it with ``plugins: { enabled: [hello] }`` in config. A fork drops a
directory like this in ``plugins/`` and never edits core ``server.py``.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.hello")


@tool
async def hello(name: str = "world") -> str:
    """Return a friendly greeting — proof the plugin loaded and its tool is live."""
    return f"Hello, {name}! (from the example protoAgent plugin)"


def _build_router(greeting: str):
    """A tiny FastAPI router — mounted at ``/plugins/hello`` (ADR 0018). Closes
    over the plugin's configured greeting (ADR 0019) to prove it reads its config."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/ping")
    async def _ping() -> dict:
        return {"ok": True, "plugin": "hello", "greeting": greeting}

    # A console view (ADR 0026): the manifest's `views:` entry points the rail
    # iframe here. A plugin serves whatever UI it wants; this demo is a tiny
    # self-contained page that matches the console's dark ground.
    @router.get("/view")
    async def _view():
        from fastapi.responses import HTMLResponse

        # The page listens for the console's `protoagent:init` postMessage (ADR
        # 0026 bridge) — the operator bearer (use it for your own API calls) + the
        # console theme tokens (apply them to match the look). Reference receiver.
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;height:100%;background:#0a0f14;color:#e6e6e6;
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;
    display:flex;align-items:center;justify-content:center;text-align:center}}
  .card{{padding:32px}} h1{{color:#9b87f2;margin:0 0 8px;font-size:22px}}
  p{{color:#9aa0aa;font-size:14px;max-width:38ch;line-height:1.6}}
  code{{background:#161b22;padding:2px 6px;border-radius:5px;color:#9b87f2}}
  #bridge{{margin-top:14px;font-size:12px;color:#46c46a}}
</style></head><body><div class="card">
  <h1>{greeting} from a plugin view</h1>
  <p>Served by <code>plugins/hello</code> at <code>/plugins/hello/view</code> and
  embedded in the console rail via the <code>views:</code> manifest block.
  A fork drops a directory like this and gets its own rail icon +
  dashboard — no console rebuild.</p>
  <p id="bridge">awaiting console handshake…</p>
</div>
<script>
  function applyTheme(t) {{
    if (!t) return;
    if (t.bg) document.body.style.background = t.bg;
    if (t.fg) document.body.style.color = t.fg;
  }}
  window.addEventListener("message", function (e) {{
    var m = e.data || {{}};
    if (m.type === "protoagent:init") {{
      document.getElementById("bridge").textContent =
        (m.token ? "✓ authed by the console" : "no token (open API)") +
        (m.theme ? " · theme received" : "");
      applyTheme(m.theme);
    }} else if (m.type === "protoagent:theme") {{
      // Live theme update — the console re-posts on any edit / agent switch.
      applyTheme(m.theme);
    }}
  }});
</script>
</body></html>"""
        return HTMLResponse(html)

    return router


async def _surface_start() -> None:
    """A no-op lifecycle surface — a real one would open a gateway/listener here
    (it runs on the server loop, like the Discord gateway)."""
    log.info("[hello] example surface started")


async def _surface_stop() -> None:
    log.info("[hello] example surface stopped")


def _build_subagent():
    """A minimal delegate the lead agent can call via ``task``/``task_batch``."""
    from graph.subagents.config import SubagentConfig

    return SubagentConfig(
        name="hello_helper",
        description="Example plugin subagent — echoes a friendly status. Proof a "
        "plugin can register a delegate without editing SUBAGENT_REGISTRY.",
        system_prompt="You are the hello_helper, a tiny example subagent. Reply "
        "briefly and cheerfully, then stop.",
        tools=["current_time"],
    )


def register(registry) -> None:
    """Entry point — called once at load with a PluginRegistry."""
    greeting = registry.config.get("greeting", "Hello")   # plugin config (ADR 0019)
    registry.register_tool(hello)
    registry.register_skill_dir("skills")            # bundled SKILL.md folder
    registry.register_router(_build_router(greeting))  # → /plugins/hello/ping (ADR 0018)
    registry.register_surface(_surface_start, stop=_surface_stop, name="hello-surface")
    registry.register_subagent(_build_subagent())
