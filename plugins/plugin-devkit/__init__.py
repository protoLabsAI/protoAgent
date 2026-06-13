"""Plugin Devkit — the plugin-authoring kit + the reference plugin (ADR 0027).

The featured full-bundle example: in ONE plugin it contributes **tools**
(`scaffold_plugin`, `scaffold_bundle`, `enable_plugin`, `reload_plugins`), a
**subagent** (`plugin-architect`), a bundled **skill** (`skills/building-plugins`),
a **workflow** (`workflows/design-plugin`), a **console view** (`/guide`), and
**config/settings** — every contribution type.

Enable it to let the agent build its own plugins *and test them live*: it has the
*how* (the skill), the *doing* (scaffold), and — new — the *running it* (enable +
hot-reload, no restart). The scaffolders themselves live in core
(`graph.plugins.scaffold`) so the `plugin new` CLI shares them; this file is the
agent-facing half + the live-enable that needs the running graph.
"""

from __future__ import annotations

from langchain_core.tools import tool

from graph.plugins import scaffold
from graph.subagents.config import SubagentConfig

# Captured at register() so the scaffold tools can broadcast on the event bus (ADR 0039) —
# the devkit dogfoods its own lesson.
_REGISTRY = None


def _emit_scaffolded(pid: str, kind: str) -> None:
    try:
        if _REGISTRY is not None:
            _REGISTRY.emit("scaffolded", {"id": pid, "kind": kind})  # → "plugin-devkit.scaffolded"
    except Exception:  # noqa: BLE001 — a bus hiccup must never fail the scaffold
        pass


def _plugin_meta(pid: str) -> dict | None:
    """The latest load meta for a plugin (``{id, loaded, error, tools, …}``) off the
    runtime — set by the loader on every (re)load. ``None`` if the loader didn't see it."""
    from runtime.state import STATE

    return next((m for m in (getattr(STATE, "plugin_meta", []) or []) if m.get("id") == pid), None)


def _live_enable(pid: str) -> tuple[bool, str]:
    """Enable a plugin in the RUNNING agent + hot-reload — the same path the console
    enable toggle uses (#822): tools/subagents/middleware/MCP rebuild with the graph
    and the plugin's router hot-mounts, so a freshly enabled plugin is live with no
    restart. No-ops cleanly when there's no live graph (the CLI / tests).

    Crucially it confirms the plugin actually LOADED — ``load_plugins`` is best-effort
    per-plugin, so a ``register()`` that raises is *skipped*, not fatal: the config
    reload "succeeds" while the plugin is silently not live. We surface that instead of
    a false 'loaded live', so the agent can fix-and-reload instead of testing a no-op."""
    try:
        from runtime.state import STATE

        if getattr(STATE, "graph", None) is None:
            return (False, "not running — enable it when the agent is live")
        cfg = STATE.graph_config
        enabled = [p for p in (getattr(cfg, "plugins_enabled", []) or []) if p != pid]
        enabled.append(pid)
        disabled = [p for p in (getattr(cfg, "plugins_disabled", []) or []) if p != pid]
        from server.agent_init import _apply_settings_changes

        ok, msgs = _apply_settings_changes(
            config={"plugins": {"enabled": enabled, "disabled": disabled}}
        )
        if not ok:
            return (False, "; ".join(msgs) or "reload failed")
        meta = _plugin_meta(pid)
        if meta is None:
            return (False, "enabled in config, but the loader didn't discover it — check the id and that it's in the plugins dir")
        if not meta.get("loaded"):
            return (False, f"enabled, but it FAILED to load: {meta.get('error') or 'unknown error'} — fix __init__.py, then call reload_plugins")
        return (True, "enabled + loaded live")
    except Exception as e:  # noqa: BLE001 — enable is best-effort; the skeleton still landed
        return (False, f"auto-enable failed: {e}")


def _build_scaffold_tool(config: dict | None):
    """Closes over the plugin's config so the tool knows where to write."""
    target_dir = (config or {}).get("target_dir") or None

    @tool
    def scaffold_plugin(
        name: str,
        summary: str = "A protoAgent plugin.",
        with_tool: bool = True,
        with_view: bool = False,
        with_skill: bool = False,
        with_workflow: bool = False,
        with_comms: bool = False,
        with_tests: bool = False,
        enable: bool = True,
    ) -> str:
        """Scaffold a new protoAgent plugin SKELETON on disk AND enable it live —
        so you can build a plugin and test it in the SAME session, no restart.

        Writes the manifest + ``register()`` + optional view/skill/workflow stubs,
        then (``enable=True``, the default) turns it on and hot-reloads the agent:
        its tools/views are live on your NEXT turn. Iterate by editing the plugin's
        ``__init__.py`` and calling ``reload_plugins`` to pick up the change live.

        Set ``with_comms=True`` for a **communication plugin** (Discord/Slack/Telegram-
        style): it writes a ``ChatAdapter`` skeleton (ADR 0029) — you fill in
        connect/receive/send, then enable it from Settings (it needs a token), so
        comms plugins are NOT auto-enabled here.

        Set ``with_tests=True`` to also write a host-free test suite + CI +
        requirements-dev + pyproject — use it when scaffolding a STANDALONE-repo
        plugin (its own git repo) so it's shippable + green from birth; skip it for a
        plugin bundled inside protoAgent (which rides the host's tests/CI).

        Use this when asked to create/build/scaffold a plugin; see the building-plugins
        skill for the contract.
        """
        try:
            res = scaffold.scaffold_plugin(
                name, summary=summary, with_tool=with_tool, with_view=with_view,
                with_skill=with_skill, with_workflow=with_workflow, with_comms=with_comms,
                with_tests=with_tests, target_dir=target_dir,
            )
        except FileExistsError as e:
            return f"✗ {scaffold.slug(name)!r} already exists at {e} — pick another name or remove it first."

        _emit_scaffolded(res.id, res.kind)
        kind_label = "communication plugin" if res.kind == "comms" else res.kind
        lines = [f"✓ scaffolded {kind_label} {res.id!r} at {res.path}", f"  wrote: {', '.join(res.made)}"]

        if res.kind == "comms":
            cls = scaffold._class_name(name, res.id.replace("-", "_"))
            lines.append(
                f"  next: implement {cls}Adapter.run/validate (see plugins/telegram), then enable it\n"
                f"        from Settings (it needs a bot token). Guide: docs/guides/communication-plugins.md"
            )
            return "\n".join(lines)

        if enable:
            ok, detail = _live_enable(res.id)
            if ok:
                hello = f"{res.id.replace('-', '_')}_hello" if with_tool else "its tools"
                lines.append(f"  ✓ {detail} — call {hello} on your NEXT turn to test it (no restart).")
                lines.append(f"  iterate: edit {res.path}/__init__.py, then call reload_plugins to go live.")
            else:
                lines.append(f"  ⚠ scaffolded but not auto-enabled ({detail}) — call enable_plugin({res.id!r}).")
        else:
            lines.append(f"  next: fill in the logic, then call enable_plugin({res.id!r}) to load it live.")
        lines.append("  (see the building-plugins skill for the full contract)")
        return "\n".join(lines)

    return scaffold_plugin


def _build_scaffold_bundle_tool(config: dict | None):
    target_dir = (config or {}).get("target_dir") or None

    @tool
    def scaffold_bundle(
        name: str,
        summary: str = "A protoAgent plugin bundle.",
        members: list[dict] | None = None,
        enabled: list[str] | None = None,
    ) -> str:
        """Scaffold a plugin BUNDLE (ADR 0040) — a ``protoagent.bundle.yaml`` that
        names a set of plugins to install + enable together (like the PM stack).

        ``members`` is a list of ``{id, url, ref}`` (a git plugin) or
        ``{id, builtin: true}`` (one that ships with protoAgent); omit it for a
        REPLACE_ME template. ``enabled`` defaults to every member.

        A bundle is a reference manifest (no code) — it's not enabled live like a
        plugin. Commit/push it, then install the whole stack with
        ``plugin install <bundle-repo-url>``.
        """
        try:
            res = scaffold.scaffold_bundle(
                name, summary=summary, members=members, enabled=enabled, target_dir=target_dir,
            )
        except FileExistsError as e:
            return f"✗ {scaffold.slug(name)!r} already exists at {e} — pick another name or remove it first."
        _emit_scaffolded(res.id, res.kind)
        return (
            f"✓ scaffolded bundle {res.id!r} at {res.path}\n"
            f"  wrote: {', '.join(res.made)}\n"
            f"  next: fill in the member plugins (git url + ref, or builtin: true), then commit/push it\n"
            f"        and install the stack: `plugin install <this-repo-url>` (ADR 0040)."
        )

    return scaffold_bundle


@tool
def enable_plugin(plugin_id: str) -> str:
    """Enable an already-present plugin (one you scaffolded or installed) by id and
    hot-reload it live — no restart. Use when a plugin is on disk but turned off."""
    ok, detail = _live_enable(plugin_id)
    return f"✓ {plugin_id}: {detail}" if ok else f"✗ {plugin_id}: {detail}"


@tool
def reload_plugins() -> str:
    """Hot-reload all enabled plugins — re-exec their code (every file, not just
    ``__init__.py``) so edits you made take effect WITHOUT a restart. Use after editing
    a plugin you're iterating on; the new tools/views are live on your NEXT turn.

    Reports which plugins FAILED to load (a syntax error, a bad import) so you fix-and-
    reload instead of testing stale/no-op code — a failed plugin is skipped, never fatal."""
    try:
        from runtime.state import STATE

        if getattr(STATE, "graph", None) is None:
            return "✗ no live agent to reload (run inside the server)."
        from server.agent_init import _apply_settings_changes

        ok, msgs = _apply_settings_changes()  # bare call = pure reload (picks up file edits)
        if not ok:
            return f"✗ reload failed: {'; '.join(msgs)}"
        failed = [
            (m["id"], m.get("error") or "unknown error")
            for m in (getattr(STATE, "plugin_meta", []) or [])
            if not m.get("loaded")
        ]
        if failed:
            detail = "; ".join(f"{pid}: {err}" for pid, err in failed)
            return f"⚠ reloaded, but these plugins FAILED to load — fix the code and reload again: {detail}"
        return "✓ reloaded — your plugin edits are live on the next turn."
    except Exception as e:  # noqa: BLE001
        return f"✗ reload failed: {e}"


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
            "installed; console views are sandboxed iframes — serve the PAGE on the public "
            "/plugins/<id> prefix (an iframe load can't carry a bearer) and its DATA on the "
            "gated /api/plugins/<id> (ADR 0026/0038); plugins coordinate via the event bus "
            "(registry.emit/on), never "
            "by importing each other (ADR 0039). Keep it to the smallest plugin that "
            "satisfies the request."
        ),
        tools=[],  # pure reasoning — it produces a spec, it doesn't act
    )


def _build_guide_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    # This page is itself a four-rules-compliant view (the reference plugin should
    # model the contract): it derives a slug-aware BASE, links the DS plugin-kit off
    # it (so it themes live via --pl-* tokens), and serves NO bearer-gated data —
    # it's a static card, so it lives on the PUBLIC /plugins/<id> prefix.
    @router.get("/guide")
    async def _guide():
        html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script>
          // RULE 3 — slug-aware base ("" on host, "/agents/<slug>" through the fleet proxy).
          var BASE = location.pathname.split("/plugins/")[0];
          // RULE 4 — link the DS kit CSS off BASE so the card themes live (no hardcoded hex).
          (function(){ var l=document.createElement("link"); l.rel="stylesheet";
            l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
        </script>
        <style>
          html,body{margin:0;background:var(--pl-color-bg);color:var(--pl-color-fg);
            font-family:var(--pl-font-sans, ui-sans-serif,system-ui,sans-serif)}
          .wrap{max-width:54ch;margin:0 auto;padding:40px 28px;line-height:1.6}
          h1{color:var(--pl-color-accent);font-size:22px;margin:0 0 4px}
          h2{color:var(--pl-color-accent);font-size:15px;margin:22px 0 6px}
          code{background:var(--pl-color-bg-raised);color:var(--pl-color-accent);
            padding:2px 6px;border-radius:var(--pl-radius,5px);font-size:13px}
          p,li{color:var(--pl-color-fg-muted);font-size:14px} ul{padding-left:18px}
        </style></head><body><div class="wrap">
          <h1>Plugin Devkit</h1>
          <p>This plugin gives the agent what it needs to build plugins — and is itself
          the full-bundle example. Ask the agent: <em>"build a plugin that …"</em>.</p>
          <h2>Build it live (no restart)</h2>
          <ul>
            <li><code>scaffold_plugin</code> — writes a skeleton <strong>and enables it</strong>; its
                tools/view are live on the next turn (pass <code>with_tests</code> for a shippable repo)</li>
            <li>edit the plugin's <code>__init__.py</code>, then <code>reload_plugins</code> — your change goes live</li>
            <li><code>enable_plugin</code> — turn on a plugin that's on disk but off</li>
            <li><code>scaffold_bundle</code> — a <code>protoagent.bundle.yaml</code> stack (ADR 0040)</li>
          </ul>
          <h2>Also contributes</h2>
          <ul>
            <li><code>plugin-architect</code> subagent + <code>design-plugin</code> workflow — request → spec</li>
            <li>the <code>building-plugins</code> skill — the authoring contract</li>
            <li>this console view + config/settings; emits <code>plugin-devkit.scaffolded</code> (ADR 0039)</li>
          </ul>
          <h2>From the CLI</h2>
          <ul>
            <li><code>python -m server plugin new "My Plugin" --view --skill</code> — scaffold from the shell</li>
            <li><code>python -m server plugin new-bundle "My Stack" --member id=url@ref --builtin delegates</code></li>
          </ul>
          <h2>The plugin contract</h2>
          <ul>
            <li><code>protoagent.plugin.yaml</code> — manifest (data; read without importing)</li>
            <li><code>__init__.py</code> — <code>register(registry)</code> (tools, subagents, routes, MCP)</li>
            <li><code>skills/</code> + <code>workflows/</code> — auto-discovered data</li>
            <li><code>views:</code> — a rail icon → a <strong>sandboxed iframe</strong> of a page your plugin
                serves (ADR 0038). Serve the <strong>page</strong> on the public <code>/plugins/&lt;id&gt;</code>
                prefix (an iframe load can't carry a bearer); its <strong>data</strong> calls ride the
                gated <code>/api/plugins/&lt;id&gt;</code> via the DS kit's <code>apiFetch</code> (ADR 0026).</li>
          </ul>
          <h2>Events (ADR 0039)</h2>
          <ul>
            <li><code>registry.emit("x", data)</code> → <code>&lt;id&gt;.x</code> on the bus · <code>registry.on("other.*", fn)</code> to react</li>
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
    _REGISTRY = registry                                              # for the bus emit (ADR 0039)
    registry.register_tool(_build_scaffold_tool(registry.config))    # scaffold a plugin (+ enable live)
    registry.register_tool(_build_scaffold_bundle_tool(registry.config))  # scaffold a bundle
    registry.register_tool(enable_plugin)                            # turn on an on-disk plugin live
    registry.register_tool(reload_plugins)                           # pick up edits live
    registry.register_subagent(_plugin_architect())                  # a subagent
    # PUBLIC /plugins/plugin-devkit (ADR 0026) — the console iframes /guide, and an
    # iframe page-load can't carry a bearer, so the page route must NOT be gated.
    registry.register_router(_build_guide_router(), prefix="/plugins/plugin-devkit")
    # skills/ + workflows/ auto-discover — no call needed (ADR 0027).
