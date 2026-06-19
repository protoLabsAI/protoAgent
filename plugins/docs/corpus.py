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


def docs_root() -> Path:
    """The `docs/` directory — repo root in dev/Docker, the bundle root when frozen."""
    return Path(__file__).resolve().parent.parent.parent / "docs"


def iter_docs(root: Path | None = None):
    """Yield ``(rel_path, abs_path)`` for every indexed markdown file. ``rel_path`` is a
    posix path rooted at `docs/` (e.g. ``guides/skills.md``) — it's the public handle the
    tools + view use, and the membership set is the read-access gate."""
    root = root or docs_root()
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
    """Ordered sections → items (`{path, title}`) for the reader's nav. Section order
    follows `SECTIONS` (the Diátaxis arc, then ADRs); items sorted by title."""
    root = root or docs_root()
    by_section: dict[str, list[dict]] = {s: [] for s in SECTIONS}
    for rel, abs_path in iter_docs(root):
        section = rel.split("/", 1)[0]
        if section in by_section:
            by_section[section].append({"path": rel, "title": doc_title(abs_path)})
    out: list[dict] = []
    for section in SECTIONS:
        items = sorted(by_section[section], key=lambda x: x["title"].lower())
        if items:
            out.append({"id": section, "label": _SECTION_LABELS.get(section, section.title()), "items": items})
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
