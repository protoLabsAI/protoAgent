"""Google MCP server (stdio) — Gmail + Calendar (Slice 2).

A standalone MCP server the agent launches as a subprocess (registered in
``config/langgraph-config.yaml`` under ``mcp.servers``). Its tools bind as
``google__<tool>``. Off until configured (needs ``credentials.json`` — see
``docs/guides/google.md``).

Run directly for a manual check:  ``python -m mcp_servers.google.server``
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("protoagent.mcp.google")


def _operator_now() -> datetime:
    """Operator-local 'now'. ``GOOGLE_TZ`` (IANA, e.g. ``America/Los_Angeles``)
    sets the timezone for day bounds; defaults to UTC."""
    import os

    tz = os.environ.get("GOOGLE_TZ", "")
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz))
        except Exception:  # noqa: BLE001
            log.warning("[google] bad GOOGLE_TZ %r — using UTC", tz)
    return datetime.now(timezone.utc)


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    from mcp_servers.google import calendar as cal
    from mcp_servers.google import gmail as gm
    from mcp_servers.google.auth import build_services

    gmail_svc, calendar_svc = build_services()
    mcp = FastMCP("google")

    @mcp.tool()
    def gmail_search(query: str = "is:unread newer_than:1d", max_results: int = 10) -> list[dict]:
        """Search Gmail (Gmail query syntax) → id/from/subject/date/snippet/unread."""
        return gm.search_messages(gmail_svc, query, max_results)

    @mcp.tool()
    def gmail_read(message_id: str) -> dict:
        """Read a full message (headers + plain-text body) by id."""
        return gm.get_message(gmail_svc, message_id)

    @mcp.tool()
    def gmail_draft(to: str, subject: str, body: str) -> dict:
        """Create a Gmail draft (never sends — the operator reviews + sends)."""
        return gm.create_draft(gmail_svc, to, subject, body)

    @mcp.tool()
    def calendar_today() -> list[dict]:
        """Today's calendar events (operator-local day), ordered by start."""
        return cal.todays_events(calendar_svc, _operator_now())

    @mcp.tool()
    def calendar_freebusy(hours: int = 24) -> list[dict]:
        """Busy intervals over the next N hours."""
        return cal.free_busy(calendar_svc, _operator_now(), hours)

    log.info("[google] MCP server ready (gmail + calendar)")
    mcp.run()


if __name__ == "__main__":
    main()
