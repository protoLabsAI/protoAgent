"""execute_code plugin — a sandboxed Python code interpreter, as an opt-in plugin.

Moved out of the lean core (bd-37i). The model writes a Python script that runs
in an isolated subprocess; its stdout comes back. The script can do anything
Python can — compute, parse, transform data. Its
headline use is programmatic tool-calling (call several tools, compose their
results in code, print only what matters — collapsing a think→call→read chain
into one turn), but it is **not** limited to tool calls.

This runs **arbitrary model-authored code** — subprocess + scrubbed env (no
credentials) + hard timeout is isolation, NOT a true sandbox (the script can
reach the disk/network as the server user; the `tools` allowlist scopes only the
bridge, not what code runs) — so it ships DISABLED; enable only for a trusted
model or inside a hardened container.

It uses the late-tools seam (``register_late_tool_factory``) because the tool
must proxy the FULLY assembled toolset, which a normal ``register_tool`` can't
see. On the packaged desktop build the child runs on the managed CPython
runtime (ADR 0094), provisioned once from Settings ▸ Tools; until then the tool
answers with the install path instead of silently not existing — the old
"don't register when frozen" gate presented a toggle that structurally did
nothing (#2137).
"""

from __future__ import annotations

import logging
import sys

from . import kernel
from .engine import _DEFAULT_BRIDGE_TOOLS, build_execute_code_tool

log = logging.getLogger("protoagent.plugins.execute_code")


async def _on_session_event(payload) -> None:
    """Stop a session's kernel when its chat ends (ADR 0134 D2 teardown).

    Subscribed in :func:`register` to BOTH bus topics; the plugin never reads chat
    state directly, it matches the publishers' payloads (read both to confirm the
    shape before matching):

    * ``session.changed`` with ``status == "deleted"`` — the chat was deleted or
      cleared (``server.session_state.note_session_deleted`` publishes the deleted
      status for a delete AND a ``retire=false`` clear). A status-only transition
      (idle/running/paused) is active use and is left alone.
    * ``session.closed`` — the tab closed (``server.session_registry``); ALWAYS stop,
      which covers ADR 0134's "always when an incognito chat closes" without the
      plugin reading incognito state.

    The payload is the bus envelope ``{event, data, seq, ts}``; the entity lives under
    ``data`` (``session_id`` key, ``status`` field). Best-effort — the bus isolates a
    raised handler, but the chat is already gone, so swallow and log."""
    try:
        data = (payload or {}).get("data") or {}
        event = (payload or {}).get("event") or ""
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            return
        if event == "session.closed" or data.get("status") == "deleted":
            await kernel.stop_session(session_id)
    except Exception:  # noqa: BLE001 — a teardown handler must never break the bus
        log.exception("[execute_code] session-end kernel teardown failed")


def _effective_tools(explicit, extra, all_tools):
    """Fold the additive ``extra_tools`` config into the bridge allowlist (#3701).

    ``explicit`` (the ``tools`` config) REPLACES the curated default when set;
    ``extra`` (the ``extra_tools`` config) is UNIONED on top of whichever base
    applies — the curated ``_DEFAULT_BRIDGE_TOOLS`` when ``tools`` is empty,
    otherwise the explicit list. That lets an operator bridge one plugin read
    tool (say ``github_search_issues``) without re-listing every core tool.

    Names that aren't registered tools — and ``execute_code`` itself, which is
    never self-bridgeable — are dropped with a single log line, never raised: a
    typo in Settings must not wedge tool assembly. Removed tool names (ADR 0125:
    ``memory_recall`` and friends) are translated to their new names in both
    lists, with a one-time warning, and an explicit ``tools`` name that isn't
    registered is warned about once. Returns ``explicit`` (``None`` still lets the
    engine apply its curated default) when ``extra_tools`` widens nothing.
    """
    from graph.tool_renames import migrate_tool_names, warn_once

    registered = {t.name for t in all_tools}
    unmatched = set()

    def expand(names, source):
        expanded = []
        for name in names:
            if isinstance(name, str) and name.startswith("mcp:") and name.endswith("/*"):
                server = name[4:-2]
                if server and not any(c in server for c in "/*?[]"):
                    matches = sorted(n for n in registered if n.startswith(f"{server}__"))
                    if not matches:
                        unmatched.add(name)
                        warn_once(
                            source, name,
                            "[execute_code] %s: '%s' matches no registered tools on this agent.",
                            source, name,
                        )
                    # An unmatched explicit selector stays ineligible, preventing an
                    # empty expansion from falling back to the curated defaults.
                    expanded.extend(matches or [name])
                    continue
            expanded.append(name)
        return expanded

    if explicit:
        # ADR 0125: an allowlist written before the memory tool rename still names the
        # old tools; translate it (warned once) rather than silently bridging nothing.
        explicit = expand(migrate_tool_names(explicit, source="execute_code.tools"), "execute_code.tools")
        for name in explicit:
            if name not in registered and name != "execute_code" and name not in unmatched:
                warn_once(
                    "execute_code.tools",
                    name,
                    "[execute_code] tools: '%s' is not a registered tool on this agent; scripts can't call it.",
                    name,
                )
    extra = expand(migrate_tool_names(extra, source="execute_code.extra_tools"), "execute_code.extra_tools")
    if not extra:
        return explicit

    additions, dropped = set(), []
    for name in extra:
        if name == "execute_code" or name not in registered:
            if name not in unmatched:
                dropped.append(name)
        else:
            additions.add(name)
    if dropped:
        log.warning(
            "[execute_code] extra_tools: ignoring unregistered/ineligible name(s): %s",
            ", ".join(sorted(dropped)),
        )
    base = set(explicit) if explicit else set(_DEFAULT_BRIDGE_TOOLS)
    return sorted(base | additions)


def register(registry) -> None:
    """Wire execute_code as a late tool (ADR 0001 + the late-tools seam)."""
    if getattr(sys, "frozen", False):
        # Packaged desktop build: the child spawns the managed CPython (ADR 0094).
        # Register regardless of provisioning state — an unprovisioned runtime answers
        # every call with the actionable install path, which is honest-and-visible
        # where the old skip was silent (#2137).
        log.info("[execute_code] packaged desktop build — child runs on the managed Python runtime")

    def _factory(all_tools, config):
        # live_config(), not the register-time snapshot: factories run at every
        # graph (re)build, so an edited `tools` allowlist takes effect on reload
        # instead of waiting for a full plugin reload (ADR 0103 S4). `config` is
        # the full graph config the seam passes — the enforcement-parity source.
        cfg = registry.live_config() if hasattr(registry, "live_config") else registry.config
        # ADR 0134 D2: push the operator's kernel limits into the process-wide pool on
        # each (re)build, so an edited idle TTL / cap takes effect without a restart.
        # Clamp to sane floors — a zero/negative TTL would sweep a just-used kernel and
        # a zero cap would stop every kernel the moment it starts.
        kernel.configure(
            idle_ttl=max(1.0, float(cfg.get("kernel_idle_ttl", 600.0))),
            max_kernels=max(1, int(cfg.get("max_kernels", 4))),
        )
        return build_execute_code_tool(
            all_tools,
            tools=_effective_tools(cfg.get("tools") or None, cfg.get("extra_tools") or [], all_tools),
            timeout=float(cfg.get("timeout", 30.0)),
            truncate=int(cfg.get("output_truncate", 6000)),
            graph_config=config,
            session_state=bool(cfg.get("session_state", False)),
        )

    registry.register_late_tool_factory(_factory)

    # ADR 0134 D2 chat teardown: stop a session's kernel when its chat is deleted or
    # cleared (session.changed → deleted) or its tab closes (session.closed). Bundle-
    # owned during register() (ADR 0039 — the server binds these after the bus is up).
    registry.on("session.changed", _on_session_event)
    registry.on("session.closed", _on_session_event)

    # Periodic idle-sweep backstop, plus stop-every-kernel on plugin unload/reload and
    # server shutdown. Fresh closures each register() so a reload is a "different stop"
    # (register_surface #3593): the outgoing surface's stop() runs — stopping every
    # kernel — before the reloaded one starts its sweeper, so no process is orphaned.
    def _start_surface():
        kernel.start_sweeper()

    async def _stop_surface():
        await kernel.shutdown()

    registry.register_surface(_start_surface, stop=_stop_surface, name="execute_code-kernels")
