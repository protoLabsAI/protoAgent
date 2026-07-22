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

from .adapters import ADAPTERS, is_secretish

SECRETS_SECTION = "delegate_secrets"

# A per-delegate env secret is keyed ``<name>.env.<VARNAME>`` in the overlay — the
# secret VALUE lives in secrets.yaml while the tracked config keeps only an empty
# reference (``env: {VARNAME: ""}``). Mirrors the single-field ``<name>.<field>``
# scheme used for auth.token / api_key.
ENV_KEY_SEP = ".env."


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


def env_secret_values(overlay: dict, name: str) -> dict:
    """The per-env secret VALUES stored for delegate ``name`` — i.e. every overlay
    entry keyed ``<name>.env.<VARNAME>`` returned as ``{VARNAME: value}``."""
    prefix = f"{name}{ENV_KEY_SEP}"
    return {k[len(prefix) :]: v for k, v in overlay.items() if k.startswith(prefix)}


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
        copied = False
        if adapter and adapter.secret_field and name:
            val = overlay.get(f"{name}.{adapter.secret_field}")
            if val:
                raw = copy.deepcopy(raw)
                copied = True
                _set_dotted(raw, adapter.secret_field, val)
        # Overlay per-env secrets back into ``raw["env"]`` so the spawned child sees
        # real values while the tracked config held only empty references (#2114).
        env_secrets = env_secret_values(overlay, name) if name else {}
        if env_secrets:
            if not copied:
                raw = copy.deepcopy(raw)
            env = raw.get("env")
            if not isinstance(env, dict):
                env = {}
                raw["env"] = env
            env.update(env_secrets)
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
    """Route the entry's secret value(s) into ``secrets.yaml`` (if present); return
    the entry with the secrets stripped, safe to persist in the tracked config.

    Two secret tiers: the adapter's single ``secret_field`` (auth.token / api_key),
    and per-``env`` values (#2114) — any env row the form marked secret (carried in
    ``env_secret``) or whose var name looks secret-bearing. An env secret's VALUE
    goes to ``<name>.env.<VARNAME>`` while its key stays in config with an empty
    value as a reference; ``merged_delegates`` overlays the value back at load."""
    from graph.config_io import save_secrets

    entry = copy.deepcopy(entry)
    secrets: dict[str, str] = {}

    adapter = ADAPTERS.get(str(entry.get("type", "")))
    if adapter and adapter.secret_field:
        val = _pop_dotted(entry, adapter.secret_field)
        if val:
            secrets[f"{name}.{adapter.secret_field}"] = val

    # ``env_secret`` is a form-only marker list — the keys the operator toggled
    # secret. Never persist it in the tracked config.
    marked = {str(k) for k in (entry.pop("env_secret", None) or [])}
    env = entry.get("env")
    if isinstance(env, dict):
        for var in list(env.keys()):
            if var not in marked and not is_secretish(var):
                continue
            val = env.get(var)
            if isinstance(val, str) and val.strip():
                secrets[f"{name}{ENV_KEY_SEP}{var}"] = val
            # Keep an empty reference in config either way (a blank secret row on
            # edit means "keep the stored value" — leave the overlay untouched).
            env[var] = ""

    if secrets:
        save_secrets({SECRETS_SECTION: secrets})
    return entry


def _prune_secrets(name: str, keep_env: set[str] | None) -> None:
    """Drop stored secrets for delegate ``name`` that are no longer referenced.

    ``keep_env`` = the env var names still present on the entry (their
    ``<name>.env.<VAR>`` values survive); ``None`` = the delegate is being deleted —
    drop EVERYTHING under ``<name>.`` including the adapter secret_field. Removing a
    secret row (or the whole delegate) MUST remove the stored value, or
    ``merged_delegates`` silently re-injects it at every spawn forever (QA panel
    major on #2150)."""
    import os

    import yaml as _yaml

    from graph.config_io import load_secrets, secrets_yaml_path

    path = secrets_yaml_path()
    current = load_secrets()
    section = current.get(SECRETS_SECTION)
    if not isinstance(section, dict) or not section:
        return
    env_prefix = f"{name}{ENV_KEY_SEP}"
    any_prefix = f"{name}."
    doomed = []
    for k in section:
        if keep_env is None:
            if k.startswith(any_prefix) or k.startswith(env_prefix):
                doomed.append(k)
        elif k.startswith(env_prefix) and k[len(env_prefix) :] not in keep_env:
            doomed.append(k)
    if not doomed:
        return
    for k in doomed:
        del section[k]
    if not section:
        current.pop(SECRETS_SECTION, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        _yaml.safe_dump(current, f, sort_keys=False, default_flow_style=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def upsert_delegate(entry: dict) -> list:
    """Add or replace a delegate by name; route its secret; persist. Returns the
    new list (secret-free, as stored)."""
    name = str(entry.get("name", "")).strip()
    entry = _route_secret(name, entry)
    env = entry.get("env")
    _prune_secrets(name, set(env.keys()) if isinstance(env, dict) else set())
    lst = [e for e in read_delegates_raw() if not (isinstance(e, dict) and e.get("name") == name)]
    lst.append(entry)
    _save_list(lst)
    return lst


def delete_delegate(name: str) -> list:
    _prune_secrets(name, None)  # a deleted delegate leaves no secrets behind
    lst = [e for e in read_delegates_raw() if not (isinstance(e, dict) and e.get("name") == name)]
    _save_list(lst)
    return lst
