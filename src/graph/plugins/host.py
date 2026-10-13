"""Host services a plugin surface/route can reach (ADR 0018+).

A plugin's *tools* run inside the graph, but a *surface* (an ingress gateway like
Discord) needs to call the agent and the event bus — host services it can't
construct itself. The server **populates** these once, before the startup hook
fires; a plugin reads them at surface-start time, via ``registry.host`` (the same
singleton) or ``from graph.plugins.host import HOST``.

Each is optional (``None`` until the server wires it / in a non-server context),
so a plugin guards: ``if registry.host.invoke: ...``.

This module also carries the plugin lifecycle timing helper (#2675) — a host-side
facility the plugin machinery shares: it lives here rather than in ``loader.py``
because ``pconfig.py`` needs it too and only imports the loader lazily (to stay a
pure-data path), while this module imports nothing from ``graph.plugins``.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("protoagent.plugins")


@dataclass
class PluginHost:
    # async (prompt, session_id, *, tool_fence=None) -> str — invoke the agent as
    # a chat surface (one conversation per session_id, the LangGraph thread key).
    # ``tool_fence`` (#2972, optional keyword) is a per-turn tool allowlist for a
    # turn that originated with an untrusted party — e.g. a surface relaying
    # another operator's agent; the host blocks tool calls outside it. Surfaces
    # that need it should feature-detect (``inspect.signature``) and refuse to
    # run unfenced on an older host.
    invoke: Optional[Callable[..., Awaitable[str]]] = None
    # async (name, prompt, conversation_key=None, *, permissions="readonly") -> str —
    # invoke a configured delegate without reaching into its plugin package. The
    # default host-enforced ceiling makes an ordinary plugin invocation read-only.
    invoke_delegate: Optional[Callable[..., Awaitable[str]]] = None
    # (event: str, data: dict) -> None — publish to the server→client event bus.
    publish: Optional[Callable[[str, dict], Any]] = None
    # () -> subscription — subscribe to the event bus (e.g. return-address delivery).
    subscribe: Optional[Callable[[], Any]] = None
    # (topic, handler) -> unsubscribe — register an in-process handler for bus topics
    # (ADR 0039). Lets a server-side plugin react to events (topic-filtered, sync/async)
    # without ever importing the plugin that emitted them.
    on: Optional[Callable[[str, Callable], Any]] = None
    # () -> LangGraphConfig — the *live* server config. A route handler reads this
    # for the current resolved values (incl. plugin_config) rather than closing
    # over a load-time snapshot, so it sees Settings changes without a restart.
    config: Optional[Callable[[], Any]] = None
    # (patch: dict) -> (ok, messages) — persist a nested config patch to YAML
    # (secrets routed automatically) and reload the graph once. Heavy (a full
    # reload) — a route should call it via ``asyncio.to_thread``. Lets a plugin
    # route apply config + reload (e.g. an OAuth Connect flow flipping enabled).
    apply_settings: Optional[Callable[[dict], Any]] = None


# Process-lifetime singleton. The server fills it in; plugins read it.
HOST = PluginHost()


# ── Plugin lifecycle timing (#2675, instrumentation point #3 of #2245) ────────

# A lifecycle stage crossing this logs at WARNING — a slow plugin drags every
# boot/reload, and the histogram alone doesn't name the culprit in the boot log.
SLOW_LIFECYCLE_THRESHOLD_S = 0.5


@contextmanager
def timed_lifecycle_phase(plugin_id: str, phase: str):
    """Time one plugin lifecycle stage (load / config / registration) — the same
    ``time.monotonic()`` idiom as ``AuditMiddleware``'s tool-call timing
    (``graph/middleware/audit.py``) and ``_timed_boot_phase`` (#2674), applied per
    plugin. Emits to the ``*_plugin_lifecycle_seconds{plugin,phase}`` histogram
    (no-op when metrics are disabled) and logs a >500ms outlier at WARNING.
    Records in ``finally`` so a stage that raises — exactly the plugin worth
    diagnosing — is still timed. Overhead is two clock reads and a label lookup
    per stage, so wrapping every stage adds nothing measurable to boot."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        duration_s = time.monotonic() - t0
        from observability import metrics

        metrics.record_plugin_lifecycle(plugin_id, phase, duration_s)
        if duration_s > SLOW_LIFECYCLE_THRESHOLD_S:
            log.warning(
                "[plugins] %s: %s stage took %.2fs (> %.1fs) — this plugin is slowing boot/reload",
                plugin_id,
                phase,
                duration_s,
                SLOW_LIFECYCLE_THRESHOLD_S,
            )
