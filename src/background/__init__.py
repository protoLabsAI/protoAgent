"""Background subagents (ADR 0050) — detached A2A-turn delegations with
reactive, exactly-once completion notifications drained back to the spawning
chat session."""

from background.manager import BackgroundManager
from background.store import BackgroundJob, BackgroundStore

__all__ = ["BackgroundManager", "BackgroundStore", "BackgroundJob"]
