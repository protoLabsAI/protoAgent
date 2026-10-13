"""Process-wide runtime state (ADR 0023)."""

from runtime.state import STATE, AppState, get_state

__all__ = ["STATE", "AppState", "get_state"]
