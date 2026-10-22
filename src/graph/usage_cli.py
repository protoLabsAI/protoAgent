"""``protoagent usage`` — subscription usage for the native-OAuth providers (ADR 0122; #4433).

One operation, three projections (ADR 0075 D2): the ``usage.snapshot`` op feeds the console,
``GET /api/usage`` (U2b), and this CLI. The CLI never calls the op directly — it reads the
*running instance's* ``GET /api/usage`` the same way the other live commands reach a server:
through :mod:`deck.hub`. So it shows exactly what the console shows, with the hub's credential,
and never resolves tokens or polls a provider itself.

Two modes:

- **local** (default) — read this instance's own ``/api/usage`` (the ``host`` slug on the
  connection ``deck.hub`` opens). One account set, per account and per window.
- **``--fleet``** — read every readable member's ``/api/usage`` through the hub proxy, the way
  ``protoagent fleet friction`` reads every ledger. Accounts are deduped by ``account_key`` and
  the members that share one are listed; a member that cannot be read is reported as
  unreachable without hiding the rest.

``--warn-at PCT`` (default 80) sets the exit code: ``2`` when any window is at or above it,
``0`` otherwise, and ``1`` when no server/hub answered. ``--json`` emits the stable schema
documented in ``docs/guides/cli.md``. Read-only, operator-authenticated, bounded, redacted —
the fleet-diagnostics contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from deck import hub as deckhub
from deck.data import display_name, presence_of, slug_of

__all__ = ["run_usage_cli"]

# Presence words whose member is a live server we can read /api/usage from.
_READABLE = ("host", "online", "remote")

_DEFAULT_WARN_AT = 80.0


def _now() -> datetime:
    """Wall clock for the reset countdown — a seam tests monkeypatch for determinism."""
    return datetime.now(UTC)


# ── parsing ────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="protoagent usage",
        description=(
            "Subscription usage for the native-OAuth providers (Claude, Codex). Reads the "
            "running instance's /api/usage; --fleet reads every member through the hub."
        ),
    )
    p.add_argument("--fleet", action="store_true", help="every fleet member through the hub, deduped by account")
    p.add_argument("--account", metavar="ID", help="only this account (provider id or account key)")
    p.add_argument(
        "--warn-at",
        dest="warn_at",
        type=float,
        default=_DEFAULT_WARN_AT,
        metavar="PCT",
        help=f"exit 2 when any window is at or above PCT used (default {_DEFAULT_WARN_AT:g})",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="emit the stable JSON schema for scripting")
    p.add_argument("--hub", metavar="URL", help="hub/server to read (default: discover the one running on this box)")
    p.add_argument(
        "--token",
        help=f"credential for --hub — a fleet token or operator bearer (env: {deckhub.ENV_TOKEN}); never printed",
    )
    p.add_argument(
        "--insecure-http",
        dest="insecure_http",
        action="store_true",
        help="allow sending a credential to a non-loopback http:// --hub (a network you trust)",
    )
    return p


# ── reading ──────────────────────────────────────────────────────────────────


def _read_usage(client: Any, slug: str) -> list[dict]:
    """``/api/usage`` for one member, as a list of account dicts. Raises ``HubError`` so the
    caller decides whether that is fatal (local) or just one unreachable member (fleet)."""
    body = client.member_get(slug, "/api/usage")
    if not isinstance(body, dict):
        raise deckhub.HubError(client.url, f"{slug}: usage answered with something that is not a snapshot")
    if body.get("error"):
        raise deckhub.HubError(client.url, f"{slug}: {body['error']}")
    return [a for a in (body.get("accounts") or []) if isinstance(a, dict)]


def _short(exc: Exception) -> str:
    if isinstance(exc, deckhub.MemberUnauthorized):
        return "rejected the credential the hub attached"
    if isinstance(exc, deckhub.HubRequestError):
        return "no usage route (member predates it)" if exc.status == 404 else str(exc.detail)
    return str(exc)


def _matches(acct: dict, ident: str) -> bool:
    """``--account`` matches either the provider id/type or the opaque account key."""
    ident = ident.strip()
    return ident in {
        str(acct.get("provider_id") or ""),
        str(acct.get("provider_type") or ""),
        str(acct.get("account_key") or ""),
    }


# ── shaping (account / window → the stable record) ──────────────────────────────


def _used_pct(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _window_label(w: dict) -> str:
    """The window's human label; a scoped window is labelled by its model (ADR 0122)."""
    kind = str(w.get("kind") or "other")
    scope = w.get("scope") if isinstance(w.get("scope"), dict) else None
    if scope:
        for key in ("model", "bucket"):
            if scope.get(key):
                return f"{kind}·{scope[key]}"
    return kind


def _countdown(resets_at: Any, now: datetime) -> tuple[int | None, str]:
    """(seconds until reset or None, compact human text). ``—`` when absent/unparseable,
    ``now`` when already past."""
    if not resets_at:
        return None, "—"
    try:
        when = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None, "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    secs = int((when - now).total_seconds())
    if secs <= 0:
        return 0, "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _s = divmod(rem, 60)
    if d:
        text = f"{d}d{h}h" if h else f"{d}d"
    elif h:
        text = f"{h}h{m}m" if m else f"{h}h"
    elif m:
        text = f"{m}m"
    else:
        text = f"{_s}s"
    return secs, text


def _account_record(acct: dict, members: list[str], warn_at: float, now: datetime) -> dict:
    # A stale account is NOT live. usage_store._present forces its status to "unknown" but
    # keeps the last-known windows (each still carrying its old, live-looking per-window
    # status) "so the console can grey out the previous numbers". Those numbers are not
    # current, so they must never trip --warn-at — otherwise the CLI would report outdated
    # usage as current and exit 2 on it indefinitely (ADR 0122 D4).
    stale = bool(acct.get("stale"))
    windows: list[dict] = []
    for w in acct.get("windows") or []:
        if not isinstance(w, dict):
            continue
        used = _used_pct(w.get("used_pct"))
        secs, human = _countdown(w.get("resets_at"), now)
        over = used is not None and used >= warn_at and not stale
        windows.append(
            {
                "id": str(w.get("id") or ""),
                "kind": str(w.get("kind") or "other"),
                "label": _window_label(w),
                "scope": w.get("scope") if isinstance(w.get("scope"), dict) else None,
                "used_pct": used,
                "status": str(w.get("status") or "ok"),
                "resets_at": w.get("resets_at"),
                "resets_in_seconds": secs,
                "resets_in": human,
                "window_seconds": w.get("window_seconds"),
                "binding": bool(w.get("binding")),
                "over_warn": over,
            }
        )
    return {
        "provider_id": str(acct.get("provider_id") or ""),
        "provider_type": str(acct.get("provider_type") or acct.get("provider_id") or ""),
        "account_key": acct.get("account_key"),
        "plan": acct.get("plan"),
        "status": str(acct.get("status") or "unknown"),
        "stale": stale,
        "error": acct.get("error"),
        "members": list(members),
        "windows": windows,
    }


def _dedupe_key(acct: dict) -> tuple[str, str]:
    """Group accounts for ``--fleet``: by ``account_key`` when present (the dedupe the task
    asks for), else by provider id so one disconnected provider collapses to a single row."""
    key = acct.get("account_key")
    return ("key", str(key)) if key else ("provider", str(acct.get("provider_id") or ""))


def _best_account(accounts: list[dict]) -> dict:
    """The richest representation of one account across the members that reported it: a real
    (``ok``) read with windows beats an ``unknown``/stale one."""
    return max(
        accounts,
        key=lambda a: (
            1 if str(a.get("status")) == "ok" else 0,
            len([w for w in (a.get("windows") or []) if isinstance(w, dict)]),
            0 if a.get("stale") else 1,
        ),
    )


# ── collection ─────────────────────────────────────────────────────────────────


def _collect_local(conn: deckhub.Connection, args: argparse.Namespace, now: datetime) -> list[dict]:
    """This instance's own usage — the ``host`` slug on the opened connection."""
    accounts = _read_usage(conn.client, "host")
    return [
        _account_record(a, [], args.warn_at, now)
        for a in accounts
        if not args.account or _matches(a, args.account)
    ]


def _collect_fleet(conn: deckhub.Connection, args: argparse.Namespace, now: datetime) -> tuple[list[dict], list[dict]]:
    """Every readable member's usage through the hub proxy, deduped by account. Returns
    (account records, unreachable members). One member failing never loses the others."""
    client, roster = conn.client, conn.roster
    groups: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    unreachable: list[dict] = []
    for row in roster:
        slug = slug_of(row)
        member = display_name(row)
        pres = presence_of(row)
        if pres not in _READABLE:
            unreachable.append({"member": member, "slug": slug, "error": pres})
            continue
        try:
            accounts = _read_usage(client, slug)
        except deckhub.HubError as exc:
            unreachable.append({"member": member, "slug": slug, "error": _short(exc)})
            continue
        for acct in accounts:
            if args.account and not _matches(acct, args.account):
                continue
            key = _dedupe_key(acct)
            g = groups.get(key)
            if g is None:
                g = {"members": [], "accounts": []}
                groups[key] = g
                order.append(key)
            if member not in g["members"]:
                g["members"].append(member)
            g["accounts"].append(acct)
    records = [_account_record(_best_account(groups[k]["accounts"]), groups[k]["members"], args.warn_at, now) for k in order]
    return records, unreachable


# ── output ───────────────────────────────────────────────────────────────────


def _payload(mode: str, args: argparse.Namespace, *, ok: bool, hub: str | None, records: list[dict], unreachable: list[dict], error: str | None) -> dict:
    warn_hit = any(w["over_warn"] for r in records for w in r["windows"])
    return {
        "mode": mode,
        "ok": ok,
        "hub": hub,
        "warn_at": args.warn_at,
        "warn_hit": warn_hit,
        "error": error,
        "accounts": records,
        "unreachable": unreachable,
    }


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _render(payload: dict) -> None:
    fleet = payload["mode"] == "fleet"
    print(f"protoagent usage{' · fleet' if fleet else ''} · {payload['hub'] or 'no server'}")
    accounts = payload["accounts"]
    if not accounts:
        print("  (no usage to show — no native-OAuth provider connected)")
    rows: list[list[str]] = []
    for a in accounts:
        provider = a["provider_id"] or "?"
        plan = str(a["plan"]) if a["plan"] else "—"
        members = ", ".join(a["members"]) if a["members"] else ""
        if a["windows"]:
            stale = a["stale"]
            for w in a["windows"]:
                used = f"{w['used_pct']:.0f}%" if w["used_pct"] is not None else "—"
                if w["over_warn"]:
                    used += "!"
                # A stale account's last-known windows are marked "stale" — not shown with
                # their old per-window status, which would read as a current live value.
                status = "stale" if stale else w["status"]
                rows.append([provider, plan, w["label"], used, w["resets_in"] or "—", status, members])
        else:
            status = f"{a['status']} ({a['error']})" if a["error"] else a["status"]
            rows.append([provider, plan, "—", "—", "—", status, members])
    ncols = 7 if fleet else 6
    widths = [max((len(r[c]) for r in rows), default=0) for c in range(ncols)]
    for r in rows:
        cells = r[:ncols]
        print("  " + "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(cells)).rstrip())
    if payload["unreachable"]:
        names = ", ".join(f"{u['member']} ({u['error']})" for u in payload["unreachable"])
        print(f"\n  unreachable: {names}", file=sys.stderr)
    if payload["warn_hit"]:
        print(f"\n  ⚠ a window is at or above {payload['warn_at']:g}% — exit 2", file=sys.stderr)


# ── entry ────────────────────────────────────────────────────────────────────


def _no_server(args: argparse.Namespace, exc: Exception) -> int:
    mode = "fleet" if args.fleet else "local"
    hub = getattr(exc, "url", "") or (deckhub.redact_url(args.hub) if args.hub else None)
    if args.as_json:
        _emit(_payload(mode, args, ok=False, hub=hub or None, records=[], unreachable=[], error=str(exc)))
    else:
        what = "--fleet needs a running hub" if args.fleet else "needs a running server"
        print(f"✗ protoagent usage {what} — start one with `protoagent up` ({exc})", file=sys.stderr)
    return 1


def run_usage_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    # The server configures INFO logging at import; keep httpx's per-request narration off
    # the operator's face (errors still surface as typed HubErrors), as the fleet CLI does.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    now = _now()
    try:
        conn = deckhub.connect(url=args.hub, token=args.token, insecure_http=args.insecure_http)
    except deckhub.HubError as exc:
        return _no_server(args, exc)

    hub_url = conn.client.url
    try:
        if args.fleet:
            records, unreachable = _collect_fleet(conn, args, now)
        else:
            records, unreachable = _collect_local(conn, args, now), []
    except deckhub.HubError as exc:
        # The server answered connect() but its /api/usage did not — nothing to read.
        return _no_server(args, exc)
    finally:
        conn.client.close()

    payload = _payload("fleet" if args.fleet else "local", args, ok=True, hub=hub_url, records=records, unreachable=unreachable, error=None)
    if args.as_json:
        _emit(payload)
    else:
        _render(payload)
    return 2 if payload["warn_hit"] else 0
