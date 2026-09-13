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
from deck.data import PRESENCE_GLYPH as _PRESENCE_GLYPH
from deck.data import presence_of  # one definition of the console's presence words (deck.data)
from graph.fleet import supervisor

__all__ = ["presence_of", "run_deck_cli", "run_fleet_cli"]


def _common(p: argparse.ArgumentParser, *, top: bool) -> None:
    """The shared flags. They live on the top-level parser (real defaults) AND on every
    subparser (``SUPPRESS`` defaults), so both ``fleet --json ls`` and ``fleet ls --json``
    work: a subparser default would otherwise clobber a value given before the verb."""
    d = {} if top else {"default": argparse.SUPPRESS}
    p.add_argument("--hub", metavar="URL", help="hub to talk to (default: discover a running hub on this box)", **d)
    p.add_argument(
        "--token",
        help=f"credential for --hub — a fleet token or operator bearer (env: {deckhub.ENV_TOKEN}); never printed",
        **d,
    )
    p.add_argument("--offline", action="store_true", help="read this instance's fleet.json instead of asking a hub", **d)
    p.add_argument(
        "--insecure-http",
        action="store_true",
        help="allow sending a credential to a non-loopback http:// hub (only for a link you know is encrypted, e.g. a tailnet)",
        **d,
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON for scripting", **d)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protoagent fleet",
        description=(
            "The fleet deck. With no verb: an interactive terminal over the running hub "
            "(roster, member detail, logs, lifecycle). With a verb: the non-interactive "
            "commands — live from the hub, or from disk when none answers (ADR 0042)."
        ),
    )
    _common(p, top=True)
    sub = p.add_subparsers(dest="cmd", required=False)
    pu = sub.add_parser("up", help="start agents — all stopped local members, or named")
    pu.add_argument("names", nargs="*")
    _common(pu, top=False)
    pd = sub.add_parser("down", help="stop agents — all running, or named")
    pd.add_argument("names", nargs="*")
    _common(pd, top=False)
    pl = sub.add_parser("ls", help="list members with live status")
    _common(pl, top=False)
    ps = sub.add_parser("status", help="alias for ls")
    _common(ps, top=False)
    # ── manage (#3471): the same operations the deck and the console drive ──
    pn = sub.add_parser("new", help="create a member — blank, or from an archetype / bundle — and start it")
    pn.add_argument("name")
    pn.add_argument("--archetype", metavar="ID", help="an archetype id from the hub's catalog (live mode)")
    pn.add_argument("--bundle", metavar="GIT_URL", help="a bundle to install into the new member")
    pn.add_argument("--port", type=int)
    pn.add_argument("--no-start", dest="start", action="store_false", help="create only; do not start it")
    pn.add_argument("--no-inherit", dest="inherit", action="store_false", help="a fully blank agent — no model connections or credentials from the hub")
    _common(pn, top=False)
    pr = sub.add_parser("rm", help="remove a member (stops it first); its data is kept unless --purge")
    pr.add_argument("name")
    pr.add_argument("--purge", action="store_true", help="also delete its workspace and data — cannot be undone")
    pr.add_argument("--yes", action="store_true", help="do not ask; required when stdin is not a terminal")
    _common(pr, top=False)
    prn = sub.add_parser("rename", help="change a member's display name (its id, slug and data never change)")
    prn.add_argument("name")
    prn.add_argument("new_name")
    _common(prn, top=False)
    prm = sub.add_parser("remote", help="remote members: add | edit | rm")
    rsub = prm.add_subparsers(dest="remote_cmd", required=True)
    ra = rsub.add_parser("add", help="register a remote protoAgent as a fleet member")
    ra.add_argument("name")
    ra.add_argument("url")
    ra.add_argument("--bearer", help="the remote's bearer (prefer --bearer-stdin: argv is visible to other processes and shell history); --token is the HUB's credential")
    ra.add_argument("--bearer-stdin", action="store_true", help="read the remote's bearer from stdin (one line)")
    _common(ra, top=False)
    re_ = rsub.add_parser("edit", help="change a remote's name, url or token in place")
    re_.add_argument("name")
    re_.add_argument("--name", dest="new_name")
    re_.add_argument("--url")
    re_.add_argument("--bearer", help="a new bearer for the remote (--token is the HUB's credential)")
    re_.add_argument("--bearer-stdin", action="store_true", help="read the new bearer from stdin (one line)")
    re_.add_argument("--clear-bearer", action="store_true", help="forget the stored bearer")
    _common(re_, top=False)
    rr = rsub.add_parser("rm", help="unregister a remote member (the remote agent itself is untouched)")
    rr.add_argument("name")
    _common(rr, top=False)
    po = sub.add_parser("order", help="persist the roster's display order: every member id, in the order wanted")
    po.add_argument("ids", nargs="+", metavar="ID")
    _common(po, top=False)
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
        return deckhub.connect(url=args.hub, token=args.token, insecure_http=args.insecure_http)
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


# ── the deck (bare `protoagent fleet`, or `protoagent top`) ──────────────────


# ── manage (#3471) ───────────────────────────────────────────────────────────


def _read_bearer(args: argparse.Namespace) -> str | None:
    """The REMOTE's bearer from ``--bearer`` or one line of stdin (``--bearer-stdin``);
    None when neither. (``--token`` is the hub credential every verb takes.)"""
    if getattr(args, "bearer_stdin", False):
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    return getattr(args, "bearer", None)


def _hub_detail(exc: Exception) -> str:
    return exc.detail if isinstance(exc, deckhub.HubRequestError) else str(exc)


def _run_op(coro_fn, *a, **kw):
    """Run one op to completion off any loop (the CLI has none)."""
    import asyncio

    return asyncio.run(coro_fn(*a, **kw))


def _cmd_new(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    results: list[dict] = []
    body: dict[str, Any] = {"name": args.name, "start": args.start, "inherit_config": args.inherit}
    if args.port:
        body["port"] = args.port
    if args.bundle:
        body["bundle"] = args.bundle
    if conn is not None:
        hub_url = conn.client.url
        try:
            if args.archetype:
                arch = next((a for a in conn.client.archetypes() if str(a.get("id")) == args.archetype), None)
                if arch is None:
                    _row_fail(args.name, args, results, f"no archetype {args.archetype!r} on the hub (see the deck's n, or GET /api/archetypes)")
                    return _finish(args, "live", results, hub_url)
                if arch.get("bundle"):
                    body["bundle"] = arch["bundle"]
                if arch.get("soul"):
                    body["soul"] = arch["soul"]
                if arch.get("requires_tools"):
                    body["requires_tools"] = list(arch["requires_tools"])
            try:
                res = conn.client.create(body)
            except deckhub.HubError as exc:
                _row_fail(args.name, args, results, _hub_detail(exc))
                return _finish(args, "live", results, hub_url)
            agent = res.get("agent") or {}
            state = f"started (:{agent.get('port')}, pid {agent.get('pid')})" if agent.get("running") else f"created (:{agent.get('port')}, not started)"
            for w in res.get("warnings") or []:
                _note(f"note: {w}", args)
            _row_ok(args.name, args, results, f"{state} via hub {hub_url}", agent=agent, installed=res.get("installed", []), warnings=res.get("warnings", []))
        finally:
            conn.client.close()
        return _finish(args, "live", results, hub_url)
    if args.archetype:
        _row_fail(args.name, args, results, "--archetype needs a running hub (its catalog lives there); offline, pass --bundle <git-url> or nothing for a blank member")
        return _finish(args, "offline", results)
    from graph.workspaces import manager
    from ops import fleet as fleet_ops

    try:
        res = _run_op(fleet_ops.create, args.name, bundle=args.bundle, port=args.port, start=args.start, inherit_config=args.inherit)
    except (manager.WorkspaceError, supervisor.FleetError) as exc:
        _row_fail(args.name, args, results, str(exc))
        return _finish(args, "offline", results)
    agent = res.get("agent") or {}
    state = f"started (:{agent.get('port')}, pid {agent.get('pid')})" if agent.get("running") else f"created (:{agent.get('port')}, not started)"
    _row_ok(args.name, args, results, f"{state} via disk (offline)", agent=agent, installed=res.get("installed", []), warnings=res.get("warnings", []))
    return _finish(args, "offline", results)


def _confirm_remove(args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        _row_fail(args.name, args, [], "refusing to remove without --yes when stdin is not a terminal")
        return False
    what = "DELETE its workspace and data" if args.purge else "remove it from the fleet (its data is kept)"
    try:
        typed = input(f"  this will stop {args.name} and {what} — type the name to confirm: ")
    except EOFError:
        return False
    return typed.strip() == args.name


def _cmd_rm(args: argparse.Namespace) -> int:
    results: list[dict] = []
    if not _confirm_remove(args):
        if not args.as_json:
            print("  aborted", file=sys.stderr)
        return _finish(args, "aborted", [{"name": args.name, "ok": False, "error": "not confirmed"}])
    conn = _open_hub(args)
    if conn is not None:
        hub_url = conn.client.url
        try:
            res = conn.client.remove(args.name, purge=args.purge)
        except deckhub.HubRequestError as exc:
            if exc.status == 409:
                # partial, retryable: the member IS stopped, only its workspace survived (#2583)
                _row_fail(args.name, args, results, "stopped, but its workspace survived — repeat to finish", retryable=True)
            else:
                _row_fail(args.name, args, results, exc.detail)
            return _finish(args, "live", results, hub_url)
        except deckhub.HubError as exc:
            _row_fail(args.name, args, results, str(exc))
            return _finish(args, "live", results, hub_url)
        finally:
            conn.client.close()
        _row_ok(args.name, args, results, ("purged" if "workspace" in (res.get("removed") or []) else "removed (data kept)") + f" via hub {hub_url}", removed=res.get("removed", []))
        return _finish(args, "live", results, hub_url)
    from graph.workspaces import manager
    from ops import fleet as fleet_ops

    try:
        res = _run_op(fleet_ops.remove, args.name, purge=args.purge)
    except manager.WorkspaceBusy as exc:
        _row_fail(args.name, args, results, f"{exc} — repeat to finish", retryable=True)
        return _finish(args, "offline", results)
    except (manager.WorkspaceError, supervisor.FleetError) as exc:
        _row_fail(args.name, args, results, str(exc))
        return _finish(args, "offline", results)
    _row_ok(args.name, args, results, ("purged" if "workspace" in (res.get("removed") or []) else "removed (data kept)") + " via disk (offline)", removed=res.get("removed", []))
    return _finish(args, "offline", results)


def _cmd_rename(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    results: list[dict] = []
    if conn is not None:
        hub_url = conn.client.url
        try:
            res = conn.client.rename(args.name, args.new_name)
        except deckhub.HubError as exc:
            _row_fail(args.name, args, results, _hub_detail(exc))
            return _finish(args, "live", results, hub_url)
        finally:
            conn.client.close()
        _row_ok(args.name, args, results, f"renamed to {res.get('name')} (id {res.get('id')} unchanged) via hub {hub_url}", id=res.get("id"), new_name=res.get("name"))
        return _finish(args, "live", results, hub_url)
    from graph.workspaces import manager
    from ops import fleet as fleet_ops

    try:
        res = _run_op(fleet_ops.rename, args.name, args.new_name)
    except manager.WorkspaceError as exc:
        _row_fail(args.name, args, results, str(exc))
        return _finish(args, "offline", results)
    _row_ok(args.name, args, results, f"renamed to {res.get('name')} (id {res.get('id')} unchanged) via disk (offline)", id=res.get("id"), new_name=res.get("name"))
    return _finish(args, "offline", results)


def _remote_text(res: dict) -> str:
    agent = res.get("agent") or {}
    reach = f"reachable, v{res.get('version')}" if res.get("reachable") else "unreachable for now (registered anyway)"
    return f"{agent.get('url', '')} · {reach}"


def _cmd_remote(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    results: list[dict] = []
    name = args.name
    token = _read_bearer(args) if args.remote_cmd in ("add", "edit") else None
    if args.remote_cmd == "edit" and args.clear_bearer:
        token = ""
    if conn is not None:
        hub_url = conn.client.url
        try:
            if args.remote_cmd == "add":
                res = conn.client.remote_add(name, args.url, token or "")
                _row_ok(name, args, results, f"added: {_remote_text(res)} via hub {hub_url}", agent=res.get("agent"), reachable=res.get("reachable"), version=res.get("version"))
            elif args.remote_cmd == "edit":
                fields = {k: v for k, v in (("name", args.new_name), ("url", args.url), ("token", token)) if v is not None}
                if not fields:
                    _row_fail(name, args, results, "nothing to change — pass --name, --url, --bearer/--bearer-stdin or --clear-bearer")
                    return _finish(args, "live", results, hub_url)
                res = conn.client.remote_update(name, **fields)
                _row_ok(name, args, results, f"updated: {_remote_text(res)} via hub {hub_url}", agent=res.get("agent"), reachable=res.get("reachable"), version=res.get("version"))
            else:
                res = conn.client.remote_remove(name)
                _row_ok(name, args, results, f"unregistered via hub {hub_url} (the remote agent itself is untouched)", id=res.get("id"))
        except deckhub.HubError as exc:
            _row_fail(name, args, results, _hub_detail(exc))
        finally:
            conn.client.close()
        return _finish(args, "live", results, hub_url)
    from graph.workspaces import manager
    from ops import fleet as fleet_ops

    try:
        if args.remote_cmd == "add":
            res = _run_op(fleet_ops.remotes_add, name, args.url, token or "")
            _row_ok(name, args, results, f"added: {_remote_text(res)} via disk (offline)", agent=res.get("agent"), reachable=res.get("reachable"), version=res.get("version"))
        elif args.remote_cmd == "edit":
            if args.new_name is None and args.url is None and token is None:
                _row_fail(name, args, results, "nothing to change — pass --name, --url, --bearer/--bearer-stdin or --clear-bearer")
                return _finish(args, "offline", results)
            res = _run_op(fleet_ops.remotes_update, name, name=args.new_name, url=args.url, token=token)
            _row_ok(name, args, results, f"updated: {_remote_text(res)} via disk (offline)", agent=res.get("agent"), reachable=res.get("reachable"), version=res.get("version"))
        else:
            res = _run_op(fleet_ops.remotes_remove, name)
            _row_ok(name, args, results, "unregistered via disk (offline)", id=res.get("id"))
    except (supervisor.FleetError, manager.WorkspaceError) as exc:
        _row_fail(name, args, results, str(exc))
    return _finish(args, "offline", results)


def _cmd_order(args: argparse.Namespace) -> int:
    conn = _open_hub(args)
    results: list[dict] = []
    if conn is not None:
        hub_url = conn.client.url
        try:
            res = conn.client.set_order(list(args.ids))
        except deckhub.HubError as exc:
            _row_fail("order", args, results, _hub_detail(exc))
            return _finish(args, "live", results, hub_url)
        finally:
            conn.client.close()
        _row_ok("order", args, results, f"saved: {' › '.join(res.get('order') or args.ids)} via hub {hub_url}", order=res.get("order"))
        return _finish(args, "live", results, hub_url)
    from ops import fleet as fleet_ops

    try:
        order = _run_op(fleet_ops.order, list(args.ids))
    except supervisor.FleetError as exc:
        _row_fail("order", args, results, str(exc))
        return _finish(args, "offline", results)
    _row_ok("order", args, results, f"saved: {' › '.join(order)} via disk (offline)", order=order)
    return _finish(args, "offline", results)


def _offline_backend(reason: str):
    """The disk backend, built here so ``deck`` never imports ``graph``: the supervisor's
    callables are handed over, and its synthesized host row is dropped by the backend."""
    import importlib

    from graph.workspaces import manager

    deckdata = importlib.import_module("deck.data")
    return deckdata.OfflineBackend(
        status=supervisor.status,
        start=lambda name: supervisor.start(name),
        stop=lambda name: supervisor.stop(name),
        fleet_json=manager.workspaces_root() / "fleet.json",
        reason=reason,
    )


def _cmd_deck(args: argparse.Namespace) -> int:
    """Open the interactive deck. Textual is imported by NAME here so the non-interactive
    verbs and ``protoagent --help`` never load it, and a frozen build that does not bundle
    it gets a one-line hint instead of a traceback (the sidecar decision is S6, #3473)."""
    import importlib

    if args.as_json:
        # "fleet, as JSON" can only mean the roster — Textual against a pipe would hang.
        return _cmd_ls(args)
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        # Checked BEFORE importing Textual: a redirected run should not load it just to say no.
        print("✗ the deck needs a terminal — for scripts use `protoagent fleet ls --json`", file=sys.stderr)
        return 2
    try:
        deckapp = importlib.import_module("deck.app")
        deckdata = importlib.import_module("deck.data")
    except ModuleNotFoundError as exc:
        print(
            f"✗ the fleet deck is not available in this build ({exc.name}) — use `protoagent fleet ls|up|down`, "
            "or run from a source checkout / `uv tool install protolabs-agent`",
            file=sys.stderr,
        )
        return 2
    conn = _open_hub(args)
    if conn is not None:
        backend = deckdata.LiveBackend(conn)
    else:
        backend = _offline_backend("no hub answered")
    return int(deckapp.run(backend))


def run_deck_cli(argv: list[str]) -> int:
    """``protoagent top`` — the deck, straight away (flags as for ``fleet``). A leading
    verb is dropped for compatibility (`top ls` is still the deck); an option VALUE that
    happens to spell a verb (`--token status`) is left alone."""
    rest = list(argv)
    if rest and rest[0] in ("ls", "up", "down", "status"):
        rest = rest[1:]
    return run_fleet_cli(rest)


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
        if args.cmd is None:
            return _cmd_deck(args)
        if args.cmd == "up":
            return _cmd_up(args)
        if args.cmd == "down":
            return _cmd_down(args)
        if args.cmd == "new":
            return _cmd_new(args)
        if args.cmd == "rm":
            return _cmd_rm(args)
        if args.cmd == "rename":
            return _cmd_rename(args)
        if args.cmd == "remote":
            return _cmd_remote(args)
        if args.cmd == "order":
            return _cmd_order(args)
        return _cmd_ls(args)
    except ValueError as exc:  # a malformed --hub (deck.hub.normalize_url)
        if args.as_json:
            _emit({"mode": "error", "hub": deckhub.redact_url(args.hub) if args.hub else None, "error": str(exc)})
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1
    except deckhub.HubError as exc:  # NoHub with something answering, or a mid-call failure
        # exc.url is already normalized (userinfo stripped); args.hub is raw operator input.
        hub_url = getattr(exc, "url", "") or (deckhub.redact_url(args.hub) if args.hub else "")
        if args.as_json:
            _emit({"mode": "error", "hub": hub_url or None, "error": str(exc)})
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1
    except supervisor.FleetError as exc:  # the disk path (offline) — same JSON error shape as the rest
        if args.as_json:
            _emit({"mode": "error", "hub": None, "error": str(exc)})
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1
