"""The deck's data layer (#3468): what the screens render, fetched by a backend.

Two backends, one interface:

- :class:`LiveBackend` — a :class:`deck.hub.HubClient` on a running hub. The roster is
  ``GET /api/fleet``; lifecycle goes through the hub's control plane; member detail is read
  through the slug proxy. Every read degrades PER PANE: a member whose diagnostics 503 or
  time out yields that pane's error string, never a dead screen.
- :class:`OfflineBackend` — the supervisor's disk view when NOTHING answered. Built by the
  fleet CLI from callables (``deck`` never imports ``graph``), badged, lifecycle only.

Everything here is synchronous and blocking on purpose: the app runs it in thread workers
and hands results back to the UI loop. No Textual imports — this module is testable with
an ``httpx.MockTransport`` and nothing else.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from typing import Any, Protocol

from deck import hub as deckhub

# ── presence ──────────────────────────────────────────────────────────────────

PRESENCE_GLYPH = {"host": "●", "online": "●", "remote": "●", "stopped": "○", "unreachable": "◌"}


def presence_of(agent: dict) -> str:
    """The console's presence vocabulary, exactly (apps/web/src/app/FleetRoom.tsx::presenceOf):
    host · online · remote · stopped · unreachable."""
    if agent.get("host"):
        return "host"
    if agent.get("running"):
        return "remote" if agent.get("remote") else "online"
    return "unreachable" if agent.get("remote") else "stopped"


def slug_of(agent: dict) -> str:
    """The routing slug — the reserved ``host`` for the hub row, else the immutable id."""
    return "host" if agent.get("host") else str(agent.get("id") or agent.get("name") or "")


def display_name(agent: dict) -> str:
    return str(agent.get("label") or agent.get("name") or slug_of(agent))


# ── snapshot ──────────────────────────────────────────────────────────────────


@dataclass
class Rollup:
    """One member's telemetry rollup, or the reason there is none."""

    turns: int = 0
    cost_usd: float = 0.0
    success_rate: float = 0.0
    cache_hit_ratio: float = 0.0
    reachable: bool = True
    enabled: bool = True


@dataclass
class Snapshot:
    """One poll of the roster (plus whatever cheap extras came with it)."""

    mode: str  # "live" | "offline"
    label: str  # the header line after "protoagent fleet · "
    roster: list[dict] = field(default_factory=list)
    host_version: str = ""
    rollups: dict[str, Rollup] = field(default_factory=dict)  # by slug
    warnings: list[str] = field(default_factory=list)
    error: str = ""  # a failed poll keeps the previous roster and shows this
    fetched_at: float = field(default_factory=time.monotonic)

    def skewed(self, agent: dict) -> bool:
        v = str(agent.get("version") or "")
        return bool(self.host_version and v and v != self.host_version and presence_of(agent) in ("online", "remote"))


@dataclass
class MemberDetail:
    """Everything the detail screen shows for one member, each pane independent."""

    slug: str
    name: str
    runtime: dict = field(default_factory=dict)
    runtime_error: str = ""
    logs: list[dict] = field(default_factory=list)
    logs_note: str = ""
    logs_error: str = ""
    sessions: list[dict] = field(default_factory=list)
    sessions_error: str = ""
    rollup: Rollup | None = None


# ── backends ──────────────────────────────────────────────────────────────────


class Backend(Protocol):
    mode: str

    def snapshot(self) -> Snapshot: ...
    def start(self, name: str) -> dict: ...
    def stop(self, name: str) -> dict: ...
    def detail(self, agent: dict) -> MemberDetail: ...
    def console_href(self, agent: dict) -> str | None: ...
    def sessions(self, agent: dict) -> list[dict]: ...
    def turns(self, agent: dict, session_id: str, limit: int = 50) -> list[dict]: ...
    def a2a(self, agent: dict) -> Any: ...
    def close(self) -> None: ...


def _seg(value: Any) -> str:
    """One path segment, fully encoded — a session id comes from the member's own list, and a
    member (or a proxy in between) must not be able to steer a request elsewhere with a
    ``..`` or a ``/`` (httpx collapses literal dot segments before sending)."""
    return quote(str(value), safe="").replace(".", "%2E")


def _num(value: Any, default: float = 0.0) -> float:
    """A rollup field as a finite float, or ``default`` — one member's malformed telemetry
    must stay local to that member, never fail the whole poll (``float("inf")`` parses, and
    ``int(inf)`` then raises OverflowError; ``nan`` is no number either)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _warning_text(w: Any) -> str:
    if isinstance(w, str):
        return w
    if isinstance(w, dict):
        return str(w.get("message") or w.get("detail") or w)
    return str(w)


class LiveBackend:
    """A running hub. ``snapshot()`` is one roster read plus best-effort extras (the
    telemetry rollup, the hub's runtime warnings); extras failing never fail the poll."""

    mode = "live"

    def __init__(self, conn: deckhub.Connection):
        self.conn = conn
        self.client = conn.client
        self._first = conn.roster

    def snapshot(self) -> Snapshot:
        client = self.client
        try:
            roster = self._first if self._first is not None else client.fleet()
        except deckhub.HubError as exc:
            return Snapshot(mode="live", label=self._label([]), error=str(exc))
        finally:
            self._first = None
        host = next((a for a in roster if a.get("host")), {})
        snap = Snapshot(mode="live", label=self._label(roster), roster=roster, host_version=str(host.get("version") or ""))
        try:
            fleet = client.telemetry_fleet()
            for slug, entry in (fleet.get("members") or {}).items():
                if not isinstance(entry, dict):
                    continue
                r = entry.get("rollup")
                r = r if isinstance(r, dict) else {}
                snap.rollups[str(slug)] = Rollup(
                    turns=int(_num(r.get("turns"))),
                    cost_usd=_num(r.get("cost_usd")),
                    success_rate=_num(r.get("success_rate")),
                    cache_hit_ratio=_num(r.get("cache_hit_ratio")),
                    reachable=bool(entry.get("reachable", True)),
                    enabled=bool(entry.get("telemetry_enabled", True)),
                )
        except (deckhub.HubError, AttributeError, TypeError, ValueError):
            pass  # extras are best-effort; the roster is the poll
        try:
            status = client.runtime_status()
            snap.warnings = [_warning_text(w) for w in (status.get("warnings") or []) if w]
        except deckhub.HubError:
            pass
        return snap

    def _label(self, roster: list[dict]) -> str:
        host = next((a for a in roster if a.get("host")), {})
        name = host.get("label") or host.get("name") or self.conn.card.get("name") or "hub"
        ver = host.get("version") or ""
        return f"live · {self.client.url} · {name}{f' v{ver}' if ver else ''} · via {self.conn.candidate.source}"

    def start(self, name: str) -> dict:
        return self.client.start(name)

    def stop(self, name: str) -> dict:
        return self.client.stop(name)

    def detail(self, agent: dict) -> MemberDetail:
        slug = slug_of(agent)
        d = MemberDetail(slug=slug, name=display_name(agent))
        try:
            d.runtime = self.client.member_runtime_status(slug)
        except deckhub.HubError as exc:
            d.runtime_error = str(exc)
        try:
            logs = self.client.diagnostics_logs(slug)
            d.logs = list(logs.get("lines") or [])
            d.logs_note = str(logs.get("note") or "")
            if not logs.get("enabled", True):
                d.logs_note = d.logs_note or "log buffer disabled on this member"
        except deckhub.HubError as exc:
            d.logs_error = str(exc)
        try:
            sessions = self.client.diagnostics_sessions(slug)
            d.sessions = list(sessions.get("sessions") or [])
            if sessions.get("detail") and not d.sessions:
                d.sessions_error = str(sessions["detail"])
        except deckhub.HubError as exc:
            d.sessions_error = str(exc)
        return d

    def console_href(self, agent: dict) -> str | None:
        return self.client.console_href(slug_of(agent))

    # ── conversations (#3469): the member's console sessions and durable turns ──

    def sessions(self, agent: dict) -> list[dict]:
        """``GET /agents/<slug>/api/chat/sessions`` — newest first; the console's own list."""
        data = self.client.member_get(slug_of(agent), "/api/chat/sessions", limit=50)
        rows = data.get("sessions") if isinstance(data, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("session_id")]

    def turns(self, agent: dict, session_id: str, limit: int = 50) -> list[dict]:
        """``GET /agents/<slug>/api/chat/sessions/<id>/turns`` — the durable turns (ADR 0104),
        oldest first, each with the status / artifacts / history the reducer replays."""
        data = self.client.member_get(slug_of(agent), f"/api/chat/sessions/{_seg(session_id)}/turns", limit=limit)
        rows = data.get("turns") if isinstance(data, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict)]

    def a2a(self, agent: dict):
        """A fresh A2A client for the member (through the hub proxy, same credential)."""
        from deck.a2a import A2AClient

        return A2AClient.for_member(self.client, slug_of(agent))

    def close(self) -> None:
        self.client.close()


class OfflineBackend:
    """The disk view: ``fleet.json`` through the supervisor, minus its synthesized host row
    (that row describes THIS process, not a server). Lifecycle only; no member detail."""

    mode = "offline"

    def __init__(
        self,
        *,
        status: Callable[[], list[dict]],
        start: Callable[[str], dict],
        stop: Callable[[str], dict],
        fleet_json: Path,
        reason: str = "",
    ):
        self._status, self._start, self._stop = status, start, stop
        self.fleet_json = fleet_json
        self.reason = reason

    def _label(self) -> str:
        why = f" · {self.reason}" if self.reason else ""
        return f"offline{why} · reading {self.fleet_json}"

    def snapshot(self) -> Snapshot:
        try:
            rows = [a for a in self._status() if not a.get("host")]
        except Exception as exc:  # noqa: BLE001 — a poll must never take the deck down
            return Snapshot(mode="offline", label=self._label(), error=str(exc))
        return Snapshot(mode="offline", label=self._label(), roster=rows)

    def start(self, name: str) -> dict:
        return self._start(name)

    def stop(self, name: str) -> dict:
        return self._stop(name)

    def detail(self, agent: dict) -> MemberDetail:
        d = MemberDetail(slug=slug_of(agent), name=display_name(agent))
        d.runtime_error = d.logs_error = d.sessions_error = "offline — no hub to read this member through"
        return d

    def console_href(self, agent: dict) -> str | None:
        return None

    def sessions(self, agent: dict) -> list[dict]:
        return []

    def turns(self, agent: dict, session_id: str, limit: int = 50) -> list[dict]:
        return []

    def a2a(self, agent: dict):
        raise RuntimeError("offline — no hub to talk to this member through")

    def close(self) -> None:
        return None
