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


def test_concurrent_search_is_thread_safe(tmp_path) -> None:
    """`docs_search` runs `search` on `asyncio.to_thread` workers, so two back-to-back
    searches hit the one shared in-memory sqlite connection from different threads. That
    race used to corrupt cursor state → a NULL bm25 score → `float(None)` TypeError (and
    InterfaceError/IndexError). Hammer it: every search must return cleanly, never raise."""
    import threading

    docs = _load_docs()
    _corpus(tmp_path)
    idx = docs.DocsIndex(root=tmp_path)
    idx.seed()

    errors: list[str] = []

    def worker(q: str) -> None:
        for _ in range(200):
            try:
                idx.search(q)
            except Exception as exc:  # noqa: BLE001 — a raised search is the regression
                errors.append(f"{type(exc).__name__}: {exc}")

    queries = ["skills tools", "configuration schema", "langgraph option", "agent"]
    threads = [threading.Thread(target=worker, args=(queries[i % len(queries)],)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent search raised: {errors[:3]}"
    # Still returns real, correctly-typed results after the pounding.
    hits = idx.search("skills tools")
    assert hits and hits[0].path == "guides/skills.md" and isinstance(hits[0].score, float)


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


def test_render_handles_tables() -> None:
    docs = _load_docs()
    html = docs.render_markdown("# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1>" in html and "<table>" in html and "<td>1</td>" in html


def test_data_routes(tmp_path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    docs = _load_docs()
    app = FastAPI()
    app.include_router(docs._build_data_router(), prefix="/api/plugins/docs")
    c = TestClient(app)

    tree = c.get("/api/plugins/docs/tree").json()["sections"]
    assert {s["id"] for s in tree} >= {"guides", "reference"}  # real corpus
    # Domain-grouped (mirrors the site sidebar): guides has the Skills domain group.
    guides = next(s for s in tree if s["id"] == "guides")
    assert any(g["label"] == "Skills, subagents & workflows" for g in guides["groups"])
    assert any(it["path"] == "guides/skills.md" for g in guides["groups"] for it in g["items"])

    res = c.get("/api/plugins/docs/search", params={"q": "skills"}).json()["results"]
    assert any(r["path"] == "guides/skills.md" for r in res)

    doc = c.get("/api/plugins/docs/doc", params={"path": "guides/skills.md"}).json()
    assert doc["path"] == "guides/skills.md" and "<h1>" in doc["html"]

    # Out-of-corpus path → 404 (the read gate).
    assert c.get("/api/plugins/docs/doc", params={"path": "../../etc/passwd"}).status_code == 404
    assert c.get("/api/plugins/docs/doc", params={"path": "dev/secret.md"}).status_code == 404


def test_nav_json_in_sync_with_sidebar() -> None:
    """The committed nav.json must match what the generator produces from the live sidebar
    (run `python scripts/gen_docs_nav.py` after editing docs/.vitepress/config.mts)."""
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("gen_docs_nav_under_test", Path("scripts/gen_docs_nav.py"))
    gen = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(gen)
    committed = json.loads(Path("plugins/docs/nav.json").read_text(encoding="utf-8"))
    assert gen.build_nav() == committed, "nav.json is stale — run `python scripts/gen_docs_nav.py`"


def test_grouped_tree_falls_back_to_sections(tmp_path) -> None:
    """With docs whose paths aren't in nav.json, the tree degrades to flat (unlabeled)
    section groups rather than going empty."""
    _load_docs()
    corpus = sys.modules["docs_plugin_under_test.corpus"]
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides" / "zzz-not-in-nav.md").write_text("# Local only\n\nbody\n", encoding="utf-8")
    tree = corpus.grouped_tree(root=tmp_path)
    guides = next(s for s in tree if s["id"] == "guides")
    assert guides["groups"][0]["label"] == ""  # fallback group has no domain label
    assert guides["groups"][0]["items"][0]["path"] == "guides/zzz-not-in-nav.md"


def test_view_is_four_rules_compliant() -> None:
    html = _load_docs()._VIEW_HTML
    assert "/_ds/plugin-kit.css" in html and "/_ds/plugin-kit.js" in html  # rule 4
    assert 'location.pathname.split("/plugins/")[0]' in html  # rule 3 (slug-aware base)
    assert 'apiFetch("/api/plugins/docs' in html  # rules 2+3 (gated data via authed fetch)
    assert "https://" not in html.split("<style>")[0]  # no CDN in the head
