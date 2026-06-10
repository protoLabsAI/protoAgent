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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

from graph.workspaces import manager

log = logging.getLogger(__name__)


class FleetError(Exception):
    """A supervisor op was rejected (no such workspace, not running, …)."""


def _state_path() -> Path:
    return manager.workspaces_root() / "fleet.json"


def _state_lock() -> FileLock:
    """Cross-process lock around fleet.json read-modify-write (#12) — the hub, the CLI, and
    concurrent requests all touch it, and an unlocked load-modify-save can drop entries."""
    return FileLock(str(_state_path()) + ".lock", timeout=5)


def _is_our_agent(pid: int) -> bool:
    """PID-reuse guard (#10): fleet.json survives reboots, so a recycled pid can make a dead
    agent look alive — and stop() could SIGKILL whatever unrelated process now owns it. Only
    treat/kill a pid as ours if its command line is actually a protoAgent server. Best-effort:
    if we can't inspect it, fall back to trusting the pid (don't break stop on odd platforms)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    if not out.strip():
        return False  # pid not found
    return "-m server" in out or ("python" in out.lower() and "server" in out)


def _load_state() -> dict:
    f = _state_path()
    if not f.exists():
        return {}
    try:
        d = json.loads(f.read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        # Tolerant load keeps the fleet usable, but say so LOUDLY: a corrupt
        # registry means every running agent is forgotten (orphaned processes
        # still holding their ports) until they're re-created.
        log.warning("[fleet] %s unreadable (%s) — treating as empty; running agents may be orphaned", f, e)
        return {}


def _save_state(state: dict) -> None:
    from paths import atomic_write

    atomic_write(_state_path(), json.dumps(state, indent=2) + "\n")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)  # signal 0 = liveness probe
        return True
    except (OSError, ValueError):
        return False


def _resolve_key(ident: str) -> str:
    """The fleet-state key for an id-or-display-name: the workspace's immutable ``id``
    (display names are editable — keying runtime state by them would orphan a running
    agent across a rename). Unknown idents pass through (state-only entries)."""
    ws = manager._find(manager._safe(ident))
    return ws["id"] if ws else manager._safe(ident)


def _log_path(ws: dict) -> Path:
    return Path(ws["path"]) / "agent.log"


def is_running(ident: str) -> bool:
    rec = _load_state().get(_resolve_key(ident))
    return bool(rec) and _alive(rec.get("pid"))


def start(ident: str) -> dict:
    """Spawn the workspace's agent (by id or display name) as a detached background
    process. No-op (returns the live record) if it's already running."""
    ws = manager._find(manager._safe(ident))
    if ws is None:
        raise FleetError(f"no workspace {ident!r} — create it: workspace new {ident}")
    wid, name = ws["id"], ws["name"]

    with _state_lock():
        state = _load_state()
        rec = state.get(wid)
        if rec and _alive(rec.get("pid")):
            return {**rec, "name": name, "running": True, "already": True}

        env, argv = manager.run_exec(wid, ["--ui", "none"])
        full_env = {**os.environ, **env}
        log_path = _log_path(ws)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "a")  # noqa: SIM115 — handed to the child; closed on its exit
        # start_new_session detaches it from this CLI's process group so it survives exit.
        proc = subprocess.Popen(argv, env=full_env, stdout=logf, stderr=logf, start_new_session=True)
        now = datetime.now(timezone.utc).isoformat()
        rec = {"pid": proc.pid, "port": ws.get("port"), "id": wid,
               "started_at": now, "last_active": now, "log": str(log_path)}
        state[wid] = rec
        _save_state(state)
    log.info("[fleet] started %s (pid %d, :%s)", name, proc.pid, rec["port"])
    return {**rec, "name": name, "running": True, "already": False}


def stop(ident: str, *, timeout: float = 8.0) -> dict:
    """SIGTERM the agent (by id or display name) and reap its registry entry (SIGKILL
    if it lingers).

    NOTE: this blocks (busy-wait) up to ``timeout`` — call it off the event loop
    (``asyncio.to_thread``); the routes do. The registry entry is removed under the lock
    first, then the kill happens OUTSIDE the lock so the wait can't freeze other state ops.
    """
    name = _resolve_key(ident)
    with _state_lock():
        state = _load_state()
        rec = state.get(name)
        if not rec or not _alive(rec.get("pid")):
            state.pop(name, None)
            _save_state(state)
            raise FleetError(f"{ident!r} is not running")
        pid = int(rec["pid"])
        state.pop(name, None)  # reserve the stop while we hold the lock
        _save_state(state)
    # Kill outside the lock. Verify the pid is actually our agent first (#10 — a recycled
    # pid after a reboot could otherwise get SIGKILLed even though it's unrelated).
    if _is_our_agent(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + timeout
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    else:
        log.warning("[fleet] %s pid %d is not our agent (pid reuse?) — reaped entry, no kill", name, pid)
    log.info("[fleet] stopped %s (pid %d)", name, pid)
    return {"name": name, "stopped": True}


def _host_entry() -> dict:
    """The instance serving this console — the agent you're *in* (ADR 0042). Always
    present + running, marked ``host: True`` so it can't be stopped/removed from within
    itself. This is the "single agent, like before" you manage until you add peers, so the
    fleet is never empty (you're always at least one — yourself)."""
    import os

    from runtime.state import STATE

    from paths import package_version

    cfg = getattr(STATE, "graph_config", None)
    name = getattr(cfg, "identity_name", "") or "main"
    port = getattr(STATE, "active_port", None)
    return {
        "name": name,
        "id": getattr(cfg, "instance_id", "") or name,
        "port": port,
        "pid": os.getpid(),
        "running": True,
        "bundle": "",
        "host": True,
        "a2a": f"http://127.0.0.1:{port}/a2a" if port else None,
        # The hub's own version — the console compares remote members against it
        # (hub↔remote version handshake, ADR 0042 §I): a remote on a different
        # release is a real, otherwise-invisible /api/* compat surface.
        "version": package_version(),
    }


# ── remote fleet members (ADR 0042 §I, the proxy half) ────────────────────────
# A remote member is another protoAgent reachable by URL (LAN / tailnet / anywhere) that
# joins this fleet as a SWITCHABLE agent: it gets a slug window like a local peer, with the
# hub reverse-proxying its console + A2A. We can't start/stop it — `running` is a cached
# reachability probe. Registry: `<workspaces_root>/remotes.json` (hub-scoped, #813);
# an optional bearer token is stored alongside (0600 + atomic write, same posture as
# secrets.yaml) and attached by the proxy — `status()` never returns it.


def _remotes_path() -> Path:
    return manager.workspaces_root() / "remotes.json"


def _remotes_lock() -> FileLock:
    """Cross-process lock around remotes.json read-modify-write — same pattern as
    ``_state_lock`` but a SIBLING lock file, so remote-registry mutations (two route
    handlers adding members concurrently, a probe persisting a version) serialize
    against each other without contending on fleet.json's lock."""
    return FileLock(str(_remotes_path()) + ".lock", timeout=5)


def _load_remotes() -> dict:
    p = _remotes_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Loud: a corrupt registry silently dropping every remote member —
        # including their stored bearer tokens — is undebuggable otherwise.
        log.warning("[fleet] %s unreadable (%s) — treating as empty; remote members dropped", p, e)
        return {}


def _save_remotes(remotes: dict) -> None:
    from paths import atomic_write

    # 0600: this file carries the remotes' bearer tokens (matching the
    # secrets.yaml posture — written atomically, never group/world readable).
    atomic_write(_remotes_path(), json.dumps(remotes, indent=2), mode=0o600)


def list_remotes() -> list[dict]:
    """Registered remote members, tokens INCLUDED — internal/proxy use only."""
    return list(_load_remotes().values())


def remote_for_slug(slug: str) -> dict | None:
    """The remote record for a slug (id), or None. Token included — proxy use."""
    return _load_remotes().get(slug)


def add_remote(name: str, url: str, token: str = "") -> dict:
    """Register a remote protoAgent as a fleet member. Name follows the workspace
    charset + uniqueness rules; the id is opaque like a local agent's (#823)."""
    name = manager._safe(name)
    if name.lower() in manager._RESERVED_NAMES:
        raise FleetError(f"{name!r} is reserved — it's how the fleet addresses this instance")
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise FleetError(f"remote url must be http(s), got {url!r}")
    with _remotes_lock():
        remotes = _load_remotes()
        taken = ({r["name"] for r in remotes.values()}
                 | {w["name"] for w in manager.list_workspaces()})
        if name in taken:
            raise FleetError(f"an agent named {name!r} already exists")
        if any(r["url"] == url for r in remotes.values()):
            raise FleetError(f"a remote at {url} is already in the fleet")
        rid = manager._new_id(name)
        rec = {"id": rid, "name": name, "url": url, "token": token,
               "added": datetime.now(timezone.utc).isoformat()}
        remotes[rid] = rec
        _save_remotes(remotes)
    log.info("[fleet] remote member added: %s (%s)", name, url)
    return {k: v for k, v in rec.items() if k != "token"}


def remove_remote(ident: str) -> dict:
    """Unregister a remote member (by id or name) — the remote agent itself is untouched."""
    with _remotes_lock():
        remotes = _load_remotes()
        rid = ident if ident in remotes else next(
            (k for k, r in remotes.items() if r["name"] == ident), None)
        if rid is None:
            raise FleetError(f"no remote member {ident!r}")
        rec = remotes.pop(rid)
        _save_remotes(remotes)
    log.info("[fleet] remote member removed: %s", rec["name"])
    return {"id": rid, "name": rec["name"], "removed": ["remote"]}


# Reachability probes are network calls — keep them OFF the status() path (it runs on
# every 3s console poll). `refresh_remote_probes()` is sync + TTL-guarded; the fleet
# route calls it via asyncio.to_thread before status(), so the loop never blocks.
_PROBE_TTL = 10.0
_probe_cache: dict[str, tuple[bool, float]] = {}


def refresh_remote_probes(timeout: float = 1.0) -> None:
    import httpx

    now = time.monotonic()
    for rec in list_remotes():
        hit = _probe_cache.get(rec["id"])
        if hit and now - hit[1] < _PROBE_TTL:
            continue
        version = ""
        try:
            r = httpx.get(f"{rec['url']}/.well-known/agent-card.json", timeout=timeout)
            alive = r.status_code == 200
            if alive:
                try:
                    # The A2A card carries the remote's app version (pyproject
                    # [project].version) — same unauthenticated endpoint the
                    # reachability probe already hits, no extra round-trip.
                    version = str(r.json().get("version", "") or "")
                except ValueError:
                    version = ""
        except httpx.HTTPError:
            alive = False
        _probe_cache[rec["id"]] = (alive, now)
        if version and version != rec.get("version"):
            _record_remote_version(rec["id"], version)


def _record_remote_version(rid: str, version: str) -> None:
    """Persist a probed remote's version on its registry record (hub↔remote version
    handshake) so ``status()`` can surface skew — last-known survives a hub restart.
    Write-on-change only; under the remotes lock so it can't lose a concurrent
    add/remove."""
    with _remotes_lock():
        remotes = _load_remotes()
        rec = remotes.get(rid)
        if rec is not None and rec.get("version") != version:
            rec["version"] = version
            _save_remotes(remotes)


def status() -> list[dict]:
    """The host (this instance) + every workspace + remote members, with live status
    (running/stopped; for remotes, the last cached reachability probe)."""
    with _state_lock():
        state = _load_state()
        dirty = False
        for name in list(state):  # prune dead entries under the lock (#12 — atomic cleanup)
            if not _alive(state[name].get("pid")):
                state.pop(name, None)
                dirty = True
        if dirty:
            _save_state(state)
    out: list[dict] = [_host_entry()]
    for ws in manager.list_workspaces():
        rec = state.get(ws["id"]) or {}  # state is keyed by the immutable id
        running = _alive(rec.get("pid"))
        port = ws.get("port")
        out.append({"name": ws["name"], "id": ws.get("id", ws["name"]),
                    "port": port, "pid": rec.get("pid") if running else None,
                    "running": running, "bundle": ws.get("bundle", ""),
                    # Direct A2A endpoint — every agent is an independent endpoint on its
                    # own port (ADR 0042), reachable regardless of console focus, so a
                    # focused agent can `delegate_to` an unfocused sibling here. Live only
                    # while running, but the address is stable.
                    "a2a": f"http://127.0.0.1:{port}/a2a" if port else None})
    for rec in list_remotes():
        alive = _probe_cache.get(rec["id"], (False, 0.0))[0]
        out.append({"name": rec["name"], "id": rec["id"], "port": None, "pid": None,
                    "running": alive, "bundle": "", "remote": True, "url": rec["url"],
                    # Last-probed remote version (from its A2A card) — NEVER the token.
                    "version": rec.get("version", ""),
                    "a2a": f"{rec['url']}/a2a"})
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


def touch(ident: str) -> None:
    """Mark an agent (by id or display name) as most-recently-active (drives LRU eviction)."""
    key = _resolve_key(ident)
    with _state_lock():
        state = _load_state()
        rec = state.get(key)
        if rec:
            rec["last_active"] = datetime.now(timezone.utc).isoformat()
            _save_state(state)


def enforce_warm_cap(keep: int | None = None, *, protect: str | None = None) -> list[str]:
    """Stop the least-recently-active running agents beyond ``keep`` (default
    ``max_warm()``). ``protect`` is never stopped. No-op when keep is 0/unlimited."""
    keep = max_warm() if keep is None else keep
    if keep <= 0:
        return []
    protect = _resolve_key(protect) if protect else None  # state keys are ids
    running = [(n, r) for n, r in _load_state().items() if _alive(r.get("pid"))]
    if len(running) <= keep:
        return []
    # Oldest last_active first; the protected target is never a candidate. Grace window (#13):
    # an agent touched within PROTOAGENT_FLEET_WARM_GRACE seconds is spared too — it may be mid
    # background turn. (Beyond the grace, eviction can interrupt a turn; the session resumes
    # from its instance.id-scoped checkpoint on the next switch — that's by design.)
    running.sort(key=lambda kv: kv[1].get("last_active", ""))
    # Opt-in grace (default 0 = pure LRU, unchanged): a positive value spares agents touched
    # within that window, trading a temporarily-over-cap fleet for not killing a recently-active
    # (possibly mid-turn) agent. Off by default so rapid switching still bounds the warm set.
    grace = int(os.environ.get("PROTOAGENT_FLEET_WARM_GRACE", "0") or "0")
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace)).isoformat()
    candidates = [n for n, r in running if n != protect and r.get("last_active", "") < cutoff]
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
