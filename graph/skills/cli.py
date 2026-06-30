"""``python -m server skills …`` — inspect/curate the skills index (ADR 0041).

``ls`` lists skills (tagged by tier when layered); ``promote <name>`` lifts a private
skill into the shared commons; ``forget <name>`` removes a skill FROM the commons.
``curate [--tier]`` runs the skill curator on ONE concrete tier: the **private** tier
gets the full pass (idle-decay + dedupe + prune-below-threshold); the shared **commons**
is trusted, so it only **dedupes** — no idle-decay (a promoted skill mustn't rot because
the fleet was idle) and no auto-prune (removal is the explicit ``forget``). Builds the
same tier paths the running agent uses, scoped to this config's ``instance.id``.
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
    fp = sub.add_parser("forget", help="remove a skill FROM the shared commons (inverse of promote)")
    fp.add_argument("name", help="the commons skill name to forget")
    cp = sub.add_parser(
        "curate",
        help="run the curator on ONE tier (private: decay+dedupe+prune · commons: dedupe only)",
    )
    cp.add_argument("--tier", choices=("private", "commons"), default="private", help="tier to curate (default: private)")
    cp.add_argument(
        "--prune",
        action="store_true",
        help="also prune below-threshold skills on the commons (private prunes by default)",
    )
    cp.add_argument("--dry-run", action="store_true", help="compute changes but write nothing")
    return p


def _layered_index():
    """Build the layered (private ∪ commons) index from the live config, scoped to its
    instance — so the CLI sees exactly what the running agent does. Returns
    ``(index, commons_path)`` so the caller can surface which commons is in use: it's
    host-level + un-scoped, so showing the path guards the shared-host footgun (ADR 0041)."""
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from graph.skills.index import SkillsIndex
    from graph.skills.layered import LayeredSkillsIndex
    from server.agent_init import _commons_dir, _resolve_skills_db

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    commons = _commons_dir(cfg)
    private = SkillsIndex(db_path=_resolve_skills_db(cfg.skills_db_path, shared=False))
    shared_path = _resolve_skills_db(cfg.skills_db_path, shared=True, commons=commons)
    shared = SkillsIndex(db_path=shared_path)
    return LayeredSkillsIndex(private, shared), shared_path


def _resolve_tier_db(tier: str) -> str:
    """Resolve the concrete on-disk DB path for ONE tier (private | commons) using the
    same resolution the running agent uses — so the curator targets a single backend
    (never a layered union, which would make rowid-based deletes ambiguous)."""
    from graph.config import LangGraphConfig
    from graph.config_io import config_yaml_path
    from server.agent_init import _commons_dir, _resolve_skills_db

    cfg = LangGraphConfig.from_yaml(config_yaml_path())
    if tier == "commons":
        return _resolve_skills_db(cfg.skills_db_path, shared=True, commons=_commons_dir(cfg))
    return _resolve_skills_db(cfg.skills_db_path, shared=False)


def _run_curate(args) -> int:
    """`skills curate --tier` — run the curator on one concrete tier with the tier's
    policy (private: full pass; commons: dedupe only unless --prune, never decay)."""
    from pathlib import Path

    from graph.skills.curator import SkillCurator
    from graph.skills.index import SkillsIndex

    tier = args.tier
    db_path = _resolve_tier_db(tier)
    index = SkillsIndex(db_path=db_path)
    decay = tier == "private"  # commons is trusted — never idle-decays
    prune = True if tier == "private" else bool(args.prune)  # commons prunes only on --prune
    # Audit next to the tier's DB (not a global default) — the commons audit lives with
    # the commons, the private audit with the private store.
    audit_path = str(Path(db_path).with_name(f"curator-audit-{tier}.jsonl"))
    curator = SkillCurator(
        db_path=db_path, audit_path=audit_path, index=index, tier=tier, decay=decay, prune=prune, dry_run=args.dry_run
    )
    try:
        entry = curator.run()
    finally:
        index.close()
    mode = "dry-run, nothing written" if args.dry_run else "applied"
    print(
        f"✓ curated tier={tier} ({mode}) — {db_path}\n"
        f"  before={entry['skills_before']} after={entry['skills_after']} · "
        f"decay={len(entry['decay_applied'])} dedup_clusters={len(entry['deduplicated'])} "
        f"pruned={len(entry['pruned'])}"
    )
    return 0


def run_skills_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "curate":
        return _run_curate(args)  # single-tier — no layered index
    idx, commons_path = _layered_index()
    try:
        if args.cmd == "ls":
            print(f"commons: {commons_path}")  # host-level + shared by every agent (ADR 0041)
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
                print(f"✓ promoted {args.name!r} into the shared commons ({commons_path})")
                return 0
            print(
                f"✗ promote {args.name!r} failed — no private skill by that name, "
                f"or the commons ({commons_path}) isn't writable",
                file=sys.stderr,
            )
            return 1
        if args.cmd == "forget":
            ok = idx.forget_from_commons(args.name)
            if ok:
                print(f"✓ forgot {args.name!r} from the shared commons ({commons_path})")
                return 0
            print(f"✗ no commons skill named {args.name!r}", file=sys.stderr)
            return 1
    finally:
        idx.close()
    return 0
