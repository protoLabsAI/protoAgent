"""Tests for scripts/plugin_directory.py — the one-source plugin-directory pipeline.

config/plugin-directory.yaml is the hand-edited source; config/plugin-catalog.json
(the in-app Discover catalog) and sites/marketing/data/plugins.json (the marketing
overlay) are derived. The sync tests here are the CI drift guard — they fail any PR
that edits a derived file without regenerating (or edits it by hand), the same
contract scripts/roadmap.py check enforces for roadmap.json.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "plugin_directory", Path(__file__).parent.parent / "scripts" / "plugin_directory.py"
)
pd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pd)


# ── the real directory parses and is well-formed ────────────────────────────────────

def test_directory_loads_and_validates() -> None:
    entries = pd.load()
    assert entries, "plugin-directory.yaml has no active entries"
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))


def test_derived_app_catalog_is_in_sync() -> None:
    assert pd.APP_CATALOG.read_text(encoding="utf-8") == pd.render_app(pd.load()), (
        "config/plugin-catalog.json is stale — run `python scripts/plugin_directory.py build`"
    )


def test_derived_marketing_overlay_is_in_sync() -> None:
    assert pd.MARKETING_JSON.read_text(encoding="utf-8") == pd.render_site(pd.load()), (
        "sites/marketing/data/plugins.json is stale — run `python scripts/plugin_directory.py build`"
    )


# ── schema contracts the consumers rely on ──────────────────────────────────────────

def test_app_catalog_schema_is_unchanged() -> None:
    """The Discover UI + /api/plugins/catalog expect exactly these entry keys."""
    doc = json.loads(pd.render_app(pd.load()))
    assert set(doc) == {"_comment", "plugins"}
    for p in doc["plugins"]:
        assert set(p) == {"id", "name", "category", "official", "repo", "tagline"}, p["id"]


def test_bundled_entries_link_the_in_tree_plugin() -> None:
    """A bundled entry's repo/source must point INTO protoAgent, never at an external
    (possibly archived) repo — the drift that motivated this pipeline."""
    for e in pd.load():
        if e.get("bundled"):
            assert pd._source_url(e) == f"{pd.TREE}/{e['id']}"
            plugin_dir = Path(__file__).parent.parent / "plugins" / e["id"]
            assert plugin_dir.is_dir(), f"{e['id']}: bundled but plugins/{e['id']} does not exist"
            if e.get("app", True):
                # An app-visible bundled row is an enable instruction — the loader must
                # actually be able to see it. Library dirs (coding_agent) are app: false.
                manifest = plugin_dir / "protoagent.plugin.yaml"
                assert manifest.is_file(), (
                    f"{e['id']}: app-visible bundled row but plugins/{e['id']} has no "
                    "manifest — the loader can never enable it; mark the row app: false "
                    "or add the manifest"
                )


def test_site_overlay_shapes() -> None:
    out = json.loads(pd.render_site(pd.load()))
    for e in out:
        assert set(e) >= {"id", "name", "category", "official", "tagline", "adds", "bundled", "links"}
        if e["bundled"]:
            assert "install" not in e and e["links"]["source"].startswith(pd.TREE)
        else:
            assert e["install"].startswith("https://github.com/protoLabsAI/")


# ── behavior on fixture entries ─────────────────────────────────────────────────────

_FIXTURE = [
    {"id": "ext", "name": "Ext", "category": "Tools", "tagline": "t",
     "repo": "https://github.com/protoLabsAI/ext-plugin", "site_id": "ext2"},
    {"id": "built", "name": "Built", "category": "Tools", "tagline": "t",
     "bundled": True, "app": False},
]


def test_site_id_overrides_overlay_key_only() -> None:
    site = json.loads(pd.render_site(_FIXTURE))
    app = json.loads(pd.render_app(_FIXTURE))
    assert site[0]["id"] == "ext2"  # overlay folds on the scraped repo-derived id
    assert app["plugins"][0]["id"] == "ext"  # in-app keeps the manifest id


def test_app_false_entries_stay_out_of_the_app_catalog() -> None:
    app = json.loads(pd.render_app(_FIXTURE))
    assert [p["id"] for p in app["plugins"]] == ["ext"]
    site = json.loads(pd.render_site(_FIXTURE))
    assert [p["id"] for p in site] == ["ext2", "built"]


def test_non_active_status_is_emitted_nowhere(tmp_path: Path) -> None:
    doc = {"plugins": [
        {"id": "gone", "name": "G", "category": "T", "tagline": "t", "status": "deprecated",
         "repo": "https://github.com/protoLabsAI/gone-plugin"},
        {"id": "kept", "name": "K", "category": "T", "tagline": "t",
         "repo": "https://github.com/protoLabsAI/kept-plugin"},
    ]}
    f = tmp_path / "dir.yaml"
    f.write_text(json.dumps(doc), encoding="utf-8")  # JSON is valid YAML
    entries = pd.load(f)
    assert [e["id"] for e in entries] == ["kept"]


@pytest.mark.parametrize("bad", [
    {"id": "x", "name": "X", "category": "T", "tagline": "t"},  # neither repo nor bundled
    {"id": "x", "name": "X", "category": "T", "tagline": "t", "bundled": True,
     "repo": "https://github.com/protoLabsAI/x-plugin"},  # both
    {"id": "x", "name": "X", "category": "T", "tagline": "t", "repo": "r", "status": "wat"},
    {"id": "x", "category": "T", "tagline": "t", "repo": "r"},  # no name
])
def test_invalid_entries_are_rejected(bad: dict, tmp_path: Path) -> None:
    f = tmp_path / "dir.yaml"
    f.write_text(json.dumps({"plugins": [bad]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        pd.load(f)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    e = {"id": "x", "name": "X", "category": "T", "tagline": "t", "repo": "r"}
    f = tmp_path / "dir.yaml"
    f.write_text(json.dumps({"plugins": [e, dict(e)]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        pd.load(f)


# ── archetype-repo registry (census Fork B) ─────────────────────────────────────────


def _load_directory_doc() -> dict:
    import yaml

    return yaml.safe_load(pd.DIRECTORY.read_text(encoding="utf-8")) or {}


def test_archetype_repo_registry_shape() -> None:
    rows = _load_directory_doc().get("archetype_repos") or []
    assert rows, "archetype_repos registry missing from plugin-directory.yaml"
    seen: set[str] = set()
    for r in rows:
        assert r["id"] not in seen, f"duplicate archetype repo id {r['id']}"
        seen.add(r["id"])
        assert r["repo"].count("/") == 1, f"{r['id']}: repo must be org/name"
        assert r["repo"].endswith(r["id"]), f"{r['id']}: repo name must match the id"
        assert r.get("tagline"), f"{r['id']}: tagline required"


def test_archetype_catalog_bundles_are_registered() -> None:
    """Every catalog row that installs a bundle must name a registered archetype repo —
    the guard that would have caught docs/examples still citing product-stack after the
    catalog moved to project-manager-stack (census F3), and any future rename drift."""
    catalog = json.loads(
        (Path(__file__).parent.parent / "config" / "archetype-catalog.json").read_text(encoding="utf-8")
    )
    rows = _load_directory_doc().get("archetype_repos") or []
    registered_urls = {f"https://github.com/{r['repo']}".lower() for r in rows}
    by_archetype = {r["archetype"] for r in rows if r.get("archetype")}
    for entry in catalog.get("archetypes") or []:
        bundle = entry.get("bundle")
        if not bundle:
            continue
        url = bundle.rstrip("/").removesuffix(".git").lower()
        assert url in registered_urls, (
            f"archetype {entry['id']}: bundle {bundle} is not in the archetype_repos "
            "registry (plugin-directory.yaml) — register it (or fix the catalog URL)"
        )
        assert entry["id"] in by_archetype, (
            f"archetype {entry['id']}: no registry row claims this catalog id via `archetype:`"
        )
