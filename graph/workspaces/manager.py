"""Workspace lifecycle (ADR 0041) — create / list / run / remove.

A workspace is a directory ``<root>/<name>/`` that *is* an agent: its
``langgraph-config.yaml`` + ``secrets.yaml`` + (once a bundle installs) ``plugins.lock``
+ ``config/plugins/`` live there (so ``PROTOAGENT_CONFIG_DIR=<ws>`` makes it the whole
identity), and ``instance.id = <name>`` scopes its private data to ``~/.protoagent/<name>/*``.
``workspace.yaml`` is the registry record (id, port, bundle, created).

This module only orchestrates the existing knobs — no new runtime, no new storage
format. ``run`` returns the env + argv for the CLI to ``exec`` the normal server.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from infra.paths import atomic_write

PORT_BASE = 7870  # workspaces get PORT_BASE+1, +2, … unless an explicit port is given


class WorkspaceError(Exception):
    """A workspace op was rejected (bad name, collision, missing workspace)."""


# Names that collide with the fleet's routing vocabulary (ADR 0042 slug routing). `host` is the
# reserved slug that addresses THIS instance (`/app/agent/host/` / `/agents/host/*`); a workspace
# named `host` would shadow it → the peer is permanently unreachable + two switcher entries both
# claim to be current. Reject at creation.
_RESERVED_NAMES = {"host"}


def workspaces_root() -> Path:
    """Where workspaces live. ``PROTOAGENT_WORKSPACES_DIR`` overrides; default
    ``~/.protoagent/workspaces``.

    Instance-scoped (ADR 0004) like every other store — ``scope_leaf`` on the final
    resolved path, so a scoped hub owns its own fleet (``~/.protoagent/<iid>/workspaces``
    + its ``fleet.json``) instead of sharing one registry with every co-located instance
    (two hubs pruning/evicting each other's agents). This also fences peers: workspace
    agents run with ``PROTOAGENT_INSTANCE=<name>``, so a peer's fleet view is its own,
    not the parent hub's. Unscoped stays the shared legacy root (#706 warning covers it).
    """
    from infra.paths import scope_leaf

    override = os.environ.get("PROTOAGENT_WORKSPACES_DIR", "").strip()
    base = Path(override).expanduser() if override else (Path.home() / ".protoagent" / "workspaces")
    return scope_leaf(base)


def _safe(name: str) -> str:
    n = (name or "").strip()
    if not n or n != "".join(c for c in n if c.isalnum() or c in "-_"):
        raise WorkspaceError(f"invalid workspace name {name!r} — use letters, digits, '-' or '_'")
    return n


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
        d = yaml.safe_load(f.read_text()) or {}
        return d if isinstance(d, dict) else None
    except yaml.YAMLError:
        return None


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
) -> dict:
    """Scaffold a workspace: its config dir, ``workspace.yaml``, and (with ``bundle``)
    an installed plugin bundle. Does not start it.

    Config base, in precedence:
      * ``from_config`` — a FULL clone of another agent's config + secrets (identity re-stamped).
      * ``inherit_model`` — a BLANK template, but with only that agent's ``model:`` section +
        secrets popped over (the gateway), so it boots ready-to-chat WITHOUT inheriting its
        plugins/skills. This is the fleet's default "new agent" (a blank agent, model carried).
      * neither — the plain blank template.
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

    cfg = ws / "langgraph-config.yaml"
    if from_config:
        src = Path(from_config).expanduser()
        src_cfg = src / "langgraph-config.yaml" if src.is_dir() else src
        if not src_cfg.exists():
            shutil.rmtree(ws, ignore_errors=True)
            raise WorkspaceError(f"--from: no langgraph-config.yaml at {src_cfg}")
        shutil.copyfile(src_cfg, cfg)
        src_sec = (src if src.is_dir() else src.parent) / "secrets.yaml"
        if src_sec.exists():
            shutil.copyfile(src_sec, ws / "secrets.yaml")
        _stamp_identity(cfg, name, shared_skills, instance_id=wid)
    else:
        cfg.write_text(_CONFIG_TEMPLATE.format(name=name, id=wid))
        (ws / "secrets.yaml").write_text("# Per-workspace secrets overlay.\n")
        if inherit_model:
            _overlay_model(cfg, ws, inherit_model)  # gateway only — not plugins/skills
        if shared_skills:
            _stamp_identity(cfg, name, True, instance_id=wid)

    import yaml

    assigned = _pick_port(port)
    rec = {
        "id": wid,
        "name": name,
        "port": assigned,
        "created": datetime.now(timezone.utc).isoformat(),
        "bundle": bundle or "",
    }
    # Reserve the port NOW — write workspace.yaml BEFORE the (possibly minutes-long) bundle
    # install, so a concurrent create can't _pick_port the same port (#11). Then clean up the
    # whole dir on any failure, so a retry doesn't 400 with "already exists" on a poisoned
    # workspace that's invisible in the list (no workspace.yaml).
    atomic_write(ws / "workspace.yaml", yaml.safe_dump(rec, sort_keys=False))
    installed: list[str] = []
    try:
        if bundle:
            installed = _install_bundle_into(ws, bundle)
    except Exception:
        shutil.rmtree(ws, ignore_errors=True)
        raise
    return {**rec, "path": str(ws), "installed": installed}


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
    host = yaml.safe_load(src_cfg.read_text()) or {}
    new = load_yaml_doc(cfg)
    if isinstance(host, dict) and isinstance(new, dict) and host.get("model"):
        new["model"] = host["model"]
        save_yaml_doc(new, cfg)  # save_yaml_doc(doc, path) — doc first
    src_sec = (src_path if src_path.is_dir() else src_path.parent) / "secrets.yaml"
    if src_sec.exists():  # carries the api_key so the gateway actually works
        shutil.copyfile(src_sec, ws / "secrets.yaml")


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


def _install_bundle_into(ws: Path, bundle: str) -> list[str]:
    """Install a bundle (or plugin) into the workspace via a scoped subprocess —
    fresh env so the installer's module-level lock path picks up this workspace."""
    env = {
        **os.environ,
        "PROTOAGENT_CONFIG_DIR": str(ws),
        "PROTOAGENT_PLUGINS_DIR": str(ws / "plugins"),
        "PROTOAGENT_PLUGINS_LOCK": str(ws / "plugins.lock"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "server", "plugin", "install", bundle],
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
        return [p["id"] for p in json.loads(lock.read_text()).get("plugins", [])] if lock.exists() else []
    except (json.JSONDecodeError, OSError):
        return []


def run_exec(ident: str, passthrough: list[str]) -> tuple[dict, list[str]]:
    """Return ``(env_overrides, argv)`` to launch this workspace's server. The CLI
    applies the env and ``exec``s — so the workspace runs as a normal server with
    its config dir + instance + port wired in. ``ident`` is an id or display name."""
    found = _find(ident)
    ws = Path(found["path"]) if found else _ws_dir(ident)
    rec = _read_record(ws)
    if rec is None:
        raise WorkspaceError(f"no workspace {ident!r} at {ws}")
    env = {
        "PROTOAGENT_CONFIG_DIR": str(ws),
        "PROTOAGENT_INSTANCE": str(rec.get("id", ident)),
        "PROTOAGENT_PLUGINS_DIR": str(ws / "plugins"),
        "PROTOAGENT_PLUGINS_LOCK": str(ws / "plugins.lock"),
    }
    argv = [sys.executable, "-m", "server", "--port", str(rec.get("port", PORT_BASE + 1)), *passthrough]
    return env, argv


def remove(ident: str, *, purge: bool = False) -> dict:
    """Delete the workspace dir (by id or display name). With ``purge``, also remove
    its scoped private data at ``~/.protoagent/<id>/``."""
    found = _find(ident)
    ws = Path(found["path"]) if found else _ws_dir(ident)
    rec = _read_record(ws)
    if not ws.exists():
        raise WorkspaceError(f"no workspace {ident!r}")
    iid = (rec or {}).get("id", ident)
    shutil.rmtree(ws)
    removed = ["workspace"]
    if purge:
        data = Path.home() / ".protoagent" / _safe(str(iid))
        if data.exists():
            shutil.rmtree(data)
            removed.append("data")
    return {"name": (rec or {}).get("name", ident), "removed": removed}


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
    atomic_write(ws / "workspace.yaml", yaml.safe_dump(rec, sort_keys=False))
    cfg = ws / "langgraph-config.yaml"
    if cfg.exists():  # keep the agent's self-identity in step with the display name
        _stamp_identity(cfg, new_name, False, instance_id=rec.get("id", found["id"]))
    return {"id": found["id"], "name": new_name}
