"""Provider registry CRUD (ADR 0106).

Its own module and its own routes rather than fields in the generic settings schema:
that schema is a flat map of dotted keys with a scalar type each, and a registry is a
LIST OF OBJECTS. Delegates and plugins already earned their own surfaces for the same
reason.

Two invariants the routes exist to enforce, neither of which a generic settings form
could express:

* **Ids are immutable.** An id appears inside stored model values
  (`prod-gateway:protolabs/reasoning`), and via the fleet host layer such a value can
  live in ANOTHER instance's config that this process cannot reach. So `PATCH` edits
  the label, endpoint and key; changing an id means remove-and-re-add, deliberately.
* **A connection in use cannot be deleted silently.** Removing one that a model slot
  names would leave that slot resolving to a bare model id against whatever remains —
  the failure mode is a wrong-provider call, not an error. So DELETE refuses and names
  the slots, and the operator repoints them first.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from fastapi import HTTPException
from pydantic import BaseModel

from runtime.state import STATE

log = logging.getLogger("protoagent.operator_api.providers")

# Slots whose value may name a provider. Read from the live config to answer "is this
# connection in use?" — the question DELETE has to get right.
_SLOT_ATTRS = (
    ("model.name", "model_name"),
    ("routing.aux_model", "aux_model"),
    ("compaction.model", "compaction_model"),
    ("goal.eval_model", "goal_eval_model"),
    ("soul.drift_judge_model", "soul_drift_judge_model"),
)

# A reported slot key whose live SETTINGS key differs from the flat label the walk uses.
# Only the drift judge: it is read from `soul.drift.judge.model`, but the referencing walk
# and the `in_use_by` strings have always labelled it `soul.drift_judge_model`. A release
# writes through the settings key so `apply_settings` addresses the right node.
_SLOT_WRITE_KEYS = {"soul.drift_judge_model": "soul.drift.judge.model"}


class ProviderCreate(BaseModel):
    id: str
    type: str = "openai-compat"
    label: str = ""
    base_url: str = ""
    api_key: str = ""


class ProviderPatch(BaseModel):
    # No `id` and no `type`: both are identity, and identity is frozen once a slot may
    # reference it. A blank api_key means "leave the stored one alone", so the console
    # never has to round-trip a secret it was never shown.
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ProviderDelete(BaseModel):
    # Optional DELETE body: the referencing slots the operator has chosen to resolve
    # in the same transaction that removes the connection. Each key is a slot key the
    # structured walk reported for this connection (`model.name`, `routing.aux_model`,
    # `model.favorites`, `subagents.<name>.model`, …); the value is `<other_pid>:<model>`
    # to repoint, or `null` to clear. An empty body (the query-only call) keeps the old
    # refuse-if-in-use behaviour verbatim.
    releases: dict[str, str | None] = {}


def _config():
    if STATE.graph_config is None:
        raise HTTPException(status_code=503, detail="config not loaded")
    return STATE.graph_config


def _referencing_view(cfg) -> dict:
    """Everything the referencing walk reads, copied out as plain, mutable data — so a
    DELETE can apply its `releases` in memory and re-run the walk without ever touching
    the live config. Subagent tiers are typed fields (`cfg.researcher.model`), so read
    them through the registry the coherence checker already uses rather than a `subagents`
    dict the config never exposes."""
    from graph.config import _SUBAGENT_MODEL_SLOTS

    return {
        "slots": {label: str(getattr(cfg, attr, "") or "") for label, attr in _SLOT_ATTRS},
        "favorites": [str(f) for f in (getattr(cfg, "model_favorites", []) or [])],
        "subagents": {
            name: str(getattr(getattr(cfg, name, None), "model", "") or "") for name in _SUBAGENT_MODEL_SLOTS
        },
        "model_provider": str(getattr(cfg, "model_provider", "") or "").lower(),
    }


def _walk_references(view: dict, pid: str) -> tuple[list[str], list[dict]]:
    """ONE walk over `view` → (the `in_use_by` display strings, the structured entries).
    Both describe the SAME dependencies from the same pass, so they can never drift.

    A structured entry is ``{"key", "value", "kind", "clearable"}``. ``model.name`` is
    never clearable (the lead model must always resolve); every other slot is. Favorites
    collapse to a SINGLE entry whose ``value`` is the list of matching favorites, while the
    display strings still list one per favorite (the shape the 409 detail has always used).
    """
    strings: list[str] = []
    entries: list[dict] = []
    for label, _attr in _SLOT_ATTRS:
        value = view["slots"][label]
        if value.lower().startswith(f"{pid}:"):
            strings.append(f"{label}={value}")
            entries.append({"key": label, "value": value, "kind": "slot", "clearable": label != "model.name"})
        elif (
            label == "model.name"
            and pid == "gateway"
            and value
            and ":" not in value
            and view["model_provider"] not in {"anthropic-oauth", "openai-codex"}
        ):
            # A pre-registry config's bare lead model implicitly runs through the migrated
            # `gateway` connection. Treat that dependency exactly like the qualified
            # `gateway:<model>` form; otherwise Settings can claim there are no connections
            # while the legacy gateway is still serving the agent.
            strings.append(f"{label}={value} (implicit gateway)")
            entries.append({"key": label, "value": value, "kind": "slot", "clearable": False})
    matched_favs = [f for f in view["favorites"] if f.lower().startswith(f"{pid}:")]
    for fav in matched_favs:
        strings.append(f"model.favorites={fav}")
    if matched_favs:
        entries.append({"key": "model.favorites", "value": matched_favs, "kind": "favorite", "clearable": True})
    for name, value in view["subagents"].items():
        if value.lower().startswith(f"{pid}:"):
            strings.append(f"subagents.{name}.model={value}")
            entries.append(
                {"key": f"subagents.{name}.model", "value": value, "kind": "subagent", "clearable": True}
            )
    return strings, entries


def _referencing_slots(cfg, pid: str) -> list[str]:
    """Dotted slot names whose value routes through `pid`, plus favorites (display form)."""
    return _walk_references(_referencing_view(cfg), pid)[0]


def _referencing_entries(cfg, pid: str) -> list[dict]:
    """The same dependencies as `_referencing_slots`, structured for the UI to act on."""
    return _walk_references(_referencing_view(cfg), pid)[1]


def _nest_updates(dotted: dict) -> dict:
    """Fold dotted settings keys (`model.name`, `subagents.researcher.model`,
    `soul.drift.judge.model`) into the nested section dict `apply_settings` /
    `apply_updates_to_yaml` understand."""
    out: dict = {}
    for key, val in dotted.items():
        parts = key.split(".")
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = val
    return out


def _write_providers(
    entries: list[dict], secret_updates: dict[str, str], extra_updates: dict | None = None
) -> None:
    """Persist the registry to YAML, routing keys to the secrets overlay.

    Same split model.api_key has always used: the key never lands in the config file,
    which gets exported, backed up and forked.

    `extra_updates` (a nested settings dict) is folded into the SAME document before the
    single save — so removing a connection and repointing/clearing the slots that named
    it land in one atomic write, not two.
    """
    from graph.config_io import apply_updates_to_yaml, load_yaml_doc, save_secrets, save_yaml_doc

    doc = load_yaml_doc()
    doc["providers"] = entries
    if extra_updates:
        apply_updates_to_yaml(doc, extra_updates)
    save_yaml_doc(doc)
    if secret_updates:
        save_secrets({"providers": secret_updates})


async def _apply_providers(
    entries: list[dict], secret_updates: dict[str, str], extra_updates: dict | None = None
) -> None:
    """Persist the registry and make it the live registry before returning.

    The server wires ``HOST.apply_settings`` to the transactional config writer: it
    validates, persists, rebuilds, and rolls back on a failed rebuild. Route-only unit
    tests have no host, so they keep using the narrow disk-writer seam they already
    exercise.

    `extra_updates` carries extra DOTTED settings keys (`model.name`,
    `subagents.researcher.model`, …) to write in the SAME transaction as the provider
    list — nested here into the section shape both writers consume, so a failed rebuild
    rolls back the released slots along with the removed connection.
    """
    from graph.plugins.host import HOST

    nested = _nest_updates(extra_updates or {})
    if HOST.apply_settings is None:
        await asyncio.to_thread(_write_providers, entries, secret_updates, nested)
        return

    updates = [dict(entry) for entry in entries]
    for entry in updates:
        secret = secret_updates.get(str(entry.get("id", "")))
        if secret:
            entry["api_key"] = secret
    ok, messages = await asyncio.to_thread(HOST.apply_settings, {"providers": updates, **nested})
    if not ok:
        raise HTTPException(status_code=400, detail=" · ".join(messages or ["connection update failed"]))


def _entries_from_config(cfg) -> list[dict]:
    return [p.as_dict() for p in cfg.providers]


def _remaining_after_releases(cfg, pid: str, releases: dict) -> list[str]:
    """Apply `releases` to a copy of the referencing view, then re-walk: the display
    strings that SURVIVE are the references the operator still hasn't resolved. Never
    mutates the live config."""
    import copy

    sim = copy.deepcopy(_referencing_view(cfg))
    for key, target in releases.items():
        if key == "model.favorites":
            sim["favorites"] = [f for f in sim["favorites"] if not f.lower().startswith(f"{pid}:")]
        elif key.startswith("subagents.") and key.endswith(".model"):
            name = key[len("subagents.") : -len(".model")]
            sim["subagents"][name] = "" if target is None else str(target)
        elif key in sim["slots"]:
            sim["slots"][key] = "" if target is None else str(target)
    return _walk_references(sim, pid)[0]


def _bad_target_detail(cfg, pid: str, key: str, target) -> str:
    """The precise 400 for a repoint target that didn't resolve to another registered
    connection — malformed, self-referential, or naming a provider that isn't here."""
    raw = str(target or "")
    prefix, sep, rest = raw.partition(":")
    prefix = prefix.strip().lower()
    if not sep or not prefix or not rest.strip():
        return f"{key}: {target!r} must be a <provider>:<model> target (e.g. 'local-vllm:qwen3-32b')."
    if prefix == pid:
        return f"{key}: cannot repoint to {pid!r} — that is the connection being removed."
    if cfg.provider_by_id(prefix) is None:
        return f"{key}: no connection named {prefix!r} to repoint to."
    return f"{key}: {target!r} must be a <provider>:<model> target (e.g. 'local-vllm:qwen3-32b')."


def register_provider_routes(app) -> None:
    @app.get("/api/config/providers")
    async def _list_providers():
        """The registry, keys redacted, each entry saying whether it can actually run.

        `in_use_by` is what makes the delete guard legible in the UI BEFORE the operator
        tries: a connection three slots depend on should look different from an unused one.
        """
        cfg = _config()
        out = []
        for p in cfg.providers:
            entry = p.as_dict()
            entry["display"] = p.display()
            entry["has_key"] = bool((p.api_key or "").strip())
            strings, structured = _walk_references(_referencing_view(cfg), p.id)
            # `in_use_by` stays the display strings existing UI/tests read; `in_use` is
            # the same dependencies structured so the Providers panel can offer a
            # repoint/clear per row (bd-v6xy) instead of parsing a sentence.
            entry["in_use_by"] = strings
            entry["in_use"] = structured
            out.append(entry)
        return {"providers": out}

    @app.post("/api/config/providers")
    async def _add_provider(req: ProviderCreate):
        from graph.config import PROVIDER_TYPES, valid_provider_id

        cfg = _config()
        pid = (req.id or "").strip().lower()
        if not valid_provider_id(pid):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid provider id {req.id!r}: use lowercase letters, digits, '-' and '_' "
                    "(no ':' or '/', which the <provider>:<model> grammar reserves)"
                ),
            )
        if cfg.provider_by_id(pid) is not None:
            raise HTTPException(status_code=409, detail=f"a provider named {pid!r} already exists")
        ptype = (req.type or "").strip().lower()
        if ptype not in PROVIDER_TYPES:
            raise HTTPException(status_code=400, detail=f"unknown provider type {req.type!r}")

        entries = _entries_from_config(cfg)
        entry = {"id": pid, "type": ptype}
        if req.label.strip():
            entry["label"] = req.label.strip()
        if req.base_url.strip():
            entry["base_url"] = req.base_url.strip()
        entries.append(entry)
        await _apply_providers(entries, {pid: req.api_key} if req.api_key.strip() else {})
        return {"ok": True, "id": pid, "restart_required": False}

    @app.patch("/api/config/providers/{pid}")
    async def _patch_provider(pid: str, req: ProviderPatch):
        cfg = _config()
        pid = (pid or "").strip().lower()
        if cfg.provider_by_id(pid) is None:
            raise HTTPException(status_code=404, detail=f"no provider named {pid!r}")
        entries = _entries_from_config(cfg)
        for entry in entries:
            if entry["id"] != pid:
                continue
            if req.label is not None:
                entry["label"] = req.label.strip()
                if not entry["label"]:
                    entry.pop("label")
            if req.base_url is not None:
                entry["base_url"] = req.base_url.strip()
                if not entry["base_url"]:
                    entry.pop("base_url", None)
        # A blank/absent api_key leaves the stored one in place — the console is never
        # shown a key, so it cannot echo one back.
        secret = {pid: req.api_key} if (req.api_key or "").strip() else {}
        await _apply_providers(entries, secret)
        return {"ok": True, "id": pid}

    @app.delete("/api/config/providers/{pid}")
    async def _delete_provider(pid: str, confirm_last: bool = False, body: ProviderDelete | None = None):
        from graph.llm import split_slot_target

        cfg = _config()
        pid = (pid or "").strip().lower()
        if cfg.provider_by_id(pid) is None:
            raise HTTPException(status_code=404, detail=f"no provider named {pid!r}")

        releases = dict((body.releases if body else None) or {})
        reported = {e["key"]: e for e in _referencing_entries(cfg, pid)}

        # Resolve each release IN MEMORY → dotted SETTINGS key -> new value, for the write.
        write_updates: dict = {}
        for key, target in releases.items():
            entry = reported.get(key)
            if entry is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key!r} does not currently route through {pid!r}; there is nothing to release there.",
                )
            write_key = _SLOT_WRITE_KEYS.get(key, key)
            if entry["kind"] == "favorite":
                if target is not None:
                    raise HTTPException(
                        status_code=400,
                        detail="model.favorites can only be cleared (send null); a favorite list is not a routed slot to repoint.",
                    )
                # Drop only THIS connection's favorites; keep every other operator favorite.
                write_updates[write_key] = [
                    f for f in (getattr(cfg, "model_favorites", []) or []) if not str(f).lower().startswith(f"{pid}:")
                ]
                continue
            if target is None:
                if key == "model.name":
                    raise HTTPException(
                        status_code=400,
                        detail="model.name is the lead model and cannot be cleared; repoint it to another connection.",
                    )
                write_updates[write_key] = ""  # clear the slot
                continue
            # A repoint: the target must resolve to ANOTHER registered connection.
            other_pid, other_model = split_slot_target(target, cfg)
            if not other_pid or not other_model or other_pid == pid or cfg.provider_by_id(other_pid) is None:
                raise HTTPException(status_code=400, detail=_bad_target_detail(cfg, pid, key, target))
            write_updates[write_key] = f"{other_pid}:{other_model}"

        # Apply the releases in memory and re-walk. Any reference the operator did NOT
        # resolve still blocks the delete, exactly as the bare (no-body) call does —
        # refusing beats cascading, and nothing is written.
        remaining = _remaining_after_releases(cfg, pid, releases)
        if remaining:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{pid!r} is still named by: {', '.join(remaining)}. "
                    "Repoint those to another connection first."
                ),
            )
        # The last-connection guard is unchanged, and evaluated AFTER releases.
        if len(cfg.providers) == 1 and not confirm_last:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{pid!r} is the last model connection. Removing it leaves this agent "
                    "without a configured model source. Confirm that explicitly to continue."
                ),
            )
        entries = [e for e in _entries_from_config(cfg) if e["id"] != pid]
        await _apply_providers(entries, {}, write_updates)
        return {"ok": True, "removed": pid, "released": sorted(releases)}

    @app.post("/api/config/providers/{pid}/models")
    async def _provider_models(pid: str):
        """That connection's model list — the gateway probed live, a subscription asked
        for its own account's models (ADR 0097)."""
        cfg = _config()
        entry = cfg.provider_by_id((pid or "").strip().lower())
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no provider named {pid!r}")
        if entry.type == "openai-compat":
            from graph.config_io import list_gateway_models

            # ONLY this connection's endpoint and key. Falling back to `model.api_key` /
            # `model.api_base` would send one connection's credential to another's
            # endpoint — a local vLLM probed with the production gateway's key — which is
            # the coupling the registry exists to remove. `available_model_lanes` already
            # refuses that; this route was the same probe with the old fallback still in
            # it, so the isolation held in the picker and leaked here.
            base = (entry.base_url or "").strip()
            if not base:
                raise HTTPException(
                    status_code=400,
                    detail=f"{entry.id!r} has no base URL configured — set one before probing it.",
                )
            models, error = await asyncio.to_thread(
                partial(list_gateway_models, base, (entry.api_key or "").strip(), allow_env_key=False)
            )
            return {"models": models, "error": error}
        from graph.providers.discovery import list_provider_models

        models, error = await asyncio.to_thread(list_provider_models, entry.type, cfg)
        return {"models": models, "error": error}
