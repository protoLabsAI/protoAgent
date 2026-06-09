"""``python -m server skills …`` — inspect/curate the skills index (ADR 0041).

``ls`` lists skills (tagged by tier when layered); ``promote <name>`` lifts a private
skill into the shared commons. Builds the same layered index the running agent uses,
scoped to this config's ``instance.id``.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server skills",
        description="Inspect/curate the skills index — shared commons + private (ADR 0041).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls", help="list skills (private + commons tiers)")
    pp = sub.add_parser("promote", help="lift a private skill into the shared commons")
    pp.add_argument("name", help="the skill name to promote")
    return p


def _layered_index():
    """Build the layered (private ∪ commons) index from the live config, scoped to its
    instance — so the CLI sees exactly what the running agent does."""
    from graph.config import LangGraphConfig
    from graph.config_io import _live_config_dir
    from graph.skills.index import SkillsIndex
    from graph.skills.layered import LayeredSkillsIndex
    from server.agent_init import _commons_dir, _resolve_skills_db, _seed_instance_env

    cfg = LangGraphConfig.from_yaml(str(_live_config_dir() / "langgraph-config.yaml"))
    _seed_instance_env(cfg)  # so the private path resolves to THIS agent's scope
    commons = _commons_dir(cfg)
    private = SkillsIndex(db_path=_resolve_skills_db(cfg.skills_db_path, shared=False))
    shared = SkillsIndex(db_path=_resolve_skills_db(cfg.skills_db_path, shared=True, commons=commons))
    return LayeredSkillsIndex(private, shared)


def run_skills_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    idx = _layered_index()
    try:
        if args.cmd == "ls":
            rows = idx.all_skills()
            if not rows:
                print("(no skills indexed)")
                return 0
            for s in rows:
                print(f"  [{s.get('tier', '?'):7}] {s.get('name', '')}")
            return 0
        if args.cmd == "promote":
            ok = idx.promote(args.name)
            if ok:
                print(f"✓ promoted {args.name!r} into the shared commons")
                return 0
            print(f"✗ no private skill named {args.name!r}", file=sys.stderr)
            return 1
    finally:
        idx.close()
    return 0
