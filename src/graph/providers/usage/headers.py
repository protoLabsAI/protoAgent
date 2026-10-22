"""Passive, bounded header parsing and failure-isolated usage capture (ADR 0122)."""

from copy import copy
from datetime import UTC, datetime
from functools import cache
import math

from graph.providers.usage import Account, Window, classify


def _headers(headers):
    # Bound work and reject unusual containers without stringifying arbitrary objects.
    if len(headers) > 256:
        raise ValueError("too many headers")
    return {key.lower(): value for key, value in headers.items()
            if isinstance(key, str) and len(key) <= 128
            and isinstance(value, str) and len(value) <= 128}


def _number(value):
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("invalid number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid number")
    return number


def parse_anthropic_headers(headers) -> list[Window]:
    """Read fractional utilization and Unix resets; absent/bad windows are skipped."""
    try:
        headers = _headers(headers)
        claim = headers.get("anthropic-ratelimit-unified-representative-claim")
        claim = {"five_hour": "5h", "seven_day": "7d"}.get(claim, claim)
        windows = []
        for index, (name, kind, seconds) in enumerate((("5h", "session", 18000), ("7d", "weekly", 604800))):
            prefix = f"anthropic-ratelimit-unified-{name}"
            try:
                used = min(100, max(0, _number(headers[f"{prefix}-utilization"]) * 100))
                reset = datetime.fromtimestamp(_number(headers[f"{prefix}-reset"]), UTC)
                status = headers.get(f"{prefix}-status")
                if status == "allowed_warning":
                    status = "warning"
                windows.append(Window(
                    f"{name}:{index}", kind, seconds, used, reset,
                    classify(used, status), binding=claim == name,
                ))
            except Exception:
                continue
        return windows
    except Exception:
        return []


def parse_codex_headers(headers) -> list[Window]:
    """Classify by duration, including weekly-only plans using the primary slot."""
    try:
        headers = _headers(headers)
        windows = []
        for name in ("primary", "secondary"):
            prefix = f"x-codex-{name}"
            try:
                seconds = _number(headers[f"{prefix}-window-minutes"]) * 60
                if not math.isfinite(seconds) or seconds <= 0 or not seconds.is_integer():
                    continue
                used = min(100, max(0, _number(headers[f"{prefix}-used-percent"])))
                reset = datetime.fromtimestamp(_number(headers[f"{prefix}-reset-at"]), UTC)
                kind = {18000: "session", 604800: "weekly", 2592000: "monthly"}.get(seconds, "other")
                windows.append(Window(f"rate_limit:{name}_window", kind, int(seconds), used, reset, classify(used)))
            except Exception:
                continue
        return windows
    except Exception:
        return []


def capture(provider_id, headers) -> None:
    """Merge observations without polling, credentials, or exceptions on model calls."""
    try:
        from observability import usage_store

        parser = {"anthropic-oauth": parse_anthropic_headers, "openai-codex": parse_codex_headers}.get(provider_id)
        windows = parser(headers) if parser else []
        if windows:
            account = Account(provider_id, provider_id, source="headers")
            usage_store.default_store().merge_windows(
                account, windows, source="headers", fetched_at=account.fetched_at,
            )
    except Exception:
        pass


def anthropic_response_hook(response) -> None:
    """Read only headers, including error responses, before any streaming body read."""
    try:
        capture("anthropic-oauth", response.headers)
    except Exception:
        pass


async def async_anthropic_response_hook(response) -> None:
    anthropic_response_hook(response)


class _BorrowedPool:
    # The cached upstream client owns the transport. Neither SDK close nor the
    # upstream wrapper destructor may close it through this per-model facade.
    def close(self):
        pass

    async def aclose(self):
        pass


@cache
def _borrowed_type(cls):
    return type(f"Usage{cls.__name__}", (_BorrowedPool, cls), {})


def with_anthropic_headers(client, *, asynchronous=False):
    """Isolate hooks on a client facade while preserving the upstream shared pool."""
    owner = client
    client = copy(owner)
    client.__class__ = _borrowed_type(type(owner))
    client._usage_pool_owner = owner  # keep the pool alive for the facade lifetime
    client.event_hooks = {key: list(hooks) for key, hooks in client.event_hooks.items()}
    client.event_hooks.setdefault("response", []).append(
        async_anthropic_response_hook if asynchronous else anthropic_response_hook
    )
    return client
