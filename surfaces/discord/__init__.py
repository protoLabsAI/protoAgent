"""Optional native Discord surface (ADR 0015 + 0016).

Off unless a bot token is set — via the in-app config (Settings / setup wizard,
ADR 0016) or the ``DISCORD_BOT_TOKEN`` env var (Docker fallback). The inbound
gateway listener (``gateway.start_in_background``) is the native half; the
stateless outbound tools live in ``tools/discord_tools.py``. ``configure`` injects
the UI config; ``validate_token`` powers the "Test connection" probe.
"""

from surfaces.discord.gateway import configure, start_in_background, stop, validate_token

__all__ = ["configure", "start_in_background", "stop", "validate_token"]
