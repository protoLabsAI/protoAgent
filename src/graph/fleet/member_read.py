"""Bounded, authenticated access to a fleet member's API (ADR 0135 D8).

A hub-side caller that needs a member's JSON — the operator rollup routes
(``/api/telemetry/fleet``, ``/api/usage/fleet``) and the hub's fleet tools —
reads it through ``_fetch_member_json``, so every member read swaps credentials,
bounds its wait and contains its failures the same way. It lives beside
``proxy._target_for_slug`` (whose per-slug resolution it reuses) because
``graph``, ``operator_api`` and ``tools`` may all import ``graph.fleet``, while
``tools`` may not import ``operator_api``, where this helper used to sit.

Callers reach it as ``member_read._fetch_member_json`` (module attribute, looked
up at call time): there is exactly one definition, and one place to patch.
``_post_member_json`` shares its target, credential and timeout path for writes
through existing member operator routes.
"""

from __future__ import annotations


async def _fetch_member_json(slug: str, path: str) -> dict | None:
    """Pure-GET read of ``<member>/<path>`` via the ADR 0042 slug-proxy resolution.

    Reuses the fleet proxy's per-slug target resolution: a LOCAL member gets the
    fleet service token in place of the hub credential (ADR 0089 D3 — the same
    swap ``/agents/{slug}/*`` does for an operator caller); a REMOTE member
    carries its stored bearer. Any failure — not running, connect/timeout,
    non-200, non-JSON — returns None: the caller REPORTS the member unreachable,
    it never retries into it or restarts it.
    """
    try:
        resp = await _member_request(slug, path, "get")
        if resp is None or resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — every failure mode is "unreachable", never a 500
        return None


async def _post_member_json(slug: str, path: str, payload: dict) -> dict:
    """Contained POST: success and HTTP status, or unreachable; never expose member text."""
    try:
        resp = await _member_request(slug, path, "post", payload)
        if resp is not None:
            return {"ok": 200 <= resp.status_code < 300, "status_code": resp.status_code}
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "error": "unreachable"}


async def _member_request(slug: str, path: str, method: str, payload: dict | None = None):
    """Shared target, credentials and timeout for contained member reads and writes."""
    # Every step is inside the boundary, resolution and token minting included
    # (#3018). Both used to sit ABOVE the try, so a raise there escaped as a 500
    # for the WHOLE rollup — the exact opposite of the contract this docstring
    # states. Not hypothetical: ``_target_for_slug`` still subscripts registry
    # records straight through (``rec["port"]`` on a local state entry the hub
    # believes is running, ``remote["url"]`` on a remote record), so one field
    # missing from one hand-edited record raises KeyError here rather than
    # resolving to None. The loaders are tolerant of an unreadable FILE (they
    # log and return {}), so what reaches this line is a bad RECORD — and the
    # ROSTER read is total over those now (``supervisor.list_remotes``/
    # ``status``, same fix), which is what lets such a member get this far at
    # all instead of taking the roster down. Here is where it becomes the
    # ``reachable: false`` row the caller reports.
    try:
        import httpx

        from graph.fleet import proxy

        target = proxy._target_for_slug(slug)
        if target is None:
            return None
        base, extra = target
        headers = dict(extra)
        # The fleet token is for LOCAL members only — a remote the hub holds no token for
        # reads anonymously (and 401s if it is secured) rather than receive the
        # loopback-only fleet credential it could not verify anyway (ADR 0113 D4).
        if slug not in proxy._remote_slugs and not any(k.lower() == "authorization" for k in headers):
            from graph.fleet.service_token import resolve_service_token

            headers["authorization"] = f"Bearer {resolve_service_token()}"
        request = getattr(proxy._get_client(), method)
        return await request(
            f"{base}/{path}",
            headers=headers,
            **({"json": payload} if method == "post" else {}),
            # The shared proxy client reads forever (SSE-safe); a member request
            # must not — a stalled member is just "unreachable" after the bound.
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    except Exception:  # noqa: BLE001
        return None
