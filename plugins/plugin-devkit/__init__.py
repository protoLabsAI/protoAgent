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


_INIT_STUB = '''"""{name} — a protoAgent plugin (scaffolded by plugin-devkit)."""

from __future__ import annotations

from langchain_core.tools import tool
{view_import}

def register(registry):
    """Wire this plugin's contributions into the agent (ADR 0018)."""
{registrations}
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
        return HTMLResponse("<!doctype html><body style='background:#0a0a0c;color:#ededed;"
                            "font-family:system-ui;padding:32px'><h1>{name}</h1>"
                            "<p>Your plugin view — replace this page.</p></body>")
    registry.register_router(router)  # mounted at /plugins/{id}
'''

_MANIFEST_STUB = """id: {id}
name: {name}
version: 0.1.0
description: >-
  {summary}
enabled: false
config_section: {id_us}
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
    ) -> str:
        """Scaffold a new protoAgent plugin SKELETON on disk (manifest + register()
        + optional view/skill/workflow stubs), ready to fill in and enable.

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

        views_block = (
            f"views:\n  - {{ id: main, label: \"{name}\", icon: Boxes, path: /plugins/{pid}/view }}\n"
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
            _INIT_STUB.format(name=name, view_import="", registrations=registrations)
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
            "SKILL.md skills / workflows / console views / config+secrets), (3) a "
            "complete `protoagent.plugin.yaml`, and (4) a `register(registry)` "
            "sketch. Follow the plugin contract: the manifest is data; code runs "
            "only on enable; config_section is a string; skills/ and workflows/ "
            "subdirs auto-load; declare requires_pip, don't assume it's installed. "
            "Keep it to the smallest plugin that satisfies the request."
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
          </ul>
          <h2>The plugin contract</h2>
          <ul>
            <li><code>protoagent.plugin.yaml</code> — manifest (data; read without importing)</li>
            <li><code>__init__.py</code> — <code>register(registry)</code> (tools, subagents, routes, MCP)</li>
            <li><code>skills/</code> + <code>workflows/</code> — auto-discovered data</li>
            <li><code>views:</code> in the manifest — console rail views</li>
          </ul>
          <p>Full guide: <code>/guides/plugin-registry</code> · install ≠ enable ≠ trust.</p>
        </div></body></html>"""
        return HTMLResponse(html)

    return router


def register(registry) -> None:
    """Every contribution type, in one plugin (the point of the devkit)."""
    registry.register_tool(_build_scaffold_tool(registry.config))  # a tool
    registry.register_subagent(_plugin_architect())                # a subagent
    registry.register_router(_build_guide_router())                # routes + the view page
    # skills/ + workflows/ auto-discover — no call needed (ADR 0027).
