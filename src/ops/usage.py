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

from datetime import UTC, datetime

from ops import OpContext, op


@op(
    name="usage.snapshot",
    mutates=False,
    summary="Best-effort subscription-usage snapshot for the native-OAuth providers (poll at most once per minute; disconnected/failed providers read as unknown).",
)
def snapshot(ctx: OpContext | None = None, account: str | None = None, force: bool = False) -> dict:
    """``{accounts: [Account.to_dict() + {stale}], fetched_at}``.

    ``account`` narrows to one provider (an unknown name still returns an ``unknown``
    entry, never an error); ``force`` bypasses the ordinary poll min-interval but still
    honours a provider's 429 back-off.
    """
    from observability import usage_store

    store = usage_store.default_store()
    provider_ids = [account] if account else list(usage_store.PROVIDER_IDS)
    accounts = []
    for pid in provider_ids:
        try:
            accounts.append(store.get(pid, force=force))
        except Exception:  # noqa: BLE001 — a snapshot never raises (ADR 0122 D4)
            accounts.append({"provider_id": pid, "provider_type": pid, "status": "unknown", "stale": False})
    return {"accounts": accounts, "fetched_at": datetime.now(UTC).isoformat()}
