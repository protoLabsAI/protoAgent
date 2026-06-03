"""Optional native Discord surface (ADR 0015).

Off unless ``DISCORD_BOT_TOKEN`` is set. The inbound gateway listener
(``gateway.start_in_background``) is the native half; the stateless outbound
tools live in ``tools/discord_tools.py``.
"""

from surfaces.discord.gateway import start_in_background, stop

__all__ = ["start_in_background", "stop"]
