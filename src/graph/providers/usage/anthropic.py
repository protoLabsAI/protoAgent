"""Claude OAuth usage endpoint adapter; undocumented payloads fail to unknown."""

from graph.providers import oauth
from graph.providers.usage import Account, Window, _fetch, _number, _reset, _scope, _status, _unknown

_DURATIONS = {"5h": ("session", 18000), "7d": ("weekly", 604800),
              "session": ("session", 18000), "weekly": ("weekly", 604800),
              "monthly": ("monthly", None)}


def parse_anthropic_usage(payload, *, account: Account | None = None) -> Account:
    account = account or Account("anthropic-oauth", "anthropic-oauth")
    try:
        limits = payload["limits"]
        if not isinstance(limits, list) or not limits:
            raise ValueError("missing limits")
        windows = []
        for index, limit in enumerate(limits):
            raw_kind = limit["kind"]
            if not isinstance(raw_kind, str):
                raise ValueError("invalid kind")
            # The live payload's kinds are "session"/"weekly_all"/"weekly_scoped"; its
            # "group" names the period, so prefer it.
            group = limit.get("group")
            kind, seconds = _DURATIONS.get(group if isinstance(group, str) else raw_kind,
                                           _DURATIONS.get(raw_kind, ("other", None)))
            used = min(100, max(0, _number(limit["percent"])))
            scope = _scope(limit.get("scope"))
            binding = limit.get("binding", limit.get("is_active", False))
            if not isinstance(binding, bool):
                raise ValueError("invalid binding")
            windows.append(Window(
                id=f"{raw_kind}:{index}", kind=kind, window_seconds=seconds,
                used_pct=used, resets_at=_reset(limit["resets_at"]),
                status=_status(used, limit),
                scope=scope, binding=binding,
            ))
        account.windows, account.status = windows, "ok"
        account.plan = payload.get("plan") if isinstance(payload.get("plan"), str) else None
        account.credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else None
        return account
    except Exception:
        return _unknown(account, "unrecognized usage payload")


def fetch_anthropic_usage(*, http=None) -> Account:
    return _fetch(
        "anthropic-oauth", "https://api.anthropic.com/api/oauth/usage",
        oauth.peek_anthropic_access_token, parse_anthropic_usage, http,
        headers={"anthropic-beta": "oauth-2025-04-20"},
    )
