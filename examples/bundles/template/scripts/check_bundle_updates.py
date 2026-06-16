#!/usr/bin/env python3
"""Pin-bump checker for a bundle manifest (ADR 0049). Zero deps — stdlib + git.

For every member pinned to a semver TAG (`ref: vX.Y.Z`), ls-remote the repo's tags
and rewrite the manifest's `ref:` to the newest tag in place — comment-preserving
(plain text substitution; member entries must stay on one line, see the manifest).
Raw-SHA pins and `builtin:` members are left alone by design.

    python3 scripts/check_bundle_updates.py protoagent.bundle.yaml

Prints one `bump: <id> <old> -> <new>` line per change. Exit 0 with changes written
(the workflow turns a dirty tree into a PR), exit 0 untouched when current.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_MEMBER = re.compile(r"^\s*-\s*\{\s*id:\s*(?P<id>[\w-]+)\s*,\s*url:\s*(?P<url>\S+?)\s*,\s*ref:\s*(?P<ref>\S+?)\s*\}")
_SEMVER_TAG = re.compile(r"^v?\d+\.\d+\.\d+$")


def _semver_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("v").split("."))


def latest_tag(url: str) -> str | None:
    """Newest semver tag at ``url`` (peeled lines preferred-equivalent: tag NAMES only)."""
    out = subprocess.run(
        ["git", "ls-remote", "--tags", url],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    tags = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name = parts[1].removeprefix("refs/tags/").removesuffix("^{}")
        if _SEMVER_TAG.match(name):
            tags.add(name)
    return max(tags, key=_semver_key) if tags else None


def main(manifest_path: str) -> int:
    path = Path(manifest_path)
    lines = path.read_text().splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        m = _MEMBER.match(line)
        if not m or not _SEMVER_TAG.match(m["ref"]):
            continue  # builtin, raw-SHA pin, or not a member line — leave alone
        newest = latest_tag(m["url"])
        if newest and _semver_key(newest) > _semver_key(m["ref"]):
            lines[i] = line.replace(f"ref: {m['ref']}", f"ref: {newest}")
            print(f"bump: {m['id']} {m['ref']} -> {newest}")
            changed = True
    if changed:
        path.write_text("".join(lines))
    else:
        print("all tag pins current")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "protoagent.bundle.yaml"))
