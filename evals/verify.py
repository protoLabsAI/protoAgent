"""Side-effect verifiers for eval cases.

Two channels:

- **Audit log** — JSONL written by ``AuditMiddleware`` at
  ``/sandbox/audit/audit.jsonl`` (override with ``AUDIT_PATH`` env).
  ``audit_entries_since`` returns entries newer than a marker, and
  ``assert_tools_fired`` confirms a tool name appears with the
  expected outcome.
- **Knowledge store** — sqlite DB at ``KNOWLEDGE_DB_PATH`` (or the
  template default). ``find_chunk_containing`` confirms a memory
  write actually landed; ``setup_chunk`` / ``teardown`` mutate the
  store directly so cases start from a known state.

The store is opened read/write so setup steps can pre-seed (BFCL's
``initial_config`` pattern). The model never sees these direct writes
— it discovers them via ``memory_recall`` / ``memory_list`` tools as
real users would.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── path resolution ─────────────────────────────────────────────────────────


def _audit_path() -> Path:
    """Audit JSONL location. Falls back to the template's docker default."""
    raw = os.environ.get("AUDIT_PATH") or "/sandbox/audit/audit.jsonl"
    p = Path(raw).expanduser()
    if p.is_file():
        return p
    # Local-dev fallback: same shape, but under the home dir.
    fallback = Path.home() / ".protoagent" / "audit" / "audit.jsonl"
    return fallback


def _kb_store():
    """Construct a ``KnowledgeStore`` against the configured path.

    Imported lazily so ``evals/verify.py`` can be loaded in a context
    where ``knowledge/`` isn't on sys.path yet (the runner adjusts
    sys.path before calling in).
    """
    from knowledge import KnowledgeStore
    return KnowledgeStore()  # honors KNOWLEDGE_DB_PATH env


# ── audit log ───────────────────────────────────────────────────────────────


def audit_now() -> str:
    """ISO-8601 marker suitable as a 'since' input to ``audit_entries_since``."""
    return datetime.now(timezone.utc).isoformat()


def audit_entries_since(ts_iso: str) -> list[dict]:
    """Return audit-log entries with ``ts`` strictly greater than ``ts_iso``."""
    p = _audit_path()
    if not p.is_file():
        return []
    out: list[dict] = []
    with p.open() as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("ts", "") > ts_iso:
                out.append(entry)
    return out


def assert_tools_fired(
    audit_entries: list[dict],
    expected: list[str],
    *,
    require_success: bool = True,
) -> tuple[bool, str]:
    """Confirm each expected tool name appears in audit entries.

    Order doesn't matter — a tool that fires twice still satisfies one
    expected entry, and extra entries (subset matching, BFCL-style) are
    allowed.

    ``require_success=True`` (default) only counts ``success=True``
    entries — use this for happy-path cases. Pass ``require_success=False``
    when the case represents an error path that the agent should still
    *attempt* (e.g. fetching a private URL the agent has no creds for).
    """
    fired: dict[str, dict[str, int]] = {}
    for e in audit_entries:
        bucket = fired.setdefault(e.get("tool", "?"), {"ok": 0, "err": 0})
        bucket["ok" if e.get("success") else "err"] += 1

    missing: list[str] = []
    for t in expected:
        if t not in fired:
            missing.append(t)
            continue
        if require_success and fired[t]["ok"] == 0:
            missing.append(f"{t} (only errors)")

    if missing:
        return False, f"missing tools: {missing}; saw: {dict(fired)}"
    return True, f"saw: {dict(fired)}"


# ── knowledge store ─────────────────────────────────────────────────────────


def find_chunk_containing(text: str, *, domain: str | None = None) -> dict | None:
    store = _kb_store()
    chunk = store.find_chunk_containing(text, domain=domain)
    return chunk.as_dict() if chunk else None


def chunks_in_domain(domain: str, *, limit: int = 50) -> list[dict]:
    store = _kb_store()
    return [c.as_dict() for c in store.list_chunks(domain=domain, limit=limit)]


# ── setup / teardown helpers ─────────────────────────────────────────────────


def apply_setup(steps: list[dict]) -> str | None:
    """Apply a list of setup steps. Each step is a dict with one key.

    Supported step kinds:

    - ``kb_ingest``: ``{content, domain, heading?}``

    Returns ``None`` on success, an error string on first failure.
    """
    store = _kb_store()
    for step in steps:
        for kind, args in step.items():
            if kind == "kb_ingest":
                if store.add_chunk(
                    args["content"],
                    domain=args.get("domain", "general"),
                    heading=args.get("heading"),
                ) is None:
                    return f"kb_ingest failed for {args!r}"
            else:
                return f"unknown setup step: {kind}"
    return None


def apply_teardown(steps: list[dict]) -> None:
    """Best-effort teardown. Never raises so a setup failure or assertion
    failure doesn't poison subsequent cases.

    Supported step kinds:

    - ``kb_delete_by_content``: ``{contains}``
    - ``kb_delete_by_heading``: ``{domain, heading}``
    """
    store = _kb_store()
    for step in steps:
        for kind, args in step.items():
            try:
                if kind == "kb_delete_by_content":
                    store.delete_by_content(args["contains"])
                elif kind == "kb_delete_by_heading":
                    store.delete_by_heading(args["domain"], args["heading"])
            except Exception as exc:  # pragma: no cover
                log.debug("[verify] teardown step %s failed: %s", kind, exc)
