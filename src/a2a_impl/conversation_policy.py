"""Shared context scope and retention window for the From others list."""

from sqlalchemy import and_

# Console chats, jobs, Activity, palette commands and Fleet Room DMs are not
# outside conversations. Private histories and tombstones are filtered separately
# by the list; retention deliberately keeps all contexts eligible by prefix.
CONVERSATION_EXCLUDE_PREFIXES = ("chat-", "job:", "background:", "system:", "palette-")
CONVERSATION_WINDOW_DAYS = 7


def listed_outside_thread(context_id):
    """SQL predicate for contexts eligible for the conversations list by prefix."""
    return and_(*(~context_id.like(f"{prefix}%") for prefix in CONVERSATION_EXCLUDE_PREFIXES))
