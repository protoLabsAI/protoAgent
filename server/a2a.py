"""A2A surface: agent-card building, skill declarations, per-turn telemetry, and
the executor terminal hook.

Extracted from ``server/__init__.py`` (ADR 0023, phase 2). These functions build
the A2A 1.0 agent card served at ``/.well-known/agent-card.json``, declare the
agent's skills, record a telemetry row per terminal turn, and surface the
Activity thread's answer on the event bus when a turn ends. The a2a-sdk route
wiring itself still lives in ``server.__init__._main`` (it calls these); only the
logic moved here.

``server/__init__.py`` re-exports every public name below so ``server.<symbol>``
keeps resolving for ``_main`` and the test suite. The symbols this module
imports from ``server`` (``agent_name``, ``_event_bus``) are all defined in
``__init__`` before its re-export line, so the import is not a cycle.
"""

import asyncio
import logging
import os

from events import ACTIVITY_CONTEXT
from graph.output_format import extract_output
from runtime.state import STATE
from server import _event_bus, agent_name

log = logging.getLogger("protoagent.server")

# Holds the fire-and-forget background-wake tasks (ADR 0050 Phase 2) so they aren't
# GC'd mid-flight; the done callback discards each on completion.
_BG_WAKE_TASKS: set = set()


def _background_wake_enabled() -> bool:
    """Whether a finished background job should autonomously wake the agent (ADR 0050
    Phase 2). On by default; ``BACKGROUND_WAKE=0`` opts out (parity with
    ``BACKGROUND_DISABLED``)."""
    return os.environ.get("BACKGROUND_WAKE", "1").strip().lower() not in ("0", "false", "no")


def _background_wake_text(job) -> str:
    """The stimulus the agent wakes to when a background job finishes."""
    result = job.result or ""
    if len(result) > 4000:
        result = result[:4000] + "\n\n…[truncated]"
    where = f" (you spawned it from session {job.origin_session})" if job.origin_session else ""
    verb = "failed" if job.status == "failed" else "finished"
    return (
        f"A background task you delegated has {verb}{where} — decide whether to act on it.\n\n"
        f"Job: {job.description} [{job.subagent_type}]\n"
        f"Status: {job.status}\n"
        f"Result:\n{result}\n\n"
        "If this calls for follow-up action, take it now. Otherwise acknowledge it briefly "
        "and stop — don't re-run the task."
    )


async def _background_wake(job) -> bool:
    """Wake the agent on a completion by adding a now-priority inbox item (ADR 0050
    Phase 2) — which fires a turn into the Activity thread via the existing inbox→
    Activity path (storm-guarded). Returns whether the fire was dispatched. The Activity
    response surfaces live in the console's Activity feed (server-driven, unlike the
    localStorage chat view)."""
    if STATE.inbox_store is None:
        return False
    from operator_api.console_handlers import _operator_inbox_add

    res = await _operator_inbox_add(
        {
            "text": _background_wake_text(job),
            "priority": "now",
            "source": "background",
            "dedup_key": f"background-wake:{job.id}",
        }
    )
    return bool(res.get("fired"))


def _spawn_background_wake(job) -> None:
    """Schedule the (async) wake fire-and-forget on the running loop. No-op off-loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _go() -> None:
        try:
            await _background_wake(job)
        except Exception:  # noqa: BLE001 — best-effort; never breaks the terminal hook
            log.exception("[background] wake fire failed for %s", getattr(job, "id", "?"))

    t = loop.create_task(_go())
    _BG_WAKE_TASKS.add(t)
    t.add_done_callback(_BG_WAKE_TASKS.discard)


def _bearer_configured() -> bool:
    return bool(os.environ.get("A2A_AUTH_TOKEN", "") or (STATE.graph_config and STATE.graph_config.auth_token))


# Skill declarations (ADR-0006 addendum / #476). A skill MAY declare an
# ``output_schema`` (JSON Schema) + ``result_mime`` — when present, the agent
# enforces the schema via a forced-tool-call finalizer in the executor and emits
# the result as a typed DataPart (``protolabs_a2a.emit_skill_result``), and the
# card advertises the MIME in that skill's ``output_modes``. No schema ⇒ free
# text (today's default). The schema lives HERE (skill config), not on the card
# — ``AgentSkill`` only carries ``output_modes`` (the MIME), per the A2A spec.
#
# This is the TEMPLATE DEFAULT — one free-text placeholder so a fresh clone is
# callable. Forks declare their real skills WITHOUT editing this file (#570):
# either in ``langgraph-config.yaml`` (``a2a.skills: [...]``) or via a plugin
# (``registry.register_a2a_skill(spec)``). ``_resolved_skill_specs()`` merges
# both and falls back here when neither is set.
_SKILL_SPECS: list[dict] = [
    {
        "id": "chat",
        "name": "Chat",
        "description": "General-purpose chat interface. Replace with your agent's real skills.",
        "tags": ["template"],
        "examples": ["hello", "what can you do?"],
        # To make a skill return schema-enforced structured output, add:
        #   "output_schema": {"type": "object", "properties": {...}, "required": [...]},
        #   "result_mime": "application/vnd.protolabs.<your-skill>-v1+json",
    },
]


def _resolved_skill_specs() -> list[dict]:
    """The agent's advertised A2A skills, resolved at runtime (#570) so a fork
    never edits this file. Sources, in order: ``a2a.skills`` from
    ``langgraph-config.yaml`` (``STATE.graph_config.a2a_skills``), then
    plugin-contributed skills (``register_a2a_skill`` → ``STATE.plugin_a2a_skills``).
    Falls back to the template placeholder ``_SKILL_SPECS`` when neither is set,
    so a fresh clone stays callable."""
    cfg = STATE.graph_config
    resolved: list[dict] = []
    if cfg is not None:
        resolved.extend(getattr(cfg, "a2a_skills", None) or [])
    resolved.extend(getattr(STATE, "plugin_a2a_skills", None) or [])
    return resolved or _SKILL_SPECS


def _agent_skills():
    """Build the card's ``AgentSkill`` list from the resolved skill specs. A spec
    with a ``result_mime`` advertises it in ``output_modes`` (the A2A-native way
    to tell consumers the skill emits that structured type)."""
    from a2a.types import AgentSkill

    skills = []
    for s in _resolved_skill_specs():
        kwargs = dict(
            id=s["id"],
            name=s["name"],
            description=s["description"],
            tags=s.get("tags", []),
            examples=s.get("examples", []),
        )
        if s.get("result_mime"):
            kwargs["output_modes"] = [s["result_mime"]]
        skills.append(AgentSkill(**kwargs))
    return skills


def structured_skill_schema(skill_id: str) -> dict | None:
    """For a skill that declares structured output, return
    ``{"schema": <JSON Schema>, "mime": <result_mime>}``; else ``None`` (free
    text). The executor's structured finalizer (#476) reads this to run the
    forced-tool-call against the schema and emit the validated object as a
    ``result_mime`` DataPart. The schema isn't on the card (``AgentSkill`` has no
    schema field) — it lives in the resolved skill specs."""
    for s in _resolved_skill_specs():
        if s["id"] == skill_id and s.get("output_schema") and s.get("result_mime"):
            return {"schema": s["output_schema"], "mime": s["result_mime"]}
    return None


def _package_version() -> str:
    """Single-source the agent-card version from the package metadata.

    Delegates to ``paths.package_version()`` (one source of truth — the
    pyproject ``[project].version`` the release pipeline bumps) so the card,
    the runtime status, and the fleet version handshake can never disagree.
    Kept as a name here for its existing importers (``server.__init__`` re-exports it).
    """
    from infra.paths import package_version

    return package_version()


def _a2a_card_url() -> str:
    """The reachable JSON-RPC endpoint to advertise in the A2A card's interface.

    The card tells other agents *where to send* ``message/send``, so this must
    be the agent's externally-reachable address — not the bind host. Prefer an
    explicit ``A2A_PUBLIC_URL`` (set this for any deployed agent: behind a proxy
    / in a container the public address isn't the bound port). Fall back to the
    actually-bound loopback port (``STATE.active_port``) for local + desktop runs —
    correct there because the client is on the same host (and the desktop's port
    is dynamic). The ``/a2a`` suffix is the JSON-RPC route.
    """
    base = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    if not base:
        base = f"http://127.0.0.1:{STATE.active_port}"
    return f"{base}/a2a"


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def assert_routable_card_url() -> None:
    """Fail fast at startup if the card would advertise a loopback URL (opt-in).

    A *deployed* agent that advertises ``http://127.0.0.1:.../a2a`` — e.g. because
    ``A2A_PUBLIC_URL`` wasn't set after a redeploy — is silently unreachable to any
    remote consumer that dials the card's interface URL. That's a deployment-config
    regression no test catches; it surfaces only at first cross-host dispatch.

    When ``a2a.require_routable_url`` is set, refuse to start (exit non-zero) rather
    than discover it later. **Off by default** — local + desktop runs *should*
    advertise loopback (the client is same-host, and the desktop port is dynamic).
    Deployed agents opt in via config.
    """
    cfg = STATE.graph_config
    if not (cfg and getattr(cfg, "a2a_require_routable_url", False)):
        return
    from urllib.parse import urlparse

    url = _a2a_card_url()
    host = (urlparse(url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        log.critical(
            "[a2a] refusing to start: the agent card would advertise a loopback "
            "URL %r (host=%r), unreachable to remote consumers. "
            "a2a.require_routable_url is set — a deployed agent must advertise its "
            "externally-reachable address. Set A2A_PUBLIC_URL to the host other "
            "agents reach (e.g. http://roxy:7870).",
            url,
            host or "<empty>",
        )
        raise SystemExit(1)
    log.info("[a2a] card URL %s is routable (require_routable_url check passed)", url)


# Template default card description — used when a fork sets no ``a2a.description``
# in config (#570). Forks override via config, not by editing this file.
_DEFAULT_CARD_DESCRIPTION = (
    "protoAgent template — A2A 1.0 LangGraph agent. Replace this description with your agent's actual purpose."
)


def _build_agent_card_proto():
    """Build the A2A 1.0 ``AgentCard`` (proto) served at
    ``/.well-known/agent-card.json``, applying the protoLabs fleet conventions
    via ``protolabs_a2a.build_agent_card``.

    Identity is config/plugin-driven (#570), so a fork shouldn't edit this file:
    ``name`` resolves from identity (``agent_name()``), ``description`` from
    ``a2a.description`` in ``langgraph-config.yaml`` (falling back to the template
    default below), and ``skills`` from config/plugins (``_resolved_skill_specs``).
    The four custom extensions (cost / confidence / worldstate-delta / tool-call)
    are declared by default — the template emits cost-v1 + confidence-v1 from
    ``_chat_langgraph_stream`` and worldstate-delta / tool-call when a tool reports
    them.

    The interface ``url`` (``_a2a_card_url``) targets the JSON-RPC endpoint
    (``/a2a``) at the agent's reachable address — set ``A2A_PUBLIC_URL`` when
    deployed; otherwise it's the bound loopback port.
    """
    import protolabs_a2a as pa

    cfg = STATE.graph_config
    description = (getattr(cfg, "a2a_description", "") or "").strip() or _DEFAULT_CARD_DESCRIPTION
    card = pa.build_agent_card(
        name=agent_name(),
        description=description,
        url=_a2a_card_url(),
        version=_package_version(),
        skills=_agent_skills(),
        bearer=_bearer_configured(),
    )
    # Card polish (ADR 0051 Slice 3) — build_agent_card doesn't set these, but the
    # 1.0 proto AgentCard has them: a docs link + an icon for consumers/registries.
    # Overridable via a2a.documentation_url / a2a.icon_url config; default to the
    # public docs + the served brand mark.
    doc_url = (getattr(cfg, "a2a_documentation_url", "") or "").strip() or "https://protolabsai.github.io/protoAgent/"
    icon_url = (getattr(cfg, "a2a_icon_url", "") or "").strip()
    try:
        card.documentation_url = doc_url
        if icon_url:
            card.icon_url = icon_url
    except Exception:  # noqa: BLE001 — card polish is best-effort, never break the card
        pass
    return card


def _record_a2a_telemetry(outcome) -> None:
    """Write one per-turn telemetry row from an executor ``TurnOutcome``
    (ADR 0006 Slice 2). No-op when the telemetry store is off; best-effort so a
    failure never affects the turn."""
    # Prometheus turn counter (independent of the SQL telemetry store) — lets
    # /metrics alert on a failing/backed-up agent. Best-effort.
    try:
        from observability import metrics

        metrics.record_a2a_turn(outcome.state, (outcome.duration_ms or 0) / 1000.0)
    except Exception:  # noqa: BLE001 — the Prometheus metric must never break a turn
        pass

    # Realtime cost/usage on the bus (ADR 0051 Slice 3) — a per-turn HUD can show live
    # spend without polling the telemetry store. Independent of the SQL store.
    try:
        _u = outcome.usage or {}
        _event_bus.publish(
            "turn.usage",
            {
                "task_id": getattr(outcome, "task_id", "") or "",
                "context_id": getattr(outcome, "context_id", "") or "",
                "state": getattr(outcome, "state", "") or "",
                "model": outcome.models[0] if getattr(outcome, "models", None) else "",
                "input_tokens": int(_u.get("input_tokens", 0) or 0),
                "output_tokens": int(_u.get("output_tokens", 0) or 0),
                "cost_usd": round(float(getattr(outcome, "cost_usd", 0.0) or 0.0), 6),
                "duration_ms": int(getattr(outcome, "duration_ms", 0) or 0),
            },
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass

    store = STATE.telemetry_store
    if store is None:
        return
    try:
        u = outcome.usage or {}
        primary_model = (
            outcome.models[0]
            if outcome.models
            else ((STATE.graph_config.model_name if STATE.graph_config else "") or "")
        )
        input_tokens = int(u.get("input_tokens", 0) or 0)
        output_tokens = int(u.get("output_tokens", 0) or 0)
        from datetime import datetime, timedelta, timezone

        ended = datetime.now(timezone.utc)
        created = ended - timedelta(milliseconds=int(outcome.duration_ms or 0))
        store.record(
            {
                "task_id": outcome.task_id,
                "session_id": outcome.context_id,
                "state": outcome.state,
                "success": 1 if outcome.state == "completed" else 0,
                "model": primary_model,
                "models": ",".join(outcome.models),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
                "cost_usd": float(outcome.cost_usd or 0.0),
                "duration_ms": int(outcome.duration_ms or 0),
                "llm_calls": int(outcome.llm_calls),
                "tool_calls": int(outcome.tool_calls),
                "created_at": created.isoformat(),
                "ended_at": ended.isoformat(),
            }
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        log.exception("[telemetry] failed to record turn %s", outcome.task_id)


def _a2a_progress(context_id: str, task_id: str, frame: dict) -> None:
    """Per-frame progress hook (ADR 0051). Only background turns get a bus push — live
    turns already stream over their own SSE. On the opening ``turn_started`` frame it
    records the A2A task id on the job row (the handle ``stop_task`` needs); on tool
    frames it republishes ``background.progress`` for the console's live job card.
    Best-effort — never raises into the executor."""
    if not context_id.startswith("background:"):
        return
    mgr = getattr(STATE, "background_mgr", None)
    if mgr is None:
        return
    job_id = context_id.split(":", 1)[1]
    if not job_id:
        return
    phase = frame.get("phase")
    if phase == "turn_started":
        try:
            mgr.store.set_a2a_task_id(job_id, task_id)
        except Exception:  # noqa: BLE001
            log.exception("[background] failed to record task id for %s", job_id)
        return
    out = frame.get("output")
    if isinstance(out, str) and len(out) > 500:
        out = out[:500] + "…"
    _event_bus.publish(
        "background.progress",
        {
            "job_id": job_id,
            "task_id": task_id,
            "phase": phase,  # "tool_start" | "tool_end"
            "tool": frame.get("name"),
            "tool_call_id": frame.get("id"),
            "output": out,
            "error": bool(frame.get("error")),
        },
    )


def _handle_background_terminal(outcome) -> None:
    """Settle a finished background subagent job (ADR 0050).

    Marks the store row terminal with the turn's final text and publishes a
    ``background.completed`` event for the console (the model learns separately, via
    the drain into the spawning session's next turn). Best-effort."""
    mgr = getattr(STATE, "background_mgr", None)
    if mgr is None:
        return
    _ctx = str(getattr(outcome, "context_id", "") or "")
    job_id = (getattr(outcome, "trigger", "") or "").strip()
    if not job_id and _ctx.startswith("background:"):
        job_id = _ctx.split(":", 1)[1]
    if not job_id:
        return
    state = getattr(outcome, "state", "completed")
    status = state if state in ("completed", "canceled") else "failed"
    text = extract_output(outcome.text) or outcome.text or ""
    try:
        mgr.store.mark_complete(job_id, status, text)
    except Exception:  # noqa: BLE001
        log.exception("[background] failed to settle job %s", job_id)
        return
    job = None
    try:
        job = mgr.store.get(job_id)
    except Exception:  # noqa: BLE001 — read is best-effort for the event payload
        pass
    # Carry a trimmed result so a still-open spawning chat can render the outcome
    # live without a refetch (the model learns separately, via the next-turn drain).
    # The console chat card offers "Read full report" for the full text, so the
    # preview just marks that it's clipped — no panel CTA.
    result_preview = text if len(text) <= 2000 else text[:2000] + "\n\n…_[truncated]_"
    _event_bus.publish(
        "background.completed",
        {
            "job_id": job_id,
            "status": status,
            "subagent_type": getattr(job, "subagent_type", "") if job else "",
            "description": getattr(job, "description", "") if job else "",
            "origin_session": getattr(job, "origin_session", "") if job else "",
            "result": result_preview,
        },
    )
    # Autonomous idle-wake (ADR 0050 Phase 2): fire an Activity turn so the agent reacts
    # to the result on its own, instead of only learning on the spawning session's next
    # turn. Gated + storm-guarded; needs the full job row for the stimulus.
    if job is not None and _background_wake_enabled():
        _spawn_background_wake(job)


def _surface_resumed_chat_turn(outcome) -> None:
    """Surface a scheduler-fired turn (a ``wait`` resume / scheduled task, ADR 0053)
    that landed in a CHAT session — not the Activity thread — on the event bus so an
    open chat tab shows the resumed turn LIVE (bd-k02). The browser only renders
    turns it streamed, so a server-fired resume is otherwise invisible until the
    next interaction. Mirrors the ADR 0050 background path. Only scheduler-origin
    turns qualify; an operator/A2A chat turn the browser already streamed does not."""
    if getattr(outcome, "origin", "") != "scheduler":
        return
    text = extract_output(outcome.text) or outcome.text
    if not text.strip():
        return
    _event_bus.publish(
        "chat.resumed",
        {
            "session_id": str(getattr(outcome, "context_id", "") or ""),
            "text": text,
            "task_id": getattr(outcome, "task_id", "") or "",
            "trigger": getattr(outcome, "trigger", "") or "",
        },
    )


def _a2a_terminal(outcome) -> None:
    """A2A terminal hook (ADR 0003 / 0006). Fired by ``ProtoAgentExecutor`` with
    a ``TurnOutcome`` when a turn reaches a terminal state. Records the per-turn
    telemetry row and surfaces the Activity thread's answer on the event bus.
    Best-effort — never raises into the executor."""
    _record_a2a_telemetry(outcome)
    # Background subagent turns (ADR 0050) live in a dedicated ``background:<id>``
    # context, not the Activity thread — settle the job + notify the UI here, before
    # the Activity early-return below.
    _ctx = str(getattr(outcome, "context_id", "") or "")
    if getattr(outcome, "origin", "") == "background" or _ctx.startswith("background:"):
        _handle_background_terminal(outcome)
        return
    if outcome.context_id != ACTIVITY_CONTEXT:
        _surface_resumed_chat_turn(outcome)
        return
    text = extract_output(outcome.text) or outcome.text
    if not text.strip():
        return
    origin = getattr(outcome, "origin", "") or "operator"
    trigger = getattr(outcome, "trigger", "") or ""
    priority = getattr(outcome, "priority", "") or ""
    stimulus = getattr(outcome, "stimulus", "") or ""
    # Provenance feed (ADR 0022): durably log the turn + what triggered it + the stimulus it
    # responds to (#1375), so the feed reads as an explicit reply.
    if STATE.activity_log is not None:
        STATE.activity_log.add(
            context_id=ACTIVITY_CONTEXT,
            origin=origin,
            trigger=trigger,
            priority=priority,
            state=getattr(outcome, "state", "completed"),
            text=text,
            task_id=getattr(outcome, "task_id", "") or "",
            stimulus=stimulus,
        )
    _event_bus.publish(
        "activity.message",
        {
            "role": "assistant",
            "text": text,
            "context_id": ACTIVITY_CONTEXT,
            "origin": origin,
            "trigger": trigger,
            "priority": priority,
            "stimulus": stimulus,
        },
    )
