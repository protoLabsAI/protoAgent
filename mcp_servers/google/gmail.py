"""Gmail operations for the Google MCP server (Slice 2).

Pure functions over a ``googleapiclient`` Gmail ``service`` resource — the
service is injected (built in ``auth.py``) so this layer is unit-testable with a
mock and carries no OAuth/dep weight. Read + draft only; **no send** in v1 (a
draft is reviewable, a send isn't reversible).
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def search_messages(service: Any, query: str, max_results: int = 10) -> list[dict]:
    """Search the mailbox (Gmail query syntax, e.g. ``is:unread newer_than:1d``)
    and return lightweight headers — id, from, subject, date, snippet."""
    max_results = max(1, min(int(max_results), 50))
    resp = (
        service.users().messages()
        .list(userId="me", q=query or "", maxResults=max_results)
        .execute()
    )
    out: list[dict] = []
    for ref in resp.get("messages", []) or []:
        msg = (
            service.users().messages()
            .get(userId="me", id=ref["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        p = msg.get("payload", {})
        out.append({
            "id": msg.get("id", ""),
            "from": _header(p, "From"),
            "subject": _header(p, "Subject"),
            "date": _header(p, "Date"),
            "snippet": (msg.get("snippet") or "").strip(),
            "unread": "UNREAD" in (msg.get("labelIds") or []),
        })
    return out


def _decode_part(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_body(payload: dict) -> str:
    """Best-effort plain-text body: prefer text/plain, walk multipart, fall back."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain":
            text = _decode_part(part)
            if text:
                return text
    # nested multipart
    for part in payload.get("parts", []) or []:
        if (part.get("mimeType") or "").startswith("multipart/"):
            text = _extract_body(part)
            if text:
                return text
    return _decode_part(payload)


def get_message(service: Any, message_id: str, max_chars: int = 4000) -> dict:
    """Full message: headers + a plain-text body (truncated)."""
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    p = msg.get("payload", {})
    body = _extract_body(p).strip()
    return {
        "id": msg.get("id", ""),
        "from": _header(p, "From"),
        "to": _header(p, "To"),
        "subject": _header(p, "Subject"),
        "date": _header(p, "Date"),
        "body": body[:max_chars] + ("…" if len(body) > max_chars else ""),
    }


def create_draft(service: Any, to: str, subject: str, body: str) -> dict:
    """Create a Gmail **draft** (not sent — the operator reviews + sends)."""
    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    draft = (
        service.users().drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    return {"draft_id": draft.get("id", ""), "to": to, "subject": subject}
