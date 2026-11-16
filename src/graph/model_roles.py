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
    """One chain entry. Its model is resolved lazily (``resolve(index)``) so a fallback that
    cannot even be built — a keyless connection, a bad alias — is constructed only if the
    chain actually reaches it. A healthy primary is never blocked by an unbuildable backup
    (ADR 0133 M4)."""

    def __init__(self, role, index, name, resolve, failures):
        self.role, self.index, self.name, self._resolve, self.failures = (
            role, index, name, resolve, failures,
        )

    @contextmanager
    def _operation(self):
        try:
            model = self._resolve(self.index)
        except Exception as exc:  # noqa: BLE001 — an unbuildable entry fails like a transient
            # Only an earlier entry's failure gets us here, so treat the build error as this
            # attempt failing: the chain falls through to the next entry, and on exhaustion
            # the first (primary) entry's original provider error is what surfaces — the same
            # contract as an invoke-time failure below.
            self.failures.append((self.name, f"unbuildable: {type(exc).__name__}"))
            raise _RetryableError(exc) from exc
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
    def __init__(self, role, names, primary, build):
        self.role, self.names, self.primary, self._build = role, names, primary, build
        # index -> built model. The primary is built eagerly by create_role_llm; fallbacks
        # are built on first reach and cached so a degraded path doesn't rebuild per call.
        self._cache = {0: primary}
        self._cache_lock = Lock()

    def _model(self, index):
        model = self._cache.get(index)
        if model is not None:
            return model
        with self._cache_lock:
            if index not in self._cache:
                self._cache[index] = self._build(self.names[index])
            return self._cache[index]

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

    Only the PRIMARY is built eagerly; each fallback is constructed lazily the first time
    the chain reaches it, so a fallback that cannot be built (e.g. a keyless connection)
    never makes a role with a healthy primary raise — matching how goal eval, memory
    selection and image description built only the primary before ADR 0133.
    """
    names = role_chain(config, role)
    if not names:
        raise ValueError(f"model role {role!r} has no configured models")
    primary = create_llm(config, model_name=names[0], **kwargs)
    if len(names) == 1:
        return primary

    def _build(name):
        return create_llm(config, model_name=name, **kwargs)

    return _RoleLLM(role, names, primary, _build)
