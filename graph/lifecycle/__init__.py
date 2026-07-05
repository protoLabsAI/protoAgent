"""System lifecycle events (ADR 0074, extends the ADR 0039 event bus).

Emit system-state transitions — ``app.loaded`` (boot finished), ``agent.active``
(idle → active), ``system.wake`` (reserved) — on the event bus, with a plugin hook
seam (``register_lifecycle_hook``) and a config-driven reaction path (``lifecycle_hooks``
in ``langgraph-config.yaml``) so an operator or plugin can react.

- :func:`fire` — the one dispatcher every server emit site calls.
- :func:`set_lifecycle_hooks` / :func:`fire_lifecycle_hook` — the plugin hook seam.
- :func:`should_emit_active` — the pure ``agent.active`` debounce helper.
"""

from graph.lifecycle.dispatch import (
    EVENTS,
    IDLE_THRESHOLD_S,
    TOPICS,
    config_reactions,
    describe,
    fire,
    should_emit_active,
)
from graph.lifecycle.hooks import (
    fire_lifecycle_hook,
    lifecycle_hooks,
    set_lifecycle_hooks,
)

__all__ = [
    "EVENTS",
    "TOPICS",
    "IDLE_THRESHOLD_S",
    "fire",
    "config_reactions",
    "describe",
    "should_emit_active",
    "fire_lifecycle_hook",
    "set_lifecycle_hooks",
    "lifecycle_hooks",
]
