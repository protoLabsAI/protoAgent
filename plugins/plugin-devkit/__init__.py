"""Plugin Devkit — the plugin-authoring kit + the reference plugin (ADR 0027).

The featured full-bundle example: in ONE plugin it contributes a **tool**
(`scaffold_plugin`), a **subagent** (`plugin-architect`), a bundled **skill**
(`skills/building-plugins`), a **workflow** (`workflows/design-plugin`), a
**console view** (`/guide`), and **config/settings** — every contribution type.
Enable it to let the agent build its own plugins: it has the *how* (the skill) and
the *doing* (the scaffold tool).

Read this file as a template — it's intentionally a worked example of each seam.
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import tool

from graph.subagents.config import SubagentConfig

# ── helpers ──────────────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-") or "plugin"


def _target_root(config: dict | None) -> Path:
    """Where scaffolded plugins are written: the configured ``target_dir`` (ADR
    0019) or, blank, the live plugins dir the loader discovers."""
    t = (config or {}).get("target_dir") or ""
    if t:
        return Path(t).expanduser()
    from graph.plugins.installer import live_plugins_dir
    return live_plugins_dir()


# Captured at register() so the scaffold tool can broadcast on the event bus (ADR 0039) —
# the devkit dogfoods its own lesson.
_REGISTRY = None


def _emit_scaffolded(pid: str, kind: str) -> None:
    try:
        if _REGISTRY is not None:
            _REGISTRY.emit("scaffolded", {"id": pid, "kind": kind})  # → "plugin-devkit.scaffolded"
    except Exception:  # noqa: BLE001 — a bus hiccup must never fail the scaffold
        pass


_INIT_STUB = '''"""{name} — a protoAgent plugin (scaffolded by plugin-devkit)."""

from __future__ import annotations

from langchain_core.tools import tool
{view_import}

def register(registry):
    """Wire this plugin's contributions into the agent (ADR 0018)."""
{registrations}
    # Event bus (ADR 0039) — coordinate without importing other plugins:
    #   registry.emit("did_something", {{"id": 1}})   # → "{id_us}.did_something" on the bus
    #   registry.on("other-plugin.*", lambda evt: ...) # react to anyone's events
'''

_TOOL_STUB = '''
    @tool
    def {id_us}_hello(name: str = "world") -> str:
        """Say hello — replace with your tool's real work."""
        return f"hello, {{name}}, from {id}"
    registry.register_tool({id_us}_hello)
'''

_VIEW_STUB = '''
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse
    router = APIRouter()

    @router.get("/view")
    async def _view():
        # The console iframes this page (ADR 0038) and postMessages it the operator
        # bearer + theme (ADR 0026). Serve a sandboxed page; for untrusted/generated
        # content keep the inner frame sandbox="allow-scripts" with no same-origin.
        return HTMLResponse("<!doctype html><body style='background:#0a0a0c;color:#ededed;"
                            "font-family:system-ui;padding:32px'><h1>{name}</h1>"
                            "<p>Your plugin view — replace this page.</p></body>")
    # Mounted under /api/plugins/{id} so it inherits the operator bearer gate (ADR 0026).
    registry.register_router(router, prefix="/api/plugins/{id}")
'''

_MANIFEST_STUB = """id: {id}
name: {name}
version: 0.1.0
description: >-
  {summary}
enabled: false
config_section: {id_us}
# Event bus (ADR 0039) — topics this plugin broadcasts / listens for (optional, for discovery):
# emits: ["{id_us}.something"]
# subscribes: ["other-plugin.*"]
{views_block}"""

_SKILL_STUB = """---
name: {id}-skill
description: >-
  Describe WHEN to use this skill (the trigger). Replace this with the cases that
  should invoke {name}.
---

# {name}

Replace this body with the procedure the agent should follow.
"""

_WORKFLOW_STUB = """name: {id}-workflow
description: A scaffolded workflow — replace the steps with your recipe.
version: 1
inputs:
  - name: request
    description: What to do.
    required: true
steps:
  - id: do
    subagent: researcher
    prompt: |
      {{{{inputs.request}}}}
output: "{{{{steps.do.output}}}}"
"""

# Communication plugin (ADR 0029) — a ChatAdapter on the shared wirer.
_COMMS_MANIFEST_STUB = """id: {id}
name: {name}
version: 0.1.0
description: >-
  {summary}
enabled: false
config_section: {id}
config:
  enabled: false
  admin_ids: []
secrets: [bot_token]
settings:
  - {{ key: enabled, label: "Enable {name}", type: bool, description: "Inbound gateway. Needs the token below; reconnects live on save." }}
  - {{ key: bot_token, label: "Bot token", type: secret, description: "Platform bot token. Use Test connection to verify." }}
  - {{ key: admin_ids, label: "Admin user IDs", type: string_list, description: "User IDs allowed to message the bot (one per line). Empty = anyone." }}
"""

_COMMS_INIT_STUB = '''"""{name} — a communication plugin (scaffolded by plugin-devkit, ADR 0029).

Implement the transport (connect / receive / send); the shared wirer handles
admin-gating, per-conversation threads, agent invoke, reply-chunking, lifecycle,
and the Test route. Reference: plugins/telegram. Guide:
docs/guides/communication-plugins.md.
"""

from __future__ import annotations

from graph.plugins.chat_surface import InboundMessage, register_chat_surface


class {cls}Adapter:
    id = "{id}"
    chunk_limit = 4000  # your platform's message-length cap (0 = no chunking)

    def configured(self, cfg) -> bool:
        return bool((cfg.get("bot_token") or "").strip())

    async def validate(self, cfg):
        """Verify the token (the Test button). Return (ok, identity|None, error|None)."""
        token = (cfg.get("bot_token") or "").strip()
        if not token:
            return (False, None, "No bot token set.")
        # TODO: call your platform's auth/me endpoint and return its identity.
        return (True, None, None)

    async def run(self, handle, *, cfg, host):
        """Connect, then loop. For each inbound message:
            async def reply(text): ...send it back...
            await handle(InboundMessage(text=..., user_id=..., channel_id=..., reply=reply))
        Runs until cancelled."""
        raise NotImplementedError("Implement {cls}Adapter.run — see plugins/telegram.")


def register(registry):
    register_chat_surface(registry, {cls}Adapter())
'''


def _build_scaffold_tool(config: dict | None):
    """Closes over the plugin's config so the tool knows where to write."""

    @tool
    def scaffold_plugin(
        name: str,
        summary: str = "A protoAgent plugin.",
        with_tool: bool = True,
        with_view: bool = False,
        with_skill: bool = False,
        with_workflow: bool = False,
        with_comms: bool = False,
    ) -> str:
        """Scaffold a new protoAgent plugin SKELETON on disk (manifest + register()
        + optional view/skill/workflow stubs), ready to fill in and enable.

        Set ``with_comms=True`` for a **communication plugin** (a chat integration —
        Discord/Slack/Telegram-style): it writes a `ChatAdapter` skeleton on the
        shared wirer (ADR 0029) instead of the default tool plugin, so you only fill
        in connect/receive/send. See the communication-plugins guide.

        Writes into the live plugins dir (or the configured target_dir). Does NOT
        enable it or run any code — review, fill in the logic, then add the id to
        plugins.enabled and restart. Returns the path + next steps. Use this when
        asked to create/build/scaffold a plugin; see the building-plugins skill for
        the contract.
        """
        pid = _slug(name)
        id_us = pid.replace("-", "_")
        root = _target_root(config)
        target = root / pid
        if target.exists():
            return f"✗ {pid!r} already exists at {target} — pick another name or remove it first."
        (target).mkdir(parents=True)

        # Communication plugin (ADR 0029) — a ChatAdapter, different shape from the
        # default tool plugin; the tool/view stubs don't apply.
        if with_comms:
            cls = "".join(w[:1].upper() + w[1:] for w in re.split(r"[-_ ]+", name) if w) or id_us.title()
            (target / "protoagent.plugin.yaml").write_text(
                _COMMS_MANIFEST_STUB.format(id=pid, name=name, summary=summary)
            )
            (target / "__init__.py").write_text(
                _COMMS_INIT_STUB.format(id=pid, name=name, cls=cls)
            )
            made = ["protoagent.plugin.yaml", "__init__.py (ChatAdapter)"]
            if with_skill:
                sk = target / "skills" / f"{pid}-skill"
                sk.mkdir(parents=True)
                (sk / "SKILL.md").write_text(_SKILL_STUB.format(id=pid, name=name))
                made.append("skills/")
            _emit_scaffolded(pid, "comms")
            return (
                f"✓ scaffolded communication plugin {pid!r} at {target}\n"
                f"  wrote: {', '.join(made)}\n"
                f"  next: implement {cls}Adapter.run/validate (see plugins/telegram), then enable —\n"
                f"        add '{pid}' to plugins.enabled and restart.\n"
                f"  (see docs/guides/communication-plugins.md)"
            )

        views_block = (
            f"views:\n  - {{ id: main, label: \"{name}\", icon: Boxes, path: /api/plugins/{pid}/view }}\n"
            if with_view else ""
        )
        (target / "protoagent.plugin.yaml").write_text(
            _MANIFEST_STUB.format(id=pid, name=name, summary=summary, id_us=id_us, views_block=views_block)
        )

        registrations = ""
        if with_tool:
            registrations += _TOOL_STUB.format(id=pid, id_us=id_us)
        if with_view:
            registrations += _VIEW_STUB.format(id=pid, name=name)
        if not registrations.strip():
            registrations = "    pass  # add registry.register_* calls here\n"
        (target / "__init__.py").write_text(
            _INIT_STUB.format(name=name, view_import="", registrations=registrations, id_us=id_us)
        )

        made = ["protoagent.plugin.yaml", "__init__.py"]
        if with_skill:
            sk = target / "skills" / f"{pid}-skill"
            sk.mkdir(parents=True)
            (sk / "SKILL.md").write_text(_SKILL_STUB.format(id=pid, name=name))
            made.append("skills/")
        if with_workflow:
            (target / "workflows").mkdir()
            (target / "workflows" / f"{pid}.yaml").write_text(_WORKFLOW_STUB.format(id=pid))
            made.append("workflows/")

        _emit_scaffolded(pid, "plugin")
        return (
            f"✓ scaffolded plugin {pid!r} at {target}\n"
            f"  wrote: {', '.join(made)}\n"
            f"  next: fill in the logic, then enable — add '{pid}' to plugins.enabled and restart.\n"
            f"  (see the building-plugins skill for the full contract)"
        )

    return scaffold_plugin


def _plugin_architect() -> SubagentConfig:
    """A text-only subagent that turns a plain-English request into a concrete
    plugin spec. Used by the design-plugin workflow."""
    return SubagentConfig(
        name="plugin-architect",
        description=(
            "Designs a protoAgent plugin from a plain-English request — picks the "
            "contribution types, drafts a complete protoagent.plugin.yaml, and "
            "sketches register(). Use before scaffolding a non-trivial plugin."
        ),
        system_prompt=(
            "You design protoAgent plugins. Given a request, output: (1) the plugin "
            "id + name, (2) which contributions it needs (tools / subagents / "
            "SKILL.md skills / workflows / console views / config+secrets / event-bus "
            "emits+subscribes), (3) a complete `protoagent.plugin.yaml`, and (4) a "
            "`register(registry)` sketch. Follow the plugin contract: the manifest is "
            "data; code runs only on enable; config_section is a string; skills/ and "
            "workflows/ subdirs auto-load; declare requires_pip, don't assume it's "
            "installed; console views are sandboxed iframes served under /api/plugins/<id> "
            "(ADR 0038); plugins coordinate via the event bus (registry.emit/on), never "
            "by importing each other (ADR 0039). Keep it to the smallest plugin that "
            "satisfies the request."
        ),
        tools=[],  # pure reasoning — it produces a spec, it doesn't act
    )


def _build_guide_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/guide")
    async def _guide():
        html = """<!doctype html><html><head><meta charset="utf-8"><style>
          html,body{margin:0;background:#0a0a0c;color:#ededed;font-family:ui-sans-serif,system-ui,sans-serif}
          .wrap{max-width:52ch;margin:0 auto;padding:40px 28px;line-height:1.6}
          h1{color:#a78bfa;font-size:22px;margin:0 0 4px} h2{color:#a78bfa;font-size:15px;margin:22px 0 6px}
          code{background:#19191d;color:#a78bfa;padding:2px 6px;border-radius:5px;font-size:13px}
          p,li{color:#a3a3ad;font-size:14px} ul{padding-left:18px}
        </style></head><body><div class="wrap">
          <h1>Plugin Devkit</h1>
          <p>This plugin gives the agent what it needs to build plugins — and is itself
          the full-bundle example. Ask the agent: <em>"build a plugin that …"</em>.</p>
          <h2>It contributes</h2>
          <ul>
            <li><code>scaffold_plugin</code> tool — writes a new plugin skeleton</li>
            <li><code>plugin-architect</code> subagent + <code>design-plugin</code> workflow — request → spec</li>
            <li>the <code>building-plugins</code> skill — the authoring contract</li>
            <li>this console view + config/settings</li>
            <li>emits <code>plugin-devkit.scaffolded</code> on the bus when it scaffolds (ADR 0039)</li>
          </ul>
          <h2>The plugin contract</h2>
          <ul>
            <li><code>protoagent.plugin.yaml</code> — manifest (data; read without importing)</li>
            <li><code>__init__.py</code> — <code>register(registry)</code> (tools, subagents, routes, MCP)</li>
            <li><code>skills/</code> + <code>workflows/</code> — auto-discovered data</li>
            <li><code>views:</code> — a rail icon → a <strong>sandboxed iframe</strong> of a page your plugin
                serves (ADR 0038). Mount its router under <code>/api/plugins/&lt;id&gt;</code> so it's
                bearer-gated; the console hands it the token + theme (ADR 0026).</li>
          </ul>
          <h2>Events (ADR 0039)</h2>
          <ul>
            <li><code>registry.emit("x", data)</code> → <code>&lt;id&gt;.x</code> on the bus · <code>registry.on("other.*", fn)</code> to react</li>
            <li>declare <code>emits:</code> / <code>subscribes:</code> in the manifest (discovery)</li>
            <li>an event under <code>&lt;id&gt;.*</code> lights your plugin's rail icon (notification dot)</li>
            <li>plugins coordinate <em>only</em> via the bus — never import each other</li>
          </ul>
          <p>Fork components (compiled in, not sandboxed) use the build-time <code>src/ext</code> seam (ADR 0038).</p>
          <p>Full guides: <code>/guides/plugins</code> · <code>/guides/plugin-registry</code> · install ≠ enable ≠ trust.</p>
        </div></body></html>"""
        return HTMLResponse(html)

    return router


def register(registry) -> None:
    """Every contribution type, in one plugin (the point of the devkit)."""
    global _REGISTRY
    _REGISTRY = registry                                            # for the bus emit (ADR 0039)
    registry.register_tool(_build_scaffold_tool(registry.config))  # a tool
    registry.register_subagent(_plugin_architect())                # a subagent
    # Gated under /api/plugins/plugin-devkit (ADR 0026) — the console iframes /guide.
    registry.register_router(_build_guide_router(), prefix="/api/plugins/plugin-devkit")
    # skills/ + workflows/ auto-discover — no call needed (ADR 0027).
