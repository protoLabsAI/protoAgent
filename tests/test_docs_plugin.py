"""Docs plugin — the FTS index, corpus path-validation, and the search/read tools."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_docs():
    """Load the multi-file docs plugin as a package (so its relative imports resolve) —
    mirrors graph.plugins.loader._load_plugin_module."""
    name = "docs_plugin_under_test"
    for n in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        sys.modules.pop(n, None)
    spec = importlib.util.spec_from_file_location(
        name,
        (ROOT / "plugins/docs/__init__.py"),
        submodule_search_locations=[str(ROOT / "plugins/docs")],
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
    assert [s["id"] for s in tree][:6] == ["start", "use", "automate", "build", "operate", "reference"]
    automate = next(s for s in tree if s["id"] == "automate")
    paths = {it["path"] for g in automate["groups"] for it in g["items"]}
    assert {"guides/skills.md", "tutorials/first-skill.md", "explanation/rooms.md"} <= paths

    res = c.get("/api/plugins/docs/search", params={"q": "skills"}).json()["results"]
    assert any(r["path"] == "guides/skills.md" for r in res)
    task = c.get("/api/plugins/docs/search", params={"q": "install a plugin"}).json()["results"]
    assert task[0]["path"] == "guides/plugin-registry.md"
    assert c.get("/api/plugins/docs/doc", params={"path": "guides/plugin-views.md"}).status_code == 200

    doc = c.get("/api/plugins/docs/doc", params={"path": "guides/skills.md"}).json()
    assert doc["path"] == "guides/skills.md" and "<h1>" in doc["html"]
    # The reader only ever renders `html`, so the raw markdown is OPT-IN (`raw=1`) — the
    # view asks for it just for its "Copy as markdown" context action (#3030).
    assert "md" not in doc
    raw = c.get("/api/plugins/docs/doc", params={"path": "guides/skills.md", "raw": 1}).json()
    assert raw["md"].lstrip().startswith("#") and "<h1>" in raw["html"]

    # Out-of-corpus path → 404 (the read gate).
    assert c.get("/api/plugins/docs/doc", params={"path": "../../etc/passwd"}).status_code == 404
    assert c.get("/api/plugins/docs/doc", params={"path": "dev/secret.md"}).status_code == 404


def test_generated_navigation_and_landings_are_in_sync() -> None:
    """One catalog generates the website, in-app reader, and task listings."""
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("gen_docs_nav_under_test", (ROOT / "scripts/gen_docs_nav.py"))
    gen = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(gen)
    committed = json.loads((ROOT / "plugins/docs/nav.json").read_text(encoding="utf-8"))
    assert gen.route("index.md") == "/"
    assert gen.route("guides/index.md") == "/guides/"
    assert gen.route("guides/reindex.md") == "/guides/reindex"
    assert gen.build_nav() == committed
    for path, expected in gen.build_outputs().items():
        assert path.read_text(encoding="utf-8") == expected, f"{path} is stale — run scripts/gen_docs_nav.py"


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


def test_view_error_messages_name_the_cause() -> None:
    """A fetch that never reached the server must not be reported as an unreadable doc.

    `api()` rejects two ways — an HTTP error (the request reached the server and it refused)
    and a network-layer rejection with no status (the agent stopped, or the tab outlived it).
    Collapsing them sent the reader looking at the corpus when the problem was the process.
    """
    html = _load_docs()._VIEW_HTML
    # One place decides which kind of failure it is — the predicate is not re-derived at
    # each call site, where a future change to it would be missed.
    assert html.count("/^HTTP /.test(") == 1
    # Both branches are reachable: the no-status case names the agent...
    assert "the agent is not reachable (is it still running?)" in html
    # ...and an HTTP failure still names the doc AND its status.
    assert '"Could not read "+path+" — "+causeOf(err)' in html
    # The message that blamed the doc for an unreachable agent is gone.
    assert 'toast("Could not read "+path)' not in html
    # The reader names the doc it failed to open — but only for an HTTP failure. An
    # unreachable agent is never reported as a named doc going bad, and the tree/search
    # reads pass no path, because "docs" is genuinely what failed there.
    assert 'showErr($reader,e,path)' in html
    assert 'path&&isHttpErr(e)?esc(path):"docs"' in html
    assert "showErr($list,e)" in html  # tree/search stay generic


# ── operator-chosen docs root (`docs.root`) ───────────────────────────────────


def _custom_tree(root: Path) -> None:
    """An arbitrary md tree: root-level file, nested dirs, a hidden dir, and a
    symlink escaping the root."""
    (root / "README.md").write_text("# Welcome\n\nintro\n", encoding="utf-8")
    (root / "runbooks").mkdir()
    (root / "runbooks" / "deploy.md").write_text("# Deploying\n\nsteps\n", encoding="utf-8")
    (root / "team-notes").mkdir()
    (root / "team-notes" / "onboarding.md").write_text("# Onboarding\n\nhello\n", encoding="utf-8")
    (root / ".vitepress").mkdir()
    (root / ".vitepress" / "hidden.md").write_text("# Hidden\n", encoding="utf-8")
    outside = root.parent / "outside.md"
    outside.write_text("# Outside\n\nsecret\n", encoding="utf-8")
    (root / "runbooks" / "escape.md").symlink_to(outside)


@pytest.mark.platform_sensitive
def test_custom_root_serves_any_md_tree(tmp_path) -> None:
    mod = _load_docs()
    corpus = sys.modules["docs_plugin_under_test.corpus"]
    docs_dir = tmp_path / "mydocs"
    docs_dir.mkdir()
    _custom_tree(docs_dir)
    corpus.set_docs_root(docs_dir)
    try:
        paths = corpus.valid_paths()
        assert paths == {"README.md", "runbooks/deploy.md", "team-notes/onboarding.md"}
        # hidden dirs and symlink escapes are NOT docs
        assert ".vitepress/hidden.md" not in paths and "runbooks/escape.md" not in paths
        # reads work by rel path; traversal and absolute stay rejected
        assert "steps" in corpus.read_doc("runbooks/deploy.md")
        assert corpus.read_doc("../outside.md") is None
        assert corpus.read_doc(str(docs_dir.parent / "outside.md")) is None
        # grouping IS the directory structure: root files first, dirs title-cased
        tree = corpus.grouped_tree()
        assert [s["label"] for s in tree] == ["Docs", "Runbooks", "Team Notes"]
        assert all(len(s["groups"]) == 1 and s["groups"][0]["label"] == "" for s in tree)
        # the index seeds over the custom corpus (tools + /search follow)
        idx = mod.DocsIndex()
        assert idx.seed() == 3
        assert idx.has("runbooks/deploy.md")
    finally:
        corpus.set_docs_root(None)


def test_custom_root_reset_restores_bundled_behavior(tmp_path) -> None:
    _load_docs()
    corpus = sys.modules["docs_plugin_under_test.corpus"]
    docs_dir = tmp_path / "mydocs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n", encoding="utf-8")
    corpus.set_docs_root(docs_dir)
    corpus.set_docs_root(None)
    # back on the bundled corpus: SECTIONS scan, real repo docs present
    assert corpus.is_custom_root() is False
    assert any(rel.startswith("guides/") for rel in corpus.valid_paths())


def test_register_falls_back_loudly_on_bad_root(tmp_path, caplog) -> None:
    mod = _load_docs()
    corpus = sys.modules["docs_plugin_under_test.corpus"]

    class _Reg:
        config = {"root": str(tmp_path / "does-not-exist")}

    try:
        with caplog.at_level("WARNING"):
            mod._resolve_custom_root(_Reg())
        assert "is not a directory" in caplog.text
        assert corpus.is_custom_root() is False
    finally:
        corpus.set_docs_root(None)


@pytest.mark.parametrize(("query", "expected"), [
    ("install a plugin", "guides/plugin-registry.md"),
    ("create a document", "guides/documents-and-files.md"),
    ("connect a model", "guides/model-connections.md"),
    ("schedule a task", "guides/scheduler.md"),
    ("backup restore", "guides/backup-and-restore.md"),
    ("deploy docker", "guides/deploy-docker.md"),
    ("build a plugin view", "guides/build-a-plugin-view.md"),
    ("access from phone", "guides/phone-access.md"),
])
def test_task_search_prefers_current_instructions(query, expected) -> None:
    """Use the shipped corpus: short aliases and ADR prose must not beat task guides."""
    index = _load_docs().DocsIndex()
    index.seed()
    assert index.search(query)[0].path == expected


def test_search_preserves_reference_and_decision_lookups() -> None:
    index = _load_docs().DocsIndex()
    index.seed()
    assert "reference/plugin-registry-api.md" in [
        row.path for row in index.search("PluginRegistry register_tool", 3)
    ]
    assert index.search("ADR 0065")[0].path == "adr/0065-two-tier-instance-paths.md"
    assert index.search("egress security")[0].path == "explanation/security-and-trust.md"
    assert any(row.path == "guides/acp-runtime.md" for row in index.search("deprecated ACP runtime", 5))


def test_alias_is_readable_but_search_returns_canonical_pages() -> None:
    docs = _load_docs()
    index = docs.DocsIndex()
    index.seed()
    alias = "guides/plugin-views.md"
    assert index.has(alias)
    assert docs.read_doc(alias)
    hits = [row.path for row in index.search("plugin views", 200)]
    assert alias not in hits
    assert "guides/building-react-plugin-views.md" in hits
    # This legacy website URL was never in the reader's bundled corpus. Search
    # classification must not silently broaden the read-access gate.
    import json
    catalog = json.loads((ROOT / "plugins/docs/catalog.json").read_text(encoding="utf-8"))
    website_alias = "how-to/build-a-plugin-view.md"
    assert catalog["website_pages"][website_alias]["canonical"] == "guides/build-a-plugin-view.md"
    assert not index.has(website_alias)
    assert docs.read_doc(website_alias) is None


def test_custom_search_does_not_apply_bundled_status(tmp_path) -> None:
    docs = _load_docs()
    corpus = sys.modules["docs_plugin_under_test.corpus"]
    (tmp_path / "guides").mkdir()
    alias = tmp_path / "guides" / "plugin-views.md"
    alias.write_text("# Team dashboards\n\nQuokka widgets\n", encoding="utf-8")
    corpus.set_docs_root(tmp_path)
    try:
        index = docs.DocsIndex()
        index.seed()
        hits = index.search("quokka")
        assert hits[0].path == "guides/plugin-views.md"
        assert hits[0].title == "Team dashboards"
    finally:
        corpus.set_docs_root(None)



def test_search_preview_skips_generator_comments_and_legacy_anchors() -> None:
    docs = _load_docs()
    corpus = sys.modules["docs_plugin_under_test.corpus"]
    text = '<!-- Generated file\nDo not edit. -->\n# A task\n<span id="old"></span>\n\nUseful instructions.'
    assert corpus.doc_preview(text) == "Useful instructions."
    index = docs.DocsIndex()
    index.seed()
    hub = next(row for row in index.search("Use protoAgent", 200) if row.path == "guides/use-protoagent.md")
    assert hub.preview.startswith("Chat, work with files")



def test_catalog_rejects_dangling_canonical_on_current_page(tmp_path, monkeypatch) -> None:
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("nav_validation", (ROOT / "scripts/gen_docs_nav.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    catalog = json.loads(gen.CATALOG.read_text(encoding="utf-8"))
    catalog["pages"]["guides/skills.md"]["canonical"] = "guides/missing.md"
    invalid = tmp_path / "catalog.json"
    invalid.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(gen, "CATALOG", invalid)
    with pytest.raises(ValueError, match="Unknown canonical destination"):
        gen.load_catalog()
