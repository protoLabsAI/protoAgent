"""Docs plugin — the FTS index, corpus path-validation, and the search/read tools."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_docs():
    """Load the multi-file docs plugin as a package (so its relative imports resolve) —
    mirrors graph.plugins.loader._load_plugin_module."""
    name = "docs_plugin_under_test"
    for n in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        sys.modules.pop(n, None)
    spec = importlib.util.spec_from_file_location(
        name,
        Path("plugins/docs/__init__.py"),
        submodule_search_locations=["plugins/docs"],
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _corpus(root: Path) -> None:
    """Write a tiny doc tree: two indexed sections + an excluded dev/ dir."""
    (root / "guides").mkdir()
    (root / "guides" / "skills.md").write_text(
        "# Skills (SKILL.md)\n\nSkills teach the agent how and when to use its tools.\n",
        encoding="utf-8",
    )
    (root / "reference").mkdir()
    (root / "reference" / "configuration.md").write_text(
        "# Configuration\n\nThe langgraph-config.yaml schema and every option.\n",
        encoding="utf-8",
    )
    (root / "dev").mkdir()  # internal — must NOT be indexed
    (root / "dev" / "secret.md").write_text("# Secret\n\ninternal handoff\n", encoding="utf-8")


def test_index_seeds_and_ranks(tmp_path) -> None:
    docs = _load_docs()
    _corpus(tmp_path)
    idx = docs.DocsIndex(root=tmp_path)
    assert idx.seed() == 2  # only the two section docs; dev/ excluded

    hits = idx.search("skills tools")
    assert hits and hits[0].path == "guides/skills.md"
    assert hits[0].section == "guides"
    assert hits[0].title == "Skills (SKILL.md)"

    # Title/body match the right doc.
    cfg = idx.search("configuration schema")
    assert cfg and cfg[0].path == "reference/configuration.md"

    # No match → empty, never an error.
    assert idx.search("zzz_no_such_term_xyz") == []
    assert idx.search("") == []


def test_dev_dir_is_excluded(tmp_path) -> None:
    docs = _load_docs()
    _corpus(tmp_path)
    idx = docs.DocsIndex(root=tmp_path)
    idx.seed()
    assert idx.search("internal handoff secret") == []  # dev/ never indexed
    assert idx.has("dev/secret.md") is False


def test_read_doc_gated_to_corpus(tmp_path) -> None:
    _load_docs()  # loads the package so its .corpus submodule is importable below
    _corpus(tmp_path)
    corpus = sys.modules["docs_plugin_under_test.corpus"]

    # In-corpus read works.
    assert "Skills teach the agent" in corpus.read_doc("guides/skills.md", root=tmp_path)
    # Out-of-corpus is refused (traversal / absolute / unknown / excluded dev).
    assert corpus.read_doc("../secret.md", root=tmp_path) is None
    assert corpus.read_doc("/etc/passwd", root=tmp_path) is None
    assert corpus.read_doc("guides/missing.md", root=tmp_path) is None
    assert corpus.read_doc("dev/secret.md", root=tmp_path) is None


async def test_tools_over_the_real_docs() -> None:
    """End-to-end against the repo's real docs/ tree (present in the test env)."""
    docs = _load_docs()
    out = await docs.docs_search.ainvoke({"query": "how do skills work"})
    assert "guides/skills.md" in out

    body = await docs.docs_read.ainvoke({"path": "guides/skills.md"})
    assert "SKILL.md" in body

    refused = await docs.docs_read.ainvoke({"path": "../../etc/passwd"})
    assert "No such doc" in refused
