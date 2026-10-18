"""Technical Writer — opt-in documentation skills, with no tools or routes."""

from __future__ import annotations


def register(registry) -> None:
    """Expose the documentation workflow through the host's skill loader."""
    registry.register_skill_dir("skills")
