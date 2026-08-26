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

import asyncio
import logging
import os
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from .adapters import DelegateError, mark_delegation_detached
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
        timeout: int = 0,
        state: Annotated[Any, InjectedState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str | Command:
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
            timeout: max seconds to wait for the delegate's reply before failing,
                overriding the delegate's configured timeout for THIS call only.
                Leave 0 (the default) to use the configured timeout. Raise it for a
                known-long job — a coding agent running a full TDD cycle, venv
                setup, or a CI gate — that would otherwise exceed the delegate's
                default (ACP coding delegates default to 1800s / 30 min).
        """
        if not str(query).strip():
            return "Error: `query` is empty — give the delegate something to do."
        # Normalize identity ONCE at the tool boundary: whitespace-padded ids must not
        # hash/claim differently from their trimmed twin (that would silently defeat
        # the one-PR-per-item dedup).
        item_id = str(item_id or "").strip()
        resume_task_id = str(resume_task_id or "").strip()
        # 0 (or anything ≤ 0) ⇒ use the delegate's configured timeout; a positive value
        # is a per-call override (seconds) threaded down to the adapter's dispatch.
        try:
            timeout_s: float | None = float(timeout) if timeout and float(timeout) > 0 else None
        except (TypeError, ValueError):
            timeout_s = None
        if background:
            return await _spawn_background_delegation(
                registry, target, query, state, item_id=item_id, resume_task_id=resume_task_id, timeout=timeout_s
            )
        try:
            return await _dispatch_into_room(
                registry,
                target,
                query,
                state,
                tool_call_id=tool_call_id,
                item_id=item_id or None,
                resume_task_id=resume_task_id or None,
                timeout=timeout_s,
            )
        except DelegateError as exc:
            # A stopped member of THIS box's fleet is recoverable: ask, start, retry.
            # Anything else — a remote peer, a timeout, an HTTP error, an operatorless
            # instance — falls through to the error, unchanged.
            retried = await _offer_start_and_retry(
                registry,
                target,
                query,
                state,
                exc,
                tool_call_id=tool_call_id,
                item_id=item_id or None,
                resume_task_id=resume_task_id or None,
                timeout=timeout_s,
            )
            if retried is not None:
                return retried
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface as a tool error string
            log.warning("[delegates] dispatch to %r failed: %s", target, exc)
            return f"Error: delegate {target!r} failed: {type(exc).__name__}: {exc}"

    delegate_to.description = f"{delegate_to.description}\n\nAvailable delegates: {listing or '(none configured)'}."
    return delegate_to


async def _offer_start_and_retry(
    registry: DelegateRegistry,
    target: str,
    query: str,
    state: Any,
    exc: DelegateError,
    *,
    tool_call_id: str,
    item_id: str | None,
    resume_task_id: str | None,
    timeout: float | None,
):
    """Ask to start a stopped local delegate, then retry once. ``None`` = not our case.

    Returning ``None`` rather than raising keeps every non-recoverable failure on
    exactly the path it had before: the caller reports the original error.
    """
    from .adapters import KIND_UNREACHABLE
    from .autostart import (
        attempt_allowed,
        consent_form,
        grant,
        granted,
        read_choice,
        start_and_wait,
        startable_member,
    )

    if getattr(exc, "kind", "") != KIND_UNREACHABLE:
        return None
    delegate = registry.get(target) if hasattr(registry, "get") else None
    url = getattr(delegate, "url", "") or ""
    member = startable_member(url)
    if member is None:
        return None
    if not attempt_allowed(member["id"]):
        # Already started once in this window and still unreachable — a member that
        # cannot come up must degrade to the plain error, not re-prompt every call.
        return None

    from tools.lg_tools import _session_id_from

    session_id = _session_id_from(state) or ""
    if not granted(session_id):
        if not _can_ask_operator():
            # On a headless instance nobody can answer, and a pending interrupt()
            # checkpoints the turn forever with every later message queued behind it
            # (the same reason run_command's approval defaults off there).
            return None
        from langgraph.types import interrupt

        choice = read_choice(interrupt(consent_form(member, coordinating=_is_coordinating(state))))
        if choice == "no":
            return (
                f"Error: {exc}\n\n{member['name']} was not started — the operator declined. "
                "Do not retry; ask how to proceed."
            )
        if choice == "session":
            grant(session_id)

    ready, detail = await asyncio.to_thread(start_and_wait, member)
    if not ready:
        return f"Error: could not start {member['name']}: {detail}"
    log.info("[delegates] started %s on demand; retrying the delegation", member["name"])
    return await _dispatch_into_room(
        registry,
        target,
        query,
        state,
        tool_call_id=tool_call_id,
        item_id=item_id,
        resume_task_id=resume_task_id,
        timeout=timeout,
    )


def _can_ask_operator() -> bool:
    """Is there a human on this instance to answer an interrupt?"""
    return os.environ.get("PROTOAGENT_UI", "").strip().lower() != "none"


def _is_coordinating(state: Any) -> bool:
    """Whether this turn already involved another participant — wording only.

    Read off the room envelopes the delegation path writes (#3042/#3102): if the
    transcript already carries one, the lead is relaying between participants rather
    than making a single call.
    """
    try:
        messages = list((state or {}).get("messages") or []) if isinstance(state, dict) else []
    except Exception:  # noqa: BLE001 — wording must never break a dispatch
        return False
    return any((getattr(m, "additional_kwargs", {}) or {}).get("room") for m in messages)


async def _dispatch_into_room(
    registry: DelegateRegistry,
    target: str,
    query: str,
    state: Any,
    *,
    tool_call_id: str,
    item_id: str | None = None,
    resume_task_id: str | None = None,
    timeout: float | None = None,
) -> str | Command:
    """Dispatch a foreground delegation and atomically add it to the room (#3102).

    A tool body runs while its graph turn owns the thread. Calling ``aupdate_state``
    here would race that turn's next checkpoint and lose the room record. Returning a
    ``Command`` lets the ToolNode reduce the address, reply, and required ToolMessage
    as part of the turn that produced them.
    """
    async def plain() -> str:
        return await registry.dispatch(
            target, query, item_id=item_id, resume_task_id=resume_task_id, timeout=timeout
        )

    # These identities have dispatch semantics the room helper deliberately does not
    # own. Preserve the managed-git claim and parked-task continuation exactly.
    if item_id or resume_task_id:
        return await plain()
    try:
        from graph.mention_op import dispatch_into_room
        from graph.thread_ids import resolve_thread_id
        from tools.lg_tools import _session_id_from

        session_id = _session_id_from(state) or ""
        if not session_id:
            return await plain()
        messages = list((state or {}).get("messages") or []) if isinstance(state, dict) else []
        outcome = await dispatch_into_room(
            registry,
            target,
            query,
            messages,
            thread_id=resolve_thread_id(None, session_id),
            speaker="assistant",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — conversation bookkeeping must never cost the reply
        log.exception("[delegates] recording delegation in the room failed")
        return await plain()

    if outcome["ok"]:
        result = outcome["reply"]
    else:
        result = f"Error: delegate {target!r} failed: {outcome['error'] or 'unknown error'}"
    # A Command update needs the matching ToolMessage: ToolNode validates that every
    # model tool call has exactly one terminator. It also leaves the usual result in the
    # current loop, so the lead can synthesize immediately while the envelopes persist.
    return Command(
        update={
            "messages": [
                *outcome["messages"],
                ToolMessage(content=result, tool_call_id=tool_call_id, status="success" if outcome["ok"] else "error"),
            ]
        }
    )


async def _spawn_background_delegation(
    registry: DelegateRegistry,
    target: str,
    query: str,
    state: Any,
    *,
    item_id: str = "",
    resume_task_id: str = "",
    timeout: float | None = None,
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
        return await registry.dispatch(
            target, query, item_id=item_id or None, resume_task_id=resume_task_id or None, timeout=timeout
        )

    try:
        from tools.lg_tools import _session_id_from

        # Injected graph state, not the tracing contextvar (empty in a tool body) — the
        # session id is what the completion drains back to (ADR 0050).
        session = _session_id_from(state) or ""
    except Exception:  # noqa: BLE001 — best-effort; job still runs, drain is degraded
        session = ""

    async def _work() -> str:
        # This coroutine runs in a COPY of the tool body's context (asyncio.create_task),
        # LangChain run context and all — so without saying otherwise it could still
        # dispatch a peer's cost row into the spawning turn's stream, but only when the
        # peer happens to answer before that turn closes (#3016). Detached work is not
        # the spawning turn's spend; mark it so the billing is skipped either way rather
        # than decided by the peer's latency.
        mark_delegation_detached()
        # item_id rides into the dispatch itself, so the managed-git claim/dedup
        # applies identically to background and foreground fan-out (one registry,
        # one event loop). timeout rides down the same way so a per-call override
        # holds for a detached long-running coding job too.
        return await registry.dispatch(
            target, query, item_id=item_id or None, resume_task_id=resume_task_id or None, timeout=timeout
        )

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



def _warn_on_identity_shadow(reg: DelegateRegistry) -> None:
    """Warn when a delegate's name shadows the agent's OWN name (#3049 live finding).

    A delegate `proto` on an agent named `protoagent` made the lead answer a status
    question AS the delegate, in first person. The room's cast line now fences identity
    in-prompt, but the collision itself is worth a loud signal at the moment it is
    configured — similar names will keep happening, and the operator picking them is the
    one person who can pick better. Prefix-of-either-direction (case-folded), because the
    live case was similarity, not equality. Warn-only: naming is the operator's call.
    """
    try:
        from runtime.state import STATE

        lead = (
            str(getattr(getattr(STATE, "graph_config", None), "identity_name", "") or "").strip()
            or os.environ.get("AGENT_NAME", "").strip()
            or "protoagent"
        ).casefold()
    except Exception:  # noqa: BLE001 — a warning must never break registration
        return
    for name in reg.names():
        folded = name.casefold()
        if folded == lead or lead.startswith(folded) or folded.startswith(lead):
            log.warning(
                "[delegates] delegate %r shadows this agent's own name %r — the lead can "
                "confuse itself with it (answering AS the delegate). Consider renaming "
                "the delegate.",
                name,
                lead,
            )


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
    host = getattr(registry, "host", None)
    # register() is re-run on hot reload. Clear the old roster-bound service before
    # rebuilding so removed delegates can never remain reachable through the host.
    if host is not None:
        host.invoke_delegate = None
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
    _warn_on_identity_shadow(reg)

    # Publish the live roster on the runtime state so the chat turn driver can route
    # `@<delegate> …` messages straight to a delegate (S1). Set only when the roster is
    # non-empty (cleared below otherwise) so a bare `@` with nothing configured falls
    # through to a normal turn instead of erroring. Re-runs every hot reload (ADR 0025),
    # keeping STATE.delegate_registry in step with the current config.
    from runtime.state import STATE as _STATE

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
        _STATE.delegate_registry = None  # nothing to @-mention → `@` stays ordinary text
        log.debug(
            "[delegates] no delegates declared — `delegate_to` not registered "
            "(propose_delegate is). Add entries under `delegates` "
            "(docs/guides/delegates.md) or use the Delegates panel."
        )
        _register_propose()
        return

    async def _invoke_delegate(
        name: str, prompt: str, conversation_key: str | None = None, *, permissions: str | None = "readonly"
    ) -> str:
        return await reg.dispatch(
            name,
            prompt,
            conversation_key=conversation_key,
            permissions=permissions,
        )

    if host is not None:
        host.invoke_delegate = _invoke_delegate
    _STATE.delegate_registry = reg  # live roster for @-delegate chat dispatch (S1)
    registry.register_tool(_build_delegate_to(reg))
    registry.register_tool(_build_list_agents(reg))
    _register_propose()
    log.info(
        "[delegates] registered delegate_to + list_agents for %d delegate(s): %s",
        len(reg.names()),
        ", ".join(reg.names()),
    )
