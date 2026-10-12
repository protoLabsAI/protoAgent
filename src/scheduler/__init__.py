"""Scheduler for future-task delivery.

``LocalScheduler`` — sqlite + asyncio. Bundled, zero external dependencies,
per-agent persistence path. It's the only backend; ``server.py`` wires it at
startup (or none at all when scheduling is disabled), and the agent loop sees the
same three tools (``schedule_task``, ``list_schedules``, ``cancel_schedule``).

Multi-agent safety: every job carries an ``agent_name`` (defaulted from
``AGENT_NAME`` env / config) so two protoAgent instances sharing one storage path
can't accidentally fire each other's scheduled prompts.
"""

from scheduler.interface import Job, SchedulerBackend
from scheduler.local import LocalScheduler

__all__ = ["Job", "LocalScheduler", "SchedulerBackend"]
