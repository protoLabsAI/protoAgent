"""Best-effort subscription usage snapshots (ADR 0122), without token refresh."""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)
_warned: set[str] = set()
_warning_lock = threading.Lock()


@dataclass
class Window:
    id: str
    kind: Literal["session", "weekly", "monthly", "other"]
    window_seconds: int | None
    used_pct: float
    resets_at: datetime
    status: Literal["ok", "warning", "blocked"]
    scope: dict[str, str] | None = None
    binding: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["resets_at"] = self.resets_at.astimezone(UTC).isoformat()
        return result


@dataclass
class Account:
    provider_id: str
    provider_type: str
    account_key: str | None = None
    plan: str | None = None
    source: str = "endpoint"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stale_after: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(seconds=60))
    windows: list[Window] = field(default_factory=list)
    credits: dict[str, Any] | None = None
    status: Literal["ok", "unknown"] = "unknown"
    error: str | None = None
    retry_after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for name in ("fetched_at", "stale_after"):
            result[name] = getattr(self, name).astimezone(UTC).isoformat()
        result["windows"] = [window.to_dict() for window in self.windows]
        return result


def classify(used_pct: float, severity: str | None = None, *, warn_at: float = 80) -> str:
    """Provider blocking/warning signals may strengthen the default thresholds."""
    severity = severity.lower() if isinstance(severity, str) else ""
    if used_pct >= 100 or severity in {"blocked", "exceeded", "exhausted", "limit_reached", "rejected"}:
        return "blocked"
    if used_pct >= warn_at or severity in {"warning", "warn"}:
        return "warning"
    return "ok"


def _status(used_pct: float, node: dict[str, Any]) -> str:
    statuses = [classify(used_pct)]
    for key in ("severity", "status"):
        value = node.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid status")
        statuses.append(classify(used_pct, value))
    return max(statuses, key={"ok": 0, "warning": 1, "blocked": 2}.__getitem__)


def usable_for(account: Account, model: str) -> bool:
    if account.status == "unknown":
        return True
    return not any(
        window.status == "blocked" and (window.scope is None or window.scope.get("model") == model)
        for window in account.windows
    )


def _unknown(account: Account, note: str, *, retry_after: int | None = None) -> Account:
    account.status, account.error, account.retry_after = "unknown", note, retry_after
    account.windows = []
    with _warning_lock:
        if account.provider_id not in _warned:
            _warned.add(account.provider_id)
            log.warning("%s usage unavailable: %s", account.provider_id, note)
    return account


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid number")
    return float(value)


def _reset(value: Any) -> datetime:
    if isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("reset has no timezone")
        return result.astimezone(UTC)
    return datetime.fromtimestamp(_number(value), UTC)


def _scope(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid scope")
    result = {}
    for key in ("model", "bucket"):
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, dict):
            # Claude names a scoped model as {"id": ..., "display_name": ...}; id may be null.
            item = next((item[k] for k in ("id", "display_name") if isinstance(item.get(k), str) and item[k]), None)
        if not isinstance(item, str) or not item:
            raise ValueError("invalid scope value")
        result[key] = item
    if not result:
        raise ValueError("unknown scope")
    return result


def _fetch(provider: str, url: str, peek, parse, http=None, *, headers=None) -> Account:
    """One read, at most one 401 re-read/retry. Error notes never expose bodies/tokens."""
    account = Account(provider, provider)
    try:
        creds = peek()
        if creds is None:
            return _unknown(account, "credentials unavailable")
        for attempt in range(2):
            account.account_key = creds.account_id
            request_headers = {"Authorization": f"Bearer {creds.access_token}", **(headers or {})}
            if provider == "openai-codex":
                if not creds.account_id:
                    return _unknown(account, "account id unavailable")
                request_headers["ChatGPT-Account-Id"] = creds.account_id
            response = (http or httpx).get(url, headers=request_headers, timeout=8.0)
            if response.status_code == 429:
                return _unknown(account, "rate limited", retry_after=300)
            if response.status_code == 401 and attempt == 0:
                current = peek()
                if current is not None and current.access_token != creds.access_token:
                    creds = current
                    continue
            if response.status_code != 200:
                return _unknown(account, f"HTTP {response.status_code}")
            return parse(response.json(), account=account)
    except Exception:
        return _unknown(account, "request or payload unavailable")
    return _unknown(account, "authentication rejected")
