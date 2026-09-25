"""Engineer — the navigator skill pack behind the Engineer archetype.

Prompt-only plugin: two agent-retrievable skills, ``repo-onboard`` and
``debug-loop``, that keep a coding agent in the NAVIGATOR seat — the operator
drives and authors every change; the agent orients, reproduces, points at
evidence, and reviews. No tools, routes, surfaces, or config — the skills are
the product. Off by default; the engineer-archetype bundle enables it.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.engineer")


def register(registry) -> None:
    """Entry point — the bundled navigator skills."""
    try:
        registry.register_skill_dir("skills")
    except Exception:
        log.exception("[engineer] failed to register skill dir")
