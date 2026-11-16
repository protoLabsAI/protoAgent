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


def _try_build(build, name: str) -> tuple[object | None, str | None]:
    """Build one chain entry: ``(model, None)`` on success, ``(None, reason)`` on failure.

    The reason is the exception's class name alone — never its message — so a redacted
    diagnostic can never leak a provider payload (an api_key echoed in an error string).
    """
    try:
        return build(name), None
    except Exception as exc:  # noqa: BLE001 — a backup that can't build is diagnosable, not fatal
        return None, type(exc).__name__


def skipped_entries(config, names: list[str], build=None, *, primary_name: str | None = None) -> list[dict]:
    """Which tail entries of this resolved chain cannot be built against ``config`` right now.

    A pure, read-only probe for the model-role status surface (ADR 0133 M6): it rebuilds each
    fallback and reports the ones that raise, mirroring the drops :func:`build_fallbacks` makes
    at graph rebuild. The primary is never a skip — its build failure is fatal, so a
    running graph already has a buildable primary. It mutates no config and logs nothing, so it
    is correct even for a role that NO production call ever builds — ``aux`` and the blank
    aux-family roles that inherit its chain — and a repeated GET neither records diagnostics nor
    re-emits the build-time warnings. ``build`` is injectable so one status pass can memoize
    construction across the many roles that resolve to the same inherited chain; it defaults to
    ``create_llm`` for this config. ``primary_name`` has the same meaning as in
    :func:`build_fallbacks`: a filtered-out lead primary leaves index 0 as a fallback.
    """
    builder = build or (lambda name: create_llm(config, model_name=name))
    skipped: list[dict] = []
    start = 1 if primary_name is None or names[:1] == [primary_name] else 0
    for index, name in enumerate(names[start:], start=start):
        _, reason = _try_build(builder, name)
        if reason is not None:
            skipped.append({"index": index, "model": name, "reason": reason})
    return skipped


def build_fallbacks(role: str, names: list[str], build, *, primary_name: str | None = None):
    """Build a chain's tail, dropping entries that cannot be built (ADR 0133 D6).

    The caller builds the primary OUTSIDE this guard, so a primary build failure stays fatal.
    A fallback that raises is logged ``[model-roles] <role>[<i>] <model> skipped: <reason>``
    (reason redacted to the exception type) and dropped; the surviving models keep their order
    and the returned names stay index-aligned with them. The read-only status surface reports
    the same drops via :func:`skipped_entries`, which re-probes the live config rather than
    reading any state recorded here — so a role never built in production still reports them.

    Pass ``primary_name`` when the primary is built separately from the resolved chain.
    If coherence filtering removed it, every surviving entry is a fallback, including index 0.
    """
    kept, models = [], []
    start = 1 if primary_name is None or names[:1] == [primary_name] else 0
    for index, name in enumerate(names[start:], start=start):
        model, reason = _try_build(build, name)
        if reason is None:
            kept.append(name)
            models.append(model)
        else:
            log.warning("[model-roles] %s[%d] %s skipped: %s", role, index, name, reason)
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

    kept, fallbacks = build_fallbacks(role, names, _build)
    if len(names) == 1:
        return primary
    return _RoleLLM(role, [names[0], *kept], [primary, *fallbacks])
