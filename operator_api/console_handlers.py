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
    return roots


def _operator_runtime_status():
    return _build_operator_status(
        config=STATE.graph_config,
        setup_complete=_operator_setup_complete(),
        graph_loaded=STATE.graph is not None,
        project_path=_resolve_operator_project_root(),
        allowed_dirs=_operator_allowed_dirs(),
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
        cache_warmer=STATE.cache_warmer,
        goal_controller=STATE.goal_controller,
        skills_index=STATE.skills_index,
        mcp={
            "enabled": bool(getattr(STATE.graph_config, "mcp_enabled", False)) if STATE.graph_config else False,
            "servers": STATE.mcp_meta,
            "tool_count": len(STATE.mcp_tools),
        },
        plugins=STATE.plugin_meta,
        telemetry_store=STATE.telemetry_store,
        checkpoint_path=STATE.checkpoint_path,
    )


def _operator_subagent_list():
    return _operator_list_subagents(STATE.graph_config)


def _operator_tools_list():
    """Live tool inventory grouped by source (core / plugin / mcp) for the
    Runtime → Tools tab. Best-effort: degrades to an empty list pre-setup."""
    cfg = STATE.graph_config
    out: list[dict] = []
    seen: set[str] = set()

    def add(tool, source):
        name = getattr(tool, "name", None)
        if not name or name in seen:
            return
        seen.add(name)
        desc = (getattr(tool, "description", "") or "").strip().split("\n")[0]
        out.append({"name": name, "description": desc, "source": source})

    try:
        from tools.lg_tools import get_all_tools

        core = get_all_tools(
            STATE.knowledge_store,
            scheduler=STATE.scheduler,
            inbox_store=STATE.inbox_store,
            beads_store=STATE.beads_store,
            goal_enabled=bool(getattr(cfg, "goal_enabled", False)) if cfg else False,
        )
        for t in core:
            add(t, "core")
    except Exception:  # noqa: BLE001
        log.exception("[tools] core enumeration failed")
    for t in (getattr(STATE, "plugin_tools", None) or []):
        add(t, "plugin")
    for t in (getattr(STATE, "mcp_tools", None) or []):
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
        emit_skill=bool(req.get("emit_skill", False)),
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
        STATE.scheduler.add_job, prompt, schedule, job_id=req.get("job_id") or None
    )
    return job.as_dict()


async def _operator_scheduler_cancel(job_id: str) -> dict:
    import asyncio
    if STATE.scheduler is None:
        raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
    canceled = await asyncio.to_thread(STATE.scheduler.cancel_job, job_id)
    return {"canceled": bool(canceled)}


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
    """Programmatic goal-set (ADR 0028 D3) — plugin-verifier goals only; the route
    maps ok=False to 400."""
    if STATE.goal_controller is None:
        return {"ok": False, "error": "goal mode is not enabled"}
    sid = str((body or {}).get("session_id") or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    ok, msg = STATE.goal_controller.set_goal_safe(
        sid, (body or {}).get("condition"), (body or {}).get("verifier") or {},
        (body or {}).get("max_iterations"),
    )
    return {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}


def _operator_workflows_list() -> dict:
    if STATE.workflow_registry is None:
        return {"workflows": []}
    return {"workflows": STATE.workflow_registry.list()}


async def _operator_workflow_run(name: str, inputs: dict) -> dict:
    if STATE.graph is None:
        raise RuntimeError("agent graph is not loaded; finish setup first")
    from graph.agent import run_manual_workflow
    return await run_manual_workflow(
        STATE.graph_config, STATE.workflow_registry,
        knowledge_store=STATE.knowledge_store, scheduler=STATE.scheduler,
        name=name, inputs=inputs or {},
        extra_tools=STATE.plugin_tools + STATE.mcp_tools,
    )


def _operator_workflow_save(recipe: dict) -> dict:
    # Validate against the live subagent registry before writing, so a
    # UI-authored recipe can't reference an unknown subagent / bad DAG.
    if STATE.workflow_registry is None:
        raise RuntimeError("workflows are not available")
    from graph.subagents.config import SUBAGENT_REGISTRY
    from graph.workflows.engine import validate_recipe
    errors = validate_recipe(recipe, known_subagents=set(SUBAGENT_REGISTRY))
    if errors:
        raise ValueError("invalid recipe: " + "; ".join(errors))
    path = STATE.workflow_registry.save(recipe)
    return {"saved": True, "name": recipe.get("name"), "path": path}


def _operator_workflow_delete(name: str) -> dict:
    if STATE.workflow_registry is None:
        raise RuntimeError("workflows are not available")
    return {"deleted": STATE.workflow_registry.delete(name)}


async def _operator_activity_list() -> dict:
    """Return the Activity provenance feed (ADR 0022) — newest-first entries
    with origin/trigger/priority — plus the thread's message history from the
    checkpointer (for the continue view). The console renders the feed and
    opens the thread on demand."""
    entries = STATE.activity_log.recent(limit=100) if STATE.activity_log is not None else []
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
    active = ((STATE.graph_config.auth_token if STATE.graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "") or "").strip()
    if not active:
        return True
    return (token or "") == active


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
    bearer = ((STATE.graph_config.auth_token if STATE.graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "")).strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    api_key = os.environ.get(f"{AGENT_NAME_ENV.upper()}_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    mid = str(uuid4())
    body = {
        "jsonrpc": "2.0", "id": mid, "method": "SendMessage",
        "params": {
            # contextId is a field of Message in 1.0 (params-level => -32602).
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": item["text"]}],
                "messageId": mid,
                "contextId": ACTIVITY_CONTEXT,
            },
            "metadata": {"origin": "inbox", "inbox_id": item.get("id"), "inbox_source": item.get("source", ""), "priority": item.get("priority", "now")},
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
    if STATE.inbox_store is None:
        raise RuntimeError("inbox not loaded; finish setup first")
    item = STATE.inbox_store.add(
        payload.get("text", ""),
        priority=payload.get("priority", "next") or "next",
        source=payload.get("source", "") or "",
        dedup_key=payload.get("dedup_key", "") or "",
    )
    if item is None:
        return {"ok": True, "deduped": True}
    _event_bus.publish("inbox.item", {
        "id": item["id"], "priority": item["priority"],
        "source": item.get("source") or "", "text": item["text"],
    })
    fired = await _fire_activity_from_inbox(item) if item["priority"] == "now" else False
    return {"ok": True, "item": item, "fired": fired}


async def _operator_inbox_list(floor: str, include_delivered: bool) -> dict:
    if STATE.inbox_store is None:
        return {"items": []}
    items = STATE.inbox_store.list(
        priority_floor=floor or "later", include_delivered=include_delivered, limit=200,
    )
    return {"items": items}


async def _operator_inbox_deliver(item_id: int) -> dict:
    if STATE.inbox_store is None:
        raise RuntimeError("inbox not loaded; finish setup first")
    return {"ok": True, "delivered": STATE.inbox_store.mark_delivered([item_id])}


def _operator_chat_commands() -> dict:
    """Slash commands the chat understands — drives the composer autocomplete.

    Currently just `/goal` (when goal mode is loaded). Register a new
    server-handled control command here and the console picks it up.
    """
    commands = []
    if STATE.goal_controller is not None:
        commands.append({
            "name": "goal",
            "description": "Set, check, or clear a self-driving goal for this chat session.",
            "usage": "/goal <condition>   ·   /goal  (status)   ·   /goal clear",
        })
    # Each registered workflow is runnable as /<name> (ADR 0002).
    wf_names: set[str] = set()
    if STATE.workflow_registry is not None:
        for wf in STATE.workflow_registry.list():
            wf_names.add(wf["name"])
            declared = wf.get("inputs", []) or []
            req = "".join(f" <{i['name']}>" for i in declared if i.get("required"))
            opt = "".join(f" [{i['name']}]" for i in declared if not i.get("required"))
            commands.append({
                "name": wf["name"],
                "description": wf.get("description") or f"Run the {wf['name']} workflow.",
                "usage": f"/{wf['name']}{req}{opt}",
            })
    # Each registered subagent is runnable as /<name> <prompt> (ADR 0020),
    # unless a workflow already claims the name (workflow wins in dispatch).
    try:
        from graph.subagents.config import SUBAGENT_REGISTRY
    except Exception:
        SUBAGENT_REGISTRY = {}
    for name, cfg in SUBAGENT_REGISTRY.items():
        if name in wf_names:
            continue
        commands.append({
            "name": name,
            "description": getattr(cfg, "description", "") or f"Run the {name} subagent.",
            "usage": f"/{name} <prompt>",
        })
    return {"commands": commands}
