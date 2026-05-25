"""Pluggable scheduler for future-task delivery.

Two backends ship by default:

- ``LocalScheduler`` — sqlite + asyncio. Bundled, zero external
  dependencies, per-agent persistence path. Use this for solo forks
  or any deployment that doesn't already run protoWorkstacean.
- ``WorkstaceanScheduler`` — HTTP adapter to a protoWorkstacean
  install. Topic-namespaced per agent so multiple ginas can share one
  Workstacean and not collide.

``server.py`` selects the backend at startup based on env vars; the
agent loop sees the same three tools (``schedule_task``,
``list_schedules``, ``cancel_schedule``) regardless of which backend
is wired up.

Multi-agent safety: every job carries an ``agent_name`` (defaulted
from ``AGENT_NAME`` env / config) so that two protoAgent instances
sharing one storage path or one Workstacean install can't accidentally
fire each other's scheduled prompts.
"""

from scheduler.interface import Job, SchedulerBackend
from scheduler.local import LocalScheduler
from scheduler.workstacean import WorkstaceanScheduler

__all__ = ["Job", "LocalScheduler", "SchedulerBackend", "WorkstaceanScheduler"]
