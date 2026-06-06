"""Delegate config store — read/write the top-level ``delegates:`` list +
route per-delegate secrets to the gitignored ``secrets.yaml`` (ADR 0025, PR2).

The delegate list lives in ``langgraph-config.yaml`` **without secret values**;
each delegate's secret (a2a ``auth.token``, openai ``api_key``) is stored in
``secrets.yaml`` under a ``delegate_secrets`` map keyed ``<name>.<field>`` and
overlaid back at load. So the tracked config never holds a secret, and the panel
never has to round-trip one it already stored.
"""

from __future__ import annotations

import copy

from .adapters import ADAPTERS

SECRETS_SECTION = "delegate_secrets"


def _set_dotted(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _pop_dotted(d: dict, dotted: str):
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            return None
        cur = cur[p]
    return cur.pop(parts[-1], None) if isinstance(cur, dict) else None


def read_delegates_raw() -> list:
    """The delegates list as stored in the live config (no secret values)."""
    from graph.config_io import load_yaml_doc

    doc = load_yaml_doc() or {}
    val = doc.get("delegates")
    return list(val) if isinstance(val, list) else []


def secret_overlay() -> dict:
    from graph.config_io import load_secrets

    sec = (load_secrets() or {}).get(SECRETS_SECTION)
    return sec if isinstance(sec, dict) else {}


def merged_delegates() -> list:
    """Delegates with their secrets overlaid from ``secrets.yaml`` — the registry
    loader's input. Does not mutate the stored config (deep-copies before inject)."""
    overlay = secret_overlay()
    out = []
    for raw in read_delegates_raw():
        if not isinstance(raw, dict):
            continue
        adapter = ADAPTERS.get(str(raw.get("type", "")))
        name = raw.get("name")
        if adapter and adapter.secret_field and name:
            val = overlay.get(f"{name}.{adapter.secret_field}")
            if val:
                raw = copy.deepcopy(raw)
                _set_dotted(raw, adapter.secret_field, val)
        out.append(raw)
    return out


def _save_list(delegates: list) -> None:
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc() or {}
    if not isinstance(doc, dict):
        doc = {}
    doc["delegates"] = delegates
    save_yaml_doc(doc)


def _route_secret(name: str, entry: dict) -> dict:
    """Pop the entry's secret value into ``secrets.yaml`` (if present); return the
    entry with the secret stripped, safe to persist in the tracked config."""
    from graph.config_io import save_secrets

    adapter = ADAPTERS.get(str(entry.get("type", "")))
    if not (adapter and adapter.secret_field):
        return entry
    entry = copy.deepcopy(entry)
    val = _pop_dotted(entry, adapter.secret_field)
    if val:
        save_secrets({SECRETS_SECTION: {f"{name}.{adapter.secret_field}": val}})
    return entry


def upsert_delegate(entry: dict) -> list:
    """Add or replace a delegate by name; route its secret; persist. Returns the
    new list (secret-free, as stored)."""
    name = str(entry.get("name", "")).strip()
    entry = _route_secret(name, entry)
    lst = [e for e in read_delegates_raw() if not (isinstance(e, dict) and e.get("name") == name)]
    lst.append(entry)
    _save_list(lst)
    return lst


def delete_delegate(name: str) -> list:
    lst = [e for e in read_delegates_raw() if not (isinstance(e, dict) and e.get("name") == name)]
    _save_list(lst)
    return lst
