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
import ipaddress
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

from infra.paths import pid_alive
from infra.proc import detached_kwargs, signal_tree

from graph.workspaces import manager

log = logging.getLogger(__name__)


class FleetError(Exception):
    """A supervisor op was rejected (no such workspace, not running, …)."""


#: Bounded grace after SIGKILL before ``stop()`` decides the process really survived.
#: SIGKILL is not instantaneous (kernel teardown, and ``_alive`` reaps a zombie child
#: first), so reporting immediately would race the very thing we're asserting (#2286).
_KILL_GRACE = 2.0


def _state_path() -> Path:
    return manager.workspaces_root() / "fleet.json"


def _state_lock() -> FileLock:
    """Cross-process lock around fleet.json read-modify-write (#12) — the hub, the CLI, and
    concurrent requests all touch it, and an unlocked load-modify-save can drop entries."""
    return FileLock(str(_state_path()) + ".lock", timeout=5)


def _server_binary_names() -> set[str]:
    """Basenames that identify a **frozen** protoAgent server in a command line.

    ``manager._server_argv()`` launches members as ``python -m server`` from a source
    checkout but as the *bare sidecar binary* when frozen (PyInstaller's entrypoint is
    already ``-m server``, so passing it dies at argparse). The desktop sidecar is
    ``binaries/protoagent-server`` (``tauri.conf.json`` ``externalBin``)."""
    names = {"protoagent-server", "protoagent_server"}
    if getattr(sys, "frozen", False):
        # This hub IS the sidecar, and members are spawned with our own sys.executable —
        # so our basename is authoritative even if the bundle is renamed downstream.
        names.add(Path(sys.executable).name)
    return {n for name in names for n in (name, f"{name}.exe")}


def _is_our_agent(pid: int) -> bool:
    """PID-reuse guard (#10): fleet.json survives reboots, so a recycled pid can make a dead
    agent look alive — and stop() could SIGKILL whatever unrelated process now owns it. Only
    treat/kill a pid as ours if its command line is actually a protoAgent server. Best-effort:
    if we can't inspect it, fall back to trusting the pid (don't break stop on odd platforms).

    Matching only the *source* form (``-m server`` / ``python …server``) made every *frozen*
    desktop member read as "not ours" (#2286): ``stop()`` then reaped the registry entry
    without signalling anything and still reported ``stopped: True``, leaving an orphan alive
    and holding its port against any replacement — and ``shutdown_all()`` skipped members
    entirely, so they outlived the hub that spawned them."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    cmd = out.strip()
    if not cmd:
        return False  # pid not found
    if "-m server" in cmd or ("python" in cmd.lower() and "server" in cmd):
        return True
    return any(name in cmd for name in _server_binary_names())


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
    from infra.paths import atomic_write

    atomic_write(_state_path(), json.dumps(state, indent=2) + "\n")


def _reap(pid: int) -> None:
    """Reap ``pid`` if it's a dead child of this hub so it stops lingering as a zombie.

    A member spawned by *this* hub is a child process (``start()`` Popen, detached but
    still our child). When it dies — e.g. a SIGKILL crash — it stays a **zombie** in the
    process table until reaped, and ``os.kill(pid, 0)`` reports a zombie as *alive*. That
    masks the crash from ``status()``/``is_running()`` (the member shows ``running`` after
    it's gone) and makes ``start()`` short-circuit on the dead pid (a no-op restart).

    A *targeted* non-blocking ``waitpid`` reaps only this pid — it never steals another
    child's exit status (the SIGCHLD-reaper footgun), and it raises ``ECHILD`` (ignored)
    when the pid isn't our child (e.g. a member reparented to init after a hub restart —
    which init then reaps itself, so it never becomes a lingering zombie here anyway).

    Windows has no zombies (and no ``os.WNOHANG`` — accessing it raised
    ``AttributeError`` straight through ``_alive``, ADR 0098): nothing to reap."""
    if os.name == "nt":
        return
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except (OSError, ValueError):
        pass  # ECHILD (not our child / already reaped), or a bad pid — nothing to do


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    _reap(pid)  # clear a crashed child's zombie first, so the probe below sees it as gone
    try:
        # pid_alive, not os.kill(pid, 0): the raw idiom isn't a liveness probe on
        # Windows (signal 0 = CTRL_C_EVENT → OSError on live pids) and misreads a
        # POSIX PermissionError (exists) as dead (#1678). A reaped zombie is gone
        # from the table, so it still probes as dead here.
        return pid_alive(int(pid))
    except ValueError:
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


def _port_listening(port: int | None, timeout: float = 0.25) -> bool:
    """Is something accepting connections on localhost:``port``? The member binds its
    HTTP port early in boot, so this is the cheap 'it came up' signal."""
    if not port:
        return False
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, OverflowError, ValueError):  # unreachable, or a nonsense port record
        return False


def _log_tail_since(log_path: Path, offset: int, limit: int = 500) -> str:
    """The last ``limit`` chars the child wrote past ``offset`` (the log is opened
    append — earlier runs' output stays; only this boot's lines are the reason)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            fresh = f.read().decode("utf-8", errors="replace").strip()
        return fresh[-limit:]
    except OSError:
        return ""


# How long start() watches a fresh spawn for insta-death before returning. It returns
# EARLY on either signal (port bound → up, process exited → raise), so a healthy boot
# pays only its own bind latency and only a hung-but-not-listening child waits it out.
_BOOT_WATCH_SECONDS = 10.0


def start(ident: str) -> dict:
    """Spawn the workspace's agent (by id or display name) as a detached background
    process. No-op (returns the live record) if it's already running.

    Raises ``FleetError`` (with the fresh ``agent.log`` tail) when the child exits
    during the boot watch — a member that dies at boot (broken spawn, bad config,
    taken port) used to report ``running: true`` and just show up dead on the next
    poll, with the reason buried in a log nothing surfaced (#1565 fallout).
    """
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
        # Fleet service token (ADR 0089): the instance's internal, loopback-only credential.
        # A hub-spawned member binds loopback and is reached in-process (the fleet proxy) or by
        # its own PM/delegates. It used to run OPEN — inheriting the hub's inbound
        # ``A2A_AUTH_TOKEN`` would have made it demand a bearer a token-free local dispatch never
        # sent (a spawned team unreachable by its own PM), so the token was stripped. That left
        # a member's ``/api`` (plugin install/enable, config rewrite — code execution) reachable
        # by any local process: a loopback RCE hole.
        from graph.fleet.service_token import ENV_VAR as _FLEET_ENV
        from graph.fleet.service_token import resolve_service_token

        fleet_token = resolve_service_token()
        full_env[_FLEET_ENV] = fleet_token
        # ADR 0089 D5 — close the member: the fleet token becomes its inbound bearer, delivered
        # by ENV (never the config file, so ``workspace new --from`` can't copy it). Every
        # in-instance caller now presents this same token — the hub's reverse proxy and the
        # delegate adapters from the OUTSIDE (ADR 0089 D3/D4), and the member's OWN
        # scheduler/background/inbox self-POSTs, which bearer from ``config.auth_token or
        # A2A_AUTH_TOKEN`` and so authenticate to its own ``/a2a`` for free. A workspace that set
        # its own token via config keeps it (``configure()`` prefers ``config.auth_token``; the
        # member still ACCEPTS the fleet token via the ``_FLEET`` tier). With a credential now
        # gating the surface, ``PROTOAGENT_ALLOW_OPEN`` is unnecessary (a non-loopback bind is
        # allowed on its own merit) — dropped rather than set.
        if "A2A_AUTH_TOKEN" not in env:
            full_env["A2A_AUTH_TOKEN"] = fleet_token
            full_env.pop("PROTOAGENT_ALLOW_OPEN", None)
        # A2A URL-based multi-tenancy (A2A spec — Multi-Tenancy § URL-based routing). The
        # hub reverse-proxies ``/agents/<slug>/*`` to this member (ADR 0042), so the member's
        # reachable A2A endpoint is the hub's tenant SUB-PATH, not the hub root. But the child
        # inherits the hub's ``A2A_PUBLIC_URL`` here, so its agent-card's interface ``url``
        # (``_a2a_card_url`` = ``{A2A_PUBLIC_URL}/a2a``) would advertise the hub root — a peer
        # that DISCOVERS the member's card would dial the hub (→ the hub agent) instead of the
        # member, and every member's card would collide on the same URL. Point the member's
        # advertised public URL at its own tenant path so its card is self-consistent and
        # directly dial-able (``{hub}/agents/<wid>/a2a``). Respect an explicit per-workspace
        # override (mirrors the token rule above); no-op when the hub itself has no public URL
        # (local/desktop runs advertise the bound loopback port, already per-member).
        if "A2A_PUBLIC_URL" not in env:
            hub_public_url = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
            if hub_public_url:
                full_env["A2A_PUBLIC_URL"] = f"{hub_public_url}/agents/{wid}"
        # AGENT_NAME namespaces several subsystems that read os.environ directly rather
        # than the resolved config identity: Prometheus metric prefixes
        # (observability/metrics.py), trace tags (observability/tracing.py,
        # graph/middleware/trace_context.py), scheduler storage
        # (scheduler/local.py — a real collision risk when members share a storage
        # path), and the ``<AGENT_NAME>_API_KEY`` lookup (server/agent_init.py,
        # evals/client.py). full_env otherwise inherits the hub's AGENT_NAME from
        # os.environ, so a member runs every one of those under the HUB's name. An
        # explicit per-workspace value still wins (mirrors the pair above).
        if "AGENT_NAME" not in env:
            full_env["AGENT_NAME"] = name
        log_path = _log_path(ws)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_offset = log_path.stat().st_size if log_path.exists() else 0
        logf = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — handed to the child; closed on its exit
        # Detached (ADR 0098): its own tree root, so it survives this CLI's exit
        # and stop()/shutdown_all() can take down the whole member tree later.
        proc = subprocess.Popen(argv, env=full_env, stdout=logf, stderr=logf, **detached_kwargs())
        from infra.paths import package_version

        now = datetime.now(timezone.utc).isoformat()
        rec = {
            "pid": proc.pid,
            "port": ws.get("port"),
            "id": wid,
            "started_at": now,
            "last_active": now,
            "log": str(log_path),
            # The spawner's app version (version-coherence P1/P2): a member is
            # this binary until restarted, so status()/version_skew_warning()
            # can surface a live member left behind by an app update.
            "version": package_version(),
        }
        state[wid] = rec
        _save_state(state)

    # Boot watch — OUTSIDE the lock (like stop()'s kill) so other state ops can't freeze
    # behind it. NOTE: blocks up to _BOOT_WATCH_SECONDS; call off the event loop
    # (``asyncio.to_thread``) — the routes do.
    deadline = time.monotonic() + _BOOT_WATCH_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # died at boot — reap the entry + surface the reason
            with _state_lock():
                state = _load_state()
                if (state.get(wid) or {}).get("pid") == proc.pid:
                    state.pop(wid, None)
                    _save_state(state)
            tail = _log_tail_since(log_path, log_offset)
            log.error("[fleet] %s exited during boot (code %s): %s", name, proc.returncode, tail)
            raise FleetError(
                f"{name!r} exited during boot (exit code {proc.returncode})."
                + (f" Log tail: {tail}" if tail else "")
                + f" Full log: {log_path}"
            )
        if _port_listening(rec["port"]):
            break
        time.sleep(0.2)
    log.info("[fleet] started %s (pid %d, :%s)", name, proc.pid, rec["port"])
    return {**rec, "name": name, "running": True, "already": False}


def stop(ident: str, *, timeout: float = 8.0) -> dict:
    """Gracefully tree-signal the agent (by id or display name) and reap its registry
    entry (hard tree-kill if it lingers) — ADR 0098: the member's children die with it.

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
    if not _is_our_agent(pid):
        # The pid belongs to something else, so OUR member is genuinely gone (its pid was
        # recycled). Reaping the entry is right and `stopped` is true — but say why we
        # never signalled, so an operator reading the log isn't left guessing.
        log.warning("[fleet] %s pid %d is not our agent (pid reuse?) — reaped entry, no kill", name, pid)
        return {
            "name": name,
            "stopped": True,
            "note": f"pid {pid} is not a protoAgent server (pid reuse?) — no signal sent",
        }
    try:
        signal_tree(pid, force=False)
        deadline = time.monotonic() + timeout
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _alive(pid):
            signal_tree(pid, force=True)
            # SIGKILL is not instantaneous — the kernel still has to tear the process down,
            # and _alive() reaps a zombie child first. Give it a bounded grace so we report
            # the settled truth rather than a race.
            hard = time.monotonic() + _KILL_GRACE
            while _alive(pid) and time.monotonic() < hard:
                time.sleep(0.1)
    except OSError:
        pass
    if _alive(pid):
        # #2286: never claim a stop we didn't achieve. Restore the entry — a live process
        # that the registry has forgotten is the worst of both states: invisible to the hub,
        # still serving, and still holding its port against any replacement.
        with _state_lock():
            state = _load_state()
            state.setdefault(name, rec)
            _save_state(state)
        log.error("[fleet] %s pid %d survived SIGTERM+SIGKILL — entry restored, still running", name, pid)
        return {
            "name": name,
            "stopped": False,
            "reason": f"process still running at PID {pid} after SIGTERM+SIGKILL",
        }
    log.info("[fleet] stopped %s (pid %d)", name, pid)
    return {"name": name, "stopped": True}


def _host_entry() -> dict:
    """The instance serving this console — the agent you're *in* (ADR 0042). Always
    present + running, marked ``host: True`` so it can't be stopped/removed from within
    itself. This is the "single agent, like before" you manage until you add peers, so the
    fleet is never empty (you're always at least one — yourself)."""
    import os

    from runtime.state import STATE

    from infra.paths import package_version

    cfg = getattr(STATE, "graph_config", None)
    name = getattr(cfg, "identity_name", "") or "main"
    port = getattr(STATE, "active_port", None)
    entry = {
        "name": name,
        "label": name,  # identity.name is already the verbatim display value
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
    # This instance is itself a workspace member SPAWNED by another hub's supervisor
    # (the `workspace.yaml` record at its instance root is the spawn-time marker).
    # Surfaced read-only on the existing /api/fleet payload so a console reaching the
    # member DIRECTLY (its own port) can gate hub-only affordances — the header's
    # "Fleet settings" item disables with a point-at-the-host tooltip (#1708).
    if manager.is_workspace_member():
        entry["member"] = True
    return entry


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
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Loud: a corrupt registry silently dropping every remote member —
        # including their stored bearer tokens — is undebuggable otherwise.
        log.warning("[fleet] %s unreadable (%s) — treating as empty; remote members dropped", p, e)
        return {}
    if not isinstance(d, dict):
        # The registry is an ``{id: record}`` map. A hand-edited file holding a
        # LIST parses fine and then breaks every reader on the first ``.get()``/
        # ``.values()`` — an AttributeError out of ``list_remotes()`` that used
        # to escape ``status()`` and 500 both /api/fleet and the telemetry
        # rollup (#3018). Same tolerance ``_load_state`` already applies to its
        # own file, and loud for the same reason as an unreadable one.
        log.warning(
            "[fleet] %s holds a %s, not an object — treating as empty; remote members dropped",
            p,
            type(d).__name__,
        )
        return {}
    return d


def _save_remotes(remotes: dict) -> None:
    from infra.paths import atomic_write

    # 0600: this file carries the remotes' bearer tokens (matching the
    # secrets.yaml posture — written atomically, never group/world readable).
    atomic_write(_remotes_path(), json.dumps(remotes, indent=2), mode=0o600)


def list_remotes() -> list[dict]:
    """Registered remote members, tokens INCLUDED — internal/proxy use only.

    Normalized for its readers the way ``manager.list_workspaces`` already
    normalizes the local half of the roster, so ``status()`` and
    ``refresh_remote_probes()`` can rely on an ``id`` being there: the registry
    is keyed BY id, so a record that lost its own ``id`` field is recovered from
    its key instead of raising KeyError at the first reader (#3018). A value
    that isn't a record at all carries nothing to recover — it is dropped, and
    said out loud, because a member vanishing in silence is undebuggable.

    Read-side only: the read-modify-write paths use ``_load_remotes`` directly,
    so nothing here can rewrite (or erase) what is actually on disk.
    """
    out: list[dict] = []
    for rid, rec in _load_remotes().items():
        if not isinstance(rec, dict):
            log.warning("[fleet] remote %r is a %s, not an object — dropped from the roster", rid, type(rec).__name__)
            continue
        out.append(rec if rec.get("id") else {**rec, "id": rid})
    return out


def remote_for_slug(slug: str) -> dict | None:
    """The remote record for a slug (id), or None. Token included — proxy use."""
    return _load_remotes().get(slug)


def _canonical_url(url: str) -> str:
    """Canonicalize a remote BASE URL, or raise ``FleetError`` — no egress check.

    Identity matters here, not just syntax: the registry matches a remote by URL (add's
    duplicate check, pair's add-vs-re-token decision) and the token-clearing rule compares
    ORIGINS. So ``HTTP://Ava.Tail:80/`` and ``http://ava.tail`` must be the same string:
    scheme + host lowercased, the scheme's default port dropped, trailing slash trimmed.

    A remote is a protoAgent's BASE URL — the hub appends ``/a2a``, ``/api/…``,
    ``/.well-known/…`` to it — so a path, query, fragment or userinfo is refused rather than
    silently carried into every proxied request (ADR 0113 review). A phone pairing link
    (``…/app/#pair=<code>``) is the likeliest thing to be pasted by mistake; it gets its own
    message.
    """
    from urllib.parse import urlsplit

    u = (url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        raise FleetError(f"remote url must be http(s), got {u!r}")
    if "#pair=" in u:
        raise FleetError("that's a phone pairing link — use the agent's base URL and an agent code")
    try:
        parts = urlsplit(u)
        port = parts.port
    except ValueError as exc:
        raise FleetError(f"remote url {u!r} is not a valid URL ({exc})") from exc
    host = (parts.hostname or "").lower()
    if not host:
        raise FleetError(f"remote url {u!r} has no host")
    if parts.username is not None or parts.password is not None:
        raise FleetError("remote url must not carry credentials — pair with a code, or pass the bearer as a token")
    if parts.path.rstrip("/") or parts.query or parts.fragment:
        raise FleetError(f"remote url must be the agent's base URL (scheme://host[:port]), not {u!r}")
    scheme = parts.scheme.lower()
    if port == {"http": 80, "https": 443}[scheme]:
        port = None
    netloc = f"[{host}]" if ":" in host else host  # IPv6 literal keeps its brackets
    if port is not None:
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def _url_key(url: object) -> str:
    """A STORED record's URL in canonical form, for identity comparisons — tolerant, so a
    record written before canonicalization (or hand-edited) still matches its canonical
    twin instead of being duplicated, and one that no longer parses compares as itself."""
    try:
        return _canonical_url(str(url or ""))
    except FleetError:
        return str(url or "").strip().rstrip("/")


def _normalize_remote_url(url: str) -> str:
    """Validate + canonicalize a remote member URL (see ``_canonical_url``), SSRF-guarded.
    Shared by add/update/pair. Raises FleetError.

    SSRF guard (#871): the hub reverse-proxies /agents/<slug>/* to this URL, so a registered
    remote can turn the hub into an internal-network proxy. Fleet remotes ARE normally private
    (LAN / tailnet / a co-located instance), so allow_private — but ALWAYS block
    link-local/cloud-metadata (169.254.169.254), multicast, reserved.
    """
    u = _canonical_url(url)
    from security import egress

    if egress.check_url(u, allow_private=True, block_unresolvable=False):
        raise FleetError(
            f"remote url {u} is blocked by the egress guard (link-local/metadata/"
            f"reserved address); allowlist it via egress.allowed_hosts if intentional"
        )
    return u


# ── transport rule (ADR 0113 D10) ─────────────────────────────────────────────
# A credential — a pairing code going out, a bearer being stored for the proxy to present on
# every call — crosses plain http:// only where the network already encrypts or never leaves
# the box: loopback, or a tailnet (WireGuard underneath; 100.64.0.0/10, Tailscale's IPv6
# ULA, or a MagicDNS ``*.ts.net`` name). Any other name is judged by the addresses it
# resolves to NOW. Everything else is refused unless the caller opts in explicitly
# (``allow_insecure`` / ``--insecure-http``) — mirroring ``deck.hub.credential_allowed``,
# plus the tailnet exception.
_TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


class InsecureTransport(FleetError):
    """Refused: a credential would cross a plaintext network (ADR 0113 D10). A 400."""


def _cleartext_host(url: str) -> str | None:
    """The host a credential sent to ``url`` would reach IN CLEARTEXT over an untrusted
    network, or ``None`` when that's not the case (https, loopback, tailnet). A name that
    doesn't resolve can't be judged, so it counts as cleartext."""
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme.lower() == "https":
        return None
    host = (parts.hostname or "").lower()
    if host.endswith(".ts.net"):
        return None
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(ai[4][0].split("%")[0]) for ai in socket.getaddrinfo(host, None)}
        except (OSError, ValueError, UnicodeError):
            return host
    if addrs and all(a.is_loopback or any(a in n for n in _TAILNET_NETS) for a in addrs):
        return None
    return host


def _require_secure_transport(url: str, *, allow_insecure: bool, what: str) -> None:
    """Raise ``InsecureTransport`` (nothing sent, nothing stored) unless ``what`` can go to
    ``url`` without crossing an untrusted network in cleartext, or the caller opted in."""
    if allow_insecure:
        return
    host = _cleartext_host(url)
    if host is not None:
        raise InsecureTransport(
            f"plain http to {host} would send {what} in cleartext on this network — use its tailnet "
            "address, https, or confirm with allow_insecure/--insecure-http"
        )


def add_remote(name: str, url: str, token: str = "", *, device_id: str = "", allow_insecure: bool = False) -> dict:
    """Register a remote protoAgent as a fleet member. Name follows the workspace
    charset + uniqueness rules; the id is opaque like a local agent's (#823). ``device_id``
    (set by ``pair_remote``) is the REMOTE's id for the device its token was minted for —
    not a secret; it is what a later re-pair revokes. A non-empty ``token`` for a plain-http
    URL off loopback/tailnet is refused unless ``allow_insecure`` (ADR 0113 D10)."""
    name = manager._safe(name)
    if name.lower() in manager._RESERVED_NAMES:
        raise FleetError(f"{name!r} is reserved — it's how the fleet addresses this instance")
    url = _normalize_remote_url(url)
    if token:
        _require_secure_transport(url, allow_insecure=allow_insecure, what="its stored token")
    with _remotes_lock():
        remotes = _load_remotes()
        taken = {r["name"] for r in remotes.values()} | {w["name"] for w in manager.list_workspaces()}
        if name in taken:
            raise FleetError(f"an agent named {name!r} already exists")
        if any(_url_key(r.get("url")) == url for r in remotes.values()):
            raise FleetError(f"a remote at {url} is already in the fleet")
        rid = manager._new_id(name)
        rec = {"id": rid, "name": name, "url": url, "token": token, "added": datetime.now(timezone.utc).isoformat()}
        if device_id:
            rec["device_id"] = device_id
        remotes[rid] = rec
        _save_remotes(remotes)
    log.info("[fleet] remote member added: %s (%s)", name, url)
    return {k: v for k, v in rec.items() if k != "token"}


def update_remote(
    ident: str,
    *,
    name: str | None = None,
    url: str | None = None,
    token: str | None = None,
    device_id: str | None = None,
    allow_insecure: bool = False,
) -> dict:
    """Edit a registered remote's ``name`` / ``url`` / ``token`` in place (by id or name).

    Only the fields you pass change — a ``None`` field is left as-is, so ``token=None`` KEEPS
    the stored bearer while ``token=""`` clears it (the recovery path for a rotated/wrong
    token). The id — and so the URL slug, open windows, and data scope — never changes. Re-runs
    the same reserved-name / SSRF-egress / collision checks as ``add_remote``. Returns the
    sanitized record (token stripped). ``FleetError`` if no such remote.

    **A token never follows a URL to a new origin** (ADR 0113 review, M1). The stored bearer
    was issued BY the old host; moving the url to a different scheme/host/port without
    passing a ``token`` in the same call CLEARS it, and the response says
    ``token_cleared: true``. Otherwise the proxy — and the auth probe, every 30s — would
    present one host's operator credential to another. Re-pairing is one code. A url edit
    that canonicalizes to the same origin (a trailing slash, case, a default port) is not a
    move and keeps the token.

    ``device_id`` is ``pair_remote``'s bookkeeping; any other token change (a paste, a
    clear, a cleared-by-move) forgets the stored one, since it no longer names the device
    the stored token belongs to.

    Storing a non-empty ``token`` for a plain-http URL off loopback/tailnet is refused
    unless ``allow_insecure`` (ADR 0113 D10) — checked against the url the record will have.
    """
    with _remotes_lock():
        remotes = _load_remotes()
        rid = ident if ident in remotes else next((k for k, r in remotes.items() if r["name"] == ident), None)
        if rid is None:
            raise FleetError(f"no remote member {ident!r}")
        rec = dict(remotes[rid])
        if name is not None:
            new_name = manager._safe(name)
            if new_name.lower() in manager._RESERVED_NAMES:
                raise FleetError(f"{new_name!r} is reserved — it's how the fleet addresses this instance")
            others = {r["name"] for k, r in remotes.items() if k != rid} | {
                w["name"] for w in manager.list_workspaces()
            }
            if new_name in others:
                raise FleetError(f"an agent named {new_name!r} already exists")
            rec["name"] = new_name
        token_cleared = False
        if url is not None:
            new_url = _normalize_remote_url(url)
            if any(_url_key(r.get("url")) == new_url for k, r in remotes.items() if k != rid):
                raise FleetError(f"a remote at {new_url} is already in the fleet")
            # A canonical remote URL IS its origin (no path is allowed), so comparing the
            # canonical forms is the origin comparison.
            if _url_key(rec.get("url")) != new_url and token is None and rec.get("token"):
                token = ""
                token_cleared = True
            rec["url"] = new_url
        if token:
            _require_secure_transport(str(rec.get("url") or ""), allow_insecure=allow_insecure, what="its stored token")
        if token is not None:
            rec["token"] = token
            if device_id:
                rec["device_id"] = device_id
            else:
                rec.pop("device_id", None)
        remotes[rid] = rec
        _save_remotes(remotes)
    _probe_cache.pop(rid, None)  # url/token may have changed reachability — force a fresh probe
    _auth_cache.pop(rid, None)  # …and the token the auth probe vouched for (ADR 0113 D5)
    if token_cleared:
        log.info("[fleet] remote member %s moved to a new origin — its stored token was cleared (re-pair)", rec["name"])
    log.info("[fleet] remote member updated: %s (%s)", rec["name"], rec["url"])
    out = {k: v for k, v in rec.items() if k != "token"}
    if token_cleared:
        out["token_cleared"] = True
    return out


def remove_remote(ident: str) -> dict:
    """Unregister a remote member (by id or name) — the remote agent itself is untouched."""
    with _remotes_lock():
        remotes = _load_remotes()
        rid = ident if ident in remotes else next((k for k, r in remotes.items() if r["name"] == ident), None)
        if rid is None:
            raise FleetError(f"no remote member {ident!r}")
        rec = remotes.pop(rid)
        _save_remotes(remotes)
    _probe_cache.pop(rid, None)
    _auth_cache.pop(rid, None)
    log.info("[fleet] remote member removed: %s", rec["name"])
    return {"id": rid, "name": rec["name"], "removed": ["remote"]}


# Reachability probes are network calls — keep them OFF the status() path (it runs on
# every 3s console poll). `refresh_remote_probes()` is sync + TTL-guarded; the fleet
# route calls it via asyncio.to_thread before status(), so the loop never blocks.
# TTL aligns with the 3s console poll so a just-downed remote doesn't keep showing
# 'running' for a full poll-and-a-bit after it dies.
_PROBE_TTL = 3.0
_probe_cache: dict[str, tuple[bool, float]] = {}

# Authenticated probe (ADR 0113 D5). The reachability probe above is the UNAUTHENTICATED
# agent card, so a remote whose stored token is wrong — or whose paired device was revoked
# on the remote — still read "running", then 401'd on the first proxied click. When a token
# is stored the hub also makes one cheap operator-gated GET with it and records
# ``auth: ok | rejected | unknown | open`` (``none`` = no token stored, computed, never probed).
# Slower TTL than reachability: a revocation is rare and 30s is soon enough, and it keeps
# the 3s poll to one request per remote on most ticks. Same off-the-status-path posture:
# ``refresh_remote_probes`` (run via to_thread by the route) fills it, ``status()`` only reads.
#
# Why ``/api/devices``: it is operator-gated like every ``/api/*`` route (a non-operator
# bearer is 403'd by the auth middleware before routing, a wrong one 401'd), it is a small
# JSON-file read with no subprocess or network fan-out (``/api/runtime/status`` shells out to
# ``ps`` for its co-location probe, ``/api/fleet`` probes ITS remotes), and it is the ADR 0087
# surface the paired token itself belongs to. A pre-0087 remote 404s it → ``unknown``, which
# is honest: we can't tell from a 404 whether the token would have been accepted.
#
# ``open``: a 200 WITH the token proves nothing when the remote answers WITHOUT one too (an
# instance with no auth configured accepts any bearer). So the probe asks unauthenticated
# first: a 200 there is ``open`` — reachable and drivable, but the token is unverified and
# anyone who can reach it can drive it — and only a 401/403 goes on to the tokened request.
_AUTH_TTL = 30.0
_AUTH_PROBE_PATH = "/api/devices"
_auth_cache: dict[str, tuple[str, float]] = {}


def _auth_get(url: str, token: str, timeout: float) -> int:
    """GET the auth-probe path (with ``token`` as the bearer when given); the status code.
    Redirects are NOT followed: a 3xx would carry the bearer to wherever it points."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(f"{url}{_AUTH_PROBE_PATH}", headers=headers, timeout=timeout, follow_redirects=False).status_code


def _auth_probe_one(rec: dict, timeout: float) -> str:
    """Probe ONE remote with its stored bearer and cache the verdict. Returns ``"ok"``
    (200 with the token, 401/403 without), ``"open"`` (200 even WITHOUT a token — the token
    is unverifiable), ``"rejected"`` (401/403 with the token), ``"unknown"`` (anything else,
    incl. transport errors), or ``"none"`` (no token — no request made). Network call — keep
    it off the event loop. The token goes in a header only; it is never logged or cached."""
    import httpx

    rid = str(rec.get("id") or "")
    token = str(rec.get("token") or "")
    url = str(rec.get("url") or "")
    if not token:
        _auth_cache.pop(rid, None)
        return "none"
    if not url:
        verdict = "unknown"
    else:
        try:
            anon = _auth_get(url, "", timeout)
            if anon == 200:
                verdict = "open"
            elif anon in (401, 403):
                code = _auth_get(url, token, timeout)
                verdict = "ok" if code == 200 else "rejected" if code in (401, 403) else "unknown"
            else:
                verdict = "unknown"  # e.g. a 404 from a remote that predates the route
        except httpx.HTTPError:
            verdict = "unknown"
    _auth_cache[rid] = (verdict, time.monotonic())
    return verdict


def remote_auth(rec: dict) -> str:
    """The cached auth verdict for a remote record (``none`` when it stores no token,
    ``unknown`` until the first authenticated probe lands). Cache-only — no network."""
    if not str(rec.get("token") or ""):
        return "none"
    return _auth_cache.get(str(rec.get("id") or ""), ("unknown", 0.0))[0]


def _probe_one(rec: dict, timeout: float) -> tuple[bool, str]:
    """Probe ONE remote's A2A card: refresh its reachability cache + persist a changed
    version. Returns ``(reachable, version-from-card-or-blank)``. Network call — keep it
    off the event loop."""
    import httpx

    version = ""
    rid = str(rec.get("id") or "")
    url = str(rec.get("url") or "")
    if not url:
        # Nothing to dial. Raising here would be worse than useless: /api/fleet
        # runs this over every remote on the 3s console poll BEFORE it reads the
        # roster, so one url-less record would 500 the whole surface (#3018).
        # Cache the miss so it degrades exactly like a member that never answers.
        _probe_cache[rid] = (False, time.monotonic())
        return False, ""
    try:
        r = httpx.get(f"{url}/.well-known/agent-card.json", timeout=timeout, follow_redirects=False)
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
    _probe_cache[rid] = (alive, time.monotonic())
    if version and version != rec.get("version"):
        _record_remote_version(rid, version)
    return alive, version


def refresh_remote_probes(timeout: float = 1.0) -> None:
    now = time.monotonic()
    for rec in list_remotes():
        hit = _probe_cache.get(rec["id"])  # list_remotes() guarantees the key
        if hit and now - hit[1] < _PROBE_TTL:
            alive = hit[0]
        else:
            alive, _ = _probe_one(rec, timeout)
        # The authenticated probe (D5) on its own, slower TTL. Skipped while the card
        # probe says the remote is down: it would only burn another timeout to learn
        # "unknown", and the last verdict (e.g. "rejected") stays the more useful thing
        # to show. Once it answers again the normal TTL decides when to re-check.
        if not alive or not rec.get("token"):
            continue
        ahit = _auth_cache.get(rec["id"])
        if ahit and now - ahit[1] < _AUTH_TTL:
            continue
        _auth_probe_one(rec, timeout)


def probe_remote(ident: str, timeout: float = 1.0) -> tuple[bool, str]:
    """Probe a SINGLE remote member (by id or name) NOW, bypassing the TTL — used at
    register time so the caller (console/CLI) learns reachability immediately instead of
    waiting for the next 3s poll. Returns ``(reachable, version)`` where version falls back
    to the last-known when the card carries none. ``FleetError`` if no such remote.

    Registration is never rejected for an unreachable peer (deferred registration is
    intentional — a peer can come online later); this just lets the caller warn up front."""
    remotes = _load_remotes()
    rid = ident if ident in remotes else next((k for k, r in remotes.items() if r["name"] == ident), None)
    if rid is None:
        raise FleetError(f"no remote member {ident!r}")
    rec = remotes[rid]
    alive, version = _probe_one(rec, timeout)
    # Also refresh the authenticated verdict NOW (ADR 0113 D5): this is the register /
    # edit / pair path, so a just-pasted wrong token (or a just-claimed good one) shows
    # up in the response instead of 30s later. Only when reachable — see refresh above.
    if alive:
        _auth_probe_one(rec, timeout)
    return alive, version or str(rec.get("version", "") or "")


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


# ── pairing (ADR 0113 D1): the hub claims a code the remote's operator minted ──────
# The remote's operator generates a pairing code (Settings ▸ Devices, or `protoagent pair`);
# the hub redeems it against the remote's EXISTING, unauthenticated `POST /api/pairing/claim`
# (ADR 0087 D4) and stores the per-device token it gets back as the remote's bearer. The hub
# never learns the remote's shared operator bearer, and the remote can revoke the hub alone.


class PairingError(FleetError):
    """A pairing attempt failed. ``status`` is the HTTP status the route should answer:
    400 when the operator's input is wrong (bad url/name, invalid or expired code), 502
    when the remote didn't answer like a protoAgent that supports pairing."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


_PAIR_TIMEOUT = 5.0
_REMOTE_TEXT_CAP = 200
_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _clean_remote_text(value: object) -> str:
    """Remote-supplied text made safe to show: strings only, control characters (ANSI
    escapes, newlines that could forge a log line or rewrite a terminal) replaced, and
    capped. Anything else reads as ``""`` — the caller supplies its own fallback."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in value).strip()
    return text if len(text) <= _REMOTE_TEXT_CAP else text[: _REMOTE_TEXT_CAP - 1] + "…"


def _clean_device_id(value: object) -> str:
    """The remote's device id if it has the shape ADR 0087 mints (``secrets.token_hex``),
    else ``""`` — it is logged, stored and later put in a URL path, so nothing looser gets in."""
    return value if isinstance(value, str) and _DEVICE_ID_RE.fullmatch(value) else ""


def _hub_display_name() -> str:
    """What the remote's Devices list calls this hub: ``<agent name> (fleet hub)``. The
    identity name when a server is running here; the hostname from the offline CLI (no
    graph config loaded) so the remote's operator can still tell which box it was."""
    name = ""
    try:
        from runtime.state import STATE

        name = str(getattr(getattr(STATE, "graph_config", None), "identity_name", "") or "")
    except Exception:  # pragma: no cover - STATE import never fails in-tree; belt + braces
        name = ""
    if not name:
        import socket

        name = socket.gethostname().split(".")[0] or "protoAgent"
    return f"{name} (fleet hub)"


def _fetch_card_name(url: str, timeout: float) -> str:
    """The remote's agent-card ``name`` (best effort, ``""`` on any failure) — the default
    member name for a pairing that didn't pass one. Same unauthenticated endpoint the
    reachability probe hits."""
    import httpx

    try:
        r = httpx.get(f"{url}/.well-known/agent-card.json", timeout=timeout, follow_redirects=False)
        if r.status_code == 200:
            return str((r.json() or {}).get("name", "") or "")
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return ""


def _free_member_name(base: str, remotes: dict) -> str:
    """``base`` if no member (remote or local workspace) holds it and it isn't reserved,
    else ``base-2``, ``base-3``… — used ONLY for a name DEFAULTED from the remote's card.
    Two protoAgents both calling themselves "protoagent" is the normal case (it's the
    default identity), and failing a pairing over a name the operator never typed — after
    the single-use code is already spent — would be the worst outcome."""
    taken = {str(r.get("name")) for r in remotes.values() if isinstance(r, dict)} | {
        w["name"] for w in manager.list_workspaces()
    }

    def _free(n: str) -> bool:
        return n not in taken and n.lower() not in manager._RESERVED_NAMES

    if _free(base):
        return base
    i = 2
    while not _free(f"{base}-{i}"):
        i += 1
    return f"{base}-{i}"


def pair_remote(
    url: str, code: str, name: str | None = None, timeout: float = _PAIR_TIMEOUT, *, allow_insecure: bool = False
) -> dict:
    """Pair this hub with a remote protoAgent by redeeming a code its operator minted
    (ADR 0113 D1), then register it — or, when a remote at that URL is already a member,
    re-token it in place (the "re-pair" path after a revoke; its id, slug and windows are
    kept, and a passed ``name`` renames it).

    Returns ``{agent, reachable, version, auth, action}`` where ``agent`` is the SANITIZED
    record (no token), ``auth`` the freshly-probed verdict (D5) and ``action`` is ``added``
    or ``retokened``. Raises ``PairingError`` (a ``FleetError``) with an honest message.

    Naming: an explicit ``name`` is validated (charset, reserved, collision) BEFORE the code
    is claimed — a code is single-use, so an error after the claim would burn it and leave
    an orphan device on the remote. A name defaulted from the remote's card is coerced to the
    member charset and suffixed (``-2``…) on a collision instead of failing.

    Security: the URL goes through ``_normalize_remote_url`` (the SSRF egress guard) and the
    D10 transport rule (plain http only to loopback/tailnet unless ``allow_insecure``) before
    ANY request, redirects are not followed, and neither the code nor the minted token is
    ever logged. Blocking network I/O — the route runs it off the loop.
    """
    import httpx

    url = _normalize_remote_url(url)  # FleetError → 400 at the route; nothing dialled yet
    code = str(code or "").strip()
    if not code:
        raise PairingError("a pairing code is required — generate one on the remote (Settings ▸ Devices)")
    try:
        _require_secure_transport(url, allow_insecure=allow_insecure, what="the pairing code and token")
    except InsecureTransport as exc:
        raise PairingError(str(exc), 400) from exc

    remotes = _load_remotes()
    existing = next((k for k, r in remotes.items() if isinstance(r, dict) and _url_key(r.get("url")) == url), None)
    if name is not None and str(name).strip():
        try:
            name = manager._safe(str(name))
        except manager.WorkspaceError as exc:
            raise PairingError(str(exc)) from exc
        if name.lower() in manager._RESERVED_NAMES:
            raise PairingError(f"{name!r} is reserved — it's how the fleet addresses this instance")
        others = {str(r.get("name")) for k, r in remotes.items() if k != existing and isinstance(r, dict)}
        if name in others | {w["name"] for w in manager.list_workspaces()}:
            raise PairingError(f"an agent named {name!r} already exists")
    else:
        name = None

    not_protoagent = f"{url} is not a protoAgent (or too old to support pairing) — unexpected reply to the claim"
    try:
        r = httpx.post(
            f"{url}/api/pairing/claim",
            json={"code": code, "name": _hub_display_name()},
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise PairingError(
            f"{url} is unreachable ({type(exc).__name__}) — is it running and bound to an address this hub can reach?",
            502,
        ) from exc
    try:
        body = r.json()
    except ValueError:
        body = None
    # Only a protoAgent-SHAPED refusal (``{"ok": false, …}``) is read as "your code was
    # wrong": a 403 from a reverse proxy, a WAF or some other app says nothing about the
    # code, and telling the operator to generate a new one would send them in circles.
    if r.status_code == 403 and isinstance(body, dict) and body.get("ok") is False:
        raise PairingError("that code is invalid or expired — generate a new one on the remote", 400)
    if not isinstance(body, dict) or r.status_code in (403, 404):
        raise PairingError(not_protoagent, 502)
    if r.status_code != 200 or not body.get("ok"):
        raise PairingError(
            f"{url} refused the pairing claim (HTTP {r.status_code}): {_clean_remote_text(body.get('error')) or 'no reason given'}",
            502,
        )
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise PairingError(f"{url} accepted the code but returned no token — not a protoAgent pairing endpoint?", 502)
    device = body.get("device") if isinstance(body.get("device"), dict) else {}
    device_id = _clean_device_id(device.get("id"))
    shown_id = device_id or "(id not reported)"
    orphan_hint = f"revoke device {shown_id} on the remote (Settings ▸ Devices) and pair again"

    if existing is not None:
        prev = remotes.get(existing) or {}
        try:
            rec = update_remote(
                existing, token=token, name=name, device_id=device_id or None, allow_insecure=allow_insecure
            )
        except (FleetError, manager.WorkspaceError) as exc:
            # Same spent-code situation as the add path below (the name was taken, or the
            # member vanished, between the pre-check and the write).
            raise PairingError(f"paired with {url} but could not store the new token: {exc} — {orphan_hint}") from exc
        action = "retokened"
        _revoke_previous_device(url, prev, new_token=token, new_device_id=device_id, timeout=timeout)
    else:
        if name is None:
            base = manager._slugify_display(_fetch_card_name(url, timeout))[:48].strip("_-") or "remote"
            name = _free_member_name(base, _load_remotes())
        try:
            rec = add_remote(name, url, token, device_id=device_id, allow_insecure=allow_insecure)
        except (FleetError, manager.WorkspaceError) as exc:
            # Lost a race between the pre-check and the write (another add took the name or
            # url). The code is spent and the remote HAS a device for us — say so, so the
            # operator can revoke it there rather than wonder.
            raise PairingError(f"paired with {url} but could not register it: {exc} — {orphan_hint}") from exc
        action = "added"
    # Device id + url only — the token and the code never reach a log line.
    log.info("[fleet] paired with remote %s (%s) as its device %s — %s", rec["name"], url, shown_id, action)
    reachable, version = probe_remote(rec["id"], timeout=min(timeout, 2.0))
    stored = _load_remotes().get(rec["id"]) or rec
    return {"agent": rec, "reachable": reachable, "version": version, "auth": remote_auth(stored), "action": action}


def _revoke_previous_device(url: str, prev: dict, *, new_token: str, new_device_id: str, timeout: float) -> None:
    """After a re-pair, retire the device the PREVIOUS token was minted for, so re-pairing
    doesn't pile up live operator credentials on the remote. Best effort, and only when:

      * the previous record names a device (``device_id`` — set by an earlier pairing; a
        pasted shared bearer has none, and must never be "revoked" by guessing), and
      * the OLD token still authenticates — if it's already revoked the device is gone, and
        if it doesn't, it's not a credential anyone can use.

    The DELETE is made with the NEW token. Failures are logged (ids + status only, never a
    token) and swallowed: the pairing itself already succeeded."""
    import httpx

    old_id = _clean_device_id(prev.get("device_id"))
    old_token = prev.get("token")
    if not old_id or not isinstance(old_token, str) or not old_token or old_id == new_device_id:
        return
    try:
        if _auth_get(url, old_token, timeout) != 200:
            return
        r = httpx.delete(
            f"{url}/api/devices/{old_id}",
            headers={"Authorization": f"Bearer {new_token}"},
            timeout=timeout,
            follow_redirects=False,
        )
        if r.status_code == 200:
            log.info("[fleet] re-pair: revoked the previous device %s on %s", old_id, url)
        else:
            log.warning(
                "[fleet] re-pair: could not revoke the previous device %s on %s (HTTP %s)", old_id, url, r.status_code
            )
    except httpx.HTTPError as exc:
        log.warning(
            "[fleet] re-pair: could not revoke the previous device %s on %s (%s)", old_id, url, type(exc).__name__
        )


# ── fleet roster order (ADR 0042 hub control-plane) ───────────────────────────
# A persisted, presentation-only ordering for the fleet list: a permutation of the
# CURRENT member ids (host + local workspaces + remotes), keyed by IMMUTABLE id so a
# display rename never disturbs it. Hub-scoped like fleet.json (#813) and kept in a
# SIBLING roster.json — NOT inside fleet.json, whose {id: record} map every liveness
# path iterates as records (a mixed-in order list would break them). This is roster
# metadata, distinct from member identity and remote credentials — none of which it
# touches. Read-modify-write is guarded by the existing supervisor state lock.


def _roster_path() -> Path:
    return manager.workspaces_root() / "roster.json"


def _load_roster_order() -> list[str]:
    """The saved roster order as a list of member ids (``[]`` when unsaved). Tolerant:
    a missing / corrupt / non-UTF-8 / wrong-shaped file reads as unsaved rather than
    raising on the ``status()`` read path that every console fleet surface goes through —
    matching the posture ``_load_state`` / ``_load_remotes`` / ``_load_archetype_catalog``
    already take for their own files. ``read_text()`` on invalid UTF-8 raises
    ``UnicodeDecodeError`` (not ``JSONDecodeError``), so it is caught explicitly here —
    otherwise a byte-corrupt roster.json would 500 both ``status()`` and GET /api/fleet."""
    p = _roster_path()
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("[fleet] %s unreadable (%s) — roster order treated as unsaved", p, e)
        return []
    order = d.get("order") if isinstance(d, dict) else d
    if not isinstance(order, list):
        return []
    return [x for x in order if isinstance(x, str) and x]


def _save_roster_order(order: list[str]) -> None:
    from infra.paths import atomic_write

    atomic_write(_roster_path(), json.dumps({"order": order}, indent=2) + "\n")


def _known_member_ids() -> list[str]:
    """Every currently-known fleet member id in default (discovery) order — the host,
    then local workspaces, then remote members — mirroring EXACTLY the entries
    ``status()`` builds, so ``set_roster_order`` validates a submission against the same
    roster the reader reconciles against. Acquires no state lock (reads
    ``manager.list_workspaces`` / ``list_remotes`` / ``_host_entry`` directly), so it is
    safe to call inside a ``_state_lock()`` block without self-deadlocking."""
    ids = [_host_entry()["id"]]
    ids += [ws.get("id", ws["name"]) for ws in manager.list_workspaces()]
    ids += [rec["id"] for rec in list_remotes()]
    return ids


def _apply_roster_order(entries: list[dict], order: list[str]) -> list[dict]:
    """Order fleet ``entries`` (each carrying an immutable ``id``) by the saved roster
    ``order``, reconciling live membership against it: an entry whose id appears in
    ``order`` sorts to that rank; an entry NOT named in ``order`` (a member added since
    the order was saved, or one never ordered) keeps its original relative position and
    follows every ordered entry. A saved id with no live entry (a removed member) simply
    contributes no row. Every input entry appears exactly once — nothing is lost or
    duplicated. An empty ``order`` is a no-op (the default host/local/remote order)."""
    if not order:
        return entries
    rank = {mid: i for i, mid in enumerate(order)}
    # Stable sort: ids absent from the saved order all share the sentinel rank, so they
    # retain discovery order among themselves and land after every id the order names.
    return sorted(entries, key=lambda e: rank.get(str(e.get("id")), len(order)))


def get_roster_order() -> list[str]:
    """The persisted roster order (member ids), or ``[]`` when none is saved."""
    return _load_roster_order()


def set_roster_order(order: object) -> list[str]:
    """Persist the fleet roster DISPLAY order — a COMPLETE permutation of the current
    member ids (host + local + remote), by immutable id. Validated under the state lock
    against the live roster so a stale or malformed submission can't corrupt saved order:

      * not a list, or any non-string / blank id  → rejected
      * a duplicate id                             → rejected
      * an id no current member has (unknown)      → rejected
      * a current member id left out (missing)     → rejected

    On ANY rejection the saved order is left untouched and a ``FleetError`` is raised;
    only a fully-valid permutation is written (atomic). Returns the persisted order.

    Hub-only by construction like the rest of the registry: a member runs
    ``PROTOAGENT_INSTANCE``-scoped, so its own ``roster.json`` is its own."""
    if not isinstance(order, list):
        raise FleetError("roster order must be a list of member ids")
    if not all(isinstance(x, str) and x.strip() for x in order):
        raise FleetError("roster order must be non-blank member id strings")
    ids = list(order)
    if len(ids) != len(set(ids)):
        raise FleetError("roster order has duplicate member ids")
    # Validate + persist atomically under the SAME lock every fleet.json RMW takes, so the
    # order can't be validated against a roster a concurrent add/remove is mid-change.
    with _state_lock():
        current = set(_known_member_ids())
        submitted = set(ids)
        unknown = submitted - current
        if unknown:
            raise FleetError(f"roster order names unknown member(s): {', '.join(sorted(unknown))}")
        missing = current - submitted
        if missing:
            raise FleetError(f"roster order is missing current member(s): {', '.join(sorted(missing))}")
        _save_roster_order(ids)
    return ids


def _remote_a2a(rid: str, url: str, hub_port: int | None) -> str | None:
    """The A2A endpoint a remote member is ADVERTISED at — the one "Add as delegate" and any
    ``delegate_to`` wiring should use (ADR 0113 D4): the hub's own loopback proxy,
    ``http://127.0.0.1:<hub-port>/agents/<rid>/a2a``, not the remote's URL.

    Why route a delegate through the hub instead of dialing the remote directly: one
    registry, one token. A tokenless loopback delegate presents the fleet service token
    (ADR 0089 D4), which the hub accepts as operator; the proxy then swaps it for the
    remote's STORED (paired) bearer (``proxy._target_for_slug``). So the remote's credential
    lives only in ``remotes.json`` — re-pairing or rotating it fixes the fleet row and every
    delegate at once — and it never leaves the hub's box: the delegate only ever carries the
    fleet token, which is loopback-only by construction. Dialing ``<url>/a2a`` directly
    needed the remote's token copied into each delegate (and 401'd without it).

    The remote's real URL stays in the entry's ``url``. Hub port unknown (no server bound —
    a CLI read, a test) → fall back to the direct ``<url>/a2a``: an address that needs its
    own token beats none. No url → None (nothing to route to either way)."""
    if not url:
        return None
    if hub_port:
        from urllib.parse import quote

        return f"http://127.0.0.1:{hub_port}/agents/{quote(rid, safe='')}/a2a"
    return f"{url}/a2a"


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
    host = _host_entry()
    out: list[dict] = [host]
    for ws in manager.list_workspaces():
        rec = state.get(ws["id"]) or {}  # state is keyed by the immutable id
        running = _alive(rec.get("pid"))
        port = ws.get("port")
        out.append(
            {
                "name": ws["name"],
                "label": ws.get("label") or ws["name"],
                "id": ws.get("id", ws["name"]),
                "port": port,
                "pid": rec.get("pid") if running else None,
                "running": running,
                "bundle": ws.get("bundle", ""),
                # The version the member was SPAWNED at (stamped in start()) —
                # the console compares it against the host entry's version so a
                # local member left behind by an app update shows the same skew
                # badge a drifted remote does. Empty while stopped (a stopped
                # member runs whatever binary the next start() spawns).
                "version": rec.get("version", "") if running else "",
                # Direct A2A endpoint — every agent is an independent endpoint on its
                # own port (ADR 0042), reachable regardless of console focus, so a
                # focused agent can `delegate_to` an unfocused sibling here. Live only
                # while running, but the address is stable.
                "a2a": f"http://127.0.0.1:{port}/a2a" if port else None,
            }
        )
    for rec in list_remotes():
        rid = rec["id"]  # list_remotes() recovers it from the registry key
        # Every other field is read defensively. The roster is the FIRST thing
        # every console surface reads, so one hand-edited record missing a field
        # used to raise KeyError right here and 500 the WHOLE read — /api/fleet
        # and the telemetry rollup alike (#3018), losing every healthy member
        # over one bad row. A remote with no url is REPORTED and simply never
        # reachable, which is exactly what it is; that is also what makes it a
        # ``reachable: false`` entry in the telemetry rollup rather than a 500.
        url = str(rec.get("url") or "")
        alive = _probe_cache.get(rid, (False, 0.0))[0]
        out.append(
            {
                "name": rec.get("name") or rid,
                "label": rec.get("label") or rec.get("name") or rid,
                "id": rid,
                "port": None,
                "pid": None,
                "running": alive,
                "bundle": "",
                "remote": True,
                "url": url,
                # Last-probed remote version (from its A2A card) — NEVER the token.
                "version": rec.get("version", ""),
                "a2a": _remote_a2a(rid, url, host.get("port")),
                # Whether the stored token is accepted (ADR 0113 D5) — the VERDICT, never
                # the token: ok | rejected | unknown | none (no token stored).
                "auth": remote_auth(rec),
            }
        )
    # Apply the saved roster display order (ADR 0042 hub control-plane), reconciling live
    # membership: ordered members first in the saved order, any member added since (or never
    # ordered) keeps discovery order and follows. Unsaved → the default host/local/remote
    # order built above. A plain, tolerant read — no lock, like list_remotes on this path.
    return _apply_roster_order(out, _load_roster_order())


def up(names: list[str] | None = None) -> list[dict]:
    """Start a set of agents (named, or all workspaces). One member dying at boot
    (start() now raises on that) doesn't abort the rest — it's reported as its own
    ``{running: False, error}`` row instead."""
    out = []
    for n in names or [w["name"] for w in manager.list_workspaces()]:
        try:
            out.append(start(n))
        except FleetError as exc:
            out.append({"name": n, "running": False, "error": str(exc)})
    return out


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


def keep_members_on_exit() -> bool:
    """Opt out of spin-down-on-host-exit (``PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT``).
    Default False — the host owns its fleet, so "host down → fleet down" is the
    expected lifecycle. Set it for genuinely long-running detached agents that must
    survive a hub restart."""
    return os.environ.get("PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT", "").strip().lower() in ("1", "true", "yes")


def shutdown_all(*, timeout: float = 3.0) -> list[str]:
    """Stop every running LOCAL member — called from the hub's shutdown hook so a
    member can't outlive the host that spawned it.

    Members are spawned detached (``detached_kwargs()`` in ``start``) so they
    survive the CLI/hub that launched them — durable by design, but it also means a
    hub rebuild+restart strands a member running the OLD code (it's in its own
    session, gets no signal, and is never re-execed — see
    ``docs/dev/version-coherence.md`` Axis 1). "Host down → fleet down" is the
    expected default; opt out via ``PROTOAGENT_FLEET_KEEP_MEMBERS_ON_EXIT=1``.
    Sessions are ``instance.id``-scoped checkpoints, so a stopped member resumes on
    its next ``activate`` — this stops PROCESSES, not work.

    **Hub-only by construction:** a member runs ``PROTOAGENT_INSTANCE``-scoped (#813),
    so inside a member ``_load_state`` reads its own (empty) ``fleet.json`` and this
    no-ops. Only the hub's registry holds members.

    Gracefully tree-signals all members **at once** (not sequentially), then waits one
    shared ``timeout`` before hard-killing straggler trees — so teardown stays bounded regardless of
    member count and fits the hub's graceful-shutdown window. Best-effort throughout.
    """
    if keep_members_on_exit():
        return []
    with _state_lock():
        state = _load_state()
        # PID-reuse guard (#10): only ours; reserve all stops under the lock.
        live = [(k, int(r["pid"])) for k, r in state.items() if _alive(r.get("pid")) and _is_our_agent(int(r["pid"]))]
        if not live:
            return []
        for k, _ in live:
            state.pop(k, None)
        _save_state(state)
    for _, pid in live:  # graceful tree-signal everyone first — concurrent, not 8s-each
        signal_tree(pid, force=False)
    deadline = time.monotonic() + timeout
    pending = [pid for _, pid in live]
    while pending and time.monotonic() < deadline:
        time.sleep(0.1)
        pending = [pid for pid in pending if _alive(pid)]
    for pid in pending:  # hard-kill straggler trees past the shared deadline
        signal_tree(pid, force=True)
    names = [k for k, _ in live]
    log.info("[fleet] host exiting — spun down %d member(s): %s", len(names), ", ".join(names))
    return names


# ── First-boot-after-update reconcile (version-coherence P2) ──────────────────
# Members are detached processes: a hub update + restart leaves any survivor (a
# crashed hub, or the KEEP_MEMBERS_ON_EXIT opt-out) running the OLD binary
# indefinitely. The boot hook stamps each boot's version beside fleet.json and logs
# the transition; the live warning is recomputed per runtime-status poll (like
# colocation_warning) so it shows while skewed members run and clears the moment
# they're restarted.


def _version_stamp_path() -> Path:
    return manager.workspaces_root() / ".last-version"


def reconcile_on_boot() -> str:
    """Stamp this boot's app version and log the transition when it changed since
    the previous boot (the first-boot-after-update signal — an in-app update, a
    DMG swap, or a `git pull` all land here).

    Returns the previously-stamped version ("" on first boot). Hub-scoped like
    fleet.json (#813); inside a member the scoped root is its own, so stamps don't
    cross. Best-effort — never raises.
    """
    from infra.paths import atomic_write, package_version

    previous = ""
    try:
        current = package_version()
        stamp = _version_stamp_path()
        try:
            previous = stamp.read_text().strip()
        except OSError:
            previous = ""
        if previous != current:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(stamp, current + "\n")
        if previous and previous != current:
            log.info("[fleet] app version changed since last boot: %s -> %s", previous, current)
            skew = version_skew_warning()
            if skew:
                log.warning("[fleet] %s", skew)
    except Exception:  # noqa: BLE001 — boot reconcile must never block startup
        log.exception("[fleet] boot version reconcile failed")
    return previous


def autostart_members() -> list[str]:
    """The configured ``fleet.autostart`` roster — member ids or display names to keep
    (re)started at hub boot. Live config first (folds in the ``PROTOAGENT_FLEET_AUTOSTART``
    comma-separated env fallback, file > env > default), else the env var directly when no
    config is loaded. Accepts a list or a comma-separated string; blanks are dropped."""
    raw: object = None
    cfg = _live_config()
    if cfg is not None:
        raw = getattr(cfg, "fleet_autostart", None)
    if raw is None:
        raw = os.environ.get("PROTOAGENT_FLEET_AUTOSTART", "")
    items = raw.split(",") if isinstance(raw, str) else raw
    try:
        return [str(x).strip() for x in items if str(x).strip()]
    except TypeError:  # a non-iterable in config — treat as empty rather than raise at boot
        return []


def start_autostart_members() -> list[str]:
    """(Re)start every configured autostart member that isn't already alive — the
    hub-boot half of ADR 0072's ``autostart``.

    A container recreate or host restart kills the detached member processes (fresh pid
    namespace), but ``fleet.json`` in the volume keeps their now-dead records; without
    this the declared crew stays down until someone re-activates each by hand. Idempotent:
    an already-running member is skipped (never double-spawned).

    Hub-only by construction, like :func:`reconcile_on_boot`: a member runs
    ``PROTOAGENT_INSTANCE``-scoped, so its own config carries no autostart roster and this
    no-ops inside a member. Best-effort per member — a missing workspace or a boot-time
    spawn failure is logged and skipped, never blocking the hub or the rest of the roster.
    Members are started sequentially (each ``start`` briefly boot-watches the child); call
    off the event loop (the boot hook does via ``asyncio.to_thread``).
    """
    # Hub-only — enforced, not merely "by construction". A member inherits the hub's
    # PROTOAGENT_FLEET_AUTOSTART env, so autostart_members() returns a NON-empty roster
    # inside a member too (the config-carries-no-roster guarantee only covers the config
    # source, not the env fallback). Acting on it there scans the member's own empty
    # workspaces root and logs a spurious "no workspace <id> — skipping" for every id.
    if manager.is_workspace_member():
        return []
    roster = autostart_members()
    if not roster:
        return []
    started: list[str] = []
    for ident in roster:
        try:
            if is_running(ident):
                continue
            if manager._find(manager._safe(ident)) is None:
                log.warning("[fleet] autostart: no workspace %r — skipping (create it first)", ident)
                continue
            start(ident)
            started.append(ident)
        except FleetError as exc:
            log.error("[fleet] autostart: %r failed to start: %s", ident, exc)
        except Exception:  # noqa: BLE001 — autostart must never block hub boot
            log.exception("[fleet] autostart: unexpected error starting %r", ident)
    if started:
        log.info("[fleet] autostart: (re)started %d member(s): %s", len(started), ", ".join(started))
    return started


def version_skew_warning() -> str | None:
    """A live warning when running LOCAL members were spawned by a different app
    version than this process runs.

    Recomputed on every runtime-status poll (same posture as
    ``infra.paths.colocation_warning``) so it appears while skewed members run and
    self-clears once they're restarted — no hub restart needed. A member spawned
    before version stamping existed reads as "unknown" and is flagged too: it
    cannot be told apart from a stale one. Best-effort: None on any failure.
    """
    try:
        from infra.paths import package_version

        current = package_version()
        state = _load_state()
        if not state:
            return None
        names = {ws["id"]: ws["name"] for ws in manager.list_workspaces()}
        stale: list[str] = []
        for wid, rec in state.items():
            if not _alive(rec.get("pid")):
                continue
            v = str(rec.get("version", "") or "")
            if v != current:
                label = names.get(wid, wid)
                stale.append(f"{label} (v{v})" if v else f"{label} (version unknown)")
        if not stale:
            return None
        return (
            f"{len(stale)} fleet member(s) run a different protoAgent version than this hub "
            f"(v{current}): {', '.join(sorted(stale))}. They keep the OLD code until restarted "
            "— restart them from the Fleet panel to close the gap "
            "(docs/dev/version-coherence.md, Axis 1)."
        )
    except Exception:  # noqa: BLE001 — a status-poll warning must never raise
        return None


# ── Keep-N-warm policy (ADR 0042 §G) ──────────────────────────────────────────
# Bound how many agents stay hot (a laptop won't run a big fleet). On a switch the
# target is resumed and the least-recently-active agents beyond the cap are stopped —
# their sessions persist (instance.id-scoped checkpoints) and resume on the next switch.


def _live_config():
    """The live ``LangGraphConfig`` (or ``None`` in a CLI/no-STATE context). Lazy
    import to avoid an import-time cycle — same idiom as ``_pick_port`` /
    ``runtime_status``."""
    try:
        from runtime.state import STATE

        return getattr(STATE, "graph_config", None)
    except Exception:  # noqa: BLE001 — no live config ⇒ caller's env fallback
        return None


def max_warm() -> int:
    """Warm-agent cap (Host layer, ADR 0047 D8) — the resolved ``fleet.warm.max``
    (0/unset = unlimited). Reads the live config (which already folds in the
    PROTOAGENT_FLEET_MAX_WARM env fallback, file > env > default); falls back to the
    env var directly when no config is loaded."""
    cfg = _live_config()
    if cfg is not None:
        try:
            return max(0, int(getattr(cfg, "fleet_max_warm", 0) or 0))
        except (TypeError, ValueError):
            return 0
    try:
        return max(0, int(os.environ.get("PROTOAGENT_FLEET_MAX_WARM", "0")))
    except ValueError:
        return 0


def _warm_grace_seconds() -> int:
    """LRU-eviction grace (Host layer, ADR 0047 D8) — the resolved
    ``fleet.warm.grace_seconds`` (0 = pure LRU). Live config first (env fallback
    folded in), else the PROTOAGENT_FLEET_WARM_GRACE env var directly."""
    cfg = _live_config()
    if cfg is not None:
        try:
            return max(0, int(getattr(cfg, "fleet_warm_grace_seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0
    try:
        return int(os.environ.get("PROTOAGENT_FLEET_WARM_GRACE", "300") or "300")
    except ValueError:
        return 300


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
    # Grace (default 300s, Swap & Resume S4): spare agents touched within the window, trading a
    # temporarily-over-cap fleet for not killing a recently-active (likely mid-turn) agent — the
    # hub proxy refreshes recency on every member turn start, so grace tracks WORK, not clicks.
    # 0 restores pure LRU. Host layer (ADR 0047 D8): fleet.warm.grace_seconds, env fallback.
    grace = _warm_grace_seconds()
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
        log.info("[fleet] keep-%d-warm: evicted %s (sessions resume on next switch)", keep, ", ".join(evicted))
    return evicted
