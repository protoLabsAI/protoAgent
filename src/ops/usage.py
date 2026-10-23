"""Usage op (ADR 0075 D2, ADR 0122 D6) — the one read every usage surface projects.

``usage.snapshot`` reads the on-demand cache in :mod:`observability.usage_store` for the
native-OAuth providers (``anthropic-oauth``, ``openai-codex``) and returns their accounts.
It is the single implementation the REST route (``GET /api/usage``), a future CLI, and the
operator MCP all call — "one operation, three projections". Read-only (``mutates=False``):
it may *trigger* a poll of a provider's usage endpoint, but it writes no agent/account state.

Degradation is total (ADR 0122 D4): a provider that is disconnected, unknown, or whose
fetch raised comes back as an ``unknown`` account — never an exception — so a surface can
render partial data and never 500.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ops import OpContext, op


@op(
    name="usage.snapshot",
    mutates=False,
    summary="Best-effort subscription-usage snapshot for the native-OAuth providers (poll at most once per minute; disconnected/failed providers read as unknown).",
)
def snapshot(ctx: OpContext | None = None, account: str | None = None, force: bool = False,
             cache_only: bool = False) -> dict:
    """``{accounts: [Account.to_dict() + {stale}], fetched_at}``.

    ``account`` narrows to one provider (an unknown name still returns an ``unknown``
    entry, never an error); ``force`` bypasses the ordinary poll min-interval but still
    honours a provider's 429 back-off. ``cache_only`` never polls, even under force;
    fleet members use it so shared accounts do not acquire additional pollers.
    """
    from observability import usage_store

    store = usage_store.default_store()
    provider_ids = [account] if account else list(usage_store.PROVIDER_IDS)
    accounts = []
    for pid in provider_ids:
        try:
            data = store.cached(pid) if cache_only else store.get(pid, force=force)
            accounts.append(data or {"provider_id": pid, "status": "unknown"})
        except Exception:  # noqa: BLE001 — a snapshot never raises (ADR 0122 D4)
            accounts.append({"provider_id": pid, "provider_type": pid, "status": "unknown", "stale": False})
    return {"accounts": accounts, "fetched_at": datetime.now(UTC).isoformat()}


def merge_fleet_usage(host: dict, peers: dict[str, dict | None]) -> dict:
    """Pure fleet projection, host first and first snapshot wins per account key.

    Missing identities remain separate per member/provider: unknown is not an
    account identity. None or malformed member payloads mean unreachable. Only
    public usage fields survive; provider errors and credential metadata do not.
    No input is mutated, and neither reads nor polls happen here.
    """
    accounts: dict[tuple, dict] = {}
    members = {}
    for slug, payload in [("host", host), *((s, p) for s, p in peers.items() if s != "host")]:
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            members[slug] = {"reachable": False, "error": "usage unavailable"}
            continue
        members[slug] = {"reachable": True}
        for index, row in enumerate(rows):
            key = row.get("account_key")
            key = key if isinstance(key, str) and key else None
            identity = ("account", key) if key else ("unknown", slug, index)
            if identity in accounts:
                if slug not in accounts[identity]["members"]:
                    accounts[identity]["members"].append(slug)
                continue
            windows = []
            for window in (row.get("windows") or []) if isinstance(row.get("windows", []), list) else []:
                if not isinstance(window, dict):
                    continue
                clean = {k: v for k, v in window.items() if k in {
                    "id", "kind", "window_seconds", "used_pct", "resets_at", "status", "binding"
                } and isinstance(v, (str, int, float, bool, type(None)))}
                scope = window.get("scope")
                clean["scope"] = {k: v for k, v in scope.items()
                                  if k in {"model", "bucket"} and isinstance(v, str)} if isinstance(scope, dict) else None
                windows.append(clean)
            accounts[identity] = {
                "account_key": key,
                "provider_id": row.get("provider_id") if isinstance(row.get("provider_id"), str) else None,
                "plan": row.get("plan") if isinstance(row.get("plan"), str) else None,
                "windows": windows,
                "status": "ok" if row.get("status") == "ok" and not row.get("stale") else "unknown",
                "stale": bool(row.get("stale")),
                "members": [slug],
            }
    return {"accounts": list(accounts.values()), "members": members}


def attribution_windows(accounts: list[dict]) -> list[dict]:
    """Current known windows, capped for bounded fleet diagnostics. Unknown stays empty."""
    result = []
    now = datetime.now(UTC)
    for account in accounts:
        if not account.get("account_key") or account.get("stale") or account.get("status") != "ok":
            continue
        for window in account.get("windows", []):
            try:
                end = datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00"))
                seconds = window["window_seconds"]
                if not isinstance(seconds, int) or not 0 < seconds <= 366 * 86400:
                    continue
                start = end - timedelta(seconds=seconds)
                if not start <= now < end:
                    continue
                scope = window.get("scope") or {}
                if scope and not scope.get("model"):
                    continue  # a provider bucket cannot be mapped to observed models
                result.append({"account_key": account["account_key"], "window_id": window["id"],
                               "since": start.isoformat(), "until": end.isoformat(),
                               "model": scope.get("model")})
            except (KeyError, TypeError, ValueError):
                continue
    return result[:32]


def local_attribution(windows: list[dict]) -> list[dict]:
    """Read only aggregate token counts, never credentials or transcript rows."""
    from runtime.state import STATE

    rows = []
    for window in windows[:32]:
        try:
            if not isinstance(window, dict) or STATE.telemetry_store is None:
                continue
            window = {k: window.get(k) for k in ("account_key", "window_id", "since", "until", "model")}
            if any(not isinstance(window[k], str) or len(window[k]) > 512
                   for k in ("account_key", "window_id", "since", "until")):
                continue
            if window["model"] is not None and not isinstance(window["model"], str):
                continue
            start, end = (datetime.fromisoformat(window[k]) for k in ("since", "until"))
            if not 0 < (end - start).total_seconds() <= 366 * 86400:
                continue
            tokens = STATE.telemetry_store.attributed_tokens(
                window["account_key"], window["since"], window["until"], window.get("model"),
            )
            rows.append({**window, "tokens": tokens})
        except Exception:  # noqa: BLE001 — disabled/unreadable telemetry is unknown
            continue
    return rows


def merge_attribution(accounts: list[dict], windows: list[dict], members: dict[str, dict | None]) -> None:
    """Attach per-window shares to a fresh projection. Only matching bounds contribute."""
    for account in accounts:
        account["by_agent"] = []
        for window in windows:
            if window["account_key"] != account.get("account_key"):
                continue
            shares = []
            unavailable = []
            for slug, payload in members.items():
                rows = payload.get("attribution", []) if isinstance(payload, dict) else []
                match = next((r for r in rows if isinstance(r, dict)
                              and all(r.get(k) == v for k, v in window.items())), None) if isinstance(rows, list) else None
                tokens = match.get("tokens") if match else None
                if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                    unavailable.append(slug)
                    continue
                shares.append({"member": slug, "tokens": tokens})
            total = sum(r["tokens"] for r in shares)
            for row in shares:
                row["share_pct"] = 100 * row["tokens"] / total if total else None
            account["by_agent"].append({**window, "attributed_tokens": total,
                                        "members": shares, "unavailable": unavailable})
