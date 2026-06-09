"""`python -m server plugin …` — manage git-installed plugins (ADR 0027).

A thin CLI over ``graph.plugins.installer``. Install fetches code only (it never
enables the plugin or installs its deps — both are explicit, by design).
"""

from __future__ import annotations

import argparse
import sys

from graph.plugins import installer


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server plugin",
        description="Install/manage plugins from git URLs (ADR 0027).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

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
