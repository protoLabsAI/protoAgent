"""ChatGPT/Codex usage endpoint adapter, including weekly-only plans."""

from graph.providers import oauth
from graph.providers.usage import Account, Window, _fetch, _number, _reset, _scope, _status, _unknown


def _windows(rate, prefix, scope=None):
    windows = []
    if not isinstance(rate, dict):
        raise ValueError("invalid rate limit")
    for name in ("primary_window", "secondary_window"):
        node = rate.get(name)
        if node is None:
            continue
        seconds = _number(node["limit_window_seconds"])
        if seconds <= 0 or not seconds.is_integer():
            raise ValueError("invalid duration")
        used = min(100, max(0, _number(node["used_percent"])))
        status = _status(used, node)
        if rate.get("limit_reached") is True or rate.get("allowed") is False:
            status = "blocked"
        kind = {18000: "session", 604800: "weekly", 2592000: "monthly"}.get(seconds, "other")
        binding = node.get("binding", False)
        if not isinstance(binding, bool):
            raise ValueError("invalid binding")
        windows.append(Window(f"{prefix}:{name}", kind, int(seconds), used,
                              _reset(node["reset_at"]), status, scope, binding))
    if not windows:
        raise ValueError("missing windows")
    return windows


def parse_codex_usage(payload, *, account: Account | None = None) -> Account:
    account = account or Account("openai-codex", "openai-codex")
    try:
        windows = _windows(payload["rate_limit"], "rate_limit")
        additional = payload.get("additional_rate_limits", [])
        if not isinstance(additional, list):
            raise ValueError("invalid additional limits")
        for index, bucket in enumerate(additional):
            scope = bucket.get("scope")
            if scope is None:
                scope = {key: bucket[key] for key in ("model", "bucket") if key in bucket}
                if "limit_name" in bucket:
                    scope.setdefault("bucket", bucket["limit_name"])
            windows.extend(_windows(bucket["rate_limit"], f"additional:{index}", _scope(scope)))
        account.windows, account.status = windows, "ok"
        account.plan = payload.get("plan_type") if isinstance(payload.get("plan_type"), str) else None
        account.credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else None
        return account
    except Exception:
        return _unknown(account, "unrecognized usage payload")


def fetch_codex_usage(*, http=None) -> Account:
    return _fetch("openai-codex", "https://chatgpt.com/backend-api/wham/usage",
                  oauth.peek_codex_access_token, parse_codex_usage, http)
