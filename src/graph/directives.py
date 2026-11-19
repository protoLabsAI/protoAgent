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
* A directive may carry ``links`` to the work it covers (ADR 0129 D4/Q2) — GitHub
  issue/PR refs (``owner/repo#N``), board card ids (``bd-…``), scheduler job ids, task
  ids. :meth:`DirectiveStore.resolve_link` marks a ref terminal and fulfils the directive
  automatically (``closed_by: "system"``, attributed reason) once EVERY linked item has
  finished; ``graph.directive_autofulfil`` wires the event paths and the GitHub sweep
  that call it. Unlinked directives behave exactly as before.
* A directive may be **instance-wide** (ADR 0135 D8/D13, #5542): a standing order every
  turn of the instance sees — every chat, and every scheduled or watch turn — not only
  the chat it was given in. Same store, same record shape (``instance_wide: true``);
  those rows live in one reserved ledger (:data:`INSTANCE_LEDGER`) so they render for
  every session without a scan and outlive the chat they were recorded in. Like any
  directive, one is recorded only from an operator chat turn.

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
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATUS_IN_FORCE = "in_force"
STATUS_FULFILLED = "fulfilled"
STATUS_REVOKED = "revoked"
STATUSES = (STATUS_IN_FORCE, STATUS_FULFILLED, STATUS_REVOKED)
CLOSED_BY = ("agent", "operator")
#: The auto-fulfil closer (ADR 0129 D4/Q2): every linked item finished, so the system
#: closed the directive itself. Valid on ``close()`` only — never a ``record`` author.
CLOSED_BY_SYSTEM = "system"

#: A directive is the operator's own words, quoted — a sentence or a short paragraph, not a
#: whole pasted message. Generous, but bounded: every in-force one renders every turn.
MAX_QUOTE_CHARS = 4000
MAX_NOTE_CHARS = 2000
#: In-force directives per scope rendered in ``<working_state>``; more point at ``list_directives``.
WS_DIRECTIVE_CAP = 20
#: Per-scope character budget for the ``STANDING DIRECTIVES`` entry text, in line with
#: the other ``<working_state>`` caps (``graph.projection``). The entry cap alone could still
#: let the section reach ~160K chars (20 maximum-size directives) that never shed; this bounds
#: it so a few large directives can't crowd out the rest of ``<working_state>`` every turn.
WS_DIRECTIVES_MAX_CHARS = 6000

#: A directive covers a bounded piece of work, not a backlog; the cap keeps one record
#: from carrying (and re-rendering) an unbounded ref list every turn.
MAX_LINKS = 20
MAX_LINK_CHARS = 200

#: The reserved ledger key instance-wide directives are stored under (ADR 0135 D13). ``*``
#: is not a legal session-id character (``runtime.session_ids``), and every session-keyed
#: store call refuses this key outright, so no chat — whatever its id — can read, write or
#: delete the instance ledger by naming it.
INSTANCE_LEDGER = "*instance"
#: Instance-wide ids carry a non-hex marker after ``dir-`` so one can never collide with a
#: chat directive's id (``dir-`` + hex): an id alone says which ledger holds it.
_INSTANCE_ID_PREFIX = "dir-i"
INSTANCE_TAG = "[instance-wide]"

# One lock for every store object: :func:`directive_store` may hand out a fresh one per call.
_LOCK = threading.RLock()

# ── linked work (ADR 0129 D4/Q2) ─────────────────────────────────────────────────

_GH_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+$")
_GH_URL_RE = re.compile(r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)/?$", re.I)


def parse_github_ref(ref: str) -> tuple[str, str, str] | None:
    """``(owner, repo, number)`` for a GitHub issue/PR ref — ``owner/repo#N`` or an
    issue/PR URL — else ``None``. The sweep uses it to ask the Issues API (which
    answers for PRs too; a merged PR reads ``closed`` there)."""
    ref = (ref or "").strip()
    if _GH_REF_RE.match(ref):
        repo_part, _, number = ref.partition("#")
        owner, _, repo = repo_part.partition("/")
        return owner, repo, number
    hit = _GH_URL_RE.match(ref)
    return (hit.group(1), hit.group(2), hit.group(3)) if hit else None


def _link_kind(ref: str) -> str:
    """Classify a link ref by shape: board card (``bd-…``), task (``task-…``), GitHub
    issue/PR ref or URL, else a scheduler job id."""
    if ref.startswith("bd-"):
        return "card"
    if ref.startswith("task-"):
        return "task"
    if parse_github_ref(ref):
        return "github"
    return "job"


def normalize_links(links) -> list[dict]:
    """Validated, deduped link entries (``{ref, kind, state, reason}``) for ``record``.

    Accepts a list/tuple of refs (a plain string is split on commas/whitespace, the way
    an agent pastes them). Raises :class:`DirectiveError` on a ref the auto-fulfil paths
    could never resolve — a bare ``#N`` (no repo) or a URL that isn't a GitHub issue/PR —
    instead of storing a link that would silently keep the directive in force forever."""
    if links is None:
        return []
    if isinstance(links, str):
        links = [part for part in re.split(r"[,\s]+", links) if part]
    if not isinstance(links, (list, tuple)):
        raise DirectiveError("links must be a list of refs (owner/repo#N, bd-… card, job or task ids)")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in links:
        ref = str(raw or "").strip().rstrip(",")
        if not ref or ref in seen:
            continue
        if len(ref) > MAX_LINK_CHARS:
            raise DirectiveError(f"link ref is {len(ref)} characters (max {MAX_LINK_CHARS})")
        if re.fullmatch(r"#\d+", ref):
            raise DirectiveError(f"GitHub ref {ref!r} names no repository — qualify it as owner/repo{ref}")
        if ref.startswith("dir-"):
            raise DirectiveError(
                f"{ref!r} is a directive, not work — link the issues, PRs, cards, tasks or jobs it covers"
            )
        if ref.lower().startswith(("http://", "https://")) and not parse_github_ref(ref):
            raise DirectiveError(f"link URL {ref!r} is not a GitHub issue/PR URL — link owner/repo#N instead")
        seen.add(ref)
        out.append({"ref": ref, "kind": _link_kind(ref), "state": "open", "reason": ""})
    if len(out) > MAX_LINKS:
        raise DirectiveError(f"{len(out)} links (max {MAX_LINKS}) — link the work this directive covers, not a backlog")
    return out


def _parse_links(value) -> list[dict]:
    """The stored ``links`` entries off a ledger row — tolerant on purpose: a legacy or
    hand-edited file must never break a read path."""
    if not isinstance(value, list):
        return []
    out = []
    for entry in value:
        if isinstance(entry, dict) and entry.get("ref"):
            out.append(
                {
                    "ref": str(entry["ref"]),
                    "kind": str(entry.get("kind") or _link_kind(str(entry["ref"]))),
                    "state": "done" if entry.get("state") == "done" else "open",
                    "reason": str(entry.get("reason") or ""),
                }
            )
    return out


def _links_reason(links: list[dict]) -> str:
    """The attributed close reason an all-links-done directive carries —
    "acme/widgets#879 closed; task-3 closed"."""
    return "; ".join(f"{e['ref']} {e.get('reason') or 'done'}" for e in links)

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


def _announce(d: Directive) -> None:
    """Publish ``directive.changed`` for one directive. An instance-wide one says so — its
    ``session_id`` is only the chat it was recorded in. (Two literal payloads on purpose:
    the generated events reference reads the keys off the call.)"""
    if d.instance_wide:
        _publish("directive.changed", {"session_id": d.session_id, "id": d.id, "instance_wide": True})
    else:
        _publish("directive.changed", {"session_id": d.session_id, "id": d.id})


def _not_found(directive_id: str, instance_wide: bool) -> str:
    if instance_wide:
        return f"no instance-wide directive {directive_id!r}"
    return f"no directive {directive_id!r} in this chat"


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
    author: str = "agent"
    #: Linked work (ADR 0129 D4/Q2): ``[{ref, kind, state, reason}, …]`` — empty for the
    #: (unchanged) unlinked case.
    links: list[dict] = field(default_factory=list)
    #: True for an instance-wide standing order (ADR 0135 D13): in force for every chat and
    #: every unattended turn. ``session_id`` is then the chat it was recorded in
    #: (provenance only). Set at ``record`` and never changed afterwards.
    instance_wide: bool = False

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
            author=str(data.get("author") or "agent"),
            links=_parse_links(data.get("links")),
            instance_wide=data.get("instance_wide") is True,
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

    @staticmethod
    def _key(session_id: str, instance_wide: bool = False) -> str:
        """The ledger a call addresses: the reserved instance ledger when ``instance_wide``,
        else the chat's own — and "" (no ledger) for a blank id or a "chat" that names the
        reserved key, so a session id can never reach the instance ledger."""
        if instance_wide:
            return INSTANCE_LEDGER
        session_id = session_id or ""
        return "" if session_id == INSTANCE_LEDGER else session_id

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
                directive = Directive.from_dict(row)
                # Which ledger a row sits in decides its reach — never a stored flag alone,
                # so a hand-edited chat file can't widen one of its rows.
                directive.instance_wide = session_id == INSTANCE_LEDGER
                if not directive.instance_wide and not directive.session_id:
                    directive.session_id = session_id  # a legacy row without its own copy
                out.append(directive)
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

    def record(self, session_id: str, quote: str, *, conditions: str = "", scope: str = "", author: str = "agent",
               links=None, instance_wide: bool = False) -> Directive:
        """Record a new in-force directive for ``session_id``. ``quote`` is the operator's
        words verbatim for operator writes; legacy agent writes trim the ends. ``links``
        (ADR 0129 D4/Q2) are the refs of the work the directive covers — the directive
        fulfils itself once every linked item finishes (:meth:`resolve_link`).

        ``instance_wide`` (ADR 0135 D13) records it in the instance ledger instead: in force
        for every chat and unattended turn. ``session_id`` is still required — the chat the
        operator gave it in — and is kept as provenance. The store does not decide who may
        record one; the caller does (``tools.directive_tools``: an operator chat turn only)."""
        session_id = (session_id or "") if author == "operator" else (session_id or "").strip()
        if not session_id.strip():
            raise DirectiveError("a directive belongs to a chat — no session id was given")
        if session_id == INSTANCE_LEDGER:
            raise DirectiveError(f"{INSTANCE_LEDGER!r} is reserved — it is not a chat")
        key = self._key(session_id, instance_wide)
        if author not in CLOSED_BY:
            raise DirectiveError("author must be agent or operator")
        quote = (quote or "") if author == "operator" else (quote or "").strip()
        if not quote.strip():
            raise DirectiveError("quote is required — the operator's exact words")
        if len(quote) > MAX_QUOTE_CHARS:
            raise DirectiveError(
                f"quote is {len(quote)} characters (max {MAX_QUOTE_CHARS}) — quote the directive itself, "
                "not the whole message"
            )
        conditions = (conditions or "").strip()[:MAX_NOTE_CHARS]
        scope = (scope or "").strip()[:MAX_NOTE_CHARS]
        link_entries = normalize_links(links)
        prefix = _INSTANCE_ID_PREFIX if instance_wide else "dir-"
        with self._lock:
            items = self._load(key)
            taken = {d.id for d in items}
            new_id = f"{prefix}{secrets.token_hex(3)}"
            while new_id in taken:
                new_id = f"{prefix}{secrets.token_hex(3)}"
            directive = Directive(
                id=new_id,
                session_id=session_id,
                quote=quote,
                conditions=conditions,
                scope=scope,
                created_at=_now(),
                author=author,
                links=link_entries,
                instance_wide=bool(instance_wide),
            )
            items.append(directive)
            self._save(key, items)
        _announce(directive)
        return directive

    def list(self, session_id: str, *, include_closed: bool = True, instance_wide: bool = False) -> list[Directive]:
        """This chat's directives, oldest first. ``include_closed=False`` → in force only.
        ``instance_wide=True`` reads the instance ledger instead (``session_id`` is ignored)."""
        key = self._key(session_id, instance_wide)
        if not key:
            return []
        with self._lock:
            items = self._load(key)
        return items if include_closed else [d for d in items if d.in_force]

    def in_force(self, session_id: str, *, instance_wide: bool = False) -> list[Directive]:
        return self.list(session_id, include_closed=False, instance_wide=instance_wide)

    def get(self, session_id: str, directive_id: str, *, instance_wide: bool = False) -> Directive | None:
        directive_id = (directive_id or "").strip()
        return next((d for d in self.list(session_id, instance_wide=instance_wide) if d.id == directive_id), None)

    def update(self, session_id: str, directive_id: str, *, scope: str | None = None,
               conditions: str | None = None, instance_wide: bool = False) -> Directive:
        """Patch editable fields under the same lock as close; preserve provenance. A
        directive's reach is not editable: nothing here moves one between a chat's ledger
        and the instance ledger."""
        key = self._key(session_id, instance_wide)
        with self._lock:
            items = self._load(key) if key else []
            hit = next((d for d in items if d.id == directive_id), None)
            if hit is None:
                raise DirectiveNotFound(_not_found(directive_id, instance_wide))
            if not hit.in_force:
                raise DirectiveClosed(f"directive {directive_id} is already {hit.status}")
            if scope is not None:
                hit.scope = scope.strip()[:MAX_NOTE_CHARS]
            if conditions is not None:
                hit.conditions = conditions.strip()[:MAX_NOTE_CHARS]
            self._save(key, items)
        _announce(hit)
        return hit

    def close(
        self,
        session_id: str,
        directive_id: str,
        *,
        status: str,
        reason: str = "",
        closed_by: str = "agent",
        instance_wide: bool = False,
    ) -> Directive:
        """Mark an in-force directive ``fulfilled`` or ``revoked``. Raises
        :class:`DirectiveNotFound` for an id this chat doesn't hold (another chat's id
        included) and :class:`DirectiveClosed` when it is already closed. ``closed_by``
        may also be :data:`CLOSED_BY_SYSTEM` — the auto-fulfil path (ADR 0129 D4/Q2).
        ``instance_wide=True`` closes one in the instance ledger (``session_id`` is ignored)."""
        if status not in (STATUS_FULFILLED, STATUS_REVOKED):
            raise DirectiveError(f"status must be {STATUS_FULFILLED!r} or {STATUS_REVOKED!r}")
        if closed_by not in (*CLOSED_BY, CLOSED_BY_SYSTEM):
            raise DirectiveError(f"closed_by must be one of {(*CLOSED_BY, CLOSED_BY_SYSTEM)}")
        directive_id = (directive_id or "").strip()
        key = self._key(session_id, instance_wide)
        with self._lock:
            items = self._load(key) if key else []
            hit = next((d for d in items if d.id == directive_id), None)
            if hit is None:
                raise DirectiveNotFound(_not_found(directive_id, instance_wide))
            if not hit.in_force:
                raise DirectiveClosed(f"directive {directive_id} is already {hit.status}")
            hit.status = status
            hit.closed_at = _now()
            hit.close_reason = (reason or "").strip()[:MAX_NOTE_CHARS] or None
            hit.closed_by = closed_by
            self._save(key, items)
        _announce(hit)
        return hit

    # ── linked-work auto-fulfil (ADR 0129 D4/Q2) ─────────────────────────────────

    def _ledger_session_ids(self, ref: str | None = None) -> list[str]:
        """Every ledger key with a file under this store — each chat's session id, plus
        :data:`INSTANCE_LEDGER` once an instance-wide directive exists (for the cross-chat
        link paths — an event names a ref, not a chat). Skips unreadable files and any file
        that doesn't sit where its own session id hashes (the ``_load`` ownership guard).
        With ``ref``, pre-filter the loaded JSON for in-force rows linking it. Scan
        outside the lock; writers must reload candidates under the lock before editing."""
        if not self._base.is_dir():
            return []
        out: list[str] = []
        for path in sorted(self._base.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            session_id = str(data.get("session_id") or "") if isinstance(data, dict) else ""
            if session_id and self._path(session_id) == path:
                if ref is not None and not any(
                    isinstance(row, dict)
                    and (row.get("status") or STATUS_IN_FORCE) == STATUS_IN_FORCE
                    and any(entry["ref"] == ref for entry in _parse_links(row.get("links")))
                    for row in data.get("directives") or []
                ):
                    continue
                out.append(session_id)
        return out

    def linked_in_force(self) -> list[Directive]:
        """Every in-force directive (any chat) that carries links — the periodic GitHub
        sweep's work list."""
        out: list[Directive] = []
        for session_id in self._ledger_session_ids():
            out.extend(d for d in self._load(session_id) if d.in_force and d.links)
        return out

    def resolve_link(self, ref: str, reason: str = "") -> list[Directive]:
        """Mark ``ref`` terminal on every in-force directive (any chat) that links it, and
        fulfil each directive whose links are now ALL done — ``closed_by: "system"`` with
        an attributed reason ("acme/widgets#879 closed; task-3 closed"). Returns the
        directives touched (resolved a link, fulfilled or not). Directives without links
        never match, so unlinked ones are untouched by construction."""
        ref = (ref or "").strip()
        if not ref:
            return []
        reason = (reason or "").strip()
        touched: list[Directive] = []
        for session_id in self._ledger_session_ids(ref):
            with self._lock:
                items = self._load(session_id)
                changed = False
                for d in items:
                    if not d.in_force:
                        continue
                    hit = False
                    for entry in d.links:
                        if entry.get("ref") == ref and entry.get("state") != "done":
                            entry["state"] = "done"
                            entry["reason"] = reason or "done"
                            hit = True
                    if not hit:
                        continue
                    changed = True
                    if all(e.get("state") == "done" for e in d.links):
                        d.status = STATUS_FULFILLED
                        d.closed_at = _now()
                        d.close_reason = _links_reason(d.links)[:MAX_NOTE_CHARS]
                        d.closed_by = CLOSED_BY_SYSTEM
                    touched.append(d)
                if changed:
                    self._save(session_id, items)
        for d in touched:
            _announce(d)
        return touched

    def forget(self, session_id: str) -> bool:
        """Drop a chat's whole ledger (the chat was deleted). True when a file was removed.
        Instance-wide directives are not the chat's: they stay, even ones recorded in it."""
        if not self._key(session_id):
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
    if d.instance_wide:
        head += f" {INSTANCE_TAG}"
    if closed_detail:
        head += f" [{d.status}]"
    head += f" (recorded {_when(d.created_at)}):"
    lines = [head, _indent(f'"{d.quote}"')]
    if d.conditions:
        lines.append(_indent(f"Conditions: {d.conditions}"))
    if d.scope:
        lines.append(_indent(f"Scope: {d.scope}"))
    if d.links:
        refs = ", ".join(e["ref"] + (" [done]" if e.get("state") == "done" else "") for e in d.links)
        suffix = "" if not d.in_force else " — fulfils itself when all of them finish"
        lines.append(_indent(f"Linked work: {refs}{suffix}"))
    if closed_detail and not d.in_force:
        by = f" by the {d.closed_by}" if d.closed_by else ""
        why = f": {d.close_reason}" if d.close_reason else ""
        lines.append(_indent(f"{d.status.capitalize()}{by} {_when(d.closed_at)}{why}"))
    return "\n".join(lines)


def _working_state_entries(items: list[Directive]) -> str:
    """Render one scope with its own entry and character allowance."""
    capped = items[:WS_DIRECTIVE_CAP]
    rendered: list[str] = []
    used = 0
    budget_stop = False
    for d in capped:
        entry = render_entry(d)
        if not rendered and len(entry) > WS_DIRECTIVES_MAX_CHARS:
            # The first entry always renders; clip it so one giant directive can't blow the budget.
            dropped = len(entry) - WS_DIRECTIVES_MAX_CHARS
            rendered.append(entry[:WS_DIRECTIVES_MAX_CHARS] + f" …[clipped, {dropped} more chars]")
            budget_stop = True
            break
        sep = 1 if rendered else 0  # the "\n" the join inserts before this entry
        if rendered and used + sep + len(entry) > WS_DIRECTIVES_MAX_CHARS:
            budget_stop = True
            break
        rendered.append(entry)
        used += sep + len(entry)
    body = "\n".join(rendered)
    if budget_stop and len(items) > len(rendered):
        body += f"\n…+{len(items) - len(rendered)} more (/directives to list them)"
    elif len(items) > len(capped):
        body += f"\n…+{len(items) - len(capped)} more (list_directives shows them all)"
    return body


def working_state_section(session_id: str) -> str:
    """The ``STANDING DIRECTIVES`` section of ``<working_state>`` for this chat, or "".

    Instance-wide directives (ADR 0135 D13) render for EVERY session — another chat, a
    scheduled or watch turn, a turn with no session id at all — ahead of the session's
    own, tagged :data:`INSTANCE_TAG`.

    Each scope gets its own :data:`WS_DIRECTIVE_CAP` entries and
    :data:`WS_DIRECTIVES_MAX_CHARS` characters of entry text, so instance orders cannot
    displace the chat's existing allowance (and vice versa). The combined body is bounded
    by twice those limits, plus clipping and overflow markers. Within each scope, whole
    entries render oldest first until the next would exceed the budget; the first always
    renders, clipped with an explicit marker if needed. Overflow ends with a ``…+N more``
    pointer to the full ledger.
    """
    store = directive_store()
    shared = store.in_force("", instance_wide=True)
    local = store.in_force(session_id) if session_id else []
    if not shared and not local:
        return ""
    body = "\n".join(_working_state_entries(items) for items in (shared, local) if items)
    return (
        f"{WS_HEADING}:\n{body}\n"
        'These are the operator\'s words, recorded when given. Act on them; close one with fulfil_directive("<id>", '
        'reason) when it is done, or revoke_directive("<id>", reason) if the operator withdraws it.'
        + (
            f" Entries marked {INSTANCE_TAG} apply to every chat and every unattended turn, this one included; "
            "they are closed only in a chat turn with the operator, or by the operator."
            if shared
            else ""
        )
    )


def render_ledger(items: list[Directive], instance_items: list[Directive] | None = None) -> str:
    """The ``/directives`` reply and the ``list_directives`` result body: this chat's
    ledger, then any instance-wide directives (``instance_items``) under their own heading."""
    shared = list(instance_items or [])
    if not items and not shared:
        return "No directives recorded in this chat."
    parts: list[str] = []
    if items:
        live = [d for d in items if d.in_force]
        closed = [d for d in items if not d.in_force]
        if live:
            parts.append("In force:\n" + "\n".join(render_entry(d) for d in live))
        else:
            parts.append("Nothing in force.")
        if closed:
            parts.append("Closed:\n" + "\n".join(render_entry(d, closed_detail=True) for d in closed))
    else:
        parts.append("No directives of this chat's own to show.")
    live = [d for d in shared if d.in_force]
    closed = [d for d in shared if not d.in_force]
    if live:
        parts.append(
            "Instance-wide, in force (every chat and every unattended turn):\n"
            + "\n".join(render_entry(d) for d in live)
        )
    if closed:
        parts.append("Instance-wide, closed:\n" + "\n".join(render_entry(d, closed_detail=True) for d in closed))
    return "\n\n".join(parts)


def withdrawal_note(quote: str, reason: str = "") -> str:
    """The operator-withdrawal note the DELETE route appends to the chat transcript
    (ADR 0127 D3, #4861): ``You withdrew: "<first line of quote>"``, plus `` — <reason>``
    when the operator gave a reason.

    Only the first line of ``quote`` is shown — a directive can be a short paragraph and the
    ledger already holds the whole thing. ``reason`` is the operator's own words: the caller
    passes the raw query param, not the store's default ``"revoked by the operator"``, so a
    reasonless withdraw shows no suffix. Both the first line and the reason are clipped to
    :data:`MAX_NOTE_CHARS` with an explicit ``…`` so one very long line can't bloat a note
    that compaction then carries verbatim every turn."""

    def _clip(text: str) -> str:
        return text[:MAX_NOTE_CHARS].rstrip() + "…" if len(text) > MAX_NOTE_CHARS else text

    lines = (quote or "").strip().splitlines()
    head = _clip(lines[0]) if lines else ""
    note = f'You withdrew: "{head}"'
    reason = (reason or "").strip()
    if reason:
        note += f" — {_clip(reason)}"
    return note


_DIRECTIVES_USAGE = "Usage: /directives  (list)   ·   /directives revoke <id> [reason]"


def directives_command_reply(session_id: str, rest: str) -> str:
    """The core ``/directives`` chat command: list this chat's ledger plus the
    instance-wide directives, or revoke one of either by id
    (``/directives revoke <id> [reason]``) as the operator."""
    args = (rest or "").strip()
    if not session_id:
        return "/directives needs a chat — there is no session on this message."
    store = directive_store()
    if not args:
        return render_ledger(store.list(session_id), store.list("", instance_wide=True))
    verb, _, tail = args.partition(" ")
    if verb.lower() not in ("revoke", "cancel"):
        return _DIRECTIVES_USAGE
    directive_id, _, reason = tail.strip().partition(" ")
    if not directive_id:
        return _DIRECTIVES_USAGE
    from observability.tracing import current_trust_tier

    if current_trust_tier() != "operator":
        return "Only the operator can revoke a directive."
    # An id names one ledger (instance-wide ids never collide with a chat's): this chat's
    # first, else the instance ledger — so an instance-wide order is withdrawn from any chat.
    instance_wide = store.get(session_id, directive_id) is None and (
        store.get("", directive_id, instance_wide=True) is not None
    )
    try:
        d = store.close(
            session_id,
            directive_id,
            status=STATUS_REVOKED,
            reason=reason.strip() or "revoked by the operator",
            closed_by="operator",
            instance_wide=instance_wide,
        )
    except DirectiveError as exc:
        return f"⚠️ {exc}"
    if instance_wide:
        return f"Revoked {d.id}. It is no longer in force in any chat or unattended turn:\n" + _indent(f'"{d.quote}"')
    return f"Revoked {d.id}. The agent no longer has it as a standing directive:\n" + _indent(f'"{d.quote}"')
