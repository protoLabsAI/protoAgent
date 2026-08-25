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


def _config():
    if STATE.graph_config is None:
        raise HTTPException(status_code=503, detail="config not loaded")
    return STATE.graph_config


def _referencing_slots(cfg, pid: str) -> list[str]:
    """Dotted slot names whose value routes through `pid`, plus favorites."""
    out: list[str] = []
    for label, attr in _SLOT_ATTRS:
        value = str(getattr(cfg, attr, "") or "")
        if value.lower().startswith(f"{pid}:"):
            out.append(f"{label}={value}")
    for fav in getattr(cfg, "model_favorites", []) or []:
        if str(fav).lower().startswith(f"{pid}:"):
            out.append(f"model.favorites={fav}")
    for name, sub in (getattr(cfg, "subagents", {}) or {}).items():
        value = str(getattr(sub, "model", "") or "")
        if value.lower().startswith(f"{pid}:"):
            out.append(f"subagents.{name}.model={value}")
    return out


def _write_providers(entries: list[dict], secret_updates: dict[str, str]) -> None:
    """Persist the registry to YAML, routing keys to the secrets overlay.

    Same split model.api_key has always used: the key never lands in the config file,
    which gets exported, backed up and forked.
    """
    from graph.config_io import load_yaml_doc, save_secrets, save_yaml_doc

    doc = load_yaml_doc()
    doc["providers"] = entries
    save_yaml_doc(doc)
    if secret_updates:
        save_secrets({"providers": secret_updates})


def _entries_from_config(cfg) -> list[dict]:
    return [p.as_dict() for p in cfg.providers]


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
            entry["in_use_by"] = _referencing_slots(cfg, p.id)
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
        await asyncio.to_thread(_write_providers, entries, {pid: req.api_key} if req.api_key.strip() else {})
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
        await asyncio.to_thread(_write_providers, entries, secret)
        return {"ok": True, "id": pid}

    @app.delete("/api/config/providers/{pid}")
    async def _delete_provider(pid: str):
        cfg = _config()
        pid = (pid or "").strip().lower()
        if cfg.provider_by_id(pid) is None:
            raise HTTPException(status_code=404, detail=f"no provider named {pid!r}")
        in_use = _referencing_slots(cfg, pid)
        if in_use:
            # Refusing beats cascading. Silently rewriting someone's slots is a bigger
            # surprise than an error, and dropping the connection under them turns a
            # qualified value into a bare model id sent to whatever provider remains —
            # a wrong-provider call rather than a failure.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{pid!r} is still named by: {', '.join(in_use)}. "
                    "Repoint those to another connection first."
                ),
            )
        entries = [e for e in _entries_from_config(cfg) if e["id"] != pid]
        await asyncio.to_thread(_write_providers, entries, {})
        return {"ok": True, "removed": pid}

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
            models, error = await asyncio.to_thread(list_gateway_models, base, (entry.api_key or "").strip())
            return {"models": models, "error": error}
        from graph.providers.discovery import list_provider_models

        models, error = await asyncio.to_thread(list_provider_models, entry.type, cfg)
        return {"models": models, "error": error}
