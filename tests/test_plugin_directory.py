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

def test_app_catalog_schema() -> None:
    """The Discover UI + /api/plugins/catalog expect exactly these entry keys."""
    doc = json.loads(pd.render_app(pd.load()))
    assert set(doc) == {"_comment", "plugins"}
    for p in doc["plugins"]:
        assert set(p) == {"id", "name", "category", "official", "repo", "tagline", "adds", "docs"}, p["id"]
        # The console runs on the operator's own host: a root-relative link would 404 there.
        assert p["docs"].startswith("https://"), f"{p['id']}: docs link {p['docs']!r} is not absolute"


def test_discover_says_what_the_website_card_says() -> None:
    """#2910 / census F3: Discover used to drop the site card's contribution chips and
    docs link, so the same plugin read thinner in-app than on the website."""
    entries = pd.load()
    app = {p["id"]: p for p in json.loads(pd.render_app(entries))["plugins"]}
    site = {s["id"]: s for s in json.loads(pd.render_site(entries)) if not s.get("hidden")}
    shared = [e for e in entries if e["id"] in app and (e.get("site_id") or e["id"]) in site]
    assert shared, "no plugin is listed on both surfaces"
    for e in shared:
        a, s = app[e["id"]], site[e.get("site_id") or e["id"]]
        assert a["adds"] == s["adds"], e["id"]
        assert a["docs"] == pd._absolute(s["links"]["docs"]), e["id"]


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
        if e.get("hidden"):
            # An unlisted entry is ONLY a drop marker — nothing the page could render.
            assert set(e) == {"id", "status", "hidden"}, e["id"]
            assert e["status"] not in pd._SITE_STATUSES
            continue
        assert set(e) >= {"id", "name", "category", "official", "tagline", "adds", "bundled", "links"}
        if e["bundled"]:
            assert "install" not in e and e["links"]["source"].startswith(pd.TREE)
            # app:false rows (libraries / always-on builtins) must carry an explicit
            # null so the site's merge suppresses the auto-discovered enable CTA.
            src = next(d for d in pd.load() if (d.get("site_id") or d["id"]) == e["id"])
            if not src.get("app", True):
                assert e["enable"] is None, f"{e['id']}: app:false row must not ship an enable CTA"
            else:
                assert e["enable"], f"{e['id']}: app-visible bundled row must name its enable id"
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


def _census(tmp_path: Path) -> list[dict]:
    doc = {"plugins": [
        {"id": s, "name": s.title(), "category": "T", "tagline": "t", "status": s,
         "repo": f"https://github.com/protoLabsAI/{s}-plugin"}
        for s in ("active", "incubating", "personal", "archived", "deprecated", "internal")
    ]}
    f = tmp_path / "dir.yaml"
    f.write_text(json.dumps(doc), encoding="utf-8")  # JSON is valid YAML
    return pd.load(f)


def test_load_keeps_the_whole_census(tmp_path: Path) -> None:
    # The file is the full census (#2910): every status survives load(); which surface
    # lists what is the renderers' call.
    assert [e["id"] for e in _census(tmp_path)] == [
        "active", "incubating", "personal", "archived", "deprecated", "internal",
    ]


def test_discover_lists_active_plugins_only(tmp_path: Path) -> None:
    # One-click install from Discover is for finished plugins — not an incubating one.
    app = json.loads(pd.render_app(_census(tmp_path)))
    assert [p["id"] for p in app["plugins"]] == ["active"]


def test_the_marketing_page_badges_incubating_and_hides_the_rest(tmp_path: Path) -> None:
    site = {e["id"]: e for e in json.loads(pd.render_site(_census(tmp_path)))}
    assert "status" not in site["active"] and not site["active"].get("hidden")
    assert site["incubating"]["status"] == "incubating" and not site["incubating"].get("hidden")
    assert site["incubating"]["install"] == "https://github.com/protoLabsAI/incubating-plugin"
    # The page renders every `protoagent-plugin`-tagged repo it finds, so "listed
    # nowhere" needs a marker to drop that scraped card — a mere omission would keep it.
    for s in ("personal", "archived", "deprecated", "internal"):
        assert site[s] == {"id": s, "status": s, "hidden": True}


def test_a_hidden_marker_uses_the_site_key(tmp_path: Path) -> None:
    doc = {"plugins": [{"id": "palmier_pro", "site_id": "palmier-pro", "name": "P", "category": "T",
                        "tagline": "t", "status": "personal",
                        "repo": "https://github.com/protoLabsAI/palmier-pro-plugin"}]}
    f = tmp_path / "dir.yaml"
    f.write_text(json.dumps(doc), encoding="utf-8")
    assert json.loads(pd.render_site(pd.load(f))) == [{"id": "palmier-pro", "status": "personal", "hidden": True}]


def test_every_external_entry_keys_onto_its_scraped_card() -> None:
    """The page folds an overlay entry onto the card it scraped from the repo by
    `<repo-name minus -plugin>`. A key that doesn't match renders a DUPLICATE for a
    listed plugin (#1772), and — worse — a hidden marker that hides nothing, so an
    archived repo that keeps its topic stays on the page. Set `site_id` when the
    manifest id differs."""
    import re

    for e in pd.load():
        if not e.get("repo"):
            continue
        scraped = re.sub(r"-plugin$", "", e["repo"].rstrip("/").rsplit("/", 1)[-1]).lower()
        assert (e.get("site_id") or e["id"]).lower() == scraped, (
            f"{e['id']}: overlay key {(e.get('site_id') or e['id'])!r} != scraped card key {scraped!r} — set site_id"
        )


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
