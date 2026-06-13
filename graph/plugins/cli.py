"""`python -m server plugin …` — scaffold + manage plugins (ADR 0027).

A thin CLI over ``graph.plugins.installer`` (install/list/…) and
``graph.plugins.scaffold`` (``new`` / ``new-bundle``). Install fetches code only
(it never enables the plugin or installs its deps — both are explicit, by design);
``new`` writes a skeleton on disk (enable it from the console or, in a running
agent, the devkit's ``enable_plugin`` tool — live, no restart).
"""

from __future__ import annotations

import argparse
import sys

from graph.plugins import installer, scaffold


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server plugin",
        description="Scaffold + install/manage plugins (ADR 0027).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("new", help="scaffold a new plugin skeleton on disk (ready to fill in + enable)")
    pn.add_argument("name", help="human name (the id is slugified from it, e.g. \"My Plugin\" → my-plugin)")
    pn.add_argument("--summary", default="A protoAgent plugin.", help="one-line description for the manifest")
    pn.add_argument("--view", action="store_true", help="include a console view (sandboxed iframe + router)")
    pn.add_argument("--skill", action="store_true", help="include a SKILL.md skill stub")
    pn.add_argument("--workflow", action="store_true", help="include a workflow YAML stub")
    pn.add_argument("--comms", action="store_true", help="a communication plugin (ChatAdapter, ADR 0029) instead of a tool plugin")
    pn.add_argument("--tests", action="store_true", help="include a host-free test suite + CI + requirements-dev (for a standalone-repo plugin)")
    pn.add_argument("--dir", default=None, help="target dir (default: the live plugins dir the loader discovers)")

    pnb = sub.add_parser("new-bundle", help="scaffold a plugin BUNDLE (protoagent.bundle.yaml, ADR 0040)")
    pnb.add_argument("name", help="human name for the bundle (slugified to its id)")
    pnb.add_argument("--summary", default="A protoAgent plugin bundle.", help="one-line description")
    pnb.add_argument("--member", action="append", metavar="id=url[@ref]", default=[],
                     help="a git plugin member; repeatable (e.g. --member board=https://github.com/you/board@v0.1.0)")
    pnb.add_argument("--builtin", action="append", metavar="id", default=[],
                     help="a built-in member that ships with protoAgent; repeatable (e.g. --builtin delegates)")
    pnb.add_argument("--dir", default=None, help="target dir (default: the live plugins dir)")

    pi = sub.add_parser("install", help="install a plugin — or a bundle of plugins — from a git URL (does NOT enable it)")
    pi.add_argument("url", help="git URL (https://, ssh://, git@, or a local path) of a plugin or a bundle repo")
    pi.add_argument("--ref", default=None, help="tag, branch, or commit SHA to pin (default: default branch HEAD)")
    pi.add_argument("--force", action="store_true", help="replace an already-installed plugin of the same id")

    sub.add_parser("list", help="list git-installed plugins (from plugins.lock)")
    pu = sub.add_parser("uninstall", help="remove a git-installed plugin (code + lock + enabled ref)")
    pu.add_argument("id")
    pu.add_argument("--purge", action="store_true", help="also remove the plugin's config section + secrets")
    sub.add_parser("sync", help="re-clone locked plugins at their pinned SHA (reproducible set)")
    pd = sub.add_parser("install-deps", help="pip-install a plugin's declared requires_pip (explicit code-exec)")
    pd.add_argument("id")
    return p


def run_plugin_cli(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "new":
            try:
                res = scaffold.scaffold_plugin(
                    args.name, summary=args.summary, with_view=args.view, with_skill=args.skill,
                    with_workflow=args.workflow, with_comms=args.comms, with_tests=args.tests,
                    target_dir=args.dir,
                )
            except FileExistsError as e:
                print(f"✗ {scaffold.slug(args.name)!r} already exists at {e}", file=sys.stderr)
                return 1
            print(f"✓ scaffolded {res.kind} {res.id!r} at {res.path}")
            print(f"  wrote: {', '.join(res.made)}")
            if res.kind == "comms":
                print("  next: implement the ChatAdapter (see plugins/telegram), then enable it from Settings.")
            else:
                print("  next: fill in __init__.py, then enable it — toggle it on in the console, or in a running")
                print(f"        agent (with plugin-devkit enabled) ask it to enable_plugin('{res.id}') — live, no restart.")
            return 0
        if args.cmd == "new-bundle":
            members: list[dict] = []
            for spec in args.member:
                if "=" not in spec:
                    print(f"✗ bad --member {spec!r} (expected id=url[@ref])", file=sys.stderr)
                    return 1
                mid, rest = spec.split("=", 1)
                url, _, ref = rest.partition("@")
                members.append({"id": mid, "url": url, "ref": ref or None})
            for bid in args.builtin:
                members.append({"id": bid, "builtin": True})
            try:
                res = scaffold.scaffold_bundle(
                    args.name, summary=args.summary, members=members or None, target_dir=args.dir,
                )
            except FileExistsError as e:
                print(f"✗ {scaffold.slug(args.name)!r} already exists at {e}", file=sys.stderr)
                return 1
            print(f"✓ scaffolded bundle {res.id!r} at {res.path}")
            print(f"  wrote: {', '.join(res.made)}")
            if not members:
                print("  fill in the REPLACE_ME member(s), then:")
            print("  commit/push it, then `plugin install <repo-url>` to install + enable the stack (ADR 0040).")
            return 0
        if args.cmd == "install":
            s = installer.install(args.url, args.ref, force=args.force,
                                  allow=installer.configured_allowlist())
            if "bundle" in s:  # a bundle: a set of plugins installed together
                print(f"✓ installed bundle {s['bundle']}" + (f" — {s['name']}" if s["name"] else ""))
                if s["description"]:
                    print(f"  {s['description']}")
                for p in s["installed"]:
                    print(f"  ✓ {p['id']} v{p['version']} @ {p['resolved_sha'][:10]}")
                if s["skipped_builtin"]:
                    print(f"  · built-in (already ships with protoAgent): {', '.join(s['skipped_builtin'])}")
                deps = sorted({d for p in s["installed"] for d in p.get("requires_pip", [])})
                if deps:
                    print(f"  ⚠ member deps (NOT installed — review, then `plugin install-deps <id>`): {', '.join(deps)}")
                if s["enabled"]:
                    print(f"  NOT enabled. To turn on the stack, set plugins.enabled to include: "
                          f"[{', '.join(s['enabled'])}], then restart.")
                if s["config"]:
                    print(f"  recommended config: {s['config']}")
                return 0
            print(f"✓ installed {s['id']} v{s['version']} @ {s['resolved_sha'][:10]}")
            if s["description"]:
                print(f"  {s['description']}")
            if s["repository"]:
                print(f"  repo: {s['repository']}")
            if s["requires_pip"]:
                print(f"  ⚠ declared deps (NOT installed — review, then install): {', '.join(s['requires_pip'])}")
                print(f"    pip install {' '.join(s['requires_pip'])}")
            if s["contributes"]["views"]:
                print(f"  contributes views: {', '.join(s['contributes']['views'])}")
            if s["capabilities"]:
                print(f"  declared capabilities: {s['capabilities']}")
            print(f"  NOT enabled. To enable, add '{s['id']}' to plugins.enabled in your config, then restart.")
            return 0
        if args.cmd == "list":
            rows = installer.list_installed()
            if not rows:
                print("(no git-installed plugins)")
                return 0
            for e in rows:
                mark = "" if e.get("present") else "  [MISSING — run `plugin sync`]"
                print(f"  {e['id']:20} {e['resolved_sha'][:10]}  {e['source_url']}{mark}")
            return 0
        if args.cmd == "uninstall":
            rep = installer.uninstall(args.id, purge=args.purge)
            print(f"✓ uninstalled {args.id} — removed: {', '.join(rep['removed'])}")
            if rep["deps_left"]:
                print(f"  declared deps left installed (shared venv — remove manually if unused): {', '.join(rep['deps_left'])}")
            if not args.purge:
                print("  config + secrets kept (reinstall restores them). Use --purge to remove them too.")
            return 0
        if args.cmd == "sync":
            for r in installer.sync(allow=installer.configured_allowlist()):
                extra = f" ({r['error']})" if r.get("error") else ""
                print(f"  {r['id']}: {r['status']}{extra}")
            return 0
        if args.cmd == "install-deps":
            deps = installer.install_deps(args.id)
            print(f"✓ installed {len(deps)} dep(s) for {args.id}: {', '.join(deps)}" if deps
                  else f"{args.id} declares no deps")
            return 0
    except installer.InstallError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0
