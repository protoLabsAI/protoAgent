"""Thread history: ACP ``session/list`` / ``session/load`` over protoAgent's durable chat
turns (ADR 0104: ``GET /api/chat/sessions`` + ``GET /api/chat/sessions/<id>/turns``).

The server knows a session's id, last activity and turns — not which editor folder it
was opened from or a title. ACP's ``SessionInfo`` needs a ``cwd`` (Zed groups threads by
it), so the shim keeps a tiny local index (``threads.json`` next to the credentials file)
of the threads it created: ``{url: {sessionId: {cwd, title}}}``. A thread missing from the
index (another machine, a wiped index) still lists — its title is read from its first
turn and it is reported under the folder the editor asked about.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from .a2a import STEER_CONSUMED_MIME, TOOL_CALL_EXT_URI, _data_by_mime, _text_from_parts
from .credentials import credentials_path

_PREAMBLE = re.compile(r"^\[Context: the operator is talking to you from the Zed editor[^\]]*\]\s*", re.S)
_TITLE_LEN = 80


def index_path() -> Path:
    return credentials_path().with_name("threads.json")


class ThreadIndex:
    def __init__(self, url: str, path: Path | None = None) -> None:
        self.url = url.rstrip("/")
        self.path = path or index_path()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, sid: str) -> dict:
        row = (self._read().get(self.url) or {}).get(sid)
        return row if isinstance(row, dict) else {}

    def put(self, sid: str, **fields: Any) -> None:
        data = self._read()
        rows = data.setdefault(self.url, {})
        rows[sid] = {**(rows.get(sid) or {}), **{k: v for k, v in fields.items() if v}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass  # the index is a convenience; listing still works without it


def clean_user_text(text: str) -> str:
    return _PREAMBLE.sub("", text or "").strip()


def title_of(text: str) -> str:
    line = " ".join(clean_user_text(text).split())
    return line[:_TITLE_LEN] + ("…" if len(line) > _TITLE_LEN else "")


def _is_user(msg: dict) -> bool:
    role = str(msg.get("role") or "").upper()
    return "USER" in role and "AGENT" not in role


def first_user_text(turn: dict) -> str:
    """The operator's message that started a turn — skipping hitl answers (``approved`` /
    a parked question's reply ride in as user messages with ``hitl_resume``)."""
    for msg in turn.get("history") or []:
        if not isinstance(msg, dict) or not _is_user(msg):
            continue
        if (msg.get("metadata") or {}).get("hitl_resume"):
            continue
        text = _text_from_parts(msg.get("parts"))
        if text:
            return text
    return ""


def turn_events(turn: dict) -> list[tuple[str, Any]]:
    """A turn's replayable middle, in stream order: ``("tool", {id, name, args, result,
    error})`` once per call (its started/completed frames merged) and ``("steer", text)``
    where a Send Now redirect was folded in (the durable ``steer_consumed`` frame)."""
    out: list[tuple[str, Any]] = []
    calls: dict[str, dict] = {}
    for msg in turn.get("history") or []:
        if not isinstance(msg, dict) or _is_user(msg):
            continue
        steer = _data_by_mime(msg.get("parts"), STEER_CONSUMED_MIME)
        if isinstance(steer, dict):
            for item in steer.get("items") or []:
                if isinstance(item, dict) and item.get("text"):
                    out.append(("steer", str(item["text"])))
            continue
        d = (msg.get("metadata") or {}).get(TOOL_CALL_EXT_URI)
        if not isinstance(d, dict) or not d.get("toolCallId"):
            continue
        cid = str(d["toolCallId"])
        if cid not in calls:
            calls[cid] = {"id": cid, "name": str(d.get("name") or "")}
            out.append(("tool", calls[cid]))
        c = calls[cid]
        if d.get("args"):
            c["args"] = d["args"]
        if d.get("phase") in ("completed", "failed"):
            c["result"] = d.get("result") if d.get("result") is not None else d.get("error")
            c["error"] = d.get("phase") == "failed"
    return out


async def fetch_turns(client: Any, sid: str, limit: int = 200) -> list[dict]:
    body = await client.get_json(f"/api/chat/sessions/{sid}/turns?limit={limit}")
    turns = (body or {}).get("turns") if isinstance(body, dict) else None
    return [t for t in turns or [] if isinstance(t, dict)]


async def list_threads(client: Any, index: ThreadIndex, prefix: str | None, cwd: str | None) -> list[dict]:
    """``[{sessionId, cwd, title, updatedAt}]`` newest first: every chat session the agent
    has (console tabs included — ``/api/chat/sessions`` already scopes to ``chat-`` ids), or
    only ``<prefix>-…`` threads when ``prefix`` is given (``--zed-threads-only``)."""
    body = await client.get_json("/api/chat/sessions?limit=200")
    rows = [r for r in (body or {}).get("sessions") or [] if isinstance(r, dict)] if isinstance(body, dict) else []
    rows = [r for r in rows if r.get("session_id")]
    if prefix:
        rows = [r for r in rows if str(r["session_id"]).startswith(prefix + "-")]
    out: list[dict] = []
    missing: list[dict] = []
    for r in rows:
        sid = str(r["session_id"])
        known = index.get(sid)
        if cwd and known.get("cwd") and known["cwd"].rstrip("/") != cwd.rstrip("/"):
            continue  # opened from another folder
        item = {
            "sessionId": sid,
            "cwd": known.get("cwd") or cwd or os.getcwd(),
            # A server-side session title when the server has one, else our index, else the
            # first user message (filled in below).
            "title": (str(r["title"]) if r.get("title") else None) or known.get("title"),
            "updatedAt": _rfc3339(r.get("last_updated")),
        }
        out.append(item)
        if not item["title"]:
            missing.append(item)
    # Titles the index doesn't have: read each thread's first turn (bounded fan-out).
    sem = asyncio.Semaphore(6)

    async def fill(item: dict) -> None:
        async with sem:
            turns = await fetch_turns(client, item["sessionId"])
        text = next((first_user_text(t) for t in turns if first_user_text(t)), "")
        item["title"] = title_of(text) or None
        if item["title"]:
            index.put(item["sessionId"], title=item["title"])

    await asyncio.gather(*(fill(i) for i in missing[:40]))
    return out


def _rfc3339(value: Any) -> str | None:
    if not value:
        return None
    s = str(value)
    # The server emits naive UTC isoformat; RFC 3339 needs an offset.
    if re.search(r"(Z|[+-]\d\d:\d\d)$", s):
        return s
    return s + "Z"
