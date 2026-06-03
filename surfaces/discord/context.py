"""Context-envelope assembly for Discord turns (ADR 0015).

Wraps the user's incoming message with a ``<recent_conversation>`` block when
prior turns exist, so the model has long-window context across conversation
timeouts and process restarts. Plain text — passed through as the user content;
sections emit only when non-empty (no empty tags, no "no prior conversation"
boilerplate). Ported from ``-deprecated-gina``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from surfaces.discord.turn_log import Turn


def assemble_discord_context(recent_turns: list[Turn], current_message: str) -> str:
    """Build a context envelope. ``recent_turns`` is oldest-first (what
    ``TurnLog.get_recent_turns`` returns); an empty list skips the history block.
    Empty ``current_message`` yields ``""`` (caller decides whether to drop)."""
    if not current_message or not current_message.strip():
        return ""

    parts: list[str] = []
    if recent_turns:
        lines = []
        for t in recent_turns:
            ts_iso = datetime.fromtimestamp(t.ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")
            label = "User" if t.role == "user" else "Assistant"
            content = t.content.replace("\n", " ").strip()
            lines.append(f"[{ts_iso}] {label}: {content}")
        parts.append("<recent_conversation>\n" + "\n".join(lines) + "\n</recent_conversation>")

    parts.append(f"<current_message>\n{current_message}\n</current_message>")
    return "\n\n".join(parts)
