"""The docs corpus — resolve the `docs/` tree and enumerate the indexed markdown.

The agent indexes + reads the SAME `docs/` the VitePress site publishes. We index the
four Diátaxis sections plus the ADRs, and deliberately EXCLUDE `docs/dev/**` (internal
handoffs — mirrors the site's `srcExclude: ["dev/**"]`) and `.vitepress/` (build config).

Resolution works in every deployment: in dev/Docker the tree is the repo's `docs/`; in the
frozen desktop sidecar the PyInstaller build bundles the doc dirs, so `docs/` sits beside
the bundled `plugins/` under `_MEIPASS` — `plugins/docs/__init__.py`'s grandparent is the
repo root (dev) or `_MEIPASS` (frozen) either way.
"""

from __future__ import annotations

import json
from pathlib import Path

# Indexed/served sections. docs/dev (internal) + .vitepress (build) are intentionally out.
SECTIONS: tuple[str, ...] = ("tutorials", "guides", "reference", "explanation", "adr")

_SECTION_LABELS = {
    "tutorials": "Tutorials",
    "guides": "Guides",
    "reference": "Reference",
    "explanation": "Explanation",
    "adr": "Architecture Decisions",
}


# Operator-chosen docs root (config `docs.root`, set once at plugin register).
# None = the bundled protoAgent docs, byte-for-byte today's behavior. When set,
# the corpus switches to CUSTOM mode: every .md under the root (any layout, not
# the Diátaxis SECTIONS), grouped by top-level directory, nav.json ignored.
_CUSTOM_ROOT: Path | None = None


def set_docs_root(path: Path | None) -> None:
    """Point the whole corpus (tools, index, view) at an operator's md tree."""
    global _CUSTOM_ROOT
    _CUSTOM_ROOT = path


def is_custom_root() -> bool:
    return _CUSTOM_ROOT is not None


def docs_root() -> Path:
    """The active docs root — the operator's configured tree when set, else the
    bundled `docs/` (repo root in dev/Docker, the bundle root when frozen)."""
    return _CUSTOM_ROOT or Path(__file__).resolve().parent.parent.parent / "docs"


def _iter_custom(root: Path):
    """Every markdown file under an operator-chosen root: any directory layout,
    hidden dirs (.git, .vitepress, …) excluded, and — because this tree is
    arbitrary, unlike the committed bundled corpus — a symlink that resolves
    OUTSIDE the root is not a doc (it would smuggle unrelated files into the
    readable set)."""
    resolved_root = root.resolve()
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            if not p.resolve().is_relative_to(resolved_root):
                continue
        except OSError:
            continue
        yield rel.as_posix(), p


def iter_docs(root: Path | None = None):
    """Yield ``(rel_path, abs_path)`` for every indexed markdown file. ``rel_path`` is a
    posix path rooted at the docs root (e.g. ``guides/skills.md``) — it's the public
    handle the tools + view use, and the membership set is the read-access gate."""
    root = root or docs_root()
    if is_custom_root() and root == _CUSTOM_ROOT:
        yield from _iter_custom(root)
        return
    for section in SECTIONS:
        base = root / section
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            yield p.relative_to(root).as_posix(), p


def valid_paths(root: Path | None = None) -> set[str]:
    """The set of readable doc rel-paths — exact membership is the security gate."""
    return {rel for rel, _ in iter_docs(root)}


def read_doc(rel_path: str, root: Path | None = None) -> str | None:
    """Read a doc by its rel-path, validated to be a real indexed doc. Returns ``None``
    for anything outside the corpus (rejects traversal / absolute / unknown paths)."""
    root = root or docs_root()
    rel = (rel_path or "").strip().lstrip("/")
    if rel not in valid_paths(root):
        return None
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError:
        return None


def doc_title(abs_path: Path) -> str:
    """The doc's first markdown H1, else its filename stem."""
    try:
        for line in abs_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("# "):
                return s[2:].strip()
    except OSError:
        pass
    return abs_path.stem


def doc_tree(root: Path | None = None) -> list[dict]:
    """Ordered sections → items (`{path, title}`) for the reader's nav.

    Bundled corpus: section order follows `SECTIONS` (the Diátaxis arc, then
    ADRs). Custom root: sections are the DISCOVERED top-level directories
    (sorted), with root-level files first under a plain "Docs" section — an
    arbitrary md tree has no Diátaxis to assume. Items sorted by title."""
    root = root or docs_root()
    custom = is_custom_root() and root == _CUSTOM_ROOT
    by_section: dict[str, list[dict]] = {} if custom else {s: [] for s in SECTIONS}
    for rel, abs_path in iter_docs(root):
        section = rel.split("/", 1)[0] if "/" in rel else ("" if custom else rel.split("/", 1)[0])
        if custom:
            by_section.setdefault(section, []).append({"path": rel, "title": doc_title(abs_path)})
        elif section in by_section:
            by_section[section].append({"path": rel, "title": doc_title(abs_path)})
    order = ((["" ] if "" in by_section else []) + sorted(k for k in by_section if k)) if custom else list(SECTIONS)
    out: list[dict] = []
    for section in order:
        items = sorted(by_section.get(section, []), key=lambda x: x["title"].lower())
        if items:
            label = ("Docs" if section == "" else section.replace("-", " ").replace("_", " ").title()) if custom else _SECTION_LABELS.get(section, section.title())
            out.append({"id": section or "root", "label": label, "items": items})
    return out


def grouped_tree(root: Path | None = None) -> list[dict]:
    """Sections → **domain groups** → items, mirroring the published site sidebar
    (`plugins/docs/nav.json`, generated by `scripts/gen_docs_nav.py`). Items are validated
    against the live corpus (a stale nav entry is dropped). Falls back to a flat section
    tree (one unlabeled group per section) if nav.json is missing/unreadable."""
    root = root or docs_root()
    if is_custom_root() and root == _CUSTOM_ROOT:
        # An operator's tree has no VitePress sidebar to mirror — grouping IS
        # the directory structure (one unlabeled group per discovered section).
        return [
            {"id": s["id"], "label": s["label"], "groups": [{"label": "", "items": s["items"]}]}
            for s in doc_tree(root)
        ]
    valid = valid_paths(root)
    try:
        nav = json.loads((Path(__file__).resolve().parent / "nav.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        nav = {}

    out: list[dict] = []
    flat = {s["id"]: s["items"] for s in doc_tree(root)}
    for section in SECTIONS:
        groups: list[dict] = []
        for g in nav.get(section, []) or []:
            items = [it for it in g.get("items", []) if it.get("path") in valid]
            if items:
                groups.append({"label": g.get("label", ""), "items": items})
        if not groups and flat.get(section):  # no nav (or all stale) → flat fallback
            groups = [{"label": "", "items": flat[section]}]
        if groups:
            out.append({"id": section, "label": _SECTION_LABELS.get(section, section.title()), "groups": groups})
    return out


def doc_preview(content: str, limit: int = 240) -> str:
    """A short plain-text lede: the first non-heading, non-frontmatter line, trimmed."""
    in_fm = False
    for raw in content.splitlines():
        line = raw.strip()
        if line == "---":  # YAML frontmatter fence
            in_fm = not in_fm
            continue
        if in_fm or not line or line.startswith("#") or line.startswith(">"):
            continue
        return line[:limit]
    return ""
