"""A2A peer federation — consult another agent over the A2A protocol.

When the answer depends on another agent's context, this agent sends it an
A2A message and relays the reply. Backported from the protoLabs fleet (gina),
simplified for core: peers come from **environment variables only** (no DB
schema change). Register a peer with:

    PEER_<HANDLE>_URL=https://other-agent.example   # base URL (its /a2a is derived)
    PEER_<HANDLE>_TOKEN=<bearer>                     # optional, if the peer requires auth

``peer_list`` / ``peer_consult`` are added to the toolset only when at least
one peer is configured (see ``get_peer_tools``).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

from langchain_core.tools import tool

from tools.fallbacks import with_fallback

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_POLL_INTERVAL_S = 1.0
_MAX_POLLS = 150  # ~150s — a delegated skill (e.g. Quinn's bug_triage) can run a while


def _resolve_peer(name: str) -> tuple[str | None, str | None]:
    if not _HANDLE_RE.match(name or ""):
        return None, None
    key = name.upper().replace("-", "_")
    return os.environ.get(f"PEER_{key}_URL"), os.environ.get(f"PEER_{key}_TOKEN")


def list_env_peers() -> list[dict]:
    """Peers registered via ``PEER_<HANDLE>_URL`` env vars."""
    peers: list[dict] = []
    for key, url in os.environ.items():
        m = re.match(r"^PEER_([A-Z0-9_]+)_URL$", key)
        if not m:
            continue
        peers.append({
            "handle": m.group(1).lower().replace("_", "-"),
            "url": url,
            "has_token": bool(os.environ.get(f"PEER_{m.group(1)}_TOKEN")),
        })
    return sorted(peers, key=lambda p: p["handle"])


def _extract_text(result) -> str | None:
    """Pull text out of an A2A 1.0 result — a ``{"task": ...}`` envelope (the
    ``SendMessage`` / ``GetTask`` response) or a bare Message. Tolerant of parts
    with or without an explicit ``kind`` tag (1.0 text parts carry just ``text``)."""
    if not isinstance(result, dict):
        return None
    task = result.get("task", result) or {}
    for art in task.get("artifacts") or []:
        chunks = [p.get("text", "") for p in art.get("parts", []) if p.get("text")]
        if any(chunks):
            return "\n".join(c for c in chunks if c)
    msg = (task.get("status") or {}).get("message") or {}
    parts = [p.get("text", "") for p in (msg.get("parts") or []) if p.get("text")]
    text = "\n".join(p for p in parts if p)
    return text or None


_TERMINAL = {"completed", "failed", "canceled"}  # v0.3 spellings (back-compat)


def _is_terminal(state) -> bool:
    """True for A2A 1.0 terminal task states (``TASK_STATE_COMPLETED`` / ``FAILED``
    / ``CANCELLED`` / ``REJECTED``) and their v0.3 lowercase spellings."""
    return str(state or "").upper().endswith(
        ("COMPLETED", "FAILED", "CANCELED", "CANCELLED", "REJECTED"))


def get_peer_tools() -> list:
    """Return the peer tools — only call when peers are configured."""

    @tool
    @with_fallback()
    async def peer_list() -> str:
        """List the peer agents this agent can consult (from PEER_<HANDLE>_URL env)."""
        peers = list_env_peers()
        if not peers:
            return "No peers configured (set PEER_<HANDLE>_URL)."
        lines = [f"{len(peers)} peer(s):"]
        for p in peers:
            lines.append(f"  - {p['handle']}: {p['url']}" + (" (auth)" if p["has_token"] else ""))
        return "\n".join(lines)

    @tool
    @with_fallback()
    async def peer_consult(name: str, message: str, skill: str = "") -> str:
        """Ask another agent (by peer handle) a question, or delegate a named skill, and return its reply.

        Deprecated: prefer ``delegate_to(target, query)`` with an ``a2a`` delegate
        (the unified delegate registry, ADR 0025) — same A2A consult over one tool
        alongside openai/acp delegates, with a console panel. This tool stays for
        back-compat and will be removed in a future release.

        Args:
            name: Peer handle (must match a configured ``PEER_<HANDLE>_URL``).
            message: The question / instruction — be specific; the peer answers from its own context.
            skill: Optional skill to route to on the peer (sent as ``metadata.skillHint``). A fleet
                peer like ``workstacean`` dispatches the named skill to the owning agent — e.g.
                ``skill="bug_triage"`` / ``"pr_review"`` routes to Quinn. Omit to hit the peer's
                default (``chat``) executor. Peers that ignore skillHint just read ``message``.
        """
        if not name.strip():
            return "Error: peer name is required."
        base, token = _resolve_peer(name)
        if not base:
            return f"Error: peer {name!r} is not configured (set PEER_{name.upper().replace('-', '_')}_URL)."

        import httpx

        url = f"{base.rstrip('/')}/a2a"
        # Opt-in SSRF/CIDR allowlist (#572): when configured, the peer must
        # resolve into the allowlist. Unset ⇒ unrestricted (today's behavior).
        import security
        _blocked = security.check_url(url)
        if _blocked:
            return _blocked.replace("destination", f"peer {name!r}", 1)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async def _rpc(client, method, params):
            body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
            r = await client.post(url, json=body, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data.get("result") or {}

        msg: dict = {
            "role": "ROLE_USER",
            "parts": [{"text": message}],
            "messageId": str(uuid.uuid4()),
        }
        params: dict = {"message": msg}
        if skill.strip():
            # Route to a named skill. A2A peers read skillHint from the message
            # metadata (and some from params.metadata) — set both for safety.
            hint = {"skillHint": skill.strip()}
            msg["metadata"] = hint
            params["metadata"] = hint

        try:
            # A delegated skill answers synchronously on SendMessage and can run
            # for a minute+ (e.g. Quinn's bug_triage ≈ 58s) — the peer holds the
            # connection until done rather than returning a task to poll. Give the
            # request room; the polling loop below covers peers that DO go async.
            async with httpx.AsyncClient(timeout=httpx.Timeout(200.0, connect=10.0)) as client:
                result = await _rpc(client, "SendMessage", params)
                # Inline reply? (some peers answer synchronously)
                text = _extract_text(result)
                if text:
                    return f"[{name}] {text}"
                # Otherwise poll the task to a terminal state (A2A 1.0: GetTask,
                # task in a `result.task` envelope, TASK_STATE_* enums).
                task = result.get("task", result) or {}
                task_id = task.get("id")
                state = (task.get("status") or {}).get("state")
                polls = 0
                while task_id and not _is_terminal(state) and polls < _MAX_POLLS:
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    polls += 1
                    result = await _rpc(client, "GetTask", {"name": task_id})
                    task = result.get("task", result) or {}
                    state = (task.get("status") or {}).get("state")
                text = _extract_text(result)
                if text:
                    return f"[{name}] {text}"
                return f"Error: peer {name!r} returned no text (state={state})."
        except Exception as exc:  # noqa: BLE001 - surface as a tool error string
            return f"Error: consulting peer {name!r} failed: {exc}"

    return [peer_list, peer_consult]
