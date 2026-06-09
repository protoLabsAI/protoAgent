"""``python -m server fleet …`` — run a fleet of workspace agents (ADR 0042).

A thin CLI over ``graph.fleet.supervisor``. ``up`` starts agents as detached
background processes, ``down`` stops them, ``ls`` shows running status.
"""

from __future__ import annotations

import argparse
import sys

from graph.fleet import supervisor


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server fleet",
        description="Run a fleet of workspace agents as background processes (ADR 0042).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pu = sub.add_parser("up", help="start agents — all workspaces, or named")
    pu.add_argument("names", nargs="*")
    pd = sub.add_parser("down", help="stop agents — all running, or named")
    pd.add_argument("names", nargs="*")
    sub.add_parser("ls", help="list workspaces + running status")
    sub.add_parser("status", help="alias for ls")
    return p


def run_fleet_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "up":
            rows = supervisor.up(args.names or None)
            if not rows:
                print("(no workspaces — create one: python -m server workspace new <name>)")
            for r in rows:
                tag = "already running" if r.get("already") else "started"
                print(f"  ✓ {r['name']:16} {tag} (:{r.get('port')}, pid {r.get('pid')})")
            return 0
        if args.cmd == "down":
            rows = supervisor.down(args.names or None)
            if not rows:
                print("(nothing running)")
            for r in rows:
                print(f"  ✓ {r['name']:16} stopped")
            return 0
        if args.cmd in ("ls", "status"):
            rows = supervisor.status()
            if not rows:
                print("(no workspaces — python -m server workspace new <name>)")
                return 0
            for w in rows:
                dot = "●" if w["running"] else "○"  # ● running / ○ stopped
                state = f"pid {w['pid']}" if w["running"] else "stopped"
                bundle = f"  [{w['bundle']}]" if w["bundle"] else ""
                print(f"  {dot} {w['name']:16} :{w['port']:<6} {state}{bundle}")
            return 0
    except supervisor.FleetError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0
