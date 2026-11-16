"""Role model construction and retryable-only fallback diagnostics (ADR 0133)."""

import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock

import httpcore
import httpx
from anthropic import APIConnectionError as AnthropicConnectionError
from langchain_core.runnables import Runnable
from openai import APIConnectionError

from graph.config import role_chain
from graph.llm import create_llm

log = logging.getLogger(__name__)
_lock = Lock()
_last_fallbacks: dict[str, dict] = {}
_streaks: set[str] = set()

# Keep the ADR's public resolver spelling while sharing M1's inheritance rules.
resolve_chain = role_chain


def retryable_error(exc: Exception) -> bool:
    """Classify provider failures, including LangChain's Responses error format."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429 or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    codes = [getattr(exc, "code", None), getattr(exc, "type", None)]
    if isinstance(error, dict):
        codes.extend((error.get("code"), error.get("type")))
    if any(code in ("rate_limit_error", "usage_limit_reached") for code in codes):
        return True
    # langchain-openai converts Responses SSE `error` / `response.failed` to
    # ValueError, losing the structured code. Match only those leading formats,
    # not a substring that could occur in a bad-request payload or refusal.
    if isinstance(exc, ValueError) and re.match(
        r"^(?:(?:rate_limit_error|usage_limit_reached):|"
        r"ResponseError\(code=['\"](?:rate_limit_error|usage_limit_reached)['\"], message=)",
        str(exc),
    ):
        return True
    return isinstance(exc, (
        TimeoutError, ConnectionError, APIConnectionError, AnthropicConnectionError,
        httpx.TimeoutException, httpx.NetworkError,
        httpcore.TimeoutException, httpcore.NetworkError,
    ))


def last_fallback(role: str) -> dict | None:
    """Return the latest successful fallback: UTC time, failed models, winner, reason.

    Records survive primary recovery; the Settings consumer controls display age.
    Reasons contain exception types/status only, never provider payloads or prompts.
    """
    with _lock:
        record = _last_fallbacks.get(role)
        return {**record, "from": list(record["from"])} if record else None


def skipped_entries(config, role: str, names: list[str]) -> list[dict]:
    """Build diagnostics for this config and chain only; never expose provider payloads."""
    with _lock:
        record = getattr(config, "_model_role_skips", {}).get(role)
        if record is None or record[0] != tuple(names):
            return []
        return [dict(entry) for entry in record[1]]


def build_fallbacks(config, role: str, names: list[str], build):
    """Build a chain's tail, keeping names aligned with models after skips.

    The caller builds the primary outside this guard so its failure remains fatal.
    Diagnostics live on the config, not globally: a failed config transaction cannot
    overwrite the running graph's status. A rebuild replaces the previous failures.
    """
    kept, models, skipped = [], [], []
    for index, name in enumerate(names[1:], start=1):
        try:
            model = build(name)
        except Exception as exc:  # noqa: BLE001 — a backup must not reject the graph
            reason = type(exc).__name__
            log.warning("[model-roles] %s[%d] %s skipped: %s", role, index, name, reason)
            skipped.append({"index": index, "model": name, "reason": reason})
        else:
            kept.append(name)
            models.append(model)
    with _lock:
        # Copy on write: per-turn model overrides may shallow-copy the config.
        config._model_role_skips = {
            **getattr(config, "_model_role_skips", {}),
            role: (tuple(names), skipped),
        }
    return kept, models


def _answered(role, model, failures):
    with _lock:
        if not failures:
            _streaks.discard(role)
            return
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "from": [name for name, _ in failures],
            "to": model,
            "reason": "; ".join(f"{name}: {reason}" for name, reason in failures),
        }
        _last_fallbacks[role] = record
        if role not in _streaks:
            log.warning("Model role %s answered from %s after %s", role, model, record["reason"])
            _streaks.add(role)


class _RetryableError(Exception):
    def __init__(self, original):
        super().__init__(type(original).__name__)
        self.original = original


class _Guarded(Runnable):
    """Preserve invoke/stream semantics while observing one complete operation.

    Runnable's default batch/abatch dispatch through invoke/ainvoke, giving each
    input its own failure list even when batches execute concurrently.
    """

    def invoke(self, input, config=None, **kwargs):
        with self._operation() as runnable:
            return runnable.invoke(input, config, **kwargs)

    async def ainvoke(self, input, config=None, **kwargs):
        with self._operation() as runnable:
            return await runnable.ainvoke(input, config, **kwargs)

    def stream(self, input, config=None, **kwargs):
        with self._operation() as runnable:
            yield from runnable.stream(input, config, **kwargs)

    async def astream(self, input, config=None, **kwargs):
        with self._operation() as runnable:
            async for chunk in runnable.astream(input, config, **kwargs):
                yield chunk


class _Attempt(_Guarded):
    """One built chain entry, observing retryable invocation failures."""

    def __init__(self, role, index, name, resolve, failures):
        self.role, self.index, self.name, self._resolve, self.failures = (
            role, index, name, resolve, failures,
        )

    @contextmanager
    def _operation(self):
        model = self._resolve(self.index)
        try:
            yield model
        except Exception as exc:
            if not retryable_error(exc):
                raise
            status = getattr(exc, "status_code", None)
            reason = type(exc).__name__ + (f" ({status})" if isinstance(status, int) else "")
            self.failures.append((self.name, reason))
            raise _RetryableError(exc) from exc
        else:
            _answered(self.role, self.name, self.failures)


class _RoleLLM(_Guarded):
    def __init__(self, role, names, models):
        self.role, self.names, self.primary = role, names, models[0]
        self._model = models.__getitem__

    @contextmanager
    def _operation(self):
        # Invocation-local state also isolates nested calls and interleaved streams.
        failures = []
        attempts = [
            _Attempt(self.role, i, name, self._model, failures)
            for i, name in enumerate(self.names)
        ]
        chain = attempts[0].with_fallbacks(attempts[1:], exceptions_to_handle=(_RetryableError,))
        try:
            yield chain
        except _RetryableError as exc:
            # Exhaustion preserves the provider error contract for existing callers.
            raise exc.original from None


def create_role_llm(config, role: str, **kwargs):
    """Build the resolved chain, falling back only on transient provider failures.

    A single entry returns create_llm's model unchanged. Empty/disabled roles
    raise ValueError instead of silently enabling them using the main model.
    Multi-model results expose the Runnable invocation, batch and stream API.
    A streak ends when the primary successfully completes an operation.

    An unbuildable fallback is skipped with a redacted diagnostic. The primary's
    build error still propagates, and invocation retries remain transient-only.
    """
    names = role_chain(config, role)
    if not names:
        raise ValueError(f"model role {role!r} has no configured models")
    primary = create_llm(config, model_name=names[0], **kwargs)

    def _build(name):
        return create_llm(config, model_name=name, **kwargs)

    kept, fallbacks = build_fallbacks(config, role, names, _build)
    if len(names) == 1:
        return primary
    return _RoleLLM(role, [names[0], *kept], [primary, *fallbacks])
