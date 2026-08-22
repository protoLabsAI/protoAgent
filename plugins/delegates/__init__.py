"""Unified delegate registry — `delegate_to` over a2a / openai / acp (ADR 0025).

One tool, ``delegate_to(target, query)``, dispatches to any configured delegate:
a fleet **A2A agent**, an OpenAI-compatible **model endpoint**, or an **ACP coding
agent**. Replaces the three split surfaces (`peer_consult`, `code_with`, and the
gateway-only model) with one hot-swappable roster.

PR1 (this slice): the registry + `delegate_to` + the three adapters, configured
via the ``delegates`` config section and hot-reloaded by Save & Reload. The CRUD
REST API (PR2) and the React panel (PR3) build on this. Enabled by default — it
contributes ``delegate_to`` only once you declare a delegate in config (a no-op
until then), so the gate is the delegate, not a plugin toggle.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from .adapters import DelegateError
from .registry import DelegateRegistry

log = logging.getLogger("protoagent.plugins.delegates")


def _build_delegate_to(registry: DelegateRegistry):
    listing = registry.listing()

    @tool
    async def delegate_to(
        target: str,
        query: str,
        background: bool = False,
        item_id: str = "",
        resume_task_id: str = "",
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Hand a question or task to one of your configured delegates and return its reply.

        Use this to reach beyond your own context: ask a fleet **agent**, consult
        another **model endpoint**, or hand a repo-scoped coding job to a **coding
        agent**. Pick the delegate whose description best fits the task.

        **Strongly prefer ``background=True``** for a goal-driven fan-out, for
        reaching multiple delegates, or for any delegation that may take more than a
        couple of seconds. Foreground (the default) is only for a single quick consult
        whose answer you need to finish the current reply. A background delegation runs
        detached: you get a job handle back immediately, and the delegate's reply is
        delivered to you automatically on a later turn (you don't hold your turn open
        waiting). In-flight background jobs are tracked in the background panel
        (``GET /api/background``).

        **After you start a background delegation, END YOUR TURN.** Do NOT try to wait
        or poll for it, and do NOT re-delegate the same work — each delegate's reply
        comes back to you automatically on a later turn; synthesize once the replies
        arrive. **When you fan out to several delegates, wait until you have received
        ALL of their replies before you synthesize — don't synthesize on the first one
        back.**

        Args:
            target: the delegate name (see the available list in this tool's
                description).
            query: the full, self-contained question or instruction — the delegate
                does not see this conversation, so restate what it needs.
            background: run the delegation detached and get the reply back on
                completion, instead of waiting inline (default False).
            item_id: stable work-item id for a coding task on a managed-git coding
                agent — one PR per id, and a second dispatch of an in-flight id is
                refused instead of duplicating the work. Use the issue/board id when
                there is one; leave empty to derive one from the query text.
            resume_task_id: answer a PARKED delegation. When a delegate replies
                "⏸ … needs input" with a parked task id, it stopped mid-task to ask
                a question — call this tool again with the SAME target, the ANSWER
                as `query`, and that task id here: the delegate resumes exactly
                where it parked. If you can't answer the question yourself, get the
                answer first (ask your own operator with ask_human if you have it —
                your question bubbles up the chain the same way), THEN resume.
                Never re-delegate the original task instead of resuming — that
                starts the work over.
        """
        if not str(query).strip():
            return "Error: `query` is empty — give the delegate something to do."
        # Normalize identity ONCE at the tool boundary: whitespace-padded ids must not
        # hash/claim differently from their trimmed twin (that would silently defeat
        # the one-PR-per-item dedup).
        item_id = str(item_id or "").strip()
        resume_task_id = str(resume_task_id or "").strip()
        if background:
            return await _spawn_background_delegation(
                registry, target, query, state, item_id=item_id, resume_task_id=resume_task_id
            )
        try:
            return await registry.dispatch(
                target, query, item_id=item_id or None, resume_task_id=resume_task_id or None
            )
        except DelegateError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface as a tool error string
            log.warning("[delegates] dispatch to %r failed: %s", target, exc)
            return f"Error: delegate {target!r} failed: {type(exc).__name__}: {exc}"

    delegate_to.description = f"{delegate_to.description}\n\nAvailable delegates: {listing or '(none configured)'}."
    return delegate_to


async def _spawn_background_delegation(
    registry: DelegateRegistry,
    target: str,
    query: str,
    state: Any,
    *,
    item_id: str = "",
    resume_task_id: str = "",
) -> str:
    """Run a delegation as a detached background job (ADR 0050): return a handle now and
    drain the delegate's reply back into the spawning session on completion — the same
    durable store + concurrency cap + drain-on-next-turn notification that
    ``task(run_in_background=True)`` uses, so a slow delegate (a coding agent building a
    PR) never holds the caller's turn open.

    Degrades gracefully: an unknown target fails fast (no orphan job), and if no
    ``BackgroundManager`` is wired (a lean/CLI/test context) it falls back to a plain
    inline dispatch so ``background=True`` is never worse than the synchronous path.
    """
    if registry.get(target) is None:
        return f"Error: unknown delegate {target!r}. Available: {registry.listing() or '(none)'}."

    try:
        from runtime.state import STATE

        mgr = getattr(STATE, "background_mgr", None)
    except Exception:  # noqa: BLE001 — no runtime state (e.g. a unit test) → inline
        mgr = None
    if mgr is None:
        return await registry.dispatch(target, query, item_id=item_id or None, resume_task_id=resume_task_id or None)

    try:
        from tools.lg_tools import _session_id_from

        # Injected graph state, not the tracing contextvar (empty in a tool body) — the
        # session id is what the completion drains back to (ADR 0050).
        session = _session_id_from(state) or ""
    except Exception:  # noqa: BLE001 — best-effort; job still runs, drain is degraded
        session = ""

    async def _work() -> str:
        # item_id rides into the dispatch itself, so the managed-git claim/dedup
        # applies identically to background and foreground fan-out (one registry,
        # one event loop).
        return await registry.dispatch(target, query, item_id=item_id or None, resume_task_id=resume_task_id or None)

    snippet = " ".join(query.split())[:80]
    job_id = await mgr.spawn_work(
        origin_session=session,
        kind="delegate",
        description=f"delegate → {target}: {snippet}",
        detail=query,
        work=_work,
    )
    return (
        f"Started a background delegation to {target!r} (job `{job_id}`). It runs detached — "
        f"its reply comes back to me automatically on a later turn, so I should END my turn "
        f"now and NOT wait or re-delegate this. If I fanned out to several delegates, I'll "
        f"hold off synthesizing until ALL their replies are back. In-flight background jobs "
        f"are listed in the background panel (GET /api/background)."
    )


def _build_list_agents(registry: DelegateRegistry):
    @tool
    def list_agents() -> str:
        """List the agents/delegates you can reach with `delegate_to`, with each one's
        type, description, and current reachability (🟢 reachable · 🔴 down · ⚪ unknown).

        Read this before assuming who's available — the roster is configuration, not a
        fixed set, and it changes as delegates are added or removed."""
        try:
            from .health import health_snapshot

            health = health_snapshot() or {}
        except Exception:  # noqa: BLE001 — prober not running; reachability stays unknown
            health = {}
        roster = registry.roster()
        if not roster:
            return "No delegates configured."
        lines = []
        for r in roster:
            ok = (health.get(r["name"]) or {}).get("ok")
            badge = "🟢" if ok is True else "🔴" if ok is False else "⚪"
            typ = f" ({r['type']})" if r["type"] else ""
            desc = f" — {r['description']}" if r["description"] else ""
            lines.append(f"{badge} {r['name']}{typ}{desc}")
        return "\n".join(lines)

    return list_agents


def _build_propose_delegate():
    @tool
    async def propose_delegate(entry: dict, reason: str = "") -> str:
        """Propose a NEW delegate (a coding agent or peer this agent could
        delegate to) for the OPERATOR to approve. Registration is consent-gated:
        this call validates the entry, probes it, then PAUSES with an approval
        card showing exactly what would be registered — the command path front
        and center — and only the operator's explicit approval writes it. It can
        never register silently, and you cannot skip the pause.

        Use when the roster is empty (or missing the delegate a task needs) and
        you know what should fill it — e.g. an ACP coding agent:
        ``{"name": "claude-code", "type": "acp", "command": "/abs/path/to/claude-agent-acp",
        "workdir": "/abs/path/to/repo"}``. ``reason`` is shown to the operator —
        say what the delegate is FOR. On approval the roster hot-reloads and the
        new delegate is usable the same turn's follow-up; on decline you get the
        operator's answer back — respect it, don't re-propose the same entry.

        Don't call this in an autonomous turn (scheduled / inbox / background):
        there is no operator to approve, the runtime auto-answers the pause, and
        anything but an explicit approval declines — fail-closed by design."""
        import json as _json

        from langgraph.types import interrupt

        from . import store
        from .api import _list_payload, _reload, _validate

        if not isinstance(entry, dict):
            return "Error: entry must be an object — e.g. {name, type, command, workdir}."
        entry = dict(entry)
        try:
            name, adapter = _validate(entry)
        except ValueError as e:
            return f"Error: {e}"
        if any(isinstance(e, dict) and e.get("name") == name for e in store.read_delegates_raw()):
            return f"Error: delegate {name!r} already exists — read list_agents instead of re-registering."

        # Probe best-effort so the operator approves something PROVEN runnable —
        # a failed probe is shown, not hidden, and the operator decides anyway.
        try:
            probed = await adapter.probe(adapter.parse(dict(entry)))
            probe_line = f"Probe: {_json.dumps(probed, default=str)[:400]}"
        except Exception as e:  # noqa: BLE001 — the probe informs consent, it doesn't gate it
            probe_line = f"Probe FAILED: {e}"

        response = interrupt(
            {
                "kind": "form",
                "title": f"Register delegate {name!r}?",
                "description": (
                    (f"Why: {reason}\n\n" if reason else "")
                    + "Approving registers a delegate THIS AGENT CAN EXECUTE "
                    + "(an ACP entry is a binary it may run). Proposed entry:\n\n"
                    + _json.dumps(entry, indent=2, default=str)[:2000]
                    + "\n\n"
                    + probe_line
                ),
                "steps": [
                    {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "approve": {
                                    "type": "boolean",
                                    "title": f"Register {name!r} on this agent",
                                    "default": False,
                                },
                                "note": {"type": "string", "title": "Note back to the agent (optional)"},
                            },
                            "required": ["approve"],
                        }
                    }
                ],
            }
        )
        # Fail-closed: ONLY an explicit boolean true registers. An auto-answered
        # autonomous turn, a bare string, or an unchecked box all decline.
        if not (isinstance(response, dict) and response.get("approve") is True):
            note = response.get("note") if isinstance(response, dict) else response
            suffix = f" Operator note: {note}" if note else ""
            return f"Declined — delegate {name!r} was NOT registered.{suffix}"
        if any(isinstance(e, dict) and e.get("name") == name for e in store.read_delegates_raw()):
            return f"Error: delegate {name!r} was registered by someone else while parked — nothing written."
        store.upsert_delegate(entry)
        ok, msg = await _reload()
        names = ", ".join(str(e.get("name")) for e in _list_payload().get("delegates", []) if isinstance(e, dict))
        reload_note = "roster reloaded" if ok else f"reload FAILED ({msg}) — a restart may be needed"
        note = response.get("note") or ""
        suffix = f" Operator note: {note}" if note else ""
        return f"Registered delegate {name!r} ({reload_note}). Roster now: {names or name}.{suffix}"

    return propose_delegate


def _load_delegates_config() -> list:
    """Read the top-level ``delegates: [...]`` list from the live config doc.

    A top-level list (ORBIS parity) doesn't fit the plugin's dict-shaped
    config_section, so we read it from the live YAML directly. register() re-runs
    on every graph build / Save & Reload, so this reflects the current config —
    that's the hot-swap (ADR 0025). Falls back to ``registry.config['delegates']``
    if a fork nests it under the plugin section.
    """
    try:
        from .store import merged_delegates

        return merged_delegates()  # delegates + secrets overlaid from secrets.yaml
    except Exception:  # noqa: BLE001 — config read is best-effort
        log.exception("[delegates] reading delegates config failed")
    return []


def register(registry) -> None:
    """Entry point — called once per graph build with the live config."""
    # CRUD API for the console panel (PR2) + the background health prober (PR4).
    # Mounted/started once at process init; the roster they serve is config, which
    # hot-reloads — so the static routes + the loop's per-tick re-read are fine.
    try:
        from .api import build_router

        registry.register_router(build_router(), prefix="")
    except Exception:  # noqa: BLE001 — API is best-effort; the tool still works
        log.exception("[delegates] mounting CRUD API failed")
    try:
        from .health import start as _health_start, stop as _health_stop

        registry.register_surface(_health_start, stop=_health_stop, name="delegate-health")
    except Exception:  # noqa: BLE001 — health is best-effort
        log.exception("[delegates] registering health prober failed")

    delegates = _load_delegates_config()
    if not delegates:
        cfg = registry.config or {}
        nested = cfg.get("delegates")
        if isinstance(nested, list):
            delegates = nested
    reg = DelegateRegistry(delegates)

    def _register_propose():
        # propose_delegate registers UNCONDITIONALLY — an empty roster is exactly
        # its moment (#2944): the agent's only move used to be prose when it
        # discovered nobody was configured to delegate to. Registered LAST so
        # delegate_to/list_agents keep their long-standing positions.
        try:
            registry.register_tool(_build_propose_delegate())
        except Exception:  # noqa: BLE001 — the consent tool must not break the plugin
            log.exception("[delegates] registering propose_delegate failed")

    if not reg.names():
        # The default state for a fresh install (the plugin is always-on): no
        # delegates declared ⇒ no `delegate_to` tool. Not an anomaly — debug, not warn.
        log.debug(
            "[delegates] no delegates declared — `delegate_to` not registered "
            "(propose_delegate is). Add entries under `delegates` "
            "(docs/guides/delegates.md) or use the Delegates panel."
        )
        _register_propose()
        return
    registry.register_tool(_build_delegate_to(reg))
    registry.register_tool(_build_list_agents(reg))
    _register_propose()
    log.info(
        "[delegates] registered delegate_to + list_agents for %d delegate(s): %s",
        len(reg.names()),
        ", ".join(reg.names()),
    )
