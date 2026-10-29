"""Demo-agent LLM tools (#4609 part 2) — create / configure / chat / delete / list, bound for
the PM and all routed through the demo-stamp guard.

These five tools are the model-facing surface over :mod:`graph.fleet.demo` (part 1). The whole
safety story is that module's stamp gate: EVERY op here calls
:func:`graph.fleet.demo.require_demo` on the target slug BEFORE touching it, so a demo tool can
never create/configure/chat/delete against a REAL fleet member (jobCoach, protoEngineer, …).
Where the core raises :class:`~graph.fleet.demo.NotDemoMember`, these wrappers turn it into a
clear refusal STRING rather than an exception — and the refusal is returned *before* any
supervisor/manager mutation or HTTP call is made, so a non-demo slug is a no-op.

``fleet_demo_chat`` reaches the member's ``/api/chat`` the SAME way
:mod:`tools.fleet_diagnostics` reads its diagnostics endpoints — through THIS instance's own
``/agents/<slug>/*`` reverse proxy over loopback (``graph.fleet.proxy``), presenting the fleet
service token (accepted as the operator tier, ADR 0089). The proxy owns per-slug port
resolution and the operator→member credential swap, so the member's scoped token stays
server-side and never rides in a tool's output. HTTP is the sanctioned seam because the
import-layering contract forbids ``tools`` importing ``operator_api`` (where the proxy route
lives).

Binding is config-gated (``tools.fleet_demo.enabled``, default off — the setting lands in part
3; read absent-safe with ``getattr`` until then). The tools are intended for a single managing
agent (the PM), so ``created_by`` is stamped with this instance's identity name.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

# HTTP timeouts for the loopback hop to this instance's own /agents/<slug>/api/chat proxy. A
# member turn can run long; the proxy rewrites a plain sync POST into submit-then-poll and holds
# the hub socket until the turn finishes or its own 3600s ceiling (#4277), answering 504 there —
# so the near-side read cap just needs to sit a little past that ceiling, never unbounded.
_CONNECT_TIMEOUT = 10.0
_CHAT_READ_TIMEOUT = 3700.0

# Proxy / member HTTP status → a compact error code (mirrors tools.fleet_diagnostics).
_STATUS_ERRORS = {
    401: "unauthorized",
    403: "unauthorized",
    404: "not_found",
    409: "not_running",
    502: "unreachable",
    503: "unavailable",
    504: "timeout",
}


def _refusal(slug: str) -> str:
    """The single refusal string every op returns for a non-demo (or unknown) slug."""
    return f"{slug} is not a demo member — demo tools never touch real agents"


def _operation_failure(exc: Exception, slug: str = "") -> dict:
    """Return operational failures without leaking credentials from exception text."""
    from graph.fleet.demo import NotDemoMember
    from graph.workspaces.manager import WorkspaceBusy

    if isinstance(exc, NotDemoMember):
        return {"ok": False, "refused": _refusal(slug)}
    if isinstance(exc, WorkspaceBusy):
        return {
            "ok": False,
            "error": "workspace_busy",
            "retryable": True,
            "detail": "Removal is incomplete; the workspace is busy. Retry deletion.",
        }
    return {
        "ok": False,
        "error": "operation_failed",
        "detail": f"Demo operation could not complete ({type(exc).__name__}); check fleet state before retrying.",
    }


def _not_demo(slug: str) -> str | None:
    """``None`` when ``slug`` is a live demo member (safe to act on), else the refusal string.

    Runs :func:`graph.fleet.demo.require_demo` — the one authoritative stamp check — and swallows
    its :class:`NotDemoMember` into the refusal, so the caller can short-circuit WITHOUT any
    mutation or HTTP having happened yet. Any other demo error (an unreadable record) also fails
    closed to the refusal: no stamp confirmed, no touch."""
    from graph.fleet.demo import DemoError, require_demo

    try:
        require_demo(slug)
    except DemoError:
        return _refusal(slug)
    return None


# ── the loopback hop to this instance's own /agents/<slug>/* proxy (chat) ──────


def _own_base() -> str | None:
    """The loopback base URL of THIS instance — where the ``/agents/<slug>/*`` proxy lives."""
    from runtime.state import STATE

    port = getattr(STATE, "active_port", None)
    return f"http://127.0.0.1:{port}" if port else None


def _auth_headers() -> dict[str, str]:
    """The operator credential for the loopback call: the fleet service token, which this
    instance's own auth middleware accepts as the operator tier (ADR 0089). The proxy then swaps
    in the per-member credential, so the member's own scoped token never leaves the server."""
    from graph.fleet.service_token import resolve_service_token

    return {"authorization": f"Bearer {resolve_service_token()}"}


def _make_client():
    """The httpx client for the loopback chat call. A seam so tests drive a MockTransport without
    a live server + fleet."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(_CHAT_READ_TIMEOUT, connect=_CONNECT_TIMEOUT))


async def _post_chat(slug: str, message: str, session_id: str) -> dict[str, Any]:
    """POST one message to a demo member's ``/api/chat`` through the hub proxy on loopback and
    return a compact result. The caller has ALREADY confirmed the demo stamp. Every network
    failure mode becomes ``{"ok": False, "error": ...}`` — never an exception."""
    import httpx

    base = _own_base()
    if base is None:
        return {"ok": False, "error": "unavailable", "detail": "this instance has no active port"}
    # Quote the slug to a single path segment: it is a system-owned registry id, but a defensive
    # quote means even an odd id can't escape ``/agents/`` to another route.
    url = f"{base}/agents/{quote(slug, safe='')}/api/chat"
    payload: dict[str, Any] = {"message": message}
    if session_id:
        payload["session_id"] = session_id
    client = _make_client()
    try:
        resp = await client.post(url, headers=_auth_headers(), json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return {"ok": False, "error": "unreachable", "detail": "demo member is not reachable"}
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout", "detail": "demo member did not respond in time"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": "request_failed", "detail": f"chat request failed: {type(exc).__name__}"}
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    return _shape_chat(resp)


def _shape_chat(resp) -> dict[str, Any]:
    """A ``/api/chat`` response → ``{"ok": True, "reply", "session_id"}`` or a compact failure.
    Every non-2xx is a structured answer (the proxy/member's own ``detail`` surfaced when it is a
    string), never an exception."""
    status = resp.status_code
    if status >= 400:
        error = _STATUS_ERRORS.get(status, "http_error")
        detail: Any = None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON error body is fine
            body = None
        if isinstance(body, dict):
            raw = body.get("detail")
            detail = raw.get("message") if isinstance(raw, dict) else raw
        return {"ok": False, "error": error, "detail": detail or f"member returned HTTP {status}", "status": status}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON 200 is a malformed member, not a crash
        return {"ok": False, "error": "malformed", "detail": "member response was not JSON"}
    reply = body.get("response") if isinstance(body, dict) else None
    session_id = body.get("session_id") if isinstance(body, dict) else None
    return {"ok": True, "reply": reply or "", "session_id": session_id or ""}


# ── the tools ──────────────────────────────────────────────────────────────────


def _build_fleet_demo_tools(config) -> list:
    """Bind the five demo-agent tools (config ``tools.fleet_demo.enabled``, default off).

    Every tool routes through :func:`graph.fleet.demo.require_demo`, so none can ever act on a
    real fleet member. ``created_by`` on a new demo is stamped with this instance's identity
    name (the managing agent), read from ``config`` at bind time."""
    from langchain_core.tools import tool

    created_by = str(getattr(config, "identity_name", "") or "agent")

    @tool
    async def fleet_demo_create(archetype: str, model_connection: str, model: str, name: str = "") -> dict:
        """Create a throwaway DEMO fleet member from an archetype and start it. Only ever creates
        a demo-stamped member — never a real agent.

        Args:
            archetype: Archetype id from the catalog (or an installed bundle).
            model_connection: Model connection id to point the demo at (ADR 0106).
            model: Model name within that connection.
            name: Optional member name; blank picks ``demo-<archetype>-<shortid>``.

        Returns ``{slug, port, archetype_ref, plugin_versions, created_at}`` — the member's
        scoped token is kept server-side (used by fleet_demo_chat), never returned here. Refuses
        with a message when the archetype is unknown or the live-demo cap is reached.
        Other operational failures return ``{ok: False, error, detail}``.
        """
        from graph.fleet.demo import DemoError, create_demo

        try:
            out = await asyncio.to_thread(
                create_demo,
                archetype,
                model_connection,
                model,
                name=name or None,
                created_by=created_by,
            )
        except DemoError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — model-facing operation boundary
            return _operation_failure(exc)
        # Strip the scoped bearer: it must not cross into the model's context (fleet_demo_chat
        # re-resolves it server-side). Keep provenance the PM needs to use the demo.
        return {
            "ok": True,
            "slug": out.get("slug"),
            "port": out.get("port"),
            "archetype_ref": out.get("archetype_ref"),
            "plugin_versions": out.get("plugin_versions") or {},
            "created_at": out.get("created_at"),
        }

    @tool
    async def fleet_demo_configure(
        slug: str,
        model_connection: str = "",
        model: str = "",
        plugin_settings: dict | None = None,
    ) -> dict:
        """Re-point a DEMO member's model and/or overlay per-plugin config. Only ever touches a
        demo-stamped member — a real agent's config is refused untouched.

        Args:
            slug: The demo member's slug.
            model_connection: New model connection id (with ``model``, both required to change it).
            model: New model name within that connection.
            plugin_settings: ``{plugin_id: {key: value}}`` config overlaid per plugin.

        Returns ``{ok, slug, changed, plugins}``, or ``{ok: False, refused: message}``.
        Operational failures return ``{ok: False, error, detail}``.
        """
        try:
            refusal = await asyncio.to_thread(_not_demo, slug)
            if refusal is not None:
                return {"ok": False, "refused": refusal}
            from graph.fleet.demo import configure_demo

            out = await asyncio.to_thread(
                configure_demo,
                slug,
                model_connection=model_connection or None,
                model=model or None,
                plugin_settings=plugin_settings,
            )
            return {"ok": True, **out}
        except Exception as exc:  # noqa: BLE001 — includes failures after the stamp check
            return _operation_failure(exc, slug)

    @tool
    async def fleet_demo_chat(slug: str, message: str, session_id: str = "") -> dict:
        """Send a message to a DEMO member and return its reply. Only ever chats with a
        demo-stamped member — a real agent is refused with no request made.

        Args:
            slug: The demo member's slug.
            message: The message to send.
            session_id: Optional session id to continue a prior exchange; blank starts a new one.

        Returns ``{ok, reply, session_id}`` on success, else a compact error / refusal.
        """
        from graph.fleet.demo import DemoError, require_demo

        try:
            record = await asyncio.to_thread(require_demo, slug)
            # The guard accepts display names; the proxy requires the checked record's ID.
            member_id = record.get("id")
            if not isinstance(member_id, str) or not member_id:
                return {"ok": False, "refused": _refusal(slug)}
            return await _post_chat(member_id, message, session_id)
        except DemoError:
            return {"ok": False, "refused": _refusal(slug)}
        except Exception as exc:  # noqa: BLE001 — model-facing operation boundary
            return _operation_failure(exc, slug)

    @tool
    async def fleet_demo_delete(slug: str) -> dict:
        """Stop and purge a DEMO member. Only ever deletes a demo-stamped member — a real agent
        is refused untouched.

        Args:
            slug: The demo member's slug.

        Returns ``{ok: True, slug, deleted: True}``, or ``{ok: False, refused: message}``.
        Operational failures return ``{ok: False, error, detail}``; busy removal is retryable.
        """
        try:
            refusal = await asyncio.to_thread(_not_demo, slug)
            if refusal is not None:
                return {"ok": False, "refused": refusal}
            from graph.fleet.demo import delete_demo

            await asyncio.to_thread(delete_demo, slug)
            return {"ok": True, "slug": slug, "deleted": True}
        except Exception as exc:  # noqa: BLE001 — includes failures after the stamp check
            return _operation_failure(exc, slug)

    @tool
    async def fleet_demo_list() -> list | dict:
        """List the live DEMO members (stamped ``demo: true``) this instance is running. Real
        fleet members are excluded by construction. Returns a list of ``{id, name, port,
        created_by, created_at, ...}`` rows, or ``{ok: False, error, detail}`` on failure."""
        from graph.fleet.demo import list_demos

        try:
            return await asyncio.to_thread(list_demos)
        except Exception as exc:  # noqa: BLE001 — model-facing operation boundary
            return _operation_failure(exc)

    return [fleet_demo_create, fleet_demo_configure, fleet_demo_chat, fleet_demo_delete, fleet_demo_list]
