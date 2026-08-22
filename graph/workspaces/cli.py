"""``python -m server workspace …`` — manage workspaces (ADR 0041).

A thin CLI over ``graph.workspaces.manager``. ``new`` scaffolds (never starts);
``run`` ``exec``s the normal server with the workspace's config dir + instance + port.
"""

from __future__ import annotations

import argparse
import os
import sys

from graph.workspaces import manager


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server workspace",
        description="Workspaces — named, isolated agents on one host (ADR 0041).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("new", help="scaffold a new workspace (does NOT start it)")
    pn.add_argument("name", help="workspace name (letters, digits, '-', '_')")
    pn.add_argument("--from", dest="from_config", default=None, help="clone an existing config dir/file as the base")
    pn.add_argument("--bundle", default=None, help="install a plugin bundle/plugin git URL into it")
    pn.add_argument("--port", type=int, default=None, help="bind port (default: auto)")
    pn.add_argument("--shared-skills", action="store_true", help="share the skills commons across the fleet (ADR 0041)")
    pn.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="answer a bundle Configure prompt (config_inputs, #2934), e.g. "
        "--input project_board.repo=/abs/repo --input project_board.coder=claude-code; "
        "a bundle's required prompts must be answered or the create is refused",
    )
    pn.add_argument(
        "--soul",
        default=None,
        metavar="FILE",
        help="persona file written as the workspace's SOUL.md (the picker does this from the archetype preset)",
    )

    sub.add_parser("ls", help="list workspaces")

    pr = sub.add_parser("run", help="start a workspace's server (config dir + instance + port)")
    pr.add_argument("name")
    pr.add_argument("rest", nargs=argparse.REMAINDER, help="extra args forwarded to the server")

    pd = sub.add_parser("rm", help="take a workspace out of the fleet (its data is kept)")
    pd.add_argument("name")
    pd.add_argument(
        "--purge",
        action="store_true",
        help="also DELETE the workspace — config, chats, knowledge, tasks. Irreversible.",
    )
    return p


def run_workspace_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "new":
            config_inputs: dict[str, object] = {}
            for item in args.input:
                key, sep, value = str(item).partition("=")
                if not sep or not key.strip():
                    print(f"✗ --input expects KEY=VALUE, got {item!r}", file=sys.stderr)
                    return 2
                config_inputs[key.strip()] = value
            soul = None
            if args.soul:
                from pathlib import Path as _Path

                soul = _Path(args.soul).expanduser().read_text(encoding="utf-8")
            # The CLI creates from inside the host process tree, so the host config dir is
            # the delegate source for a `type: delegate` answer — same as the console path.
            from graph.config_io import config_yaml_path

            host_cfg = config_yaml_path()
            s = manager.create(
                args.name,
                from_config=args.from_config,
                bundle=args.bundle,
                port=args.port,
                shared_skills=args.shared_skills,
                soul=soul,
                config_inputs=config_inputs or None,
                delegate_source=str(host_cfg.parent) if host_cfg.exists() else None,
            )
            print(f"✓ created workspace {s['name']} (id={s['id']}, port={s['port']})")
            print(f"  {s['path']}")
            if s.get("installed"):
                print(f"  installed: {', '.join(s['installed'])}")
            print(f"  edit langgraph-config.yaml (model + secrets), then: python -m server workspace run {s['name']}")
            return 0
        if args.cmd == "ls":
            rows = manager.list_workspaces()
            if not rows:
                print("(no workspaces — create one: python -m server workspace new <name>)")
                return 0
            for w in rows:
                b = f"  bundle={w['bundle']}" if w["bundle"] else ""
                print(f"  {w['name']:16} id={w['id']:16} :{w['port']}{b}")
            return 0
        if args.cmd == "run":
            env, cmd = manager.run_exec(args.name, args.rest or [])
            os.environ.update(env)
            print(
                f"→ workspace {args.name}: {env['PROTOAGENT_HOME']} (instance={env['PROTOAGENT_INSTANCE']})",
                file=sys.stderr,
            )
            os.execvp(cmd[0], cmd)  # replace this process with the server
            return 0  # unreachable
        if args.cmd == "rm":
            rep = manager.remove(args.name, purge=args.purge)
            # Without --purge nothing is deleted, so say where the data is rather than
            # printing an empty "(…)" that reads like the removal half-failed.
            if rep["removed"]:
                print(f"✓ removed workspace {args.name} ({', '.join(rep['removed'])})")
            else:
                print(f"✓ retired workspace {args.name} — data kept at {rep['retired_at']}")
            return 0
    except manager.WorkspaceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0
