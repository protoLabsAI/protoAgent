"""Workspace lifecycle (ADR 0041) — create / list / run / remove.

A workspace is a directory ``<root>/<name>/`` that *is* an agent's instance root:
its ``config/langgraph-config.yaml`` + ``config/secrets.yaml`` + (once a bundle
installs) ``plugins.lock`` + ``plugins/`` live there, so launching it with
``PROTOAGENT_HOME=<ws>`` makes the workspace dir the member's whole instance root
(config under ``<ws>/config``, plugins under ``<ws>/plugins``). ``PROTOAGENT_INSTANCE=<id>``
scopes its private data stores to ``~/.protoagent/<id>/*``. ``workspace.yaml`` is the
registry record (id, port, bundle, created), kept at the workspace root.

This module only orchestrates the existing knobs — no new runtime, no new storage
format. ``run`` returns the env + argv for the CLI to ``exec`` the normal server.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from infra.paths import atomic_write, read_text_utf8

PORT_BASE = 7870  # workspaces get PORT_BASE+1, +2, … unless an explicit port is given


class WorkspaceError(Exception):
    """A workspace op was rejected (bad name, collision, missing workspace)."""


class WorkspaceBusy(WorkspaceError):
    """A purge stopped the member but could not delete its workspace — files were still
    locked (#2583).

    Distinct from ``WorkspaceError`` because the situations differ in kind: that one means
    "the request was wrong", this one means "the request was right, it partially completed,
    and retrying will finish it". The API maps them to different statuses so an operator
    isn't told a destructive half-done operation simply failed.
    """


# Names that collide with the fleet's routing vocabulary (ADR 0042 slug routing). `host` is the
# reserved slug that addresses THIS instance (`/app/agent/host/` / `/agents/host/*`); a workspace
# named `host` would shadow it → the peer is permanently unreachable + two switcher entries both
# claim to be current. Reject at creation.
_RESERVED_NAMES = {"host"}


def workspaces_root() -> Path:
    """Where workspaces live. ``PROTOAGENT_WORKSPACES_DIR`` overrides (verbatim);
    default the per-instance ``instance_root/workspaces`` store.

    HUB-instance-scoped (ADR 0004), so a scoped hub owns its own fleet
    (``<instance_root>/workspaces`` + its ``fleet.json``) instead of sharing one registry
    with every co-located instance (two hubs pruning/evicting each other's agents). This
    also fences peers: workspace agents run with ``PROTOAGENT_HOME=<ws>``, so a member's
    own (empty) workspaces root keeps the supervisor's ``shutdown_all`` hub-only by
    construction."""
    from infra.paths import instance_paths

    override = os.environ.get("PROTOAGENT_WORKSPACES_DIR", "").strip()
    return Path(override).expanduser() if override else instance_paths().workspaces_dir


def _safe(name: str) -> str:
    n = (name or "").strip()
    if not n or n != "".join(c for c in n if c.isalnum() or c in "-_"):
        raise WorkspaceError(f"invalid workspace name {name!r} — use letters, digits, '-' or '_'")
    return n


def _slugify_display(name: str) -> str:
    """Coerce a free-form name into one a workspace record can hold.

    Workspace display names are constrained to ``[A-Za-z0-9_-]`` (see :func:`_safe`) because
    the fleet control plane accepts an agent by name as well as by id. ``identity.name`` has
    no such limit — "Blood Bowl Coach" is a perfectly reasonable thing to call an agent — so
    a member syncing its own record **coerces** instead of refusing: any run of other
    characters collapses to a single ``_``, and leading/trailing separators are trimmed.

    Returns ``""`` when nothing usable survives (e.g. ``"!!!"``); that's the caller's call.
    Only the derived record is normalized — the agent keeps the name the operator typed.
    """
    return re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_-")


def _ws_dir(name: str) -> Path:
    return workspaces_root() / _safe(name)


def _find(ident: str) -> dict | None:
    """Resolve a workspace by ``id`` OR display ``name`` (ids are opaque + immutable —
    the slug/scoping key; names are editable display labels). Id match wins."""
    ws = list_workspaces()
    return next((w for w in ws if w["id"] == ident), None) or next((w for w in ws if w["name"] == ident), None)


def _new_id(name: str) -> str:
    """An opaque, immutable workspace id: ``<name>-<4hex>`` (e.g. ``ava-7f3a``). The id keys
    the dir, the URL slug and the data scope (``~/.protoagent/<id>/*``), so a display rename
    never moves storage or breaks open windows."""
    import uuid

    existing = {w["id"] for w in list_workspaces()}
    while True:
        cand = f"{_safe(name)}-{uuid.uuid4().hex[:4]}"
        if cand not in existing:
            return cand


def _read_record(ws: Path) -> dict | None:
    import yaml

    f = ws / "workspace.yaml"
    if not f.exists():
        return None
    try:
        d = yaml.safe_load(read_text_utf8(f)) or {}
        return d if isinstance(d, dict) else None
    except yaml.YAMLError:
        return None


def is_workspace_member() -> bool:
    """True when THIS process runs as a fleet workspace member (ADR 0042): the hub's
    supervisor spawns members with ``PROTOAGENT_HOME=<ws>``, so a member's instance
    root IS a workspace dir — and every workspace dir carries its registry record
    (``workspace.yaml``, written by ``create()``). A hub or standalone instance root
    never has one, so the record doubles as the read-only "this instance is managed
    by a host" signal (#1708) with no new state or endpoint. Remote members (ADR
    0042 §I) are out of scope by design: registration is one-sided on the hub, and a
    remote is a full independent instance that may legitimately run its own fleet."""
    from infra.paths import instance_paths

    return _read_record(instance_paths().instance_root) is not None


def sync_self_display_name(new_name: str) -> str | None:
    """Restamp THIS member's own ``workspace.yaml`` from an identity rename.

    The console's agent switcher / header / Fleet page read the HUB's fleet list,
    which comes from each workspace's ``workspace.yaml`` — *not* from the member's
    own config. Settings ▸ Agent ▸ Identity is an agent-scoped path, so on a member
    console a rename moves ``identity.name`` (tab title, A2A card, chat placeholder)
    while the record kept the create-time name. This is the mirror image of hub-side
    :func:`rename`, called from the reload commit.

    Two fields, two jobs (#2520): ``label`` holds the operator's name **verbatim**
    (free-form UTF-8 — what every user-facing surface renders), while ``name`` stays
    the ``[A-Za-z0-9_-]`` addressing handle the control plane accepts alongside the
    id (:func:`_slugify_display`; unchanged when nothing usable survives or the slug
    is reserved). Before ``label`` existed the record held ONLY the slug, so
    "PA Windows Lifecycle Café" silently rendered as ``PA_Windows_Lifecycle_Caf``.

    Returns a note for the operator only when the *display* couldn't follow (a
    reserved label — a member masquerading as "host" in the switcher is worse than a
    stale name); ``None`` otherwise. Never raises: the agent is already running under
    the new identity, so a stale label must never fail the reload.

    Uniqueness against SIBLING workspaces is deliberately not checked: a member
    cannot see its peers (``workspaces_root()`` is hub-scoped). Display names are
    not unique — the fleet control plane addresses agents by the immutable ``id``.
    """
    from infra.paths import instance_paths

    root = instance_paths().instance_root
    rec = _read_record(root)
    if rec is None:  # host / standalone — no record to keep in step
        return None
    label = (new_name or "").strip()
    if not label:
        return None
    if label.lower() in _RESERVED_NAMES:
        return f"fleet label unchanged: {label!r} is reserved — it's how the fleet addresses the host"
    slug = _slugify_display(label)
    changed = False
    if rec.get("label") != label:
        rec["label"] = label
        changed = True
    if slug and slug.lower() not in _RESERVED_NAMES and slug != rec.get("name"):
        rec["name"] = slug
        changed = True
    if not changed:
        return None

    import yaml

    atomic_write(root / "workspace.yaml", yaml.safe_dump(rec, sort_keys=False))
    return None

def capability_contract_warning(bound_tool_names) -> str | None:
    """Warn when THIS agent's persona commits to actions it has no tool for (#2277).

    An archetype preset is the shipped artifact that defines an agent's identity — the
    template every fork starts from. When it commits to an action the bundle's DEFAULT
    config never provisions, every instance inherits the lie, and the failure mode is
    silent by construction: the model fills the impossible instruction with narration and
    reports the action as completed, so nothing errors and nothing crashes. The shipped
    case was ``project-manager``'s "pain points get filed as issues" against a
    ``github.write`` that defaults false, so ``github_create_issue`` was never registered.

    The archetype records the tools its doctrine depends on (``requires_tools`` in
    ``archetype-catalog.json`` / a bundle's ``archetype:`` block); ``create()`` copies
    that contract onto the workspace. Here — inside the member, after the graph has bound
    its tools — is the one place where both the doctrine and the live tool set are
    knowable at once, so the contract is checked against ground truth rather than against
    manifest metadata that can't see a config-gated registration.

    Returns ``None`` for a non-member, a member with no declared contract, or a satisfied
    one. Deliberately a warning, not a refusal: the operator may have turned a capability
    off on purpose, and an agent that boots degraded beats one that won't boot.
    """
    from infra.paths import instance_paths

    rec = _read_record(instance_paths().instance_root)
    declared = [str(t) for t in (rec or {}).get("requires_tools") or []]
    if not declared:
        return None
    missing = [t for t in declared if t not in set(bound_tool_names or ())]
    if not missing:
        return None
    return (
        f"[archetype] this agent's persona commits to actions it has no tool for: "
        f"{', '.join(missing)}. The archetype declared them, but they aren't bound — most "
        f"often a per-agent capability flag that defaults off (e.g. github.write gating "
        f"github_create_issue), or a plugin that didn't enable. Asked to do these, the model "
        f"will narrate a completion rather than fail, so the breakage is invisible in its own "
        f"output. Enable the capability, or edit SOUL.md so the doctrine matches what this "
        f"agent can actually do."
    )


def list_workspaces() -> list[dict]:
    """Every workspace under the root (each dir with a ``workspace.yaml``)."""
    root = workspaces_root()
    out: list[dict] = []
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        rec = _read_record(d)
        if rec:
            out.append(
                {
                    "name": rec.get("name", d.name),
                    "label": rec.get("label") or rec.get("name", d.name),
                    "id": rec.get("id", d.name),
                    "port": rec.get("port"),
                    "bundle": rec.get("bundle") or "",
                    "created": rec.get("created", ""),
                    "path": str(d),
                }
            )
    return out


def _port_base() -> int:
    """Base port for fleet workspace agents (Host layer, ADR 0047 D8). Reads the
    resolved ``fleet.port_base`` from the live config (which already folds in the
    PROTOAGENT_* env fallback); falls back to the module default in a CLI/no-STATE
    context."""
    try:
        from runtime.state import STATE

        cfg = getattr(STATE, "graph_config", None)
        if cfg is not None:
            return int(getattr(cfg, "fleet_port_base", PORT_BASE) or PORT_BASE)
    except Exception:  # noqa: BLE001 — best-effort; no live config ⇒ the constant
        pass
    return PORT_BASE


def _port_is_free(port: int) -> bool:
    """True if 127.0.0.1:<port> can be bound right now — i.e. nothing (fleet OR an
    unrelated process) is already listening.

    ``_pick_port`` used to consider only fleet-registered ports, so it could hand out a
    port already held by an UNRELATED instance (a dev server, another protoAgent fork on
    the conventional :7871) — the spawned agent then died with ``EADDRINUSE`` at bind.
    Probing the OS closes that gap. Best-effort: any bind failure reads as 'not free' (the
    safe choice — skip it). A TOCTOU window remains between this check and the agent's own
    bind, but it eliminates the common, durable collision."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(explicit: int | None) -> int:
    if explicit:
        return int(explicit)
    used = {w["port"] for w in list_workspaces() if w.get("port")}
    # Don't collide with the HUB itself — the host instance (this process) self-registers as a
    # fleet agent on its own port but isn't a workspace, so it's invisible to list_workspaces().
    try:
        from runtime.state import STATE

        if getattr(STATE, "active_port", None):
            used.add(int(STATE.active_port))
    except Exception:  # noqa: BLE001 — best-effort; CLI/no-STATE context just skips it
        pass
    base = _port_base() + 1
    # Skip registry-known ports AND OS-occupied ones (an unrelated process on the port).
    # Bounded scan so a saturated range fails loudly instead of looping forever.
    for p in range(base, base + 1000):
        if p not in used and _port_is_free(p):
            return p
    raise WorkspaceError(f"no free port in {base}..{base + 999} — too many agents, or the range is occupied")


_CONFIG_TEMPLATE = """\
# Workspace {name} — a protoAgent agent (ADR 0041).
# Edit the model / plugins / secrets below, then `workspace run {name}`.

identity:
  name: {name}

# Data isolation (ADR 0004/0041) — scopes this agent's private stores to
# ~/.protoagent/{id}/* so it never collides with other agents on this host.
# The id is opaque + immutable (renames only change the display name above).
instance:
  id: {id}

model:
  provider: openai
  name: protolabs/reasoning
  api_base: ""        # set your gateway / OpenAI-compat base URL
  api_key: ""         # or set OPENAI_API_KEY in this workspace's secrets.yaml

plugins:
  # `delegates` is on by default for fleet agents (ADR 0042 + 0025) so they can delegate to
  # each other out of the box — enabled at startup, so the /api/delegates routes are registered
  # with no restart-to-enable (a hot-reload alone doesn't bind new plugin routes).
  enabled: [delegates]
  sources:
    allow: [github.com/protoLabsAI/*]

# Shared skills commons (ADR 0041) — opt in to share the fleet's skill library:
# skills:
#   shared: true
"""


def create(
    name: str,
    *,
    from_config: str | None = None,
    inherit_model: str | None = None,
    bundle: str | None = None,
    port: int | None = None,
    shared_skills: bool = False,
    snapshot_config: Path | None = None,
    soul: str | None = None,
    inputs: Mapping[str, str] | None = None,
    secrets: list[dict] | None = None,
    config_inputs: Mapping[str, object] | None = None,
    requires_tools: list[str] | None = None,
) -> dict:
    """Scaffold a workspace: its config dir, ``workspace.yaml``, and (with ``bundle``)
    an installed plugin bundle. Does not start it.

    Config base, in precedence:
      * ``snapshot_config`` — an agent SNAPSHOT's config (ADR 0091 D3, #2104). A secret-free
        entry, deliberately NOT ``from_config``: that path copies ``secrets.yaml`` verbatim,
        which is the exact property a snapshot exists to avoid. The overlay is created EMPTY
        and credentials arrive only as operator-supplied values.
      * ``from_config`` — a FULL clone of another agent's config + secrets (identity re-stamped).
      * ``inherit_model`` — a BLANK template, but with only that agent's ``model:`` section +
        secrets popped over (the gateway), so it boots ready-to-chat WITHOUT inheriting its
        plugins/skills. This is the fleet's default "new agent" (a blank agent, model carried).
      * neither — the plain blank template.

    ``soul`` (the picked archetype's base SOUL.md, ADR 0042) is written into the workspace's
    ``config/SOUL.md`` — the member's live persona — so an agent created from an archetype
    arrives with its persona, not just its tools. Blank leaves the agent on the default SOUL.

    ``inputs`` (``{input_key: value}``) are operator-supplied MCP template values (#2041):
    they fill a bundle ``mcp:`` template's ``${input}`` placeholders with priority over the
    seed-time env, so an operator token seeds the server ENABLED instead of visible-but-inert.
    ``secrets`` (``[{key, value}]``) are operator-supplied values for the bundle's *declared*
    secrets, written to the member's ``config/secrets.yaml`` nested under the bundle's section.
    ``config_inputs`` (``{dotted_key: value}``, #2934) are the operator's answers to the
    bundle's declared ``config_inputs:`` prompts, written into the workspace config at each
    declared dotted key path. All are seeded AFTER install and only apply on the bundle path;
    operator-supplied only — never auto-copied from the host's environment.
    """
    name = _safe(name)
    if name.lower() in _RESERVED_NAMES:
        raise WorkspaceError(f"{name!r} is reserved — it's how the fleet addresses this instance")
    if _find(name) is not None:
        raise WorkspaceError(f"an agent named {name!r} already exists")
    # Opaque id keys the dir + slug + data scope; `name` is the editable display label.
    wid = _new_id(name)
    ws = _ws_dir(wid)
    if ws.exists():
        raise WorkspaceError(f"workspace {wid!r} already exists at {ws}")
    ws.mkdir(parents=True)

    # The member reads its config at <ws>/config/ (instance_root=<ws> → config tier
    # under <ws>/config), so scaffold there.
    cfg_dir = ws / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "langgraph-config.yaml"
    if snapshot_config:
        src_cfg = Path(snapshot_config).expanduser()
        if not src_cfg.exists():
            shutil.rmtree(ws, ignore_errors=True)
            raise WorkspaceError(f"snapshot config not found at {src_cfg}")
        shutil.copyfile(src_cfg, cfg)
        # ALWAYS a blank overlay — never copied, never inherited. A snapshot carries no
        # credentials by construction (ADR 0091 D2), so anything here would be a leak from
        # somewhere else.
        (cfg_dir / "secrets.yaml").write_text("# Per-workspace secrets overlay.\n", encoding="utf-8")
        _stamp_identity(cfg, name, shared_skills, instance_id=wid)
    elif from_config:
        src = Path(from_config).expanduser()
        src_cfg = src / "langgraph-config.yaml" if src.is_dir() else src
        if not src_cfg.exists():
            shutil.rmtree(ws, ignore_errors=True)
            raise WorkspaceError(f"--from: no langgraph-config.yaml at {src_cfg}")
        shutil.copyfile(src_cfg, cfg)
        src_sec = (src if src.is_dir() else src.parent) / "secrets.yaml"
        if src_sec.exists():
            shutil.copyfile(src_sec, cfg_dir / "secrets.yaml")
        _stamp_identity(cfg, name, shared_skills, instance_id=wid)
    else:
        # UTF-8 explicitly: the template carries em dashes, and the locale default
        # (CP1252 on Windows) wrote a config a strict UTF-8 reader crashes on (#2521).
        cfg.write_text(_CONFIG_TEMPLATE.format(name=name, id=wid), encoding="utf-8")
        (cfg_dir / "secrets.yaml").write_text("# Per-workspace secrets overlay.\n", encoding="utf-8")
        if inherit_model:
            _overlay_model(cfg, ws, inherit_model)  # gateway only — not plugins/skills
        if shared_skills:
            _stamp_identity(cfg, name, True, instance_id=wid)

    # The archetype persona (ADR 0042) — the member reads its SOUL at <ws>/config/SOUL.md
    # (instance_root=<ws>), so scaffold it there. Only when non-empty: a blank archetype
    # (or none) leaves the agent on the default SOUL rather than writing an empty persona.
    if soul and soul.strip():
        (cfg_dir / "SOUL.md").write_text(soul, encoding="utf-8")

    import yaml

    assigned = _pick_port(port)
    rec = {
        "id": wid,
        "name": name,
        # The user-facing display label, verbatim (#2520). Same as `name` at create
        # (creation names are charset-checked); an identity rename may diverge them.
        "label": name,
        "port": assigned,
        "created": datetime.now(timezone.utc).isoformat(),
        "bundle": bundle or "",
    }
    # The archetype's capability contract (#2277): the tools its persona commits to
    # performing. Recorded here because the member's instance root IS this workspace, so
    # it can read its own contract at boot and check it against what actually got bound —
    # the only place both the doctrine and the live tool set are knowable at once.
    if requires_tools:
        rec["requires_tools"] = [str(t) for t in requires_tools if str(t).strip()]
    # Reserve the port NOW — write workspace.yaml BEFORE the (possibly minutes-long) bundle
    # install, so a concurrent create can't _pick_port the same port (#11). Then clean up the
    # whole dir on any failure, so a retry doesn't 400 with "already exists" on a poisoned
    # workspace that's invisible in the list (no workspace.yaml).
    atomic_write(ws / "workspace.yaml", yaml.safe_dump(rec, sort_keys=False))
    installed: list[str] = []
    try:
        if bundle:
            installed = _install_bundle_into(ws, bundle)
            # Auto-enable the bundle's plugins so a new agent boots WITH its tools live —
            # matching the console install path (which auto-enables on install, ADR 0027).
            # The CLI installer deliberately doesn't enable, so without this the agent
            # comes up with the bundle installed-but-off and the operator has to flip each
            # one on in Settings ▸ Plugins (#1346). Then seed the bundle's recommended
            # per-plugin config defaults (#1350) — a fresh workspace, so nothing to clobber.
            _enable_installed_in_config(cfg, ws / "plugins.lock")
            _apply_bundle_config_defaults(cfg, ws / "plugins.lock")
            # Operator answers to the bundle's declared config_inputs prompts (#2934) —
            # written AFTER the defaults overlay so an explicit answer wins over a
            # bundle-recommended default for the same key.
            _apply_bundle_config_inputs(cfg, ws / "plugins.lock", config_inputs or {})
            # Seed the bundle's MCP servers (ADR 0083 D5, #2011). Separate from the config
            # defaults above because `mcp.servers` is a LIST — the dict-leaf overlay can't
            # merge it — and because its `${input}` placeholders resolve from the env here.
            # Operator-supplied `inputs` (#2041) fill those placeholders ahead of the env.
            _apply_bundle_mcp_servers(cfg, ws / "plugins.lock", inputs or {})
            # Seed operator-supplied values for the bundle's DECLARED secrets (#2041) into the
            # member's secrets.yaml — separate from mcp because these are standalone secrets, not
            # `${input}` fills, and land in the untracked 0600 overlay rather than the config.
            _apply_bundle_secrets(cfg, ws / "plugins.lock", secrets or [])
    except Exception:
        shutil.rmtree(ws, ignore_errors=True)
        raise
    return {**rec, "path": str(ws), "installed": installed}


def _enable_installed_in_config(cfg: Path, lock: Path) -> list[str]:
    """Add a freshly-installed bundle's plugins to ``plugins.enabled`` in the workspace
    config, so the agent starts with them on. Honors each bundle's curated ``enabled``
    subset (cached in the lock by ``_install_bundle``), falling back to every installed
    member; for a bare single-plugin install with no bundle entry, enables that plugin.
    Unions with whatever the template already enabled (``delegates``); returns the ids
    newly added. Best-effort — a malformed lock/config leaves enablement untouched."""
    import json

    from graph.config_io import load_yaml_doc, save_yaml_doc

    try:
        data = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
    except (json.JSONDecodeError, OSError):
        return []
    bundles = data.get("bundles") or []
    want: list[str] = []
    if bundles:
        for b in bundles:
            want += [str(x) for x in (b.get("enabled") or b.get("plugins") or [])]
    else:  # a bare plugin install (no bundle record) — enable what landed
        want += [str(p["id"]) for p in (data.get("plugins") or []) if p.get("id")]
    if not want:
        return []

    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        return []
    plugins = doc.setdefault("plugins", {})
    enabled = list(plugins.get("enabled") or [])
    added = [p for p in want if p not in enabled]
    if added:
        plugins["enabled"] = enabled + added
        save_yaml_doc(doc, cfg)
    return added


def _apply_bundle_config_defaults(cfg: Path, lock: Path) -> dict:
    """Seed a freshly-installed bundle's recommended per-plugin ``config:`` defaults
    into the workspace config (#1350). Defaults only — `bundle_config_overlay` drops any
    key already set in the config, so an operator value is never clobbered. Each plugin's
    config is a top-level section keyed by its id (ADR 0019). Returns the applied overlay.
    Best-effort — a malformed lock/config is a no-op."""
    import json

    from graph.config_io import load_yaml_doc, save_yaml_doc
    from graph.plugins.installer import bundle_config_overlay

    try:
        data = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}
    # Merge every bundle's `config` into one {section: {...}} map. Last-write-wins per
    # section (`dict.update`): if two installed bundles ship defaults for the SAME plugin
    # section, the later bundle's block replaces the earlier one's. That's acceptable —
    # a fresh workspace installs a single archetype bundle, so collisions don't arise in
    # the create() path; the order is lock order if they ever do.
    merged: dict = {}
    for b in data.get("bundles") or []:
        merged.update(b.get("config") or {})
    if not merged:
        return {}

    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        return {}
    overlay = bundle_config_overlay(merged, doc)
    if not overlay:
        return {}
    for section, fill in overlay.items():
        dest = doc.setdefault(section, {})
        if not isinstance(dest, dict):
            continue
        for k, v in fill.items():
            dest[k] = v
    save_yaml_doc(doc, cfg)
    return overlay


def apply_bundle_mcp_servers(cfg: Path, lock: Path, inputs: Mapping[str, str] | None = None) -> list[str]:
    """Seed a freshly-installed bundle's ``mcp:`` servers into the workspace config
    (ADR 0083 D5, #2011). A bundle can carry catalog-shaped MCP templates
    (``{template, inputs}``); each is resolved against the seed-time environment (an
    ``${input}`` whose ``env`` var / ``default`` is set is filled in), normalized, and
    unioned BY NAME into ``mcp.servers`` — a name the config already has always wins, so an
    operator/template value is never clobbered — with ``mcp.enabled`` flipped on. An entry
    whose *required* inputs can't be resolved at seed time lands ``enabled: false``: visible
    in the console MCP panel but inert, so the operator supplies the secret there instead of
    the agent booting a half-templated server.

    ``inputs`` (``{input_key: value}``) are the operator's create-time values (#2041); they
    fill matching ``${input}`` placeholders with priority over the env, so a token supplied at
    create time seeds the server ENABLED rather than visible-but-inert. Empty ``inputs`` keeps
    the pre-existing env-only behavior. Best-effort — a malformed lock/config is a no-op.
    Returns the server names added."""
    import json
    import os

    from graph.config_io import load_yaml_doc, save_yaml_doc
    from graph.mcp_config import resolve_bundle_mcp_item

    try:
        data = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
    except (json.JSONDecodeError, OSError):
        return []
    items: list = []
    for b in data.get("bundles") or []:
        items += list(b.get("mcp") or [])
    if not items:
        return []

    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        return []
    mcp = doc.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        return []
    servers = mcp.get("servers")
    servers = list(servers) if isinstance(servers, list) else []
    have = {s.get("name") for s in servers if isinstance(s, dict)}
    added: list[str] = []
    for item in items:
        try:
            entry, unresolved = resolve_bundle_mcp_item(item, os.environ, inputs or {})
        except ValueError:
            continue  # a malformed template is skipped, never fails the whole create()
        if entry["name"] in have:
            continue  # a name already in the config wins — never clobber
        if unresolved:
            entry["enabled"] = False  # visible-but-inert until the operator fills the secret
        servers.append(entry)
        have.add(entry["name"])
        added.append(entry["name"])
    if not added:
        return []
    mcp["servers"] = servers
    mcp["enabled"] = True  # a bundle that ships servers wants MCP on (matches the add route)
    save_yaml_doc(doc, cfg)
    return added


def apply_bundle_secrets(cfg: Path, lock: Path, secrets_list: list[dict]) -> list[str]:
    """Write operator-supplied values for a bundle's DECLARED secrets into the member's
    ``secrets.yaml`` (#2041). The bundle declares its secrets in the lock
    (``{key, label, placeholder, secret, required}`` per bundle, cached by the installer);
    each declared key is anchored to its bundle's section (the bundle id). ``secrets_list`` is
    the operator's create-time ``[{key, value}]``: for every entry whose ``key`` the bundle
    actually declares, the value lands under that bundle's section via ``config_io.save_secrets``
    (0600, atomic, merge-not-clobber — a sibling secret like the model API key survives).

    Security: values come from ``secrets_list`` (the operator) only — this NEVER reads the host's
    ``os.environ``, so a member never inherits a host secret it wasn't explicitly handed. An
    entry whose key isn't a declared bundle secret, or with a blank value, is ignored. Writes to
    the workspace overlay (``<ws>/config/secrets.yaml``), not the hub's. Best-effort — a
    malformed lock is a no-op. Returns the secret keys written."""
    if not secrets_list:
        return []
    import json

    from graph.config_io import save_secrets

    try:
        data = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
    except (json.JSONDecodeError, OSError):
        return []
    # Map each DECLARED secret key → its bundle's section (the bundle id). Only keys a bundle
    # declares are writable; the operator can't smuggle an arbitrary key into the overlay.
    section_of: dict[str, str] = {}
    for b in data.get("bundles") or []:
        section = str(b.get("id") or "").strip()
        if not section:
            continue
        for dec in b.get("secrets") or []:
            if isinstance(dec, dict):
                key = str(dec.get("key", "")).strip()
                if key:
                    section_of.setdefault(key, section)
    if not section_of:
        return []

    updates: dict[str, dict[str, str]] = {}
    written: list[str] = []
    for item in secrets_list:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        val = item.get("value")
        section = section_of.get(key)
        if not key or not val or section is None:
            continue  # only declared secrets with an operator value are written
        updates.setdefault(section, {})[key] = str(val)
        written.append(key)
    if not updates:
        return []
    # The member reads its secrets at <ws>/config/secrets.yaml (instance_root=<ws>), sibling of
    # the config — write THERE, not the hub's overlay that save_secrets() would resolve by default.
    save_secrets(updates, cfg.parent / "secrets.yaml")
    return written


def apply_bundle_config_inputs(cfg: Path, lock: Path, values: Mapping[str, object] | None = None) -> list[str]:
    """Write operator-supplied answers to a bundle's DECLARED ``config_inputs:`` (#2934)
    into the config YAML at each declared dotted key path. The bundle declares its
    prompts in the lock (``{key, label, type, required, default?}`` per bundle, cached by
    the installer — same pattern as ``apply_bundle_mcp_servers``); ``values`` is the
    operator's create-time ``{dotted_key: value}`` map from the SetupWizard/NewAgentPanel
    Configure step.

    Per declared input: an operator value is coerced to the declared type and written —
    overwriting, because the operator just typed it for THIS install; with no operator
    value, the declared ``default`` fills in only when the key is still absent, so a
    re-install never clobbers a live setting. Only DECLARED keys are writable — an entry
    in ``values`` no bundle declares is ignored, so the operator (or a compromised
    caller) can't smuggle an arbitrary config path. Best-effort — a malformed lock or
    config is a no-op. Returns the dotted keys written."""
    import json

    from graph.config_io import load_yaml_doc, save_yaml_doc
    from graph.plugins.installer import coerce_config_input_value

    try:
        data = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {}
    except (json.JSONDecodeError, OSError):
        return []
    declared: dict[str, dict] = {}
    for b in data.get("bundles") or []:
        for dec in b.get("config_inputs") or []:
            if isinstance(dec, dict) and str(dec.get("key", "")).strip():
                declared.setdefault(str(dec["key"]).strip(), dec)
    if not declared:
        return []

    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        return []

    def _dotted_get(dotted: str) -> object | None:
        node: object = doc
        for seg in dotted.split("."):
            if not isinstance(node, dict) or seg not in node:
                return None
            node = node[seg]
        return node

    def _dotted_set(dotted: str, value: object) -> bool:
        parts = dotted.split(".")
        node = doc
        for seg in parts[:-1]:
            nxt = node.setdefault(seg, {})
            if not isinstance(nxt, dict):
                return False  # an operator scalar sits mid-path — never clobber it with a dict
            node = nxt
        node[parts[-1]] = value
        return True

    written: list[str] = []
    for key, dec in declared.items():
        typ = str(dec.get("type") or "string")
        supplied = (values or {}).get(key)
        if supplied is not None:
            coerced = coerce_config_input_value(typ, supplied)
            if coerced is not None and _dotted_set(key, coerced):
                written.append(key)
                continue
        # No usable operator value → the declared default, and only into an absent key.
        if "default" in dec and dec["default"] is not None and _dotted_get(key) is None:
            coerced = coerce_config_input_value(typ, dec["default"])
            if coerced is not None and _dotted_set(key, coerced):
                written.append(key)
    if written:
        save_yaml_doc(doc, cfg)
    return written


# Public since #2118 — ops/plugins.py::install_and_activate seeds a HOST bundle install
# with the same helpers (host config + host plugins.lock). The private aliases keep the
# workspace-create call sites and existing tests unchanged.
_apply_bundle_mcp_servers = apply_bundle_mcp_servers
_apply_bundle_secrets = apply_bundle_secrets
_apply_bundle_config_inputs = apply_bundle_config_inputs


def _overlay_model(cfg: Path, ws: Path, src: str) -> None:
    """Pop only the ``model:`` section + secrets from another agent's config into this blank one
    — the gateway (provider/api_base/key) carries over so the agent boots ready-to-chat, but its
    plugins/skills/identity stay the blank-template defaults. Best-effort + comment-preserving."""
    src_path = Path(src).expanduser()
    src_cfg = src_path / "langgraph-config.yaml" if src_path.is_dir() else src_path
    if not src_cfg.exists():
        return
    import yaml

    from graph.config_io import load_yaml_doc, save_yaml_doc

    # Read the host's model as PLAIN data (not ruamel) — a ruamel node carries a parent ref and
    # can't be grafted into another document. The destination stays ruamel (comment-preserving).
    host = yaml.safe_load(read_text_utf8(src_cfg)) or {}
    new = load_yaml_doc(cfg)
    if isinstance(host, dict) and isinstance(new, dict) and host.get("model"):
        new["model"] = host["model"]
        save_yaml_doc(new, cfg)  # save_yaml_doc(doc, path) — doc first
    src_sec = (src_path if src_path.is_dir() else src_path.parent) / "secrets.yaml"
    if src_sec.exists():  # carries the api_key so the gateway actually works — sits next to cfg
        shutil.copyfile(src_sec, cfg.parent / "secrets.yaml")


def _stamp_identity(cfg: Path, name: str, shared_skills: bool, *, instance_id: str | None = None) -> None:
    """Force identity.name (display) + instance.id (the opaque data-scope key) on a
    (possibly cloned) config, and optionally set skills.shared — comment-preserving."""
    from graph.config_io import load_yaml_doc, save_yaml_doc

    doc = load_yaml_doc(cfg)
    if not isinstance(doc, dict):
        return
    doc.setdefault("identity", {})["name"] = name
    doc.setdefault("instance", {})["id"] = instance_id or name
    if shared_skills:
        doc.setdefault("skills", {})["shared"] = True
    save_yaml_doc(doc, cfg)


def _server_argv() -> list[str]:
    """Argv prefix that re-invokes THIS server binary. A source checkout launches
    ``python -m server``; in the frozen desktop sidecar ``sys.executable`` *is* the
    server entrypoint, and passing ``-m server`` to it dies at argparse with
    "unrecognized arguments" — the fleet's created-agents never booted there."""
    if getattr(sys, "frozen", False):  # PyInstaller sidecar — entrypoint is already `-m server`
        return [sys.executable]
    return [sys.executable, "-m", "server"]


def _install_bundle_into(ws: Path, bundle: str) -> list[str]:
    """Install a bundle (or plugin) into the workspace via a scoped subprocess —
    ``PROTOAGENT_HOME=<ws>`` makes the workspace the installer's instance root, so
    plugins land at ``<ws>/plugins`` and the lock at ``<ws>/plugins.lock``."""
    env = {
        **os.environ,
        "PROTOAGENT_HOME": str(ws),
    }
    proc = subprocess.run(
        [*_server_argv(), "plugin", "install", bundle],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"bundle install failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    import json

    lock = ws / "plugins.lock"
    try:
        return [p["id"] for p in json.loads(lock.read_text(encoding="utf-8")).get("plugins", [])] if lock.exists() else []
    except (json.JSONDecodeError, OSError):
        return []


def run_exec(ident: str, passthrough: list[str]) -> tuple[dict, list[str]]:
    """Return ``(env_overrides, argv)`` to launch this workspace's server. The CLI
    applies the env and ``exec``s — so the workspace runs as a normal server with
    its instance root + id + port wired in. ``ident`` is an id or display name.

    ``PROTOAGENT_HOME=<ws>`` makes the workspace dir the member's instance root
    (config at ``<ws>/config``, plugins at ``<ws>/plugins``, lock at
    ``<ws>/plugins.lock`` — all derived); ``PROTOAGENT_INSTANCE=<id>`` scopes its
    private data stores."""
    found = _find(ident)
    ws = Path(found["path"]) if found else _ws_dir(ident)
    rec = _read_record(ws)
    if rec is None:
        raise WorkspaceError(f"no workspace {ident!r} at {ws}")
    env = {
        "PROTOAGENT_HOME": str(ws),
        "PROTOAGENT_INSTANCE": str(rec.get("id", ident)),
    }
    argv = [*_server_argv(), "--port", str(rec.get("port", PORT_BASE + 1)), *passthrough]
    return env, argv


#: A retired workspace's record, renamed aside so :func:`list_workspaces` stops seeing it.
#: Renaming it back is the whole undo.
_RETIRED_RECORD = "workspace.yaml.removed"


def _rmtree_resilient(path: Path, *, what: str, attempts: int = 5, delay: float = 0.2) -> None:
    """``shutil.rmtree`` that survives the brief post-exit file lock on Windows (#2583).

    A member's own files (its SQLite stores, its log) can stay open for a moment after the
    process is gone — the handles are released asynchronously, and an antivirus scanner can
    hold them a little longer still. The purge route stops the member and deletes immediately,
    so the delete lost that race often enough to be reported three times across separate test
    rounds: the member was stopped, its port freed and its record cleared, and then rmtree
    raised, so the endpoint answered 500 having already half-completed a destructive op. The
    same request retried by hand always succeeded.

    Retries with a short backoff, and clears the read-only bit on the way — a read-only file
    makes rmtree raise on Windows even when nothing holds it open. Raises
    :class:`WorkspaceBusy` if the tree still won't go, so the caller can report *which* half
    happened instead of a generic failure.
    """
    import stat
    import time as _time

    def _clear_readonly(func, target, _exc):
        # Windows refuses unlink on a read-only file; drop the bit and let rmtree retry it.
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            raise

    last: OSError | None = None
    for attempt in range(attempts):
        try:
            # onexc replaced onerror in 3.12; keep working on both.
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_clear_readonly)
            else:  # pragma: no cover - the frozen desktop pins 3.12+
                shutil.rmtree(path, onerror=lambda f, t, e: _clear_readonly(f, t, e))
            return
        except OSError as exc:
            last = exc
            if attempt < attempts - 1:
                _time.sleep(delay * (attempt + 1))  # 0.2s, 0.4s, 0.6s, 0.8s — ~2s total
    raise WorkspaceBusy(
        f"stopped the agent, but its {what} is still in use and was not deleted ({last}). "
        "The agent IS stopped; retry to remove the workspace."
    )


def remove(ident: str, *, purge: bool = False) -> dict:
    """Take a workspace out of the fleet (by id or display name).

    ``purge=False`` **keeps the agent's data.** The supervisor spawns members with
    ``PROTOAGENT_HOME=<ws>``, so the workspace dir IS the member's entire instance root —
    config, SOUL, chat checkpoints, knowledge, inbox, tasks and memory all live inside it.
    This used to ``rmtree`` that dir unconditionally, so the console's opt-in "Also purge its
    workspace data (irreversible)" switch changed nothing: leaving it off destroyed exactly
    the data it implied you were keeping (#2384). Retiring the workspace now just renames its
    record aside, which is what drops it out of :func:`list_workspaces` — the dir and every
    byte in it stay put, and renaming the record back restores the agent.

    ``purge=True`` is the irreversible one the switch always described: the whole workspace
    dir goes, plus the legacy ``box_root/<id>`` data scope if this install has one. That path
    was hard-coded to ``~/.protoagent/<id>``, which ignores ``PROTOAGENT_BOX_ROOT`` — so on
    the desktop app (box root under ``~/Library/Application Support``) the purge branch could
    never find anything to delete. It resolves through ``infra.paths`` now.

    ``removed`` reports what actually went: ``[]`` for a retire, ``["workspace"]`` (plus
    ``"data"`` when a legacy scope existed) for a purge.
    """
    found = _find(ident)
    ws = Path(found["path"]) if found else _ws_dir(ident)
    rec = _read_record(ws)
    if not ws.exists():
        raise WorkspaceError(f"no workspace {ident!r}")
    iid = (rec or {}).get("id", ident)
    name = (rec or {}).get("name", ident)

    if not purge:
        (ws / "workspace.yaml").rename(ws / _RETIRED_RECORD)
        return {"name": name, "removed": [], "retired_at": str(ws)}

    _rmtree_resilient(ws, what=f"workspace for {name!r}")
    removed = ["workspace"]
    # Pre-ADR-0041 installs scoped a member's private data to a sibling of the box root
    # rather than into the workspace. Resolved (not hard-coded) so a non-default
    # PROTOAGENT_BOX_ROOT is honored; absent on any modern install, hence the exists() guard.
    from infra.paths import instance_paths

    legacy_data = instance_paths().box_root / _safe(str(iid))
    if legacy_data.exists() and legacy_data != ws:
        _rmtree_resilient(legacy_data, what=f"legacy data scope for {name!r}")
        removed.append("data")
    return {"name": name, "removed": removed}


def rename(ident: str, new_name: str) -> dict:
    """Change a workspace's DISPLAY name (by id or current name). The id — and with it
    the dir, the URL slug and the ``~/.protoagent/<id>/*`` data scope — never changes,
    so open windows and checkpoints survive the rename. Also restamps ``identity.name``
    in the workspace config; a RUNNING agent picks that up on its next restart."""
    new_name = _safe(new_name)
    if new_name.lower() in _RESERVED_NAMES:
        raise WorkspaceError(f"{new_name!r} is reserved — it's how the fleet addresses this instance")
    found = _find(ident)
    if found is None:
        raise WorkspaceError(f"no workspace {ident!r}")
    clash = _find(new_name)
    if clash is not None and clash["id"] != found["id"]:
        raise WorkspaceError(f"an agent named {new_name!r} already exists")

    import yaml

    ws = Path(found["path"])
    rec = _read_record(ws) or {}
    rec["name"] = new_name
    rec["label"] = new_name  # display follows (#2520) — else a stale member label would win
    atomic_write(ws / "workspace.yaml", yaml.safe_dump(rec, sort_keys=False))
    cfg = ws / "config" / "langgraph-config.yaml"
    if cfg.exists():  # keep the agent's self-identity in step with the display name
        _stamp_identity(cfg, new_name, False, instance_id=rec.get("id", found["id"]))
    return {"id": found["id"], "name": new_name}
