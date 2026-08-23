"""Delegate config store — read/write the top-level ``delegates:`` list +
route per-delegate secrets to the gitignored ``secrets.yaml`` (ADR 0025, PR2).

The delegate list lives in ``langgraph-config.yaml`` **without secret values**;
each delegate's secret (a2a ``auth.token``, openai ``api_key``) is stored in
``secrets.yaml`` under a ``delegate_secrets`` map keyed ``<name>.<field>`` and
overlaid back at load. So the tracked config never holds a secret, and the panel
never has to round-trip one it already stored.

Two layers (ADR 0105): an entry is either **agent**-scoped (this instance's
``langgraph-config.yaml`` + ``secrets.yaml`` — the default) or **host**-scoped
(``scope: host``: the box's ``host-config.yaml`` ``delegates:`` list + the 0600
``host-secrets.yaml`` beside it). Every instance under the box READS both — agent
entries shadow host entries by name — so a coder registered once on the hub is on
every member's bench without a per-member copy, and a rotated key reaches them all.
Only the hub WRITES the host layer (members never write box state); a member that
tries gets :class:`DelegateScopeError`.
"""

from __future__ import annotations

import copy

from .adapters import ADAPTERS, is_secretish

SECRETS_SECTION = "delegate_secrets"

SCOPE_AGENT = "agent"
SCOPE_HOST = "host"


class DelegateScopeError(ValueError):
    """A write to the host (fleet-shared) layer from an instance that may not write it."""

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


def _scope_of(entry: dict) -> str:
    return SCOPE_HOST if str(entry.get("scope") or "").strip().lower() == SCOPE_HOST else SCOPE_AGENT


def can_write_host_layer() -> bool:
    """Only a hub / standalone instance writes the box's host layer — a fleet member
    never writes box state (the same rule ``sync_host_model_layer`` follows)."""
    try:
        from graph.workspaces.manager import is_workspace_member

        return not is_workspace_member()
    except Exception:  # noqa: BLE001 — unknown → behave like a member (refuse)
        return False


def _read_host_doc_for_write() -> dict:
    """The raw ``host-config.yaml`` mapping as the base for a rewrite. An ABSENT file is
    ``{}``; a file that exists but doesn't parse to a mapping REFUSES — rewriting it
    would destroy whatever the operator had there (the Host ``model:`` group), the same
    rule ``sync_host_model_layer`` follows."""
    import yaml as _yaml

    from infra.paths import host_config_path, read_text_utf8

    hp = host_config_path()
    if not hp.exists():
        return {}
    try:
        doc = _yaml.safe_load(read_text_utf8(hp)) or {}
    except (OSError, _yaml.YAMLError) as exc:
        raise DelegateScopeError(f"host-config.yaml at {hp} is unreadable ({exc}) — not overwriting it") from exc
    if not isinstance(doc, dict):
        raise DelegateScopeError(f"host-config.yaml at {hp} is not a mapping — not overwriting it")
    return doc


def read_host_delegates_raw() -> list:
    """The fleet-shared roster from the box's ``host-config.yaml`` (no secret values),
    every entry stamped ``scope: host``. Tolerant read (absent/unreadable → ``[]``)."""
    from graph.config_io import read_host_delegates

    out = []
    for e in read_host_delegates():
        e = dict(e)
        e["scope"] = SCOPE_HOST
        out.append(e)
    return out


def read_agent_delegates_raw() -> list:
    """This instance's own roster from ``langgraph-config.yaml`` (no secret values),
    every entry stamped ``scope: agent``."""
    from graph.config_io import load_yaml_doc

    doc = load_yaml_doc() or {}
    val = doc.get("delegates")
    out = []
    for e in (val if isinstance(val, list) else []):
        if isinstance(e, dict):
            e = dict(e)
            e["scope"] = SCOPE_AGENT
            out.append(e)
    return out


def read_delegates_raw() -> list:
    """The EFFECTIVE roster: this instance's entries ∪ the fleet-shared ones, an agent
    entry shadowing a host entry of the same name (ADR 0105). Every entry carries
    ``scope``. No secret values."""
    agent = read_agent_delegates_raw()
    names = {str(e.get("name") or "") for e in agent}
    return agent + [e for e in read_host_delegates_raw() if str(e.get("name") or "") not in names]


def _host_secret_overlay() -> dict:
    from graph.config_io import load_secrets
    from infra.paths import host_secrets_path

    try:
        host = (load_secrets(host_secrets_path()) or {}).get(SECRETS_SECTION)
    except Exception:  # noqa: BLE001 — an unreadable host overlay just contributes nothing
        return {}
    return dict(host) if isinstance(host, dict) else {}


def _agent_secret_overlay() -> dict:
    from graph.config_io import load_secrets

    sec = (load_secrets() or {}).get(SECRETS_SECTION)
    return dict(sec) if isinstance(sec, dict) else {}


def secret_overlay(scope: str | None = None) -> dict:
    """``delegate_secrets`` for one LAYER'S entries: an agent-scoped entry resolves
    against this instance's overlay only (a member that shadows a shared coder must be
    able to opt OUT of the shared key); a host-scoped entry against host ∪ instance
    (instance wins). ``scope=None`` = the merged view, for callers that don't know the
    entry's layer."""
    if scope == SCOPE_AGENT:
        return _agent_secret_overlay()
    merged = _host_secret_overlay()
    merged.update(_agent_secret_overlay())
    return merged


def env_secret_values(overlay: dict, name: str) -> dict:
    """The per-env secret VALUES stored for delegate ``name`` — i.e. every overlay
    entry keyed ``<name>.env.<VARNAME>`` returned as ``{VARNAME: value}``."""
    prefix = f"{name}{ENV_KEY_SEP}"
    return {k[len(prefix) :]: v for k, v in overlay.items() if k.startswith(prefix)}


def merged_delegates() -> list:
    """Delegates with their secrets overlaid from ``secrets.yaml`` — the registry
    loader's input. Does not mutate the stored config (deep-copies before inject)."""
    overlays = {SCOPE_AGENT: secret_overlay(SCOPE_AGENT), SCOPE_HOST: secret_overlay(SCOPE_HOST)}
    out = []
    for raw in read_delegates_raw():
        if not isinstance(raw, dict):
            continue
        overlay = overlays[_scope_of(raw)]
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


def _strip_scope(delegates: list) -> list:
    out = []
    for e in delegates:
        if isinstance(e, dict):
            e = dict(e)
            e.pop("scope", None)
        out.append(e)
    return out


def _save_list(delegates: list, scope: str = SCOPE_AGENT) -> None:
    """Persist one LAYER's roster: the agent layer into ``langgraph-config.yaml``, the
    host layer into the box's ``host-config.yaml`` (other keys preserved, atomic)."""
    if scope == SCOPE_HOST:
        if not can_write_host_layer():
            raise DelegateScopeError("fleet-shared delegates are managed on the hub — this agent can't edit them")
        import yaml as _yaml

        from infra.paths import atomic_write, host_config_path

        hp = host_config_path()
        doc = _read_host_doc_for_write()
        doc["delegates"] = _strip_scope(delegates)
        try:
            hp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(hp, _yaml.safe_dump(doc, sort_keys=False))
        except OSError as exc:  # a read-only sidecar mount (PROTOAGENT_HOST_CONFIG) is a refusal, not a 500
            raise DelegateScopeError(f"host layer is not writable ({exc}) — fleet-shared delegates can't be saved here") from exc
        return
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc() or {}
    if not isinstance(doc, dict):
        doc = {}
    doc["delegates"] = _strip_scope(delegates)
    save_yaml_doc(doc)


def _secrets_path_for(scope: str):
    from graph.config_io import secrets_yaml_path
    from infra.paths import host_secrets_path

    return host_secrets_path() if scope == SCOPE_HOST else secrets_yaml_path()


def _route_secret(name: str, entry: dict, scope: str = SCOPE_AGENT) -> dict:
    """Route the entry's secret value(s) into the layer's secrets overlay (if present);
    return the entry with the secrets stripped. The returned dict also carries a
    transient ``_routed_keys`` set (the overlay keys just written) that
    ``upsert_delegate`` pops before persisting — it is NOT persist-ready as returned.

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
        if scope == SCOPE_HOST:
            save_secrets({SECRETS_SECTION: secrets}, _secrets_path_for(scope))
        else:
            save_secrets({SECRETS_SECTION: secrets})
    entry["_routed_keys"] = set(secrets)  # consumed by upsert_delegate; never persisted
    return entry


def _prune_secrets(
    name: str, keep_env: set[str] | None, secret_field: str | None = None, scope: str = SCOPE_AGENT
) -> None:
    """Drop stored secrets for delegate ``name`` that are no longer referenced.

    ``keep_env`` = the env var names still SECRET-ROUTED on the entry (marked by the
    operator or secret-ish by name) — their ``<name>.env.<VAR>`` values survive; every
    other ``<name>.env.*`` entry is pruned, including a var whose secret toggle was
    turned OFF (its stale stored value would otherwise overlay the operator's new
    plaintext at every load — QA panel round 2 on #2150). ``keep_env=None`` = the
    delegate is being deleted: all its env secrets go, plus its adapter
    ``secret_field`` entry when given. Matching is STRUCTURED (``<name>.env.`` and the
    exact ``<name>.<secret_field>`` key) — never a bare ``<name>.`` prefix, which
    would swallow another delegate whose dotted name extends this one."""
    import os

    import yaml as _yaml

    from graph.config_io import load_secrets

    path = _secrets_path_for(scope)
    current = load_secrets(path) if scope == SCOPE_HOST else load_secrets()
    section = current.get(SECRETS_SECTION)
    if not isinstance(section, dict) or not section:
        return
    env_prefix = f"{name}{ENV_KEY_SEP}"
    doomed = []
    for k in section:
        if k.startswith(env_prefix):
            if keep_env is None or k[len(env_prefix) :] not in keep_env:
                doomed.append(k)
        elif keep_env is None and secret_field and k == f"{name}.{secret_field}":
            doomed.append(k)
    if not doomed:
        return
    for k in doomed:
        del section[k]
    if not section:
        current.pop(SECRETS_SECTION, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _yaml.safe_dump(current, f, sort_keys=False, default_flow_style=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _layer(scope: str) -> list:
    return read_host_delegates_raw() if scope == SCOPE_HOST else read_agent_delegates_raw()


def _other(scope: str) -> str:
    return SCOPE_AGENT if scope == SCOPE_HOST else SCOPE_HOST


def _migrate_secrets(name: str, from_scope: str, to_scope: str, already: set[str]) -> None:
    """Carry a delegate's stored secrets across layers on a scope change, except keys
    the incoming entry just supplied (``already``). Without this, flipping *Share with
    fleet* on an entry whose key the form left blank ("keep stored") would prune the
    old layer and write nothing to the new one — no layer holding the credential."""
    from graph.config_io import save_secrets

    src = _host_secret_overlay() if from_scope == SCOPE_HOST else _agent_secret_overlay()
    env_prefix = f"{name}{ENV_KEY_SEP}"
    adapter_keys = {f"{name}.{a.secret_field}" for a in ADAPTERS.values() if a.secret_field}
    moving = {
        k: v
        for k, v in src.items()
        if (k.startswith(env_prefix) or k in adapter_keys) and k not in already and v not in (None, "")
    }
    if not moving:
        return
    if to_scope == SCOPE_HOST:
        save_secrets({SECRETS_SECTION: moving}, _secrets_path_for(SCOPE_HOST))
    else:
        save_secrets({SECRETS_SECTION: moving})


def _remove_from_layer(name: str, scope: str) -> bool:
    """Drop ``name`` (and its secrets) from one layer; True when something was removed."""
    layer = _layer(scope)
    doomed = next((e for e in layer if isinstance(e, dict) and e.get("name") == name), None)
    if doomed is None:
        return False
    adapter = ADAPTERS.get(str(doomed.get("type", "")))
    _prune_secrets(name, None, secret_field=adapter.secret_field if adapter else None, scope=scope)
    _save_list([e for e in layer if not (isinstance(e, dict) and e.get("name") == name)], scope)
    return True


def upsert_delegate(entry: dict) -> list:
    """Add or replace a delegate by name in its layer (``scope: host`` = fleet-shared,
    default ``agent``); route its secret to that layer's overlay; persist. Moving an
    entry between layers (re-saving with the other scope) removes it from the old one
    — a name lives in one layer at a time as far as the writer is concerned. Returns
    the EFFECTIVE roster (secret-free, scope-stamped)."""
    entry = dict(entry)
    name = str(entry.get("name", "")).strip()
    scope = _scope_of(entry)
    entry.pop("scope", None)
    if scope == SCOPE_HOST and not can_write_host_layer():
        raise DelegateScopeError("fleet-shared delegates are managed on the hub — this agent can't edit them")
    # Which env vars remain SECRET-routed after this save — captured BEFORE
    # _route_secret pops the form's env_secret marker list.
    marked = {str(k) for k in (entry.get("env_secret") or [])}
    env_in = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    keep = {v for v in env_in if v in marked or is_secretish(v)}
    # A scope change is a MOVE: the same name must not linger in the other layer —
    # but only when this instance may write that layer (a member shadowing a host
    # entry with its own is legitimate and leaves the host copy alone). Detect it
    # BEFORE writing so the old layer's stored secrets can travel with the entry.
    other = _other(scope)
    moving = (scope == SCOPE_HOST or can_write_host_layer()) and any(
        isinstance(e, dict) and e.get("name") == name for e in _layer(other)
    )
    entry = _route_secret(name, entry, scope)
    routed = set(entry.pop("_routed_keys", set()))
    if moving:
        _migrate_secrets(name, other, scope, already=routed)
    _prune_secrets(name, keep, scope=scope)
    lst = [e for e in _layer(scope) if not (isinstance(e, dict) and e.get("name") == name)]
    lst.append(entry)
    _save_list(lst, scope)
    if moving:
        _remove_from_layer(name, other)
    return read_delegates_raw()


def delete_delegate(name: str) -> list:
    """Remove ``name`` from whichever layer holds it (agent first — a member deleting
    a name that exists only in the host layer is refused). Secrets go with it,
    matched structurally (never a bare name prefix)."""
    if not _remove_from_layer(name, SCOPE_AGENT):
        host_has = any(isinstance(e, dict) and e.get("name") == name for e in read_host_delegates_raw())
        if host_has:
            if not can_write_host_layer():
                raise DelegateScopeError("fleet-shared delegates are managed on the hub — this agent can't delete them")
            _remove_from_layer(name, SCOPE_HOST)
        else:
            # Not in either layer: still sweep orphaned secrets for the name (the
            # pre-0105 delete always pruned, entry or not — a half-removed delegate
            # must not leave its key behind).
            _prune_secrets(name, None, scope=SCOPE_AGENT)
            if can_write_host_layer():
                _prune_secrets(name, None, scope=SCOPE_HOST)
    return read_delegates_raw()
