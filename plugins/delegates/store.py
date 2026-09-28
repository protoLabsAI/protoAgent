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
import functools

from .adapters import ADAPTERS, is_secretish

SECRETS_SECTION = "delegate_secrets"

SCOPE_AGENT = "agent"
SCOPE_HOST = "host"


class DelegateConflictError(Exception):
    """A create found the name already taken — checked under the config lock."""


class DelegateNotFoundError(Exception):
    """An update found the delegate gone — deleted meanwhile, checked under the lock."""


class DelegateScopeError(ValueError):
    """A write to the host (fleet-shared) layer from an instance that may not write it."""


class DelegateReferencedError(Exception):
    """A delete/rename left ``name`` still NAMED by live config (the board's coder
    ladder, a per-project coders map, a delegate fallback list), so the board loop
    would strand itself at save time with no warning (#3692). ``refs`` is every dotted
    config path that names it; the message lists them so the console error surface
    shows the operator exactly what to repoint. ``force`` (proceed anyway) or
    ``repoint_to`` (rewrite every reference to another delegate) override it."""

    def __init__(self, name: str, refs: list[str], *, action: str = "Deleting"):
        self.name = name
        self.refs = list(refs)
        listed = ", ".join(self.refs)
        plural = "reference" if len(self.refs) == 1 else "references"
        super().__init__(
            f"{action} delegate {name!r} would strand {len(self.refs)} config {plural}: "
            f"{listed}. Repoint {'it' if len(self.refs) == 1 else 'them'} to another "
            f"delegate, remove the reference, or pass force to proceed anyway."
        )

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


def _under_config_lock(fn):
    """Run a roster write as ONE unit under the config write lock (#2743).

    A save here is a read-modify-write of the live config: load the layer's roster, route
    and prune its secrets, write the roster back, maybe move the entry out of the other
    layer. The server's settings applier rewrites the same file under
    ``graph.config_io.CONFIG_WRITE_LOCK``; without taking it here, the two interleave and
    one side's change — a delegate, a secret, an unrelated setting — silently vanishes.
    Blocking by nature: call from a worker thread, never the event loop."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from graph.config_io import CONFIG_WRITE_LOCK

        with CONFIG_WRITE_LOCK:
            return fn(*args, **kwargs)

    return wrapper


@_under_config_lock
def upsert_delegate(entry: dict, *, expect: str | None = None) -> list:
    """Add or replace a delegate by name in its layer (``scope: host`` = fleet-shared,
    default ``agent``); route its secret to that layer's overlay; persist. Moving an
    entry between layers (re-saving with the other scope) removes it from the old one
    — a name lives in one layer at a time as far as the writer is concerned. Returns
    the EFFECTIVE roster (secret-free, scope-stamped).

    ``expect="absent"`` makes it a create (raises ``DelegateConflictError`` if the name is
    taken), ``expect="present"`` an update (``DelegateNotFoundError`` if it's gone); both
    checked under the config lock. ``None`` keeps the plain add-or-replace."""
    entry = dict(entry)
    name = str(entry.get("name", "")).strip()
    scope = _scope_of(entry)
    entry.pop("scope", None)
    if scope == SCOPE_HOST and not can_write_host_layer():
        raise DelegateScopeError("fleet-shared delegates are managed on the hub — this agent can't edit them")
    # `expect` is the caller's precondition, re-checked HERE, under the lock, against the
    # roster as it is now (#2743 follow-up). Checked before the lock, two creates of one
    # name both passed and the second silently replaced the first, and an edit could
    # bring back a delegate a concurrent delete had just removed.
    if expect is not None:
        clash = next((e for e in read_delegates_raw() if isinstance(e, dict) and e.get("name") == name), None)
        # Same rule the create route always applied: a member may shadow a fleet-shared
        # entry with its own; a hub may not double-register.
        if expect == "absent" and clash is not None and (clash.get("scope") == scope or can_write_host_layer()):
            where = "fleet-shared" if clash.get("scope") == SCOPE_HOST else "this agent's"
            raise DelegateConflictError(
                f"delegate {name!r} already exists in {where} list — edit it and toggle 'Share with fleet' to move it"
            )
        if expect == "present" and clash is None:
            raise DelegateNotFoundError(f"delegate {name!r} not found")
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


# ── reference scan (#3692) ──────────────────────────────────────────────────────
#
# A delegate lives at its NAME; other config fields name it back. Deleting or renaming
# one that config still names strands the reference — the board loop then pauses itself
# at save time with no warning. These helpers find (and, for repoint, rewrite) every
# such reference so the delete/rename path can refuse and name them.
#
# Config fields that NAME a delegate — grepped from how delegate names are consumed:
#   • ``project_board.coder``            — the board's single coder (a bare name)
#   • ``project_board.coders``           — the model-tier ladder (bare name, list, or a
#                                          {rung: name-or-list} map — .reasoning/.opus…)
#   • ``project_board.projects.<p>.coder`` / ``.coders`` — the same, per registered project
#   • ``delegates[].fallback`` / ``.fallbacks``          — a delegate entry naming other
#                                          delegates as fallbacks (a fork/plugin field)
# A value in any of these slots may be a bare name, a list of names, or a nested
# {key: name-or-list} map; ``_nested_refs`` walks all three, so a fork that adds the
# same shape under one of these keys is covered by the shared walk.


def _nested_refs(val, path: str, name: str, target: str | None = None) -> list[str]:
    """Dotted subpaths under ``path`` where ``val`` (a list or dict of names-or-lists)
    names ``name``. When ``target`` is given, rewrite each hit to it in place."""
    out: list[str] = []
    if isinstance(val, list):
        for i, item in enumerate(val):
            if isinstance(item, str):
                if item.strip() == name:
                    if target is not None:
                        val[i] = target
                    out.append(f"{path}[{i}]")
            elif isinstance(item, (list, dict)):
                out.extend(_nested_refs(item, f"{path}[{i}]", name, target))
    elif isinstance(val, dict):
        for k, v in list(val.items()):
            if isinstance(v, str):
                if v.strip() == name:
                    if target is not None:
                        val[k] = target
                    out.append(f"{path}.{k}")
            elif isinstance(v, (list, dict)):
                out.extend(_nested_refs(v, f"{path}.{k}", name, target))
    return out


def _slot_refs(container: dict, key: str, path: str, name: str, target: str | None = None) -> list[str]:
    """Refs to ``name`` in ``container[key]`` — a bare name (str), a list, or a nested
    map. When ``target`` is given, rewrite hits to it in place."""
    val = container.get(key)
    if isinstance(val, str):
        if val.strip() == name:
            if target is not None:
                container[key] = target
            return [path]
        return []
    if isinstance(val, (list, dict)):
        return _nested_refs(val, path, name, target)
    return []


def _scan_references(doc: dict, name: str, target: str | None = None) -> list[str]:
    """The shared find/rewrite walk over the config doc. Read-only when ``target`` is
    None; rewrites every hit to ``target`` (mutating ``doc``) otherwise."""
    refs: list[str] = []
    pb = doc.get("project_board")
    if isinstance(pb, dict):
        refs += _slot_refs(pb, "coder", "project_board.coder", name, target)
        refs += _slot_refs(pb, "coders", "project_board.coders", name, target)
        projects = pb.get("projects")
        if isinstance(projects, dict):
            for pname, pcfg in projects.items():
                if not isinstance(pcfg, dict):
                    continue
                base = f"project_board.projects.{pname}"
                refs += _slot_refs(pcfg, "coder", f"{base}.coder", name, target)
                refs += _slot_refs(pcfg, "coders", f"{base}.coders", name, target)
    delegates = doc.get("delegates")
    if isinstance(delegates, list):
        for entry in delegates:
            if not isinstance(entry, dict):
                continue
            # An entry's OWN fields aren't a reference TO it — skip the delegate itself.
            if str(entry.get("name") or "").strip() == name:
                continue
            ename = str(entry.get("name") or "").strip() or "?"
            for field in ("fallback", "fallbacks"):
                if field in entry:
                    refs += _slot_refs(entry, field, f"delegates.{ename}.{field}", name, target)
    return refs


def find_delegate_references(name: str, doc: dict | None = None) -> list[str]:
    """Every dotted config path that NAMES delegate ``name``, so a delete/rename can
    refuse instead of silently stranding the board loop (#3692). Reads the live agent
    config doc when ``doc`` is None."""
    name = str(name or "").strip()
    if not name:
        return []
    if doc is None:
        from graph.config_io import load_yaml_doc

        doc = load_yaml_doc() or {}
    if not isinstance(doc, dict):
        return []
    return _scan_references(doc, name)


def _apply_repoint(name: str, target: str) -> list[str]:
    """Rewrite every config reference to delegate ``name`` to ``target`` in the agent
    config doc, persisted in place under the caller's config lock. Returns the paths
    rewritten."""
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc() or {}
    if not isinstance(doc, dict):
        return []
    changed = _scan_references(doc, name, target=target)
    if changed:
        save_yaml_doc(doc)
    return changed


def _rekey_secrets(old: str, new: str, scope: str = SCOPE_AGENT) -> None:
    """Copy delegate ``old``'s stored secrets to the ``new`` name (same layer) so a
    rename keeps its credentials. Matching is STRUCTURED — ``<old>.env.`` and each
    ``<old>.<secret_field>`` — never a bare ``<old>.`` prefix (a dotted neighbour is
    safe). The old keys are pruned afterwards by the delete that finishes the rename."""
    from graph.config_io import load_secrets, save_secrets

    path = _secrets_path_for(scope)
    current = load_secrets(path) if scope == SCOPE_HOST else load_secrets()
    section = current.get(SECRETS_SECTION)
    if not isinstance(section, dict) or not section:
        return
    env_prefix = f"{old}{ENV_KEY_SEP}"
    adapter_fields = {a.secret_field for a in ADAPTERS.values() if a.secret_field}
    moved: dict[str, str] = {}
    for k, v in section.items():
        if k.startswith(env_prefix):
            moved[f"{new}{ENV_KEY_SEP}{k[len(env_prefix):]}"] = v
        elif k in {f"{old}.{f}" for f in adapter_fields}:
            field = k[len(old) + 1 :]
            moved[f"{new}.{field}"] = v
    if not moved:
        return
    if scope == SCOPE_HOST:
        save_secrets({SECRETS_SECTION: moved}, _secrets_path_for(SCOPE_HOST))
    else:
        save_secrets({SECRETS_SECTION: moved})


def _validate_repoint_target(name: str, repoint_to: str) -> str:
    """A repoint target must name ANOTHER configured delegate — repointing to a missing
    one just moves the dangling reference."""
    repoint_to = str(repoint_to or "").strip()
    names = {str(e.get("name") or "") for e in read_delegates_raw() if isinstance(e, dict)}
    if repoint_to == name or repoint_to not in names:
        raise ValueError(f"repoint_to must name another configured delegate, not {repoint_to!r}")
    return repoint_to


@_under_config_lock
def rename_delegate(old_name: str, entry: dict, *, force: bool = False, repoint_to: str | None = None) -> list:
    """Rename delegate ``old_name`` to ``entry['name']``. A delegate lives at its name,
    so a rename strands every config reference to the OLD name exactly as a delete does
    (#3692): refuse (``DelegateReferencedError``) when the old name is still referenced,
    unless ``force`` or ``repoint_to``. On success the entry is re-created under the new
    name with its stored secrets re-keyed, the old entry removed, and references
    repointed to the new name (``repoint_to`` overrides the target so a rename never
    leaves a dangling reference behind)."""
    old_name = str(old_name).strip()
    new_name = str(entry.get("name") or "").strip()
    if not new_name:
        raise ValueError("rename needs a new name")
    if new_name == old_name:
        return upsert_delegate(entry, expect="present")  # a plain edit, not a rename
    roster = read_delegates_raw()
    cur = next((e for e in roster if isinstance(e, dict) and e.get("name") == old_name), None)
    if cur is None:
        raise DelegateNotFoundError(f"delegate {old_name!r} not found")
    if any(isinstance(e, dict) and e.get("name") == new_name for e in roster):
        raise DelegateConflictError(f"delegate {new_name!r} already exists — pick another name")
    refs = find_delegate_references(old_name)
    if repoint_to:
        repoint_to = _validate_repoint_target(old_name, repoint_to)
    elif refs and not force:
        raise DelegateReferencedError(old_name, refs, action="Renaming")
    scope = _scope_of(cur)
    _rekey_secrets(old_name, new_name, scope)
    upsert_delegate(entry, expect="absent")
    delete_delegate(old_name, force=True)  # drops the old entry + its now-moved secrets
    if refs:
        _apply_repoint(old_name, repoint_to or new_name)
    return read_delegates_raw()


@_under_config_lock
def delete_delegate(name: str, *, force: bool = False, repoint_to: str | None = None) -> list:
    """Remove ``name`` from whichever layer holds it (agent first — a member deleting
    a name that exists only in the host layer is refused). Secrets go with it,
    matched structurally (never a bare name prefix).

    Refuses (``DelegateReferencedError``) when live config still NAMES ``name`` — the
    board loop would strand itself otherwise (#3692) — unless ``force`` (delete anyway)
    or ``repoint_to`` (rewrite every reference to another delegate in the same save)."""
    name = str(name).strip()
    # Decide the repoint up front but DON'T save it yet — the removal below can still
    # refuse (a host-shared entry on a member that can't write the host layer raises
    # DelegateScopeError; an OSError on the layer write does too). Saving the repoint
    # first would leave every reference permanently rewritten while the delegate stayed
    # and the operator got a 403 — so the rewrite is deferred until AFTER the removal
    # succeeds (#3692 review). The target is validated here (no writes), so an invalid
    # ``repoint_to`` refuses without touching config either.
    repoint_target: str | None = None
    if not force:
        refs = find_delegate_references(name)
        if refs:
            if repoint_to:
                repoint_target = _validate_repoint_target(name, repoint_to)
            else:
                raise DelegateReferencedError(name, refs)
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
    # The delegate is gone — only now is it safe to rewrite the references that named it.
    if repoint_target:
        _apply_repoint(name, repoint_target)
    return read_delegates_raw()
