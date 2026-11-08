"""Per-chat operator directive ledger (ADR 0127 D6).

An operator's standing instruction or approval ("cut the release once #A and #B merge;
if the review gives a WARN or FAIL, come back to me instead of merging") must outlive the
transcript it was given in: a compaction can summarise it away, and an agent that can no
longer see it decides it never had approval. The ledger keeps each one as a durable,
chat-scoped record, independent of the transcript. It is the system of record; the
transcript is not.

* The agent writes it — ``record_directive`` / ``fulfil_directive`` / ``revoke_directive``
  and reads it with ``list_directives`` (``tools/directive_tools.py``).
* The operator reads and revokes through ``GET`` / ``DELETE
  /api/chat/sessions/{id}/directives`` and the ``/directives`` chat command.
* Every in-force directive of the current chat is rendered into ``<working_state>`` each
  model call (:func:`working_state_section`, called by ``graph.projection``).

Storage: one JSON file per chat under the per-instance ``instance_root/directives`` store
(ADR 0065). The file name is a hash of the session id — session ids may carry ``:`` and
other characters a sanitising mapper would alias — and the id is also stored inside the
file and checked on read. Writes are atomic (temp file + ``os.replace``) under one
process-wide lock, so two store objects over the same directory can't interleave a
read-modify-write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATUS_IN_FORCE = "in_force"
STATUS_FULFILLED = "fulfilled"
STATUS_REVOKED = "revoked"
STATUSES = (STATUS_IN_FORCE, STATUS_FULFILLED, STATUS_REVOKED)
CLOSED_BY = ("agent", "operator")

#: A directive is the operator's own words, quoted — a sentence or a short paragraph, not a
#: whole pasted message. Generous, but bounded: every in-force one renders every turn.
MAX_QUOTE_CHARS = 4000
MAX_NOTE_CHARS = 2000
#: In-force directives rendered in ``<working_state>``; more point at ``list_directives``.
WS_DIRECTIVE_CAP = 20

# One lock for every store object: :func:`directive_store` may hand out a fresh one per call.
_LOCK = threading.RLock()

WS_HEADING = "STANDING DIRECTIVES (from the operator — in force until fulfilled or revoked)"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _publish(topic: str, data: dict) -> None:
    """Best-effort bus push so a console view can refresh on a change. No-op when the host
    hasn't wired a publisher; a bus hiccup must never break a ledger write."""
    try:
        from graph.plugins.host import HOST

        if HOST.publish:
            HOST.publish(topic, data)
    except Exception:  # noqa: BLE001
        pass


class DirectiveError(ValueError):
    """A ledger operation the caller can correct (empty quote, unknown id, already closed)."""


class DirectiveNotFound(DirectiveError):
    """No directive with that id in that chat."""


class DirectiveClosed(DirectiveError):
    """The directive is already fulfilled or revoked."""


@dataclass
class Directive:
    id: str
    session_id: str
    quote: str
    conditions: str = ""
    scope: str = ""
    status: str = STATUS_IN_FORCE
    created_at: str = ""
    closed_at: str | None = None
    close_reason: str | None = None
    closed_by: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Directive:
        return cls(
            id=str(data["id"]),
            session_id=str(data.get("session_id") or ""),
            quote=str(data.get("quote") or ""),
            conditions=str(data.get("conditions") or ""),
            scope=str(data.get("scope") or ""),
            status=str(data.get("status") or STATUS_IN_FORCE),
            created_at=str(data.get("created_at") or ""),
            closed_at=data.get("closed_at"),
            close_reason=data.get("close_reason"),
            closed_by=data.get("closed_by"),
        )

    @property
    def in_force(self) -> bool:
        return self.status == STATUS_IN_FORCE


def _resolve_base() -> Path:
    from infra.paths import instance_paths

    return instance_paths().store("directives")


def _file_name(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".json"


class DirectiveStore:
    """The durable ledger. Thread-safe via the module lock (one process; contention is low)."""

    def __init__(self, base_dir: str | os.PathLike | None = None):
        self._base = Path(base_dir) if base_dir else _resolve_base()
        self._lock = _LOCK

    @property
    def path(self) -> Path:
        return self._base

    def _path(self, session_id: str) -> Path:
        return self._base / _file_name(session_id)

    def _load(self, session_id: str) -> list[Directive]:
        path = self._path(session_id)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[directives] failed to read %s: %s", path, exc)
            return []
        if not isinstance(data, dict) or data.get("session_id") != session_id:
            # A hash collision or a hand-edited file: never show one chat another's ledger.
            log.warning("[directives] %s does not belong to session %r — ignored", path, session_id)
            return []
        out: list[Directive] = []
        for row in data.get("directives") or []:
            try:
                out.append(Directive.from_dict(row))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("[directives] skipping a malformed row in %s: %s", path, exc)
        return out

    def _save(self, session_id: str, items: list[Directive]) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        payload = {"session_id": session_id, "directives": [d.to_dict() for d in items]}
        fd, tmp = tempfile.mkstemp(dir=self._base, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
            tmp = ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── operations ──────────────────────────────────────────────────────────────

    def record(self, session_id: str, quote: str, *, conditions: str = "", scope: str = "") -> Directive:
        """Record a new in-force directive for ``session_id``. ``quote`` is the operator's
        words verbatim (whitespace at the ends trimmed, nothing else touched)."""
        session_id = (session_id or "").strip()
        if not session_id:
            raise DirectiveError("a directive belongs to a chat — no session id was given")
        quote = (quote or "").strip()
        if not quote:
            raise DirectiveError("quote is required — the operator's exact words")
        if len(quote) > MAX_QUOTE_CHARS:
            raise DirectiveError(
                f"quote is {len(quote)} characters (max {MAX_QUOTE_CHARS}) — quote the directive itself, "
                "not the whole message"
            )
        conditions = (conditions or "").strip()[:MAX_NOTE_CHARS]
        scope = (scope or "").strip()[:MAX_NOTE_CHARS]
        with self._lock:
            items = self._load(session_id)
            taken = {d.id for d in items}
            new_id = f"dir-{secrets.token_hex(3)}"
            while new_id in taken:
                new_id = f"dir-{secrets.token_hex(3)}"
            directive = Directive(
                id=new_id,
                session_id=session_id,
                quote=quote,
                conditions=conditions,
                scope=scope,
                created_at=_now(),
            )
            items.append(directive)
            self._save(session_id, items)
        _publish("directive.changed", {"session_id": session_id, "id": directive.id})
        return directive

    def list(self, session_id: str, *, include_closed: bool = True) -> list[Directive]:
        """This chat's directives, oldest first. ``include_closed=False`` → in force only."""
        if not session_id:
            return []
        with self._lock:
            items = self._load(session_id)
        return items if include_closed else [d for d in items if d.in_force]

    def in_force(self, session_id: str) -> list[Directive]:
        return self.list(session_id, include_closed=False)

    def get(self, session_id: str, directive_id: str) -> Directive | None:
        directive_id = (directive_id or "").strip()
        return next((d for d in self.list(session_id) if d.id == directive_id), None)

    def close(
        self,
        session_id: str,
        directive_id: str,
        *,
        status: str,
        reason: str = "",
        closed_by: str = "agent",
    ) -> Directive:
        """Mark an in-force directive ``fulfilled`` or ``revoked``. Raises
        :class:`DirectiveNotFound` for an id this chat doesn't hold (another chat's id
        included) and :class:`DirectiveClosed` when it is already closed."""
        if status not in (STATUS_FULFILLED, STATUS_REVOKED):
            raise DirectiveError(f"status must be {STATUS_FULFILLED!r} or {STATUS_REVOKED!r}")
        if closed_by not in CLOSED_BY:
            raise DirectiveError(f"closed_by must be one of {CLOSED_BY}")
        directive_id = (directive_id or "").strip()
        with self._lock:
            items = self._load(session_id) if session_id else []
            hit = next((d for d in items if d.id == directive_id), None)
            if hit is None:
                raise DirectiveNotFound(f"no directive {directive_id!r} in this chat")
            if not hit.in_force:
                raise DirectiveClosed(f"directive {directive_id} is already {hit.status}")
            hit.status = status
            hit.closed_at = _now()
            hit.close_reason = (reason or "").strip()[:MAX_NOTE_CHARS] or None
            hit.closed_by = closed_by
            self._save(session_id, items)
        _publish("directive.changed", {"session_id": session_id, "id": directive_id})
        return hit

    def forget(self, session_id: str) -> bool:
        """Drop a chat's whole ledger (the chat was deleted). True when a file was removed."""
        if not session_id:
            return False
        with self._lock:
            try:
                self._path(session_id).unlink()
            except FileNotFoundError:
                return False
        _publish("directive.changed", {"session_id": session_id})
        return True


def directive_store() -> DirectiveStore:
    """The live store: ``STATE.directive_store`` (built once at boot — the instance root
    never changes for the life of a process, so it survives every graph reload), else a
    store over this instance's directory (a pre-setup console, a test). Not cached on
    STATE here: a lazily cached store would pin whatever instance root was current then."""
    from runtime.state import STATE

    store = getattr(STATE, "directive_store", None)
    return store if store is not None else DirectiveStore()


# ── rendering ───────────────────────────────────────────────────────────────────


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def render_entry(d: Directive, *, closed_detail: bool = False) -> str:
    """One directive: id, when, the quote verbatim, then any conditions/scope."""
    head = f"- {d.id}"
    if closed_detail:
        head += f" [{d.status}]"
    head += f" (recorded {_when(d.created_at)}):"
    lines = [head, _indent(f'"{d.quote}"')]
    if d.conditions:
        lines.append(_indent(f"Conditions: {d.conditions}"))
    if d.scope:
        lines.append(_indent(f"Scope: {d.scope}"))
    if closed_detail and not d.in_force:
        by = f" by the {d.closed_by}" if d.closed_by else ""
        why = f": {d.close_reason}" if d.close_reason else ""
        lines.append(_indent(f"{d.status.capitalize()}{by} {_when(d.closed_at)}{why}"))
    return "\n".join(lines)


def working_state_section(session_id: str) -> str:
    """The ``STANDING DIRECTIVES`` section of ``<working_state>`` for this chat, or ""."""
    if not session_id:
        return ""
    items = directive_store().in_force(session_id)
    if not items:
        return ""
    shown = items[:WS_DIRECTIVE_CAP]
    body = "\n".join(render_entry(d) for d in shown)
    if len(items) > len(shown):
        body += f"\n…+{len(items) - len(shown)} more (list_directives shows them all)"
    return (
        f"{WS_HEADING}:\n{body}\n"
        'These are the operator\'s words, recorded when given. Act on them; close one with fulfil_directive("<id>", '
        'reason) when it is done, or revoke_directive("<id>", reason) if the operator withdraws it.'
    )


def render_ledger(items: list[Directive]) -> str:
    """The ``/directives`` reply and the ``list_directives`` result body."""
    if not items:
        return "No directives recorded in this chat."
    live = [d for d in items if d.in_force]
    closed = [d for d in items if not d.in_force]
    parts: list[str] = []
    if live:
        parts.append("In force:\n" + "\n".join(render_entry(d) for d in live))
    else:
        parts.append("Nothing in force.")
    if closed:
        parts.append("Closed:\n" + "\n".join(render_entry(d, closed_detail=True) for d in closed))
    return "\n\n".join(parts)


_DIRECTIVES_USAGE = "Usage: /directives  (list)   ·   /directives revoke <id> [reason]"


def directives_command_reply(session_id: str, rest: str) -> str:
    """The core ``/directives`` chat command: list this chat's ledger, or revoke one by id
    (``/directives revoke <id> [reason]``) as the operator."""
    args = (rest or "").strip()
    if not session_id:
        return "/directives needs a chat — there is no session on this message."
    store = directive_store()
    if not args:
        return render_ledger(store.list(session_id))
    verb, _, tail = args.partition(" ")
    if verb.lower() not in ("revoke", "cancel"):
        return _DIRECTIVES_USAGE
    directive_id, _, reason = tail.strip().partition(" ")
    if not directive_id:
        return _DIRECTIVES_USAGE
    from observability.tracing import current_trust_tier

    if current_trust_tier() != "operator":
        return "Only the operator can revoke a directive."
    try:
        d = store.close(
            session_id,
            directive_id,
            status=STATUS_REVOKED,
            reason=reason.strip() or "revoked by the operator",
            closed_by="operator",
        )
    except DirectiveError as exc:
        return f"⚠️ {exc}"
    return f"Revoked {d.id}. The agent no longer has it as a standing directive:\n" + _indent(f'"{d.quote}"')
