#!/usr/bin/env python3
"""Publish stable docs media slots and generate the internal production inventory."""

from __future__ import annotations

import argparse
from datetime import date
from html import escape
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "docs/dev/media/registry.json"
REPORT = "docs/dev/media-audit.md"
ASSETS = "plugins/docs/media-assets.json"
BLOCK = re.compile(r"<!-- docs-media: ([a-z0-9-]+) -->.*?<!-- /docs-media -->", re.S)
STATES = {"planned", "in-production", "in-review", "ready", "refresh-needed", "retired"}
PUBLISHED = {"ready", "refresh-needed"}
EXTENSIONS = {
    "image": {".png", ".jpg", ".webp", ".svg"},
    "animation": {".gif"},
    "video": {".mp4", ".webm"},
    "diagram": {".svg", ".png", ".webp"},
}


def asset_path(root: Path, value: str) -> Path:
    p = PurePosixPath(value)
    if (
        not re.fullmatch(r"[a-zA-Z0-9_./-]+", value)
        or p.is_absolute()
        or ".." in p.parts
        or "\\" in value
        or p.as_posix() != value
    ):
        raise ValueError(f"Invalid media path: {value!r}")
    base = root / "docs/public/media"
    target = base / value
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Media escapes its directory: {value}")
    return target


def load_registry(root: Path = ROOT) -> dict:
    data = json.loads((root / REGISTRY).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Unsupported media registry version")
    catalog = json.loads((root / "plugins/docs/catalog.json").read_text(encoding="utf-8"))
    pages = {j["landing"] for j in catalog["journeys"]} | {
        p for j in catalog["journeys"] for g in j["groups"] for p in g["pages"]
    }
    if set(data["pages"]) != pages:
        raise ValueError("Media audit must cover every current journey page and hub")
    for p, audit in data["pages"].items():
        if audit["decision"] not in {"media", "text", "reuse"} or not audit["reason"]:
            raise ValueError(f"Missing page disposition: {p}")
    allowed = set()
    placements = set()
    for key, item in data["media"].items():
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key):
            raise ValueError(f"Invalid media ID: {key}")
        if item["status"] not in STATES or item["kind"] not in EXTENSIONS or item["priority"] not in {"P1", "P2", "P3"}:
            raise ValueError(f"Invalid state, kind, or priority: {key}")
        for field in ("purpose", "brief", "alt", "caption", "owner", "refresh_when"):
            if not item.get(field):
                raise ValueError(f"Missing {field}: {key}")
        if not item["placements"] or not item["watch"]:
            raise ValueError(f"Missing placement/source: {key}")
        for watched in item["watch"]:
            path = (root / watched).resolve()
            if not path.is_relative_to(root.resolve()) or not path.exists():
                raise ValueError(f"Missing or unsafe watched source: {watched}")
        for place in item["placements"]:
            page = place["page"]
            if page not in pages or (page, key) in placements:
                raise ValueError(f"Invalid/duplicate placement: {key} in {page}")
            placements.add((page, key))
            if place["heading"] not in (root / "docs" / page).read_text(encoding="utf-8").splitlines():
                raise ValueError(f"Placement heading changed: {key} in {page}")
        if item["status"] not in PUBLISHED:
            continue
        for field in ("asset", "verified_on", "verified_commit", "source", "reviewer"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"Published media needs {field}: {key}")
        date.fromisoformat(item["verified_on"])
        if not re.fullmatch(r"[0-9a-f]{40}", item["verified_commit"]):
            raise ValueError(f"A full capture/review commit is required: {key}")
        if not item.get("source") or not item.get("reviewer"):
            raise ValueError(f"Missing editable source or reviewer: {key}")
        if Path(item["asset"]).suffix not in EXTENSIONS[item["kind"]]:
            raise ValueError(f"Wrong asset format: {key}")
        fields = ["asset"]
        if item["kind"] in {"video", "animation"}:
            fields += ["poster", "transcript"]
            for field in ("poster", "transcript"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise ValueError(f"Motion needs {field}: {key}")
            if (
                Path(item["poster"]).suffix not in {".png", ".jpg", ".webp"}
                or Path(item["transcript"]).suffix != ".txt"
            ):
                raise ValueError(f"Motion requires a still poster and text transcript: {key}")
        if item["kind"] == "video":
            if not isinstance(item.get("has_audio"), bool):
                raise ValueError(f"Declare has_audio for video: {key}")
            if item["has_audio"]:
                fields += ["captions"]
                if not isinstance(item.get("captions"), str) or Path(item["captions"]).suffix != ".vtt":
                    raise ValueError(f"Narrated video needs WebVTT captions: {key}")
        for field in fields:
            value = item[field]
            if not value.startswith(key + "/") or not asset_path(root, value).is_file():
                raise ValueError(f"Missing {field} under {key}/: {value}")
            allowed.add(value)
    for p, audit in data["pages"].items():
        if audit["decision"] == "media" and not any(page == p for page, _ in placements):
            raise ValueError(f"Media decision has no slot: {p}")
    # Public assets ship on the site regardless of slot state. Never put unreviewed captures here.
    if any(p.is_symlink() for p in (root / "docs/public/media").rglob("*")):
        raise ValueError("Public media must be regular files, not symlinks")
    disk = {
        p.relative_to(root / "docs/public/media").as_posix()
        for p in (root / "docs/public/media").rglob("*")
        if p.is_file() and p.name != ".gitkeep"
    }
    if disk != allowed:
        raise ValueError(f"Unregistered public media: {sorted(disk - allowed)}")
    found = []
    for section in ("tutorials", "guides", "reference", "explanation", "adr"):
        for p in (root / "docs" / section).rglob("*.md"):
            text = p.read_text(encoding="utf-8")
            blocks = list(BLOCK.finditer(text))
            if text.count("<!-- docs-media:") != len(blocks) or text.count("<!-- /docs-media -->") != len(blocks):
                raise ValueError(f"Malformed media block: {p}")
            found += [(p.relative_to(root / "docs").as_posix(), b[1]) for b in blocks]
    if len(found) != len(set(found)) or set(found) != placements:
        raise ValueError(f"Slot placements differ from registry: {set(found) ^ placements}")
    return data


def render(item: dict) -> str:
    if item["status"] not in PUBLISHED:
        return ""

    def url(field):
        return escape("/media/" + item[field], quote=True)

    caption, alt = escape(item["caption"]), escape(item["alt"], quote=True)
    if item["kind"] == "video":
        track = (
            f'<track kind="captions" src="{url("captions")}" srclang="en" label="English" default>'
            if item["has_audio"]
            else ""
        )
        content = f'<video controls playsinline preload="none" poster="{url("poster")}" aria-label="{alt}" style="max-width:100%"><source src="{url("asset")}">{track}<a href="{url("asset")}">Download video</a></video>'
    else:
        field = "poster" if item["kind"] == "animation" else "asset"
        content = f'<img src="{url(field)}" alt="{alt}" loading="lazy" style="max-width:100%;height:auto">'
    if item["kind"] in {"video", "animation"}:
        content += f'<p><a href="{url("asset")}">Open {item["kind"]}</a> · <a href="{url("transcript")}">Read the text walkthrough</a></p>'
    return f'<figure class="docs-media">{content}<figcaption>{caption}</figcaption></figure>'


def outputs(data: dict, root: Path = ROOT) -> dict[str, str]:
    result = {}
    for page in {p["page"] for m in data["media"].values() for p in m["placements"]}:
        path = "docs/" + page
        result[path] = BLOCK.sub(
            lambda m: f"<!-- docs-media: {m[1]} -->\n{render(data['media'][m[1]])}\n<!-- /docs-media -->",
            (root / path).read_text(encoding="utf-8"),
        )
    assets = sorted(
        {
            m[k]
            for m in data["media"].values()
            if m["status"] in PUBLISHED
            for k in (
                ["asset"]
                + (["poster", "transcript"] if m["kind"] in {"video", "animation"} else [])
                + (["captions"] if m["kind"] == "video" and m["has_audio"] else [])
            )
        }
    )
    result[ASSETS] = json.dumps({"files": assets}, indent=2) + "\n"
    lines = [
        "# Documentation media audit",
        "",
        "Generated by `python scripts/docs_media.py`; edit `docs/dev/media/registry.json`.",
        "See [the production workflow](media/README.md) for publishing and refreshing assets.",
        "",
        f"Audit baseline: `{data['baseline']}`. {len(data['pages'])} primary pages/hubs screened; {len(data['media'])} media records.",
        "",
        "This is a content/structure audit, not a visual QA of newly captured media. Existing README captures are reuse candidates, not approved current screenshots.",
        "",
        f"Production tracker: {data.get('tracker', 'not assigned')}",
        "",
        "## Production queue",
        "",
        "| ID | Priority | Format | Status | Owner | Verified |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for key, m in sorted(data["media"].items(), key=lambda kv: (kv[1]["priority"], kv[0])):
        lines.append(
            f"| [{key}](#{key}) | {m['priority']} | {m['kind']} | {m['status']} | {m['owner']} | {m.get('verified_on') or '—'} |"
        )
    for key, m in data["media"].items():
        lines += [
            "",
            f"## {key}",
            "",
            m["purpose"],
            "",
            f"**Capture / drawing brief:** {m['brief']}",
            "",
            "**Placement:** "
            + "; ".join(f"[{p['page']}](../{p['page']}) → {p['heading'].lstrip('# ')}" for p in m["placements"]),
            "",
            f"**Caption:** {m['caption']}",
            "",
            f"**Alt text draft:** {m['alt']}",
            "",
            f"**Refresh when:** {m['refresh_when']}",
            "",
            "**Watch:** " + ", ".join(f"`{p}`" for p in m["watch"]),
        ]
        lines += [
            "",
            f"**Published asset:** `{m.get('asset') or 'not created'}` · **Source:** {m.get('source') or 'not created'}",
            "",
            f"**Review:** {m.get('reviewer') or 'pending'} · {m.get('verified_on') or 'not reviewed'} · commit `{m.get('verified_commit') or 'none'}`",
        ]
    lines += ["", "## Page dispositions", "", "| Page | Decision | Reason |", "| --- | --- | --- |"]
    for p, audit in data["pages"].items():
        lines.append(f"| [{p}](../{p}) | {audit['decision']} | {audit['reason']} |")
    result[REPORT] = "\n".join(lines) + "\n"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated blocks/inventory differ")
    parser.add_argument(
        "--changed-since", help="report media affected by source changes since a Git ref (includes working tree)"
    )
    args = parser.parse_args()
    data = load_registry()
    if args.changed_since:
        ref = subprocess.check_output(
            ["git", "rev-parse", "--verify", args.changed_since + "^{commit}"], cwd=ROOT, text=True
        ).strip()
        changed = subprocess.check_output(["git", "diff", "--name-only", ref, "--"], cwd=ROOT, text=True).splitlines()
        for key, item in data["media"].items():
            hits = [p for p in changed if any(p == w or p.startswith(w.rstrip("/") + "/") for w in item["watch"])]
            if hits:
                print(f"{key} [{item['status']}]: " + ", ".join(hits))
        return
    stale = []
    for path, text in outputs(data).items():
        target = ROOT / path
        if target.exists() and target.read_text(encoding="utf-8") == text:
            continue
        if args.check:
            stale.append(path)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    if stale:
        raise SystemExit("Run python scripts/docs_media.py: " + ", ".join(stale))
    print("Docs media slots, asset allowlist, and audit are in sync")


if __name__ == "__main__":
    main()
