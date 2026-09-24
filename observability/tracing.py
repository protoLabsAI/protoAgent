"""Langfuse tracing for protoAgent.

Primary entry point is ``trace_session`` — an async context manager that
opens a Langfuse observation and makes it the current parent for any
observations created inside its scope (including tool calls, LLM calls
from the LiteLLM gateway, and nested subagent sessions).

Hierarchy
─────────
A typical A2A-submitted task produces a trace like::

    a2a-stream (session root)
    ├── agent-turn (LangGraph run)
    │   ├── tool:<my_tool_a>
    │   ├── tool:<my_tool_b>
    │   └── litellm-acompletion            ← from the gateway callback
    └── subagent:worker                    ← if a subagent is dispatched
        ├── tool:<my_tool_c>
        └── litellm-acompletion

Every span shares the same Langfuse trace_id, so clicking one surfaces the
whole run. The trace_id is also stamped onto every audit-log entry via
``current_trace_id()`` so operators can cross-reference a JSONL line to
the Langfuse UI without grep-archaeology.

The OTel "Failed to detach context" filter below handles a real bug that
appears any time an SSE consumer closes the stream early (e.g. an A2A
client breaks out of the `for await` loop after capturing the initial
task event). Keep it.

Configuration
─────────────
``init`` takes the credentials from the environment (``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST``) and falls back to the config's
``tracing.{enabled,host,public_key,secret_key}`` — the env layer WINS, so a
container deploy is untouched, while a desktop-launched fleet member (which has
no ``LANGFUSE_*`` in its environment and so could not enable tracing at all
before #3017) can be configured from Settings ▸ Tracing. The two keys are
declared secrets, stored in ``secrets.yaml`` and never in the tracked YAML.
The host travels with the keys — whichever layer supplies the pair supplies the
destination, so config can't redirect deployment-owned credentials (#3039).

Graceful degrade
────────────────
When Langfuse isn't configured (or its client errors), every helper in
this module is a no-op. The agent continues; tracing just doesn't land.
``is_enabled()`` is the honest read of that state — the telemetry API reports
it so the console can say "tracing is off" instead of rendering an empty trace
column that reads as "this turn wasn't traced".
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
from typing import Any, AsyncIterator, Iterator

_langfuse = None
_enabled = False

# Where Langfuse is assumed to live when neither the environment nor the config
# names a host — the bundled compose service. Kept as the default so an env-only
# deploy that exports only the key pair connects exactly where it did before the
# config fallback existed (#3017).
_DEFAULT_HOST = "http://host.docker.internal:3001"

# W3C/OTel id shapes (what Langfuse v3+/v4 uses on the wire). A caller's ids
# must match these to be JOINable — anything else falls back to a fresh trace
# rather than crashing the turn or poisoning the SDK with a bogus context.
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


# Silence the harmless "Failed to detach context" error emitted by
# OpenTelemetry when Langfuse's span context manager exits in a
# different async context than where it was attached. OTel logs this
# as `_logger.error("Failed to detach context", exc_info=True)` —
# the actual "was created in a different Context" string lives only
# in `exc_info`, NOT in the formatted message. Match on the message
# text instead. The detach-context call site only fires this line
# for the cross-context case in practice, so the filter is precise
# enough to leave other OTel errors alone.
logging.getLogger("opentelemetry.context").addFilter(
    lambda record: "Failed to detach context" not in record.getMessage()
)

# Holds the current Langfuse trace_id for the active async context. Audit
# logging + error handlers read this to cross-reference records back to
# the trace that produced them.
_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_protoagent_trace_id",
    default="",
)

# Holds the A2A/chat session_id so middleware (AuditMiddleware) and
# audit logging can stamp it without needing access to graph state.
_session_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_protoagent_session_id",
    default="",
)

# The ``trace_session`` root span of the active turn, so the answer can be written back
# onto it (and onto the trace) from the model-call seam — the only place that sees the
# final reply on every return path of the chat/A2A drivers.
_session_span_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "_protoagent_session_span",
    default=None,
)

# How many ``trace_span`` boundaries (subagent:, a2a:, acp:) enclose the current code.
# 0 = the turn itself. A subagent's closing answer is not the turn's answer.
_span_depth_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_protoagent_span_depth",
    default=0,
)

# Caps on the text that goes into a generation's input/output. A lead agent's system
# prompt is ~50k tokens and the whole history rides EVERY call of a turn; uncapped, one
# 30-call turn ships tens of megabytes. Per message, then per call (system prompt + the
# NEWEST messages that fit; older ones are counted, not sent). The exact prompt is kept
# in full by prompt capture (``prompts.capture``) — the trace needs enough to read the
# exchange.
MAX_IO_CHARS = 8000
MAX_IO_CALL_CHARS = 32000

# True inside an incognito turn (ADR 0069 D3b): spans still record structure, timing,
# usage and cost, but NO content — the same line prompt capture and trace export draw.
_io_suppressed_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_protoagent_io_suppressed",
    default=False,
)

# Holds the request's ALREADY-classified trust tier (a2a_impl.auth sets this from
# request.state.trust_tier) so the structured request telemetry can carry it as a
# bounded, non-secret dimension. Default "" = unclassified — the dimension is then
# omitted so an unclassified request keeps its prior telemetry shape.
_trust_tier_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_protoagent_trust_tier",
    default="",
)

#: The only trust tiers ever surfaced in telemetry (#1504). A closed vocabulary keeps
#: the dimension bounded and guarantees a credential value can never ride into a span
#: even if a caller hands ``set_trust_tier`` something unexpected — anything outside
#: this set collapses to "" (unclassified).
_TELEMETRY_TRUST_TIERS = frozenset({"operator", "federation"})


def resolve_credentials(config: Any = None) -> tuple[str, str, str, str]:
    """The Langfuse credentials to connect with: ``(public_key, secret_key, host, source)``.

    **The environment wins** (#3017). A container / systemd deploy exports
    ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` and has no config file to
    consult, so a complete env pair is used exactly as it was before this seam
    existed — including the case where ``tracing.enabled`` is off, which is a
    fallback toggle and not a kill switch for an env-configured deploy.

    Config is the FALLBACK, and it is the only path that reaches the shape the
    fleet actually deploys: a desktop-app member (``protoagent-server --port …
    --ui none``) is spawned with no ``LANGFUSE_*`` in its environment, so before
    #3017 tracing could not be turned on there at all. The config path needs both
    ``tracing.enabled`` AND a full key pair — an enabled toggle with no keys is
    the operator having started but not finished, not a reason to connect.

    ENV-supplied keys never follow a config host (#3039). #3017 resolved the host as one
    ``env_host or cfg_host or _DEFAULT_HOST`` chain regardless of which layer answered for
    the keys, which let config data choose the destination for DEPLOYMENT-owned credentials:
    ``docker-compose.yml`` passes ``LANGFUSE_HOST=${LANGFUSE_HOST:-}``, so an operator who
    exports only the key pair has an empty env host, ``cfg_host`` won the chain, and the
    deployment's keys left the process as a Basic auth header aimed wherever ``tracing.host``
    said. ``tracing.host`` is not a secret field, and config reaches an instance through more
    paths than deployment env does (snapshot import, a fork's committed config, any write to
    ``langgraph-config.yaml``), so env keys resolve to ``LANGFUSE_HOST``/``LANGFUSE_URL`` or
    ``_DEFAULT_HOST`` and stop there.

    The block is DIRECTIONAL, not symmetric. Config keys still fall back to ``env_host``
    (``cfg_host or env_host or _DEFAULT_HOST``): env is the more trusted layer — only whoever
    starts the process sets it — so an env host aiming config-owned keys was never the hole,
    and host-in-env + keys-in-Settings is a shape this repo tells operators to use. Compose
    persists ``/sandbox/config`` and the example YAML says the two keys are secrets that
    belong in Settings, and every fleet member inherits the hub's ``LANGFUSE_HOST`` through
    ``{**os.environ, **env}`` while its keys come from its own per-agent Settings — the #3017
    target shape. Refusing that fallback would send those members to ``_DEFAULT_HOST``, an
    address that resolves only inside compose, and take tracing dark to fix nothing.

    A host that loses is NAMED rather than dropped (#3039). Both discards happen on paths
    that worked before, so the operator's only other signal is a Trace column that quietly
    stops filling — the exact silence #3017 exists to remove.

    ``source`` is ``"env"``, ``"config"``, or ``""`` (nothing usable); it only
    feeds the boot log line so an operator can see which layer answered.
    """
    env_public = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    env_secret = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    env_host = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_URL") or "").strip()

    # getattr, not attribute access: callers pass a LangGraphConfig, but this module
    # sits below graph/ in the import layering and must not depend on that type.
    cfg_public = str(getattr(config, "tracing_public_key", "") or "").strip()
    cfg_secret = str(getattr(config, "tracing_secret_key", "") or "").strip()
    cfg_host = str(getattr(config, "tracing_host", "") or "").strip()

    if env_public and env_secret:
        host = env_host or _DEFAULT_HOST
        if cfg_host and cfg_host != host:
            print(
                f"[tracing] ignoring tracing.host={cfg_host} : the Langfuse key pair came from the "
                f"environment, so the host does too (#3039). Sending to {host} — set LANGFUSE_HOST to change it."
            )
        return env_public, env_secret, host, "env"
    if getattr(config, "tracing_enabled", False) and cfg_public and cfg_secret:
        host = cfg_host or env_host or _DEFAULT_HOST
        if env_host and env_host != host:
            print(
                f"[tracing] ignoring LANGFUSE_HOST={env_host} : the Langfuse key pair came from "
                f"config, so tracing.host wins (#3039). Sending to {host}."
            )
        return cfg_public, cfg_secret, host, "config"
    return "", "", "", ""


def init(config: Any = None) -> None:
    """Connect to Langfuse from the environment, falling back to ``config``.

    ``config`` is the live ``LangGraphConfig`` (``tracing.{enabled,host,public_key,
    secret_key}``); omit it and this is the env-only behavior that predates #3017.
    See ``resolve_credentials`` for the precedence.

    Idempotent, and re-entrant on purpose: once connected this returns immediately
    rather than rebuilding the client, so a later config-aware call can't replace a
    live client (or double-register the SDK's OTel provider) behind a running turn.
    """
    global _langfuse, _enabled

    if _enabled:
        return

    public_key, secret_key, host, source = resolve_credentials(config)

    if not public_key or not secret_key:
        # Name the half-finished case rather than folding it into "not configured"
        # (#3017). An operator who flipped tracing.enabled in Settings and saved
        # without both keys has done the visible half of the work; telling them
        # "Langfuse not configured" reads as "your toggle didn't take". This whole
        # issue is about something being off with nothing saying so — don't add a
        # new instance of it one layer down.
        if getattr(config, "tracing_enabled", False):
            print("[tracing] tracing.enabled is on but the Langfuse key pair is incomplete. Tracing disabled.")
        else:
            print("[tracing] Langfuse not configured. Tracing disabled.")
        return

    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        _enabled = True
        print(f"[tracing] Langfuse initialized from {source} -> {host}")
    except ImportError:
        print("[tracing] langfuse not installed. Tracing disabled.")
    except Exception as e:
        print(f"[tracing] Langfuse init failed: {e}. Tracing disabled.")


def is_enabled() -> bool:
    return _enabled


def current_trace_id() -> str:
    """Return the trace_id of the currently-active session (or empty)."""
    return _trace_id_ctx.get()


def current_session_id() -> str:
    """Return the session_id of the currently-active session (or empty)."""
    return _session_id_ctx.get()


def current_trust_tier() -> str:
    """The classified trust tier of the active request — ``operator``/``federation`` — or
    empty when the request was never classified (#1504).

    A bounded, non-secret dimension: it is one of two fixed labels, never a credential.
    """
    return _trust_tier_ctx.get()


def set_trust_tier(tier: str | None) -> contextvars.Token[str]:
    """Record the request's already-classified trust tier for structured telemetry (#1504).

    Fed the label ``a2a_impl.auth`` derived from the matched credential (via
    ``request.state.trust_tier``) — never any credential material itself. Only the two known
    labels are stored; every other value, including ``None`` and any unclassified request,
    collapses to ``""`` so an unexpected string can never become a telemetry dimension and an
    unclassified request keeps its prior (dimension-absent) behavior.
    """
    return _trust_tier_ctx.set(tier if tier in _TELEMETRY_TRUST_TIERS else "")


def reset_trust_tier(token: contextvars.Token[str] | None) -> None:
    """Reset the request trust-tier context after the authenticated request exits."""
    if token is None:
        return
    try:
        _trust_tier_ctx.reset(token)
    except ValueError:
        pass


def current_trace_context() -> dict | None:
    """The active trace context as ``{"trace_id": ..., "span_id": ...}``, or None.

    Used to PROPAGATE the trace across process boundaries — outbound A2A
    dispatches attach it as ``a2a.trace`` metadata (the receiver's
    ``trace_session`` joins it via ``trace_context``), and the gateway
    trace-join middleware stamps it onto LLM request metadata so LiteLLM's
    Langfuse callback lands generations in the SAME trace.

    ``span_id`` is the current observation (present only when the SDK exposes
    one); ``trace_id`` alone is still a valid, joinable context. Returns None
    when tracing is disabled or no trace is active — callers skip propagation.
    """
    if not _enabled or _langfuse is None:
        return None
    trace_id = ""
    try:
        trace_id = _langfuse.get_current_trace_id() or ""
    except Exception:  # noqa: BLE001 — propagation is best-effort
        pass
    trace_id = trace_id or _trace_id_ctx.get()
    if not trace_id:
        return None
    ctx: dict = {"trace_id": trace_id}
    try:
        span_id = _langfuse.get_current_observation_id()
        if span_id:
            ctx["span_id"] = span_id
    except Exception:  # noqa: BLE001
        pass
    return ctx


def _caller_trace_context(metadata: dict | None) -> dict | None:
    """Build a Langfuse ``trace_context`` from ``caller_trace_id`` /
    ``caller_span_id`` metadata (the ids an A2A caller sent as ``a2a.trace``).

    Malformed ids → None (fresh trace) — never let a bad caller id crash a
    turn or feed the SDK an invalid W3C context."""
    if not metadata:
        return None
    trace_id = str(metadata.get("caller_trace_id") or "").strip().lower()
    if not _TRACE_ID_RE.match(trace_id):
        return None
    ctx: dict = {"trace_id": trace_id}
    span_id = str(metadata.get("caller_span_id") or "").strip().lower()
    if _SPAN_ID_RE.match(span_id):
        ctx["parent_span_id"] = span_id
    return ctx


@contextlib.asynccontextmanager
async def trace_session(
    session_id: str,
    name: str = "agent-session",
    metadata: dict | None = None,
    input: Any = None,
    incognito: bool = False,
) -> AsyncIterator[Any]:
    """Open a session-level Langfuse observation that child observations nest under.

    Any ``_langfuse.start_observation(...)`` call (including those made by
    ``trace_tool_call`` below) becomes a child of this span for the duration
    of the ``async with`` block.

    The block always runs — if Langfuse isn't configured or raises on setup,
    the manager yields None and proceeds. Never let tracing failures cascade
    into the agent's execution path.

    Usage::

        async with tracing.trace_session(session_id, name="a2a-stream",
                                         metadata={"task_id": tid}):
            ... # LangGraph run, tool calls, subagent dispatches
            ... # all land as children of this span

    ``session_id`` is threaded into both the metadata and the Langfuse
    contextvar so audit records created inside the scope can be cross-
    referenced to the trace.

    Fleet tracing: when ``metadata`` carries ``caller_trace_id`` (and
    optionally ``caller_span_id``) — the ids an upstream agent sent as
    ``a2a.trace`` — the session span JOINS that trace via Langfuse's
    ``trace_context`` instead of opening a fresh one, so a hub→member
    delegation renders as ONE distributed trace. The ids are still stamped
    into the span metadata; malformed ids degrade to a fresh trace.

    ``input`` (the user's message) becomes the root span's input — which Langfuse
    shows as the trace's — and the turn's answer is written back as its output by
    ``set_session_output``. The session id and agent tag are propagated as real trace
    attributes, so Langfuse's Sessions view groups a conversation's turns.

    ``incognito`` suppresses ALL content for the scope — the input here, the answer,
    and every generation's messages (see ``io_allowed``) — while the structure,
    timing, usage and cost still land.
    """
    # Always set session_id so AuditMiddleware can read it even when
    # Langfuse is disabled.
    sid_token = _session_id_ctx.set(session_id)
    io_token = _io_suppressed_ctx.set(bool(incognito) or _io_suppressed_ctx.get())

    if not _enabled or _langfuse is None:
        try:
            yield None
        finally:
            # reset can raise if the generator is torn down in a different
            # context than the one that set the token (e.g. an SSE client
            # disconnects mid-stream and the async generator is closed by a
            # different task). The contextvar resets itself on context exit,
            # so swallowing here is safe.
            for var, tok in ((_session_id_ctx, sid_token), (_io_suppressed_ctx, io_token)):
                try:
                    var.reset(tok)
                except ValueError:
                    pass
        return

    ctx = None
    token = None
    span_token = None
    attrs = None
    try:
        trace_context = _caller_trace_context(metadata)
        # Surface the request's classified trust tier as a bounded, non-secret dimension
        # (#1504). The value comes only from the auth middleware's contextvar, not caller
        # metadata; absent (unclassified) → the key is omitted, so prior telemetry is
        # unchanged.
        tier = _trust_tier_ctx.get()
        tier_meta = {"trust_tier": tier} if tier else {}
        safe_metadata = {k: v for k, v in (metadata or {}).items() if k != "trust_tier"}
        ctx = _langfuse.start_as_current_observation(
            trace_context=trace_context,
            name=name,
            metadata={
                **safe_metadata,
                **tier_meta,
                "session_id": session_id,
                "tags": [os.environ.get("AGENT_NAME", "protoagent")],
            },
        )
        span = ctx.__enter__()
        try:
            from langfuse import propagate_attributes

            attrs = propagate_attributes(
                session_id=(session_id or None) and str(session_id)[:200],
                tags=[os.environ.get("AGENT_NAME", "protoagent")],
            )
            attrs.__enter__()
        except Exception:  # noqa: BLE001 — older SDK: metadata still carries both
            attrs = None
        if input is not None and io_allowed():
            try:
                span.update(input=input)
            except Exception:  # noqa: BLE001
                pass
        span_token = _session_span_ctx.set(span)
        # Joined session: the span reports the CALLER's trace id — that's what
        # audit records and downstream propagation must carry.
        trace_id = (
            getattr(span, "trace_id", "")
            or (trace_context or {}).get("trace_id", "")
            or getattr(span, "id", "")
        )
        token = _trace_id_ctx.set(trace_id)
        yield span
    except Exception as e:
        print(f"[tracing] trace_session({name}) error: {e}")
        yield None
    finally:
        try:
            _session_id_ctx.reset(sid_token)
        except Exception:
            pass
        try:
            _io_suppressed_ctx.reset(io_token)
        except Exception:
            pass
        if token is not None:
            try:
                _trace_id_ctx.reset(token)
            except Exception:
                pass
        if span_token is not None:
            try:
                _session_span_ctx.reset(span_token)
            except Exception:
                pass
        if attrs is not None:
            try:
                attrs.__exit__(None, None, None)
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass


@contextlib.contextmanager
def trace_span(
    name: str,
    metadata: dict | None = None,
    as_type: str = "span",
) -> Iterator[Any]:
    """Open a child observation in the CURRENT trace for the duration of the block.

    Used for boundary spans — e.g. ``subagent:<type>`` around a subagent run so
    the subagent's tool/LLM observations nest under one node instead of
    scattering across the session span. Nests under whatever observation is
    current (the ``trace_session`` root, or an outer ``trace_span``).

    Contract mirrors ``trace_session``: the block ALWAYS runs; when tracing is
    disabled or the SDK errors on setup, it yields None and proceeds. A body
    exception propagates unchanged (the span still closes) — tracing never
    alters control flow.
    """
    if not _enabled or _langfuse is None:
        yield None
        return

    ctx = None
    span = None
    try:
        ctx = _langfuse.start_as_current_observation(
            name=name,
            as_type=as_type,
            metadata=metadata or {},
        )
        span = ctx.__enter__()
    except Exception as e:  # noqa: BLE001 — never fail the wrapped work for tracing
        print(f"[tracing] trace_span({name}) error: {e}")
        ctx = None
    depth_token = _span_depth_ctx.set(_span_depth_ctx.get() + 1)
    try:
        yield span
    finally:
        try:
            _span_depth_ctx.reset(depth_token)
        except ValueError:
            pass
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def trace_tool_call(
    tool_name: str,
    args: dict,
    result: str,
    duration_ms: int,
    success: bool,
    session_id: str = "",
    parent: Any = None,
) -> Any:
    """Log a completed tool execution as a child observation.

    When called inside a ``trace_session`` scope, this nests under the
    session span automatically — Langfuse's internal current-observation
    stack threads the parent without explicit wiring.

    ``parent`` (a span yielded by ``trace_span``) parents it EXPLICITLY, for a
    caller whose events arrive on a task that does not carry the span's context —
    an ACP client's reader loop, which outlives the turn it is reporting on.
    """
    if not _enabled or _langfuse is None:
        return None

    # Truncate oversize args to keep Langfuse payloads lean. The full args
    # are already in the audit log for forensic reconstruction.
    safe_args = {}
    for k, v in (args or {}).items():
        sv = str(v)
        safe_args[k] = sv[:500] if len(sv) > 500 else v

    try:
        span = (parent or _langfuse).start_observation(
            name=f"tool:{tool_name}",
            as_type="tool",
            input=safe_args,
            output=(result or "")[:1000],
            metadata={
                "duration_ms": duration_ms,
                "success": success,
                "session_id": session_id,
                "trace_id": _trace_id_ctx.get(),
            },
            level="ERROR" if not success else "DEFAULT",
        )
        span.end()
        return span
    except Exception:
        return None


def update_span(span: Any, **fields: Any) -> None:
    """Set ``output`` / ``metadata`` / ``level`` / … on a span from ``trace_span``.

    For outcomes only known once the block's work is done. No-op for the ``None``
    a disabled ``trace_span`` yields; swallow-all, like every helper here.
    """
    if span is None:
        return
    try:
        span.update(**fields)
    except Exception:  # noqa: BLE001 — tracing never alters the traced work
        pass


def io_allowed() -> bool:
    """False inside an incognito turn — callers then send no message content."""
    return not _io_suppressed_ctx.get()


def set_session_output(output: Any) -> None:
    """Record ``output`` as the active turn's answer, on its ``trace_session`` root
    span (which Langfuse shows as the trace's output). Called after each final
    (tool-call-free) model reply; the last one wins, which is the turn's answer.
    Ignored inside a ``trace_span`` boundary — a subagent's or coder's closing line is
    that span's business, not the turn's — and in an incognito turn.
    """
    span = _session_span_ctx.get()
    if span is None or _span_depth_ctx.get() > 0 or not io_allowed():
        return
    try:
        span.update(output=output)
    except Exception:  # noqa: BLE001
        pass


def trace_generation(
    name: str,
    model: str = "",
    usage: dict | None = None,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    session_id: str = "",
    input: Any = None,
    output: Any = None,
) -> Any:
    """Log a completed LLM generation as a child observation in the CURRENT trace.

    Why this exists — fleet tracing across TWO Langfuse projects. The LiteLLM
    gateway logs the FULL-fidelity generation (prompt + completion) into its
    OWN project via its success callback; ``TraceContextMiddleware`` joins it
    to the active ``trace_id``. That's ideal when the agent and the gateway
    share a project. But when an agent runs in a DIFFERENT project than the
    gateway — a dedicated fleet project — that generation lands in the
    gateway's project, leaving the agent's own trace with a HOLE where its
    model call should be (only the structural spans remain). This emits the
    generation — model + token usage + cost — into the AGENT's project so its
    trace is whole.

    ``input``/``output`` carry the call's messages and reply, capped per message
    by the caller. Leaving them out on the assumption the gateway had the IO left
    every call BLANK on an agent that doesn't use the gateway (native OAuth
    providers — the flagship PM among them): the prompt and reply were logged
    nowhere at all.

    Mirrors ``trace_tool_call``: ``start_observation`` + ``end`` so it nests
    under the current session span without becoming the parent. No-op /
    swallow-all when tracing is disabled or the SDK errors — never alters the
    turn.
    """
    if not _enabled or _langfuse is None:
        return None
    try:
        usage_details = None
        if usage:
            candidates = {
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "total": usage.get("total_tokens"),
            }
            usage_details = {k: int(v) for k, v in candidates.items() if v} or None
        span = _langfuse.start_observation(
            name=name,
            as_type="generation",
            input=input,
            output=output,
            model=model or None,
            usage_details=usage_details,
            cost_details=({"total": float(cost_usd)} if cost_usd else None),
            metadata={
                "session_id": session_id,
                "trace_id": _trace_id_ctx.get(),
                "duration_ms": duration_ms,
                # Capped per message (MAX_IO_CHARS); the gateway's LiteLLM callback
                # logs the uncapped call into its own project when the call went
                # through it (joined by trace_id).
            },
        )
        span.end()
        return span
    except Exception:
        return None


def score_current_trace(name: str, value: float, comment: str = "") -> None:
    """Attach a numeric score to the currently-active trace.

    Examples:
        - score_current_trace("tool_success_rate", 1.0 if all_succeeded else 0.0)
        - score_current_trace("latency_p95_ms", duration_ms)
        - score_current_trace("verdict", 1.0 if verdict == "PASS" else 0.0,
                              comment="triage outcome")
    """
    if not _enabled or _langfuse is None:
        return
    try:
        _langfuse.score_current_trace(name=name, value=value, comment=comment)
    except Exception:
        pass


def flush() -> None:
    """Flush any buffered observations. Call before process exit."""
    if _enabled and _langfuse is not None:
        try:
            _langfuse.flush()
        except Exception:
            pass
