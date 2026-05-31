"""In-process server→client event bus (ADR 0003)."""

from events.bus import EventBus

# The single durable "Activity" thread (ADR 0003). Reactive producers (the
# scheduler, the inbox) route their turns into this A2A context, which maps to
# checkpointer thread ``a2a:system:activity`` — so the conversation persists and
# can be opened/continued in the console's Activity surface.
ACTIVITY_CONTEXT = "system:activity"

__all__ = ["EventBus", "ACTIVITY_CONTEXT"]
