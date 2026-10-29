"""Demo-agent core (#4609 part 1) — create / configure / reap throwaway demo members.

A demo member is a *real* fleet member (ADR 0041/0042) created through the SAME path the
console new-agent picker and ``POST /api/fleet`` use — :func:`ops.fleet.create_sync` →
:func:`graph.workspaces.manager.create` — with one extra registry stamp: its
``workspace.yaml`` carries ``demo: true`` + ``created_by`` + ``created_at``.

The stamp is the whole point. These ops will grow LLM tools in part 2, and a demo tool must
NEVER act on a real agent (jobCoach, protoEngineer, …). So EVERY op here calls
:func:`require_demo` FIRST, which refuses anything not stamped ``demo: true``. There is no
override flag: an unstamped member is untouchable by design, so a prompt-injected or
buggy call can delete at most a throwaway.

Two safety limits keep demos from piling up: a live-count cap (``create_demo`` refuses once
the stamped members reach ``max_live``) and a TTL reap (``reap_expired`` purges stamped
members older than ``ttl_hours``; ``create_demo`` runs it first). Both take their limit as a
param here — the Settings plumbing that feeds them arrives in part 2.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from graph.fleet.archetypes import resolve_archetype
from graph.workspaces import manager

log = logging.getLogger("protoagent.fleet.demo")

DEFAULT_MAX_LIVE = 3
DEFAULT_TTL_HOURS = 24


class DemoError(Exception):
    """A demo op was rejected."""


class NotDemoMember(DemoError):
    """The target member isn't stamped ``demo: true`` — the hard gate every op passes through
    FIRST, so a demo op can never reach a real agent. Fails closed: an unknown slug or an
    unreadable record is refused too (there's no stamp to confirm)."""


class DemoCapReached(DemoError):
    """Refused ``create_demo`` because the live demo members already reached ``max_live``."""


# ── the stamp gate ───────────────────────────────────────────────────────────


def _record_for(slug: str) -> tuple[Path, dict] | None:
    """``(workspace_path, record)`` for a member resolved by id OR display name, or ``None``
    when no such workspace exists / its record is unreadable."""
    found = manager._find(slug)
    if found is None:
        return None
    ws = Path(found["path"])
    rec = manager._read_record(ws)
    return (ws, rec) if isinstance(rec, dict) else None


def require_demo(slug: str) -> dict:
    """Return the member's workspace record, or raise :class:`NotDemoMember` unless it is
    stamped ``demo: true``. The first call in every other op — the single authoritative
    signal that a target is a throwaway demo and safe to mutate/delete."""
    resolved = _record_for(slug)
    if resolved is None:
        raise NotDemoMember(f"{slug!r}: no such fleet member — refusing (no demo stamp to confirm)")
    _, rec = resolved
    if rec.get("demo") is not True:
        raise NotDemoMember(
            f"{slug!r} is not a demo member — refusing. Demo ops only ever touch members "
            f"stamped `demo: true`; there is no override."
        )
    return rec


# ── enumerate ────────────────────────────────────────────────────────────────


def list_demos() -> list[dict]:
    """Every live (stamped) demo member — the registry row plus its demo stamp
    (``demo``/``created_by``/``created_at``). Real members are filtered out by construction."""
    out: list[dict] = []
    for w in manager.list_workspaces():
        rec = manager._read_record(Path(w["path"]))
        if isinstance(rec, dict) and rec.get("demo") is True:
            out.append(
                {
                    **w,
                    "demo": True,
                    "created_by": rec.get("created_by"),
                    "created_at": rec.get("created_at") or rec.get("created"),
                }
            )
    return out


# ── create ───────────────────────────────────────────────────────────────────


def _plugin_versions(ws_path: Path, installed: list[str]) -> dict[str, str]:
    """``{plugin_id: version}`` for the plugins a bundle install landed — the ids come from
    the create result (``installed``), the versions from the member's ``plugins.lock``.
    ``{}`` for a code-free archetype (nothing installed) or an unreadable lock."""
    if not installed:
        return {}
    import json

    lock = ws_path / "plugins.lock"
    try:
        data = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}
    want = set(installed)
    versions: dict[str, str] = {}
    for p in data.get("plugins") or []:
        pid = str(p.get("id") or "").strip()
        if pid in want:
            versions[pid] = str(p.get("version") or "")
    return versions


def _archetype_ref(archetype: str, bundle: str | None, ref: str | None) -> str:
    """A provenance string for which archetype a demo was built from: the bundle (``@ref`` when
    pinned) for a bundle archetype, else the archetype id for a code-free one."""
    if bundle:
        return f"{bundle}@{ref}" if ref else bundle
    return archetype


def _set_model(ws_path: Path, model_connection: str, model: str) -> None:
    """Point the member's config at ``<model_connection>:<model>`` (ADR 0106 — a connection
    id and a model within it). Written before the member is started so it boots on it."""
    from graph.config_io import load_yaml_doc, save_yaml_doc

    cfg = ws_path / "config" / "langgraph-config.yaml"
    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        return
    model_section = doc.setdefault("model", {})
    if isinstance(model_section, dict):
        model_section["name"] = f"{model_connection}:{model}"
        save_yaml_doc(doc, cfg)


def create_demo(
    archetype: str,
    model_connection: str,
    model: str,
    name: str | None = None,
    *,
    created_by: str,
    max_live: int = DEFAULT_MAX_LIVE,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict:
    """Create a stamped demo member from ``archetype``, point it at
    ``<model_connection>:<model>``, and start it.

    Goes through :func:`ops.fleet.create_sync` → :func:`manager.create` (the console picker's
    path), resolving ``archetype`` with the shared catalog lookup the picker reads. The
    ``demo``/``created_by``/``created_at`` stamp is persisted into ``workspace.yaml`` via
    ``extra_meta``. Default ``name`` is ``demo-<archetype>-<shortid>``.

    Expired demos are reaped FIRST, then the live-count cap is enforced: refuses with
    :class:`DemoCapReached` once the stamped members reach ``max_live``.

    Returns ``{slug, port, token, archetype_ref, plugin_versions, created_at}`` — ``token`` is
    the member's scoped bearer (the fleet service token the hub reaches it with),
    ``archetype_ref`` names the resolved bundle/archetype, and ``plugin_versions`` is the
    name→version map of what the bundle installed.
    """
    reap_expired(ttl_hours=ttl_hours)

    live = list_demos()
    if len(live) >= max_live:
        raise DemoCapReached(
            f"the demo cap is {max_live} live members and {len(live)} are already running — "
            f"delete one (or let the {ttl_hours}h TTL reap it) before creating another"
        )

    arch = resolve_archetype(archetype)
    if arch is None:
        raise DemoError(f"unknown archetype {archetype!r} — not in the catalog or any installed bundle")

    bundle = arch.get("bundle") or None
    ref = arch.get("ref") or None
    created_at = datetime.now(timezone.utc).isoformat()
    member_name = name or f"demo-{archetype}-{uuid.uuid4().hex[:6]}"

    from ops.fleet import create_sync

    out = create_sync(
        member_name,
        bundle=bundle,
        ref=ref,
        soul=arch.get("soul") or None,
        requires_tools=arch.get("requires_tools") or None,
        start=False,  # set the model BEFORE the member boots, then start it below
        extra_meta={"demo": True, "created_by": str(created_by), "created_at": created_at},
    )
    agent = out.get("agent") or {}
    slug = str(agent.get("id") or member_name)
    installed = list(out.get("installed") or [])

    resolved = _record_for(slug)
    if resolved is None:  # created but immediately unreadable — a broken create, surface it
        raise DemoError(f"demo member {slug!r} was created but its workspace is unreadable")
    ws_path, _ = resolved

    _set_model(ws_path, model_connection, model)

    from graph.fleet import supervisor
    from graph.fleet.service_token import resolve_service_token

    started = supervisor.start(slug)
    port = started.get("port") or agent.get("port")

    return {
        "slug": slug,
        "port": port,
        "token": resolve_service_token(),
        "archetype_ref": _archetype_ref(archetype, bundle, ref),
        "plugin_versions": _plugin_versions(ws_path, installed),
        "created_at": created_at,
    }


# ── configure / delete ─────────────────────────────────────────────────────────


def configure_demo(
    slug: str,
    *,
    model_connection: str | None = None,
    model: str | None = None,
    plugin_settings: dict | None = None,
) -> dict:
    """Re-point a demo member's model and/or overlay per-plugin config. Refuses a non-demo
    member (:func:`require_demo` first, so this can never edit a real agent's config).

    ``model_connection`` + ``model`` together set ``<connection>:<model>`` (both required to
    change it). ``plugin_settings`` is ``{plugin_id: {key: value}}`` — each plugin's config is
    a top-level section keyed by its id (ADR 0019); values are merged in, not clobbered. A
    running member re-reads its config on its next restart."""
    require_demo(slug)
    resolved = _record_for(slug)
    assert resolved is not None  # require_demo already confirmed it resolves
    ws_path, _ = resolved

    from graph.config_io import load_yaml_doc, save_yaml_doc

    cfg = ws_path / "config" / "langgraph-config.yaml"
    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        doc = {}
    changed: list[str] = []

    if model_connection and model:
        section = doc.setdefault("model", {})
        if isinstance(section, dict):
            section["name"] = f"{model_connection}:{model}"
            changed.append("model")

    applied_plugins: list[str] = []
    for pid, settings in (plugin_settings or {}).items():
        if not isinstance(settings, dict):
            continue
        dest = doc.setdefault(str(pid), {})
        if isinstance(dest, dict):
            dest.update(settings)
            applied_plugins.append(str(pid))
    if applied_plugins:
        changed.append("plugins")

    if changed:
        save_yaml_doc(doc, cfg)
    return {"slug": slug, "changed": changed, "plugins": applied_plugins}


def delete_demo(slug: str) -> dict:
    """Stop a demo member and purge its workspace. Refuses a non-demo member
    (:func:`require_demo` first). Reuses the shared fleet remove path (stop-then-purge), so
    delete behaves exactly as the console's purge does — only gated to demos."""
    require_demo(slug)
    from ops.fleet import remove_sync

    return remove_sync(slug, purge=True)


# ── reap ───────────────────────────────────────────────────────────────────────


def reap_expired(ttl_hours: int = DEFAULT_TTL_HOURS) -> list[str]:
    """Delete every demo member stamped more than ``ttl_hours`` ago; return the slugs reaped.

    Only stamped members are even considered (:func:`list_demos`), so a real agent is never
    at risk whatever its age. A demo whose ``created_at`` is missing or unparseable is left
    alone (fail safe — don't delete what you can't age)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    reaped: list[str] = []
    for d in list_demos():
        stamp = d.get("created_at")
        when = _parse_iso(stamp)
        if when is None or when >= cutoff:
            continue
        slug = str(d.get("id") or d.get("name") or "")
        try:
            delete_demo(slug)
            reaped.append(slug)
        except DemoError:
            # Lost the stamp between listing and deleting (a race) — never force it.
            log.warning("[fleet] demo reap skipped %s: it is no longer a demo member", slug)
        except Exception:  # noqa: BLE001 — one stuck member must not block reaping the rest
            log.warning("[fleet] demo reap failed for %s", slug, exc_info=True)
    return reaped


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 stamp into an aware UTC datetime, or ``None`` if it can't be read.
    A naive stamp is assumed UTC so the TTL comparison never crosses tz-aware/naive."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
