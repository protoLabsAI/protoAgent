"""Bring a stopped local delegate up, with the operator's consent (#3126).

A delegate is a URL. When nothing answers it, the dispatch reported
``unreachable`` and stopped there — even when the target was a member of THIS box's
own fleet, sitting in the roster with a port and a workspace, one
``supervisor.start()`` away. The operator's recourse was to notice the error, find
the fleet surface, start the agent, and ask again.

Consent rather than silent spawn: starting a process is the operator's call, and a
cold start takes seconds they should know they are waiting for. The ask renders as an
ordinary approval card through the same ``interrupt()`` the shell tool uses.

The coordinating case is why the card offers more than yes/no. When the lead is asked
to run a discussion between several participants (#3042) it reaches them one at a
time, so a per-call prompt would mean three cards for one "have proto and reviewer
sort this out". "For this chat" grants the rest of the session, once.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

log = logging.getLogger("protoagent.delegates.autostart")

# One start attempt per member per window. A member that dies at boot would otherwise
# be re-proposed on every delegation in the round — a prompt loop over something that
# cannot come up. After the window it degrades to the plain unreachable error.
_RETRY_WINDOW_S = 60.0
# How long a "for this chat" grant lasts. Bounded rather than session-lifetime: a grant
# is permission to start processes, and one given an hour ago for a different task
# should not silently apply now.
_GRANT_TTL_S = 3600.0
# Bounded wait for the member's port. Long enough for a normal boot + graph compile,
# short enough that the turn does not read as a hang; past it the caller reports
# "still starting" instead of blocking.
_READY_TIMEOUT_S = 25.0

_LAST_ATTEMPT: dict[str, float] = {}
_GRANTS: dict[str, float] = {}

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _now() -> float:
    return time.monotonic()


def startable_member(url: str) -> dict | None:
    """The stopped local fleet member this delegate URL points at, or ``None``.

    Three conditions, all required. **Loopback** — a remote peer is someone else's
    machine and never ours to spawn. **In the roster with a matching port** — the
    identity comes from the fleet's own record, not from parsing a name out of a URL.
    **Not running** — if something is already up on that port, the failure is not a
    stopped member and starting would be wrong.
    """
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _LOOPBACK or not parsed.port:
        return None
    try:
        from graph.fleet import supervisor
    except Exception:  # noqa: BLE001 — no fleet on this instance is a normal answer
        return None
    try:
        entries = supervisor.status()
    except Exception:  # noqa: BLE001 — a roster we cannot read is not a member we can start
        log.debug("[delegates] fleet status unavailable; not offering a start", exc_info=True)
        return None
    for entry in entries:
        if entry.get("port") != parsed.port or entry.get("running"):
            continue
        # The host entry has no workspace to spawn; only members do. Keep looking —
        # bailing here let an id-less entry mask a real member sharing the port.
        if not entry.get("id"):
            continue
        return {"name": entry.get("name") or entry["id"], "id": entry["id"], "port": parsed.port}
    return None


def attempt_allowed(member_id: str) -> bool:
    """False when this member was already started within the retry window."""
    last = _LAST_ATTEMPT.get(member_id)
    return last is None or (_now() - last) >= _RETRY_WINDOW_S


def record_attempt(member_id: str) -> None:
    _LAST_ATTEMPT[member_id] = _now()


def granted(session_id: str) -> bool:
    """Has the operator already said "start agents as needed" for this chat?"""
    if not session_id:
        return False
    until = _GRANTS.get(session_id)
    if until is None:
        return False
    if _now() >= until:
        _GRANTS.pop(session_id, None)
        return False
    return True


def grant(session_id: str) -> None:
    if session_id:
        _GRANTS[session_id] = _now() + _GRANT_TTL_S


def start_and_wait(member: dict) -> tuple[bool, str]:
    """Start the member and wait for its port. ``(ready, detail)``.

    ``detail`` is what the operator reads when it did not come up — a boot failure
    carries the agent's own log tail, which is the whole reason ``supervisor.start``
    raises instead of reporting a cheerful ``running: true`` (#1565).
    """
    from graph.fleet import supervisor

    record_attempt(member["id"])
    try:
        supervisor.start(member["id"])
    except Exception as exc:  # noqa: BLE001 — FleetError carries the log tail; show it
        return False, str(exc)
    deadline = _now() + _READY_TIMEOUT_S
    while _now() < deadline:
        if supervisor._port_listening(member["port"]):
            return True, ""
        time.sleep(0.5)
    return False, (
        f"{member['name']} was started but has not answered on port {member['port']} yet. "
        "It may still be compiling its graph — ask again in a moment."
    )


def consent_form(member: dict, *, coordinating: bool) -> dict:
    """The approval payload. A form, not a yes/no, because of the coordinating case.

    ``coordinating`` only changes the wording — the "for this chat" option is always
    offered, since the lead may reach for a second participant whether or not this
    particular call looked like coordination.
    """
    why = (
        "The lead needs it to run this discussion."
        if coordinating
        else "The delegation cannot be delivered until it is running."
    )
    return {
        "kind": "form",
        "title": f"Start {member['name']}?",
        "description": (
            f"{member['name']} is a member of this machine's fleet and is not running "
            f"(port {member['port']}). {why} Starting takes a few seconds, then the "
            "delegation is retried."
        ),
        "steps": [
            {
                "schema": {
                    "type": "object",
                    "required": ["choice"],
                    "properties": {
                        "choice": {
                            "type": "string",
                            "title": "Start it?",
                            "default": "once",
                            "oneOf": [
                                {
                                    "const": "once",
                                    "title": f"Start {member['name']}",
                                    "description": "Just this one, this time.",
                                },
                                {
                                    "const": "session",
                                    "title": "Start agents as needed for this chat",
                                    "description": (
                                        "Don't ask again while this chat is open — for when the "
                                        "lead is coordinating several participants."
                                    ),
                                },
                                {
                                    "const": "no",
                                    "title": "Don't start it",
                                    "description": "Report the delegate as unreachable.",
                                },
                            ],
                        }
                    },
                }
            }
        ],
    }


def read_choice(response) -> str:
    """The operator's answer, normalized to once | session | no.

    Accepts the shapes a resume may arrive in — the submitted form object, a bare
    string, or the approve/decline words the approval cards use — so a console that
    answers this like an ordinary approval still works.
    """
    if isinstance(response, dict):
        value = str(response.get("choice") or response.get("answer") or "").strip().lower()
    else:
        value = str(response or "").strip().lower()
    if value in {"once", "session", "no"}:
        return value
    if value in {"approve", "approved", "yes", "y", "true", "ok"}:
        return "once"
    return "no"
