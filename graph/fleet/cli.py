"""``protoagent fleet …`` — run and inspect the fleet (ADR 0042; #3467, epic #3466).

Two modes, chosen by evidence rather than by the environment the shell inherited:

- **live** — a running hub answered (``deck.hub.connect``): the roster is the hub's
  ``GET /api/fleet`` and ``up`` / ``down`` go through the hub's control plane so the hub
  keeps owning its child processes. The header names the hub.
- **offline** — NOTHING answered (or ``--offline``): the roster is this instance's
  ``fleet.json`` via ``graph.fleet.supervisor``, badged as such. The supervisor's
  synthesized host row is dropped here: it describes THIS process (the CLI), not a server.

A hub that answered but could not be opened — rejected credential, timeout, 5xx, or only
a member answering — is an ERROR (exit 1), never a fallback: driving processes from disk
beside a running hub is the two-hubs bug. ``--json`` on every verb emits per-member result
rows of one shape (``{name, ok, …}``) plus ``mode`` / ``hub``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from deck import hub as deckhub
from graph.fleet import supervisor

_PRESENCE_GLYPH = {"host": "●", "online": "●", "remote": "●", "stopped": "○", "unreachable": "◌"}


def presence_of(agent: dict) -> str:
    """The console's presence vocabulary, exactly (apps/web/src/app/FleetRoom.tsx::presenceOf):
    host · online · remote · stopped · unreachable. Two surfaces, one set of words."""
    if agent.get("host"):
        return "host"
    if agent.get("running"):
        return "remote" if agent.get("remote") else "online"
    return "unreachable" if agent.get("remote") else "stopped"


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--hub", metavar="URL", default=None, help="hub to talk to (default: discover a running hub on this box)")
    p.add_argument(
        "--token",
        default=None,
        help=f"credential for --hub — a fleet token or operator bearer (env: {deckhub.ENV_TOKEN}); never printed",
    )
    p.add_argument("--offline", action="store_true", help="read this instance's fleet.json instead of asking a hub")
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON for scripting")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protoagent fleet",
        description="Run and inspect the fleet — live from the running hub, or from disk when none answers (ADR 0042).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pu = sub.add_parser("up", help="start agents — all stopped local members, or named")
    pu.add_argument("names", nargs="*")
    _common(pu)
    pd = sub.add_parser("down", help="stop agents — all running, or named")
    pd.add_argument("names", nargs="*")
    _common(pd)
    pl = sub.add_parser("ls", help="list members with live status")
    _common(pl)
    ps = sub.add_parser("status", help="alias for ls")
    _common(ps)
    return p


# ── output helpers ───────────────────────────────────────────────────────────


def _note(msg: str, args: argparse.Namespace) -> None:
    """Human-mode side note (stderr so ``--json`` stdout stays clean)."""
    if not args.as_json:
        print(f"  {msg}", file=sys.stderr)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _row_ok(name: str, args: argparse.Namespace, out: list[dict], text: str, **extra: Any) -> None:
    out.append({"name": name, "ok": True, **extra})
    if not args.as_json:
        print(f"  ✓ {name:16} {text}")


def _row_fail(name: str, args: argparse.Namespace, out: list[dict], text: str, **extra: Any) -> None:
    out.append({"name": name, "ok": False, "error": text, **extra})
    if not args.as_json:
        print(f"  ✗ {name:16} {text}", file=sys.stderr)


def _finish(args: argparse.Namespace, mode: str, results: list[dict], hub_url: str | None = None) -> int:
    if args.as_json:
        payload: dict[str, Any] = {"mode": mode, "results": results}
        if hub_url:
            payload["hub"] = hub_url
        _emit(payload)
    return 1 if any(not r.get("ok") for r in results) else 0


# ── mode selection ───────────────────────────────────────────────────────────


def _open_hub(args: argparse.Namespace) -> deckhub.Connection | None:
    """A live hub connection, or None for offline mode.

    ``--offline`` skips the search. Otherwise the fallback to disk happens ONLY when
    nothing protoAgent-shaped answered anywhere; a hub that answered but could not be
    opened (or an explicit ``--hub`` that failed for any reason) propagates as an error."""
    if args.offline:
        return None
    try:
        return deckhub.connect(url=args.hub, token=args.token)
    except deckhub.NoHub as exc:
        if args.hub or exc.answered:
            raise
        _note(f"offline · {exc} — reading this instance's fleet.json", args)
        return None


# ── ls ───────────────────────────────────────────────────────────────────────


def _hub_label(conn: deckhub.Connection, roster: list[dict]) -> str:
    host = next((a for a in roster if a.get("host")), {})
    name = host.get("label") or host.get("name") or conn.card.get("name") or "hub"
    ver = host.get("version") or ""
    via = conn.candidate.source
    return f"live · {conn.client.url} · {name}{f' v{ver}' if ver else ''} · via {via}"


def _print_rows(rows: list[dict], host_version: str) -> None:
    width = max((len(str(a.get("label") or a.get("name") or "")) for a in rows), default=8)
    for a in rows:
        pres = presence_of(a)
        glyph = _PRESENCE_GLYPH[pres]
        name = str(a.get("label") or a.get("name") or "")
        port = f":{a['port']}" if a.get("port") else "—"
        extra: list[str] = []
        if a.get("pid") and pres in ("online", "host"):
            extra.append(f"pid {a['pid']}")
        ver = str(a.get("version") or "")
        if ver:
            skew = " !skew" if (host_version and ver != host_version and pres in ("online", "remote")) else ""
            extra.append(f"v{ver}{skew}")
        if a.get("remote") and a.get("url"):
            extra.append(str(a["url"]))
        if a.get("bundle"):
            extra.append(f"[{a['bundle']}]")
        print(f"  {glyph} {name:<{width}}  {port:<7} {pres:<11} {'  '.join(extra)}".rstrip())


def _cmd_ls(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    if conn is not None:
        roster = conn.roster  # the read that opened the connection — no second GET
        conn.client.close()
        if args.as_json:
            _emit({"mode": "live", "hub": conn.client.url, "via": conn.candidate.source, "agents": roster})
            return 0
        print(f"protoagent fleet · {_hub_label(conn, roster)}")
        host_version = str(next((a.get("version") for a in roster if a.get("host")), "") or "")
        _print_rows(roster, host_version)
        return 0

    # offline — the supervisor's disk view, minus its synthesized host row (that row is
    # built from THIS process's pid and this shell's config; it is not a server).
    from graph.workspaces import manager
    from infra.paths import instance_paths

    fleet_json = manager.workspaces_root() / "fleet.json"
    rows = [a for a in supervisor.status() if not a.get("host")]
    if args.as_json:
        _emit(
            {
                "mode": "offline",
                "fleet_json": str(fleet_json),
                "instance_root": str(instance_paths().instance_root),
                "agents": rows,
            }
        )
        return 0
    print(f"protoagent fleet · offline · reading {fleet_json}")
    if not rows:
        print("  (no workspaces here — protoagent workspace new <name>; or start a hub: protoagent up)")
        return 0
    _print_rows(rows, "")
    return 0


# ── up ───────────────────────────────────────────────────────────────────────


def _cmd_up(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    results: list[dict] = []
    if conn is not None:
        hub_url = conn.client.url
        try:
            names = list(args.names)
            if not names:
                # The hub has no start-all route; "all" means every LOCAL stopped member.
                names = [
                    str(a.get("name"))
                    for a in conn.roster
                    if not a.get("host") and not a.get("remote") and not a.get("running")
                ]
                if not names and not args.as_json:
                    print("  (nothing to start — every local member is already running)")
            for name in names:
                try:
                    res = conn.client.start(name)
                except deckhub.HubError as exc:
                    # One member's failure (400, a boot timeout, a torn connection) must
                    # not lose the others' results.
                    _row_fail(name, args, results, exc.detail if isinstance(exc, deckhub.HubRequestError) else str(exc))
                    continue
                agent = res.get("agent") or {}
                tag = "already running" if agent.get("already") else "started"
                _row_ok(name, args, results, f"{tag} (:{agent.get('port')}, pid {agent.get('pid')}) via hub {hub_url}", agent=agent)
        finally:
            conn.client.close()
        return _finish(args, "live", results, hub_url)

    try:
        rows = supervisor.up(args.names or None)
    except supervisor.FleetError as exc:
        _row_fail(args.names[0] if args.names else "fleet", args, results, str(exc))
        return _finish(args, "offline", results)
    if not rows and not args.as_json:
        print("(no workspaces — create one: protoagent workspace new <name>)")
    for r in rows:
        name = str(r.get("name", "?"))
        if r.get("error"):  # died at boot — start() surfaced the log tail
            _row_fail(name, args, results, str(r["error"]), agent=r)
            continue
        tag = "already running" if r.get("already") else "started"
        _row_ok(name, args, results, f"{tag} (:{r.get('port')}, pid {r.get('pid')}) via disk (offline)", agent=r)
    return _finish(args, "offline", results)


# ── down ─────────────────────────────────────────────────────────────────────


def _cmd_down(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    results: list[dict] = []
    if conn is not None:
        hub_url = conn.client.url
        try:
            if args.names:
                for name in args.names:
                    try:
                        res = conn.client.stop(name)
                    except deckhub.HubError as exc:
                        _row_fail(name, args, results, exc.detail if isinstance(exc, deckhub.HubRequestError) else str(exc))
                        continue
                    if res.get("ok"):
                        _row_ok(name, args, results, f"stopped via hub {hub_url}", stopped=True)
                    else:
                        _row_fail(name, args, results, f"not stopped — {res.get('reason', 'still running')}", stopped=False)
            else:
                running = sum(1 for a in conn.roster if a.get("running") and not a.get("host") and not a.get("remote"))
                try:
                    res = conn.client.down(running)
                except deckhub.HubError as exc:
                    _row_fail("fleet", args, results, str(exc))
                    return _finish(args, "live", results, hub_url)
                stopped = [str(n) for n in (res.get("stopped") or [])]
                fails = [f for f in (res.get("failed") or []) if isinstance(f, dict)]
                if not stopped and not fails and not args.as_json:
                    print("(nothing running)")
                for name in stopped:
                    _row_ok(name, args, results, f"stopped via hub {hub_url}", stopped=True)
                for f in fails:
                    _row_fail(str(f.get("name", "?")), args, results, str(f.get("reason") or "not stopped"), stopped=False)
        finally:
            conn.client.close()
        return _finish(args, "live", results, hub_url)

    try:
        rows = supervisor.down(args.names or None)
    except supervisor.FleetError as exc:
        _row_fail(args.names[0] if args.names else "fleet", args, results, str(exc))
        return _finish(args, "offline", results)
    seen: set[str] = set()
    for r in rows:
        name = str(r.get("name", "?"))
        seen.add(name)
        if r.get("stopped", True):
            _row_ok(name, args, results, "stopped via disk (offline)", stopped=True)
        else:
            _row_fail(name, args, results, f"not stopped — {r.get('reason', 'still running')}", stopped=False)
    # supervisor.down(names) silently skips a name it does not know or that is not running;
    # the operator asked for it by name, so say so and fail like the live path does.
    for name in args.names or []:
        if name not in seen and not any(r.get("id") == name for r in rows):
            _row_fail(name, args, results, "not running (or not a member of this instance)")
    if not rows and not args.names and not args.as_json:
        print("(nothing running)")
    return _finish(args, "offline", results)


# ── entry ────────────────────────────────────────────────────────────────────


def run_fleet_cli(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.offline and args.hub:
        parser.error("--offline and --hub are mutually exclusive (one reads disk, the other names a hub)")
    # `python -m server` configures INFO logging at import; httpx then narrates every
    # request onto stderr. The hub probe is chatty by design (a 401 per rejected token),
    # so keep that out of the operator's face — errors still surface as typed HubErrors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.cmd == "up":
            return _cmd_up(args)
        if args.cmd == "down":
            return _cmd_down(args)
        return _cmd_ls(args)
    except ValueError as exc:  # a malformed --hub (deck.hub.normalize_url)
        if args.as_json:
            _emit({"mode": "error", "hub": args.hub, "error": str(exc)})
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1
    except deckhub.HubError as exc:  # NoHub with something answering, or a mid-call failure
        if args.as_json:
            _emit({"mode": "error", "hub": getattr(exc, "url", "") or args.hub, "error": str(exc)})
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1
    except supervisor.FleetError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
