"""``protoagent fleet friction`` — every member's friction ledger, in one place.

Each agent keeps its own append-only ledger (``plugins/friction``). The console's fleet
rollup merges open rows inside the hub process and drops everything triage needs — per-entry
rows, detail, tool payloads, resolved history. This verb reads each member's ledger through
the hub's ``/agents/<slug>/…`` proxy (the same path ``fleet`` uses for every per-member read),
so it reaches local and remote members with the credential the hub already holds, and an
unreachable member is reported, never silently skipped.

Live mode only: there is no fleet to read without a hub. ``resolve`` is a mutation, so it
never defaults to a blast radius — the operator names the agents or passes ``--all-agents``.

The legacy ``/api/friction`` paths are called, not ``/api/plugins/friction/…``: a hub is
routinely newer than its members and the old path exists in both (see
``plugins/friction.__init__._member_friction`` for the same reasoning).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from deck import hub as deckhub
from deck.data import presence_of, slug_of

_READABLE = ("host", "online", "remote")  # presence words that can answer a ledger read
_SEVERITY_RANK = {"minor": 1, "major": 2}
_SINCE = re.compile(r"^(\d+)([smhdw])$")
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_LIST_CAP = 500  # the ledger route's own ceiling


def add_parser(sub: Any, common: Any) -> None:
    """Register ``friction`` (and ``friction resolve``) on the fleet parser."""
    pf = sub.add_parser(
        "friction",
        help="every member's friction ledger — list for triage, or `friction resolve` once fixed",
    )
    pf.add_argument("--kind", choices=("harness", "model"), help="only this channel")
    pf.add_argument("--severity", choices=("minor", "major"), help="at least this severe")
    pf.add_argument("--since", metavar="WINDOW", help="seen within a window — 12h, 7d, 2w — or an ISO date")
    pf.add_argument("--agent", action="append", metavar="NAME", help="only this member (repeatable; `host` is the hub itself)")
    pf.add_argument("--resolved", action="store_true", help="include entries already resolved (default: open only)")
    pf.add_argument("--raw", action="store_true", help="one row per recorded entry, not per signal (repeats are not collapsed)")
    pf.add_argument("--limit", type=int, default=100, metavar="N", help="at most N rows per agent (default 100, max 500)")
    pf.add_argument("-v", "--detail", action="store_true", help="print each row's detail (the reproduction and what would have helped)")
    common(pf, top=False)
    fsub = pf.add_subparsers(dest="friction_cmd", required=False)
    pr = fsub.add_parser("resolve", help="mark a signal resolved (or --reopen) on the named agents")
    pr.add_argument("id", help="the signal id `fleet friction` printed (8 hex chars)")
    pr.add_argument("--agent", action="append", metavar="NAME", help="resolve on this member (repeatable)")
    pr.add_argument("--all-agents", action="store_true", help="resolve on EVERY member that has it — never the default")
    pr.add_argument("--reason", default="", help="why — stamped on the entry and kept in the ledger")
    pr.add_argument("--reopen", action="store_true", help="clear the resolved stamp instead")
    common(pr, top=False)


# ── parsing ──────────────────────────────────────────────────────────────────


def parse_since(text: str, *, now: datetime | None = None) -> str:
    """``7d`` / ``12h`` / an ISO date → an ISO-8601 UTC cutoff string (ledger timestamps
    are ISO strings, so the comparison is lexicographic). Raises ``ValueError``."""
    now = now or datetime.now(timezone.utc)
    raw = text.strip()
    m = _SINCE.match(raw.lower())
    if m:
        return (now - timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})).isoformat()
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"--since {text!r}: use a window like 12h, 7d, 2w — or an ISO date") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat()


def _keep(item: dict, *, severity: str | None, since: str | None) -> bool:
    if severity and _SEVERITY_RANK.get(str(item.get("severity")), 0) < _SEVERITY_RANK[severity]:
        return False
    if since:
        seen = str(item.get("last_seen") or item.get("ts") or "")
        # Compare as instants where both parse; the ledger writes ISO-8601 with an offset.
        try:
            return datetime.fromisoformat(seen.replace("Z", "+00:00")) >= datetime.fromisoformat(since)
        except ValueError:
            return True  # an unparseable stamp is not evidence the row is old
    return True


def _rank(item: dict) -> tuple:
    return (
        _SEVERITY_RANK.get(str(item.get("severity")), 0),
        int(item.get("count") or 1),
        str(item.get("last_seen") or item.get("ts") or ""),
    )


# ── reading ──────────────────────────────────────────────────────────────────


def _targets(roster: list[dict], names: list[str] | None) -> tuple[list[tuple[str, dict]], list[dict], list[str]]:
    """(readable members as (slug, row), skipped rows as ``{name, reason}``, requested names
    that match no member). The last matters: a mistyped ``--agent`` must read as an error,
    never as an agent with no friction."""
    wanted = {n.lower() for n in (names or [])}
    matched: set[str] = set()
    readable: list[tuple[str, dict]] = []
    skipped: list[dict] = []
    for row in roster:
        slug = slug_of(row)
        label = str(row.get("label") or row.get("name") or slug)
        keys = {slug.lower(), label.lower(), str(row.get("name") or "").lower()}
        if wanted and not (keys & wanted):
            continue
        matched |= keys & wanted
        pres = presence_of(row)
        if pres in _READABLE:
            readable.append((slug, row))
        else:
            skipped.append({"name": label, "reason": pres})
    unmatched = [n for n in (names or []) if n.lower() not in matched]
    return readable, skipped, unmatched


def _fetch(client: Any, slug: str, *, grouped: bool, resolved: bool, kind: str | None, limit: int) -> list[dict]:
    params: dict[str, Any] = {"grouped": "true" if grouped else "false", "limit": max(1, min(limit, _LIST_CAP))}
    if resolved:
        params["resolved"] = "true"
    if kind:
        params["kind"] = kind
    body = client.member_get(slug, "/api/friction", **params)
    if not isinstance(body, dict):
        raise deckhub.HubError(client.url, f"{slug}: friction answered with something that is not a ledger")
    if body.get("error"):
        raise deckhub.HubError(client.url, f"{slug}: {body['error']}")
    return [i for i in (body.get("items") or []) if isinstance(i, dict)]


def _short(err: Exception) -> str:
    if isinstance(err, deckhub.MemberUnauthorized):
        return "rejected the credential the hub attached"
    if isinstance(err, deckhub.HubRequestError):
        # A member on a core that predates the friction plugin has no ledger route at all.
        return "no friction ledger (member predates the plugin, or has it disabled)" if err.status == 404 else str(err.detail)
    return str(err)


def collect(
    client: Any,
    roster: list[dict],
    args: argparse.Namespace,
) -> dict:
    """The read: per-agent rows plus who was skipped/unreachable, and signals shared across
    agents. One member failing never loses the others."""
    since = parse_since(args.since) if args.since else None
    readable, skipped, unmatched = _targets(roster, args.agent)
    results: list[dict] = [
        {"name": n, "slug": n, "ok": False, "error": "no such member (`protoagent fleet ls` lists them)", "items": []} for n in unmatched
    ]
    for slug, row in readable:
        name = str(row.get("label") or row.get("name") or slug)
        try:
            items = _fetch(client, slug, grouped=not args.raw, resolved=args.resolved, kind=args.kind, limit=args.limit)
        except deckhub.HubError as exc:
            results.append({"name": name, "slug": slug, "ok": False, "error": _short(exc), "items": []})
            continue
        items = sorted((i for i in items if _keep(i, severity=args.severity, since=since)), key=_rank, reverse=True)
        results.append({"name": name, "slug": slug, "ok": True, "items": items, "total": len(items)})

    # A signal several agents hit is a harness bug, not one agent's quirk — the whole reason
    # to read the fleet at once. The id is derived from (kind, summary), so it is equal
    # across agents with no extra bookkeeping.
    seen: dict[str, dict] = {}
    for r in results:
        for i in r["items"]:
            sid = str(i.get("id") or "")
            if not sid:
                continue
            row = seen.setdefault(sid, {"id": sid, "kind": i.get("kind", ""), "summary": i.get("summary", ""), "agents": [], "count": 0})
            if r["name"] not in row["agents"]:
                row["agents"].append(r["name"])
            row["count"] += int(i.get("count") or 1)
    shared = sorted((s for s in seen.values() if len(s["agents"]) > 1), key=lambda s: (len(s["agents"]), s["count"]), reverse=True)
    return {"results": results, "skipped": skipped, "shared": shared}


# ── output ───────────────────────────────────────────────────────────────────


def _line(i: dict, raw: bool) -> str:
    sev = str(i.get("severity") or "minor")
    count = int(i.get("count") or 1)
    tail = f" ×{count}" if count > 1 else ""
    ident = str(i.get("id") or "········")
    src = f" [{i['tool']}]" if i.get("tool") else ""
    return f"    {ident}  {sev:<5} {str(i.get('kind') or ''):<7}{tail} {i.get('summary', '')}{src}"


def render(data: dict, args: argparse.Namespace, header: str) -> None:
    print(header)
    results = data["results"]
    total = sum(r["total"] for r in results if r["ok"])
    if not results:
        print("  (no running members to read — `protoagent fleet ls`)")
    for r in results:
        if not r["ok"]:
            print(f"  ✗ {r['name']}: {r['error']}")
            continue
        n = r["total"]
        print(f"  {r['name']} — {n} {'signal' if n == 1 and not args.raw else ('entry' if args.raw and n == 1 else ('entries' if args.raw else 'signals'))}")
        for i in r["items"]:
            print(_line(i, args.raw))
            if args.detail and i.get("detail"):
                for ln in str(i["detail"]).strip().splitlines()[:12]:
                    print(f"        {ln}")
    if data["shared"]:
        print("\n  Shared across agents (fix once, clears everywhere):")
        for s in data["shared"][:15]:
            print(f"    {s['id']}  {len(s['agents'])} agents ×{s['count']}  {s['summary']}")
    if data["skipped"]:
        names = ", ".join(f"{s['name']} ({s['reason']})" for s in data["skipped"])
        print(f"\n  not read: {names}", file=sys.stderr)
    print(f"\n  {total} {'row' if total == 1 else 'rows'} across {sum(1 for r in results if r['ok'])} agent(s)"
          + (f" · {sum(1 for r in results if not r['ok'])} unreachable" if any(not r['ok'] for r in results) else ""))


# ── resolve ──────────────────────────────────────────────────────────────────


def resolve(client: Any, roster: list[dict], args: argparse.Namespace) -> list[dict]:
    """Resolve (or reopen) one signal id on the named agents. Finds the row by id on each
    agent first — the POST matches the full (kind, summary) exactly, so what is stamped is
    precisely the row that was listed, never a neighbour whose text overlaps."""
    names = args.agent or None
    readable, _, unmatched = _targets(roster, names)
    out: list[dict] = [{"name": n, "ok": False, "error": "no such member (`protoagent fleet ls` lists them)"} for n in unmatched]
    for slug, row in readable:
        name = str(row.get("label") or row.get("name") or slug)
        try:
            # Resolving looks at open rows; reopening looks at resolved ones.
            items = _fetch(client, slug, grouped=True, resolved=args.reopen, kind=None, limit=_LIST_CAP)
            hit = next((i for i in items if str(i.get("id")) == args.id), None)
            if hit is None:
                if names:  # named explicitly → say so; a sweep just skips agents without it
                    out.append({"name": name, "ok": False, "error": f"no {'resolved' if args.reopen else 'open'} signal {args.id}"})
                continue
            res = client.member_post(
                slug,
                "/api/friction/resolve",
                {"summary": hit.get("summary"), "kind": hit.get("kind"), "reason": args.reason, "resolved": not args.reopen},
            )
        except deckhub.HubError as exc:
            out.append({"name": name, "ok": False, "error": _short(exc)})
            continue
        if isinstance(res, dict) and res.get("error"):
            out.append({"name": name, "ok": False, "error": str(res["error"])})
            continue
        out.append({"name": name, "ok": True, "changed": int((res or {}).get("changed") or 0), "summary": hit.get("summary"), "reopened": args.reopen})
    return out
