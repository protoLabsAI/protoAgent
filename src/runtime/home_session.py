"""The instance's one Home conversation (ADR 0135 D9, ADR 0129 D6 amendment, #5545).

Each instance has one Home chat: an ordinary operator chat with a stable id, which the
console pins. This module owns the two facts about it that are instance state — its
session id and when the operator last read it — in one small JSON file under the
instance root (``<instance_root>/home/home.json``).

The id is **generated once and stored**, shaped ``chat-home-<32 hex>``:

* the ``chat-`` prefix is what makes it an ordinary operator chat everywhere the server
  asks — the console session index and the task-store retention both key on it, so Home
  is listed with the operator's chats and its turns are kept like theirs;
* it is neither ``system:`` nor ``job:`` (nor ``background:`` / ``palette-``), so it stays
  inside compaction, session gists and the memory harvest;
* it is not derived from the instance name, so renaming or re-scoping an instance cannot
  change which chat is Home, and the unscoped (legacy) instance needs no special case.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from infra.paths import atomic_write, instance_paths

log = logging.getLogger("protoagent.home")

#: The developer flag (``runtime.flags``) the Home conversation ships behind.
FLAG_ID = "home_conversation"

HOME_SESSION_PREFIX = "chat-home-"
HOME_TITLE = "Home"

_lock = threading.Lock()


def _path() -> Path:
    return instance_paths().store("home") / "home.json"


def _mint() -> str:
    return f"{HOME_SESSION_PREFIX}{uuid.uuid4().hex}"


def _load(path: Path) -> dict:
    """The stored state, or ``{}`` when there is none yet.

    Only a missing file or unparseable content reads as "none". Any other read failure
    raises: treating a file that is there but unreadable right now as absent would mint
    a second Home id over the first.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("[home] %s is not valid JSON; a new Home session id will be stored", path)
        return {}
    return data if isinstance(data, dict) else {}


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and value.startswith(HOME_SESSION_PREFIX) and len(value) > len(HOME_SESSION_PREFIX)


def _state_locked(path: Path) -> dict:
    """Read the state, minting and persisting the id on first use. Caller holds ``_lock``."""
    data = _load(path)
    if not _valid_id(data.get("session_id")):
        data = {"session_id": _mint(), "last_read_at": None}
        atomic_write(path, json.dumps(data))
    last_read = data.get("last_read_at")
    return {
        "session_id": data["session_id"],
        "last_read_at": last_read if isinstance(last_read, str) and last_read else None,
    }


def home_state() -> dict:
    """``{session_id, last_read_at}`` for this instance's Home chat. The id is minted on
    the first call and is the same on every later one, across restarts. ``last_read_at``
    is zoned ISO-8601 UTC, or ``None`` when Home has never been marked read."""
    with _lock:
        return _state_locked(_path())


def home_session_id() -> str:
    """This instance's Home session id (minted and stored on first use)."""
    return home_state()["session_id"]


def mark_home_read(now: datetime | None = None) -> dict:
    """Set ``last_read_at`` to now and return the new ``{session_id, last_read_at}``."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with _lock:
        path = _path()
        state = _state_locked(path)
        state["last_read_at"] = stamp
        atomic_write(path, json.dumps(state))
        return state
