"""Operator-console request handlers (the bodies behind `register_operator_routes`).

ADR 0023 phase 3 finishes the half-done `operator_api/` extraction: the React
console's runtime-status / subagent / scheduler / goal / workflow / activity /
inbox / chat-command handlers used to be inline closures in ``server._main`` that
closed over the (then-ambient) globals. Now that runtime state lives in
``runtime.state.STATE``, they're plain module-level functions here; ``_main``
imports this module and passes the functions to ``register_operator_routes``
instead of defining 21 closures.

Bodies are unchanged from their former in-``_main`` form — dependencies are
imported under the same alias names the bodies use, and the one captured local
(the operator project root) is resolved live via
``server._resolve_operator_project_root()``.
"""

from __future__ import annotations

import hmac
import logging
import os

from events import ACTIVITY_CONTEXT
from graph.config_io import is_setup_complete as _operator_setup_complete
from graph.output_format import extract_output
from operator_api.runtime import build_runtime_status as _build_operator_status
from operator_api.subagents import (
    list_subagents as _operator_list_subagents,
    run_manual_subagent as _operator_run_manual_subagent,
    run_manual_subagent_batch as _operator_run_manual_subagent_batch,
)
from runtime.state import STATE
from server import AGENT_NAME_ENV, _event_bus, _resolve_operator_project_root

log = logging.getLogger("protoagent.server")


def _operator_allowed_dirs() -> list[str]:
    # The repo root is always operable (it's the default project);
    # config adds any extra project roots. Read live so a settings
    # reload takes effect without restarting the server.
    roots = [_resolve_operator_project_root()]
    if STATE.graph_config is not None:
        roots.extend(getattr(STATE.graph_config, "operator_allowed_dirs", []) or [])
    # Dedup (first-seen wins) — the project root is now commonly folded
    # into operator_allowed_dirs by the setup wizard, so duplicates are
    # frequent (bd-a7f).
    return list(dict.fromkeys(roots))


async def _operator_runtime_status():
    import asyncio

    # Live co-location check (#706) — re-evaluated per poll so the shell banner
    # appears/clears as siblings come and go. Quiet (empty `.instances/`) costs one
    # is_dir(); the `ps` guard only runs when sibling heartbeats actually exist.
    # The probe shells out to `ps` per sibling, so it's offloaded off the event
    # loop (#875) — matching the startup-path co-location check in server._main.
    from infra.paths import colocation_warning, instance_uid, package_version

    try:
        warn = await asyncio.to_thread(colocation_warning)
        warnings = [warn] if warn else []
    except Exception:  # noqa: BLE001 — status must never raise
        warnings = []
    # Fleet version skew (version-coherence P2) — also live + self-clearing: a
    # member that survived an app update keeps running the OLD binary until
    # restarted; banner it the same way as a co-located sibling. Inside a member
    # the scoped fleet.json is empty, so this no-ops. It probes sibling liveness
    # (a `ps` shell-out under the hood), so it's offloaded off the loop too (#875).
    try:
        from graph.fleet import supervisor as _sup

        skew = await asyncio.to_thread(_sup.version_skew_warning)
        if skew:
            warnings.append(skew)
    except Exception:  # noqa: BLE001 — status must never raise
        pass
    return _build_operator_status(
        config=STATE.graph_config,
        setup_complete=_operator_setup_complete(),
        graph_loaded=STATE.graph is not None,
        project_path=_resolve_operator_project_root(),
        allowed_dirs=_operator_allowed_dirs(),
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
        cache_warmer=STATE.cache_warmer,
        skills_index=STATE.skills_index,
        mcp={
            "enabled": bool(getattr(STATE.graph_config, "mcp_enabled", False)) if STATE.graph_config else False,
            "servers": STATE.mcp_meta,
            "tool_count": len(STATE.mcp_tools),
        },
        plugins=STATE.plugin_meta,
        telemetry_store=STATE.telemetry_store,
        checkpoint_path=STATE.checkpoint_path,
        warnings=warnings,
        instance_uid=instance_uid(),
        # App version (pyproject [project].version) — the hub↔remote version
        # handshake (ADR 0042 §I) needs skew between consoles + agents visible.
        version=package_version(),
    )


def _operator_subagent_list():
    return _operator_list_subagents(STATE.graph_config)


# Group the CORE tool inventory by subsystem so the console sections the list instead
# of a wall of 30 (the old single "General" bucket held filesystem + skills + the long
# tail). Name → subsystem. Plugin tools group by their OWNING PLUGIN (not a flat
# "Plugin"); MCP tools by "MCP". Unmapped core names fall back to "General".
_TOOL_CATEGORY = {
    # Filesystem / operator workspace
    "list_dir": "Filesystem",
    "read_file": "Filesystem",
    "find_files": "Filesystem",
    "search_files": "Filesystem",
    "write_file": "Filesystem",
    "edit_file": "Filesystem",
    "run_command": "Filesystem",
    "list_projects": "Filesystem",
    # Skills
    "load_skill": "Skills",
    "list_skills": "Skills",
    "save_skill": "Skills",
    # Web & research
    "web_search": "Web & research",
    "fetch_url": "Web & research",
    # Memory
    "memory_ingest": "Memory",
    "knowledge_ingest": "Memory",
    "memory_recall": "Memory",
    "memory_list": "Memory",
    "memory_stats": "Memory",
    "forget_memory": "Memory",
    # Scheduler
    "schedule_task": "Scheduler",
    "list_schedules": "Scheduler",
    "cancel_schedule": "Scheduler",
    # Inbox
    "check_inbox": "Inbox",
    # Tasks
    "task_create": "Tasks",
    "task_list": "Tasks",
    "task_update": "Tasks",
    "task_close": "Tasks",
    # Goals
    "set_goal": "Goals",
    # Delegation (subagents)
    "task": "Delegation",
    "task_batch": "Delegation",
    "task_output": "Delegation",
    "stop_task": "Delegation",
    # Workflows
    "run_workflow": "Workflows",
    "save_workflow": "Workflows",
    # Discovery
    "search_tools": "Discovery",
}


def _tool_category(
    name: str, source: str, plugin_owner: str | None = None, mcp_servers: list[str] | None = None
) -> str:
    # Plugin tools group by the plugin that contributed them (its display name), so the
    # console organizes by plugin instead of one flat "Plugin" dump.
    if source == "plugin":
        return plugin_owner or "Plugin"
    if source == "mcp":
        # MCP tools are namespaced "<server>__<tool>" (tool_name_prefix=True), so group by
        # the originating server — match the known server names first (handles a name that
        # itself contains "__"), else fall back to the prefix before the first "__".
        for s in mcp_servers or []:
            if name.startswith(f"{s}__"):
                return s
        return name.split("__", 1)[0] if "__" in name else "MCP"
    # Core tools group by subsystem; the long tail falls back to "General".
    return _TOOL_CATEGORY.get(name, "General")


def _operator_tools_list():
    """Live tool inventory for the Tools tab — name, one-line description, source
    (core/plugin/mcp), and a subsystem category for grouping.

    Reads the tools ACTUALLY BOUND to the compiled graph (``graph.bound_tools``,
    stamped by ``create_agent_graph``) so the Tools tab can't drift from what the
    model can really call — it covers task/task_batch, filesystem, execute_code,
    and the deferred search tool, not just the shared ``get_all_tools`` base
    (bd-2aa / bd-67j). Falls back to re-deriving the base pre-setup, before the
    graph exists."""
    out: list[dict] = []
    seen: set[str] = set()
    # Source is derived by cross-referencing the plugin/mcp tool name sets;
    # everything else bound to the graph is core.
    plugin_names = {getattr(t, "name", None) for t in (getattr(STATE, "plugin_tools", None) or [])}
    mcp_names = {getattr(t, "name", None) for t in (getattr(STATE, "mcp_tools", None) or [])}
    # tool name -> owning plugin display name (Tools tab grouping), stamped by the loader.
    plugin_owner = getattr(STATE, "plugin_tool_owner", None) or {}
    # Configured MCP server names (mcp_meta = [{name, transport, tool_count}]) → group MCP
    # tools by the server that serves them, mirroring the plugin grouping.
    mcp_servers = [m.get("name") for m in (getattr(STATE, "mcp_meta", None) or []) if m.get("name")]

    def add(tool, source=None):
        name = getattr(tool, "name", None)
        if not name or name in seen:
            return
        seen.add(name)
        src = source or ("plugin" if name in plugin_names else "mcp" if name in mcp_names else "core")
        desc = (getattr(tool, "description", "") or "").strip().split("\n")[0]
        out.append(
            {
                "name": name,
                "description": desc,
                "source": src,
                "category": _tool_category(name, src, plugin_owner.get(name), mcp_servers),
            }
        )

    bound = getattr(STATE.graph, "bound_tools", None)
    if bound is not None:
        for t in bound:
            add(t)
        return {"tools": out, "count": len(out)}

    # Pre-setup fallback (no compiled graph yet): re-derive the shared base.
    cfg = STATE.graph_config
    try:
        from tools.lg_tools import get_all_tools

        core = get_all_tools(
            STATE.knowledge_store,
            scheduler=STATE.scheduler,
            inbox_store=STATE.inbox_store,
            tasks_store=STATE.tasks_store,
            goal_enabled=bool(getattr(cfg, "goal_enabled", False)) if cfg else False,
        )
        for t in core:
            add(t, "core")
    except Exception:  # noqa: BLE001
        log.exception("[tools] core enumeration failed")
    for t in getattr(STATE, "plugin_tools", None) or []:
        add(t, "plugin")
    for t in getattr(STATE, "mcp_tools", None) or []:
        add(t, "mcp")
    return {"tools": out, "count": len(out)}


async def _operator_subagent_run(req: dict):
    if STATE.graph is None:
        raise RuntimeError("agent graph is not loaded; finish setup first")
    return await _operator_run_manual_subagent(
        config=STATE.graph_config,
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
        description=req.get("description", ""),
        prompt=req.get("prompt", ""),
        subagent_type=req.get("type") or req.get("subagent_type", "researcher"),
        extra_tools=STATE.plugin_tools + STATE.mcp_tools,
    )


async def _operator_subagent_batch(req: dict):
    if STATE.graph is None:
        raise RuntimeError("agent graph is not loaded; finish setup first")
    return await _operator_run_manual_subagent_batch(
        config=STATE.graph_config,
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
        tasks=req.get("tasks", []),
        extra_tools=STATE.plugin_tools + STATE.mcp_tools,
    )


async def _operator_scheduler_list() -> dict:
    import asyncio

    if STATE.scheduler is None:
        return {"jobs": [], "backend": "disabled"}
    jobs = await asyncio.to_thread(STATE.scheduler.list_jobs)
    return {
        "jobs": [j.as_dict() for j in jobs],
        "backend": getattr(STATE.scheduler, "name", "local"),
    }


async def _operator_scheduler_add(req: dict) -> dict:
    import asyncio

    if STATE.scheduler is None:
        raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
    prompt = (req.get("prompt") or "").strip()
    schedule = (req.get("schedule") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if not schedule:
        raise ValueError("schedule is required")
    job = await asyncio.to_thread(
        STATE.scheduler.add_job,
        prompt,
        schedule,
        job_id=req.get("job_id") or None,
        timezone=req.get("timezone") or None,
    )
    return job.as_dict()


async def _operator_scheduler_cancel(job_id: str) -> dict:
    import asyncio

    if STATE.scheduler is None:
        raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
    canceled = await asyncio.to_thread(STATE.scheduler.cancel_job, job_id)
    return {"canceled": bool(canceled)}


async def _operator_scheduler_update(job_id: str, req: dict) -> dict:
    import asyncio

    if STATE.scheduler is None:
        raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
    prompt = (req.get("prompt") or "").strip()
    schedule = (req.get("schedule") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if not schedule:
        raise ValueError("schedule is required")
    job = await asyncio.to_thread(
        STATE.scheduler.update_job,
        job_id,
        prompt,
        schedule,
        timezone=req.get("timezone") or None,
    )
    return job.as_dict()


async def _operator_goals_list() -> dict:
    import asyncio

    if STATE.goal_controller is None:
        return {"goals": [], "enabled": False}
    states = await asyncio.to_thread(STATE.goal_controller.store.all)
    return {"goals": [s.to_dict() for s in states], "enabled": True}


async def _operator_goals_clear(session_id: str) -> dict:
    import asyncio

    if STATE.goal_controller is None:
        return {"cleared": False, "enabled": False}
    cleared = await asyncio.to_thread(STATE.goal_controller.store.clear, session_id)
    return {"cleared": bool(cleared)}


async def _operator_goals_set(body: dict) -> dict:
    """Operator goal-set (ADR 0066) — the trusted operator channel. This route lives on the
    ``/api`` operator surface, which the auth path ceiling restricts to the operator
    credential, so it accepts ANY verifier type (command/test/ci/data included) — unlike the
    plugin-only programmatic ``set_goal_safe``. The route maps ok=False to 400."""
    if STATE.goal_controller is None:
        return {"ok": False, "error": "goal mode is not enabled"}
    body = body or {}
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    from graph.goals.controller import GoalController

    ok, msg = STATE.goal_controller.set_goal_operator(
        sid,
        body.get("condition"),
        body.get("verifier") or {},
        max_iterations=body.get("max_iterations"),
        no_progress_limit=body.get("no_progress_limit"),
        mode="monitor" if body.get("mode") == "monitor" else "drive",
        deadline=GoalController._parse_deadline(body.get("deadline")),
        stall_after=GoalController._parse_stall_after(body.get("stall_after")),
    )
    return {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}


async def _operator_watches_list() -> dict:
    import asyncio

    if STATE.watch_controller is None:
        return {"watches": [], "enabled": False}
    watches = await asyncio.to_thread(STATE.watch_controller.list_watches)
    return {"watches": [w.to_dict() for w in watches], "enabled": True}


async def _operator_watches_clear(watch_id: str) -> dict:
    import asyncio

    if STATE.watch_controller is None:
        return {"cleared": False, "enabled": False}
    cleared = await asyncio.to_thread(STATE.watch_controller.clear, watch_id)
    return {"cleared": bool(cleared)}


async def _operator_watches_set(body: dict) -> dict:
    """Operator watch-create (ADR 0067) — the trusted operator channel on the ``/api`` surface
    (operator-tier by the ADR 0066 ceiling), so it accepts ANY verifier type (command/test/ci/
    data), unlike the plugin-only agent/SDK path. Maps ok=False → 400."""
    if STATE.watch_controller is None:
        return {"ok": False, "error": "watch mode is not available"}
    body = body or {}
    from graph.goals.controller import GoalController

    ok, msg, _w = STATE.watch_controller.create(
        condition=body.get("condition"),
        verifier=body.get("verifier") or {},
        watch_id=body.get("watch_id"),
        interval_s=body.get("interval_s"),
        deadline=GoalController._parse_deadline(body.get("deadline")),
        stall_after=GoalController._parse_stall_after(body.get("stall_after")),
        run_prompt=body.get("run_prompt") or "",
        run_session=body.get("run_session") or "",
        trusted=True,
    )
    return {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}


async def _operator_activity_list() -> dict:
    """Return the Activity provenance feed (ADR 0022) — newest-first entries
    with origin/trigger/priority — plus the thread's message history from the
    checkpointer (for the continue view). The console renders the feed and
    opens the thread on demand."""
    import asyncio

    # recent() is sync sqlite — offload it off the loop (#875), mirroring the
    # scheduler/goals handlers above.
    entries = (
        await asyncio.to_thread(STATE.activity_log.recent, 100) if STATE.activity_log is not None else []
    )
    messages: list[dict] = []
    if STATE.checkpointer is not None:
        thread_id = f"a2a:{ACTIVITY_CONTEXT}"
        try:
            tup = await STATE.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
            raw = (tup.checkpoint or {}).get("channel_values", {}).get("messages", []) if tup else []
        except Exception:
            log.exception("[activity] failed to read thread %s", thread_id)
            raw = []
        for m in raw:
            role = getattr(m, "type", "")
            content = getattr(m, "content", "")
            if not isinstance(content, str):
                content = str(content)
            if role == "human":
                messages.append({"role": "user", "content": content})
            elif role == "ai":
                visible = extract_output(content) or content
                if visible.strip():
                    messages.append({"role": "assistant", "content": visible})
            # tool/system messages are omitted from the surface view
    return {"context_id": ACTIVITY_CONTEXT, "entries": entries, "messages": messages}


def _inbox_authorized(token: str | None) -> bool:
    """Validate the inbound bearer token (ADR 0003). Mirrors the A2A posture:
    when no token is configured the endpoint is open (dev), else it must match."""
    active = (
        (STATE.graph_config.auth_token if STATE.graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "") or ""
    ).strip()
    if not active:
        return True
    return hmac.compare_digest(token or "", active)


async def _fire_activity_from_inbox(item: dict) -> bool:
    """Fire a now-priority inbox item as a turn into the Activity thread.
    Self-POSTs to /a2a (parity with the scheduler), guarded against storms."""
    import time
    from uuid import uuid4
    import httpx

    if STATE.storm_guard is not None and not STATE.storm_guard.allow(time.monotonic()):
        log.warning("[inbox] storm guard suppressed now-fire for item %s", item.get("id"))
        return False
    # A2A 1.0 (a2a-sdk ≥1.1): the version header + proto method name are
    # mandatory — the 0.3 `message/send` 404s with -32601. Mirrors the
    # scheduler's fire (scheduler/local.py).
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    bearer = (
        (STATE.graph_config.auth_token if STATE.graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "")
    ).strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    api_key = os.environ.get(f"{AGENT_NAME_ENV.upper()}_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    mid = str(uuid4())
    body = {
        "jsonrpc": "2.0",
        "id": mid,
        "method": "SendMessage",
        "params": {
            # contextId is a field of Message in 1.0 (params-level => -32602).
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": item["text"]}],
                "messageId": mid,
                "contextId": ACTIVITY_CONTEXT,
            },
            "metadata": {
                "origin": "inbox",
                "inbox_id": item.get("id"),
                "inbox_source": item.get("source", ""),
                "priority": item.get("priority", "now"),
            },
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"http://127.0.0.1:{STATE.active_port}/a2a", headers=headers, json=body)
        # A JSON-RPC error rides a 200, so status alone isn't enough.
        if r.status_code >= 400:
            return False
        err = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else None
        if err:
            log.warning("[inbox] now-fire rejected for item %s: %s", item.get("id"), err)
            return False
        return True
    except Exception:
        log.exception("[inbox] now-fire failed for item %s", item.get("id"))
        return False


async def _operator_inbox_add(payload: dict) -> dict:
    """Ingest an inbound item (ADR 0003). now-priority fires an Activity turn;
    others queue for check_inbox. Dedup is handled by the store."""
    import asyncio

    if STATE.inbox_store is None:
        raise RuntimeError("inbox not loaded; finish setup first")
    # add() is sync sqlite — offload it off the loop (#875).
    item = await asyncio.to_thread(
        STATE.inbox_store.add,
        payload.get("text", ""),
        priority=payload.get("priority", "next") or "next",
        source=payload.get("source", "") or "",
        dedup_key=payload.get("dedup_key", "") or "",
    )
    if item is None:
        return {"ok": True, "deduped": True}

    fired = False
    if item["priority"] == "now":
        # Deliver-BEFORE-fire (#1375): mark the now-item delivered before its Activity turn
        # runs, so the fired turn can't re-read its own trigger via check_inbox (double
        # processing). If the fire never happens (storm-blocked / failed), restore it to
        # pending so it isn't lost — check_inbox stays the fallback delivery path.
        try:
            await asyncio.to_thread(STATE.inbox_store.mark_delivered, [item["id"]])
        except Exception:  # noqa: BLE001 — best-effort; a missed mark just means a double-read
            log.warning("[inbox] could not pre-mark now-item %s delivered", item.get("id"))
        fired = await _fire_activity_from_inbox(item)
        if not fired:
            try:
                await asyncio.to_thread(STATE.inbox_store.mark_pending, [item["id"]])
            except Exception:  # noqa: BLE001 — restore is best-effort
                log.warning("[inbox] could not restore unfired now-item %s to pending", item.get("id"))

    # Badge dedup (#1375): publish `inbox.item` ONLY for items that actually LAND in the queue
    # — next/later items, or a now-item whose fire failed (now pending again). A fired now-item
    # is an Activity event (the `activity.message` push covers it), not an inbox arrival, so it
    # no longer double-bumps both the Inbox and Activity widget badges.
    if not fired:
        _event_bus.publish(
            "inbox.item",
            {
                "id": item["id"],
                "priority": item["priority"],
                "source": item.get("source") or "",
                "text": item["text"],
            },
        )
    return {"ok": True, "item": item, "fired": fired}


async def _operator_inbox_list(floor: str, include_delivered: bool) -> dict:
    import asyncio

    if STATE.inbox_store is None:
        return {"items": []}
    # list() is sync sqlite — offload it off the loop (#875).
    items = await asyncio.to_thread(
        STATE.inbox_store.list,
        priority_floor=floor or "later",
        include_delivered=include_delivered,
        limit=200,
    )
    return {"items": items}


async def _operator_inbox_deliver(item_id: int) -> dict:
    import asyncio

    if STATE.inbox_store is None:
        raise RuntimeError("inbox not loaded; finish setup first")
    # mark_delivered() is sync sqlite — offload it off the loop (#875).
    delivered = await asyncio.to_thread(STATE.inbox_store.mark_delivered, [item_id])
    return {"ok": True, "delivered": delivered}


def _operator_chat_commands() -> dict:
    """Slash commands the chat understands — drives the composer autocomplete.

    The workflow/subagent/skill/plugin-command inventory + precedence comes from
    the SAME resolver the chat dispatcher uses (``server.chat.resolve_slash_commands``),
    so the palette can't drift from what actually runs. ``/goal`` (a core
    server-handled control command) is surfaced here; ``/issue`` is now owned by the
    github plugin and arrives via the resolver as a ``plugin_command``."""
    from graph.slash_commands import resolve_slash_commands

    commands = []
    if STATE.goal_controller is not None:
        commands.append(
            {
                "name": "goal",
                "kind": "control",
                "description": "Set, check, or clear a self-driving goal for this chat session.",
                "usage": "/goal <condition>   ·   /goal  (status)   ·   /goal clear",
            }
        )
    commands.extend(resolve_slash_commands())
    return {"commands": commands}
