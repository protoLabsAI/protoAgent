"""Agent-process lifecycle (ADR 0042 slice 1).

Starts a workspace agent as a **detached background process** (``python -m server
--ui none`` with the workspace's config-dir + instance + port, via
``workspaces.manager.run_exec``), stops it (SIGTERM → reap), and reports status. A
small JSON registry (``<workspaces_root>/fleet.json``) survives the supervisor CLI's
own exit — the agents outlive it, so subsequent ``ls``/``down`` can find them.

Pure orchestration: an agent is an ordinary server; the supervisor just owns its
process. Session continuity is free (each agent's stores are ``instance.id``-scoped).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from graph.workspaces import manager

log = logging.getLogger(__name__)


class FleetError(Exception):
    """A supervisor op was rejected (no such workspace, not running, …)."""


def _state_path() -> Path:
    return manager.workspaces_root() / "fleet.json"


def _load_state() -> dict:
    f = _state_path()
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    f = _state_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2) + "\n")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)  # signal 0 = liveness probe
        return True
    except (OSError, ValueError):
        return False


def _log_path(name: str) -> Path:
    return manager.workspaces_root() / manager._safe(name) / "agent.log"


def is_running(name: str) -> bool:
    rec = _load_state().get(manager._safe(name))
    return bool(rec) and _alive(rec.get("pid"))


def start(name: str) -> dict:
    """Spawn the workspace's agent as a detached background process. No-op (returns
    the live record) if it's already running."""
    name = manager._safe(name)
    ws = next((w for w in manager.list_workspaces() if w["name"] == name), None)
    if ws is None:
        raise FleetError(f"no workspace {name!r} — create it: workspace new {name}")

    state = _load_state()
    rec = state.get(name)
    if rec and _alive(rec.get("pid")):
        return {**rec, "name": name, "running": True, "already": True}

    env, argv = manager.run_exec(name, ["--ui", "none"])
    full_env = {**os.environ, **env}
    log_path = _log_path(name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a")  # noqa: SIM115 — handed to the child; closed on its exit
    # start_new_session detaches it from this CLI's process group so it survives exit.
    proc = subprocess.Popen(argv, env=full_env, stdout=logf, stderr=logf, start_new_session=True)
    now = datetime.now(timezone.utc).isoformat()
    rec = {"pid": proc.pid, "port": ws.get("port"), "id": ws.get("id", name),
           "started_at": now, "last_active": now, "log": str(log_path)}
    state[name] = rec
    _save_state(state)
    log.info("[fleet] started %s (pid %d, :%s)", name, proc.pid, rec["port"])
    return {**rec, "name": name, "running": True, "already": False}


def stop(name: str, *, timeout: float = 8.0) -> dict:
    """SIGTERM the agent and reap its registry entry (SIGKILL if it lingers)."""
    name = manager._safe(name)
    state = _load_state()
    rec = state.get(name)
    if not rec or not _alive(rec.get("pid")):
        state.pop(name, None)
        _save_state(state)
        raise FleetError(f"{name!r} is not running")
    pid = int(rec["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    state.pop(name, None)
    _save_state(state)
    log.info("[fleet] stopped %s (pid %d)", name, pid)
    return {"name": name, "stopped": True}


def status() -> list[dict]:
    """Every workspace + its live status (running/stopped, pid, port)."""
    state = _load_state()
    dirty = False
    out: list[dict] = []
    for ws in manager.list_workspaces():
        rec = state.get(ws["name"]) or {}
        running = _alive(rec.get("pid"))
        if rec and not running:  # stale entry — agent died; clean it
            state.pop(ws["name"], None)
            dirty = True
        port = ws.get("port")
        out.append({"name": ws["name"], "id": ws.get("id", ws["name"]),
                    "port": port, "pid": rec.get("pid") if running else None,
                    "running": running, "bundle": ws.get("bundle", ""),
                    # Direct A2A endpoint — every agent is an independent endpoint on its
                    # own port (ADR 0042), reachable regardless of console focus, so a
                    # focused agent can `delegate_to` an unfocused sibling here. Live only
                    # while running, but the address is stable.
                    "a2a": f"http://127.0.0.1:{port}/a2a" if port else None})
    if dirty:
        _save_state(state)
    return out


def up(names: list[str] | None = None) -> list[dict]:
    """Start a set of agents (named, or all workspaces)."""
    targets = names or [w["name"] for w in manager.list_workspaces()]
    return [start(n) for n in targets]


def down(names: list[str] | None = None) -> list[dict]:
    """Stop a set of agents (named, or all running)."""
    if names is None:
        names = [k for k, r in _load_state().items() if _alive(r.get("pid"))]
    out = []
    for n in names:
        try:
            out.append(stop(n))
        except FleetError:
            pass
    return out


# ── Keep-N-warm policy (ADR 0042 §G) ──────────────────────────────────────────
# Bound how many agents stay hot (a laptop won't run a big fleet). On a switch the
# target is resumed and the least-recently-active agents beyond the cap are stopped —
# their sessions persist (instance.id-scoped checkpoints) and resume on the next switch.

def max_warm() -> int:
    """Warm-agent cap from ``PROTOAGENT_FLEET_MAX_WARM`` (0/unset = unlimited)."""
    try:
        return max(0, int(os.environ.get("PROTOAGENT_FLEET_MAX_WARM", "0")))
    except ValueError:
        return 0


def touch(name: str) -> None:
    """Mark an agent as most-recently-active (drives LRU eviction)."""
    name = manager._safe(name)
    state = _load_state()
    rec = state.get(name)
    if rec:
        rec["last_active"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)


def enforce_warm_cap(keep: int | None = None, *, protect: str | None = None) -> list[str]:
    """Stop the least-recently-active running agents beyond ``keep`` (default
    ``max_warm()``). ``protect`` is never stopped. No-op when keep is 0/unlimited."""
    keep = max_warm() if keep is None else keep
    if keep <= 0:
        return []
    running = [(n, r) for n, r in _load_state().items() if _alive(r.get("pid"))]
    if len(running) <= keep:
        return []
    # Oldest last_active first; the protected agent is never a candidate.
    running.sort(key=lambda kv: kv[1].get("last_active", ""))
    candidates = [n for n, _ in running if n != protect]
    evicted: list[str] = []
    for n in candidates[: len(running) - keep]:
        try:
            stop(n)
            evicted.append(n)
        except FleetError:
            pass
    if evicted:
        log.info("[fleet] keep-%d-warm: evicted %s (sessions resume on next switch)",
                 keep, ", ".join(evicted))
    return evicted
