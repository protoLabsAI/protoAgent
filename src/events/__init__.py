"""In-process server→client event bus (ADR 0003)."""

from events.bus import EventBus

# The single durable "Activity" thread (ADR 0003). Since ADR 0129 it is a
# summary/feed surface: reactive producers (the scheduler, the inbox) execute on
# isolated per-job contexts and only their provenance lands here. It still maps
# to checkpointer thread ``a2a:system:activity`` for the legacy history and can
# be opened/continued in the console's Activity surface.
ACTIVITY_CONTEXT = "system:activity"

# Isolated background execution sessions (ADR 0129): every independently fired
# job — a cron, a background wait, a watch reaction, an inbox now-item — runs on
# a durable ``job:<execution-uuid>`` A2A context owned by exactly one job. The
# prefix is the provenance signal consumers route by (never context equality
# with ``ACTIVITY_CONTEXT``).
JOB_CONTEXT_PREFIX = "job:"

__all__ = ["EventBus", "ACTIVITY_CONTEXT", "JOB_CONTEXT_PREFIX"]
