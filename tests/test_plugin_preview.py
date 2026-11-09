"""`protoagent plugin preview` — the host-free view harness (#4809, ADR 0034/0039).

Covers the host-free CORE of ``graph.plugins.preview`` (the browser/Node parts are exercised
by apps/web/e2e/plugin-preview-harness.spec.ts): the manifest→descriptor derivation, mounting
the fixture plugin's REAL routers on a FastAPI app, the layout seed, and the
console-bundle-missing guard. The fixture under apps/web/e2e/fixtures/preview-fixture is the
SAME one the e2e drives, so the two stay in lockstep (the descriptor-parity test enforces it).
"""

from __future__ import annotations

import json

import pytest

from graph.plugins import preview
from graph.plugins.manifest import load_manifest

FIXTURE = preview.app_root() / "apps" / "web" / "e2e" / "fixtures" / "preview-fixture"


def test_build_preview_descriptor_matches_the_committed_e2e_json():
    """The descriptor the CLI feeds the mock (derived from the manifest) must equal the
    preview.json the e2e reads off disk — otherwise the two harness entry points discover
    different views. This is the parity contract that lets one fixture serve both."""
    manifest = load_manifest(FIXTURE)
    assert manifest is not None
    descriptor = preview.build_preview_descriptor(manifest)

    committed = json.loads((FIXTURE / "preview.json").read_text())
    assert descriptor == committed
    # And it is the shape the console expects: two views, one of them right-docked.
    assert descriptor["id"] == "preview-fixture"
    assert [v["id"] for v in descriptor["views"]] == ["main", "side"]
    assert descriptor["views"][1]["placement"] == "right"


def test_build_preview_app_mounts_the_real_routers_host_free():
    """The plugin's REAL routers answer behind the console, with no protoAgent host — the
    gated data route AND the public view page, each at the prefix register_router asked for."""
    from fastapi.testclient import TestClient

    built = preview.build_preview_app(FIXTURE)
    assert built.descriptor["id"] == "preview-fixture"
    # register() wired two routers (the two-router pattern: public page + gated data).
    assert len(built.registry.routers) == 2

    client = TestClient(built.app)
    ping = client.get("/api/plugins/preview-fixture/ping")
    assert ping.status_code == 200
    assert ping.json() == {"ok": True, "plugin": "preview-fixture"}

    view = client.get("/plugins/preview-fixture/view")
    assert view.status_code == 200
    assert view.headers["content-type"].startswith("text/html")
    # The REAL fixture page — the one the e2e mounts (background subscribe + publish button).
    assert "protoagent:subscribe" in view.text
    assert "preview-fixture" in view.text


@pytest.mark.parametrize(
    ("entrypoint", "files", "expected"),
    [
        (None, ["__init__.py"], "__init__.py"),
        (None, ["plugin.py"], "plugin.py"),
        (None, ["__init__.py", "plugin.py"], "__init__.py"),
        ("custom.py", ["__init__.py", "plugin.py", "custom.py"], "custom.py"),
        ("nested/entry.py", ["nested/entry.py"], "nested/entry.py"),
    ],
)
def test_preview_loads_runtime_entrypoint_and_relative_imports(tmp_path, entrypoint, files, expected):
    from fastapi.testclient import TestClient

    manifest = "id: preview-entry\nname: Preview Entry\n"
    if entrypoint:
        manifest += f"entrypoint: {entrypoint}\n"
    (tmp_path / "protoagent.plugin.yaml").write_text(manifest)
    (tmp_path / "helper.py").write_text("VALUE = 'relative import works'\n")
    for name in files:
        entry = tmp_path / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            "from fastapi import APIRouter\n"
            "from .helper import VALUE\n"
            "def register(registry):\n"
            "    router = APIRouter()\n"
            "    @router.get('/entry')\n"
            "    def selected():\n"
            f"        return {{'entry': {name!r}, 'value': VALUE}}\n"
            "    registry.register_router(router, prefix='/api/plugins/preview-entry')\n"
        )

    built = preview.build_preview_app(tmp_path)
    with TestClient(built.app) as client:
        response = client.get("/api/plugins/preview-entry/entry")
    assert response.status_code == 200
    assert response.json() == {"entry": expected, "value": "relative import works"}


@pytest.mark.parametrize("entrypoint", [None, "missing.py"])
def test_preview_reports_missing_entry_without_falling_back(tmp_path, entrypoint):
    manifest = "id: preview-missing\nname: Preview Missing\n"
    if entrypoint:
        manifest += f"entrypoint: {entrypoint}\n"
        (tmp_path / "__init__.py").write_text("raise AssertionError('must not fall back')\n")
        (tmp_path / "plugin.py").write_text("raise AssertionError('must not fall back')\n")
    (tmp_path / "protoagent.plugin.yaml").write_text(manifest)

    with pytest.raises(RuntimeError, match="no entry module"):
        preview.build_preview_app(tmp_path)


def test_layout_seed_defaults_to_first_rail_view_on_the_stage():
    """No layout → the first rail view mounts as the surface and the first right view docks,
    so a bare `preview` still shows something. railOrder keeps chat + the work hub present."""
    manifest = load_manifest(FIXTURE)
    seed = preview.layout_seed_payload(preview.build_preview_descriptor(manifest), None)

    assert seed["version"] == preview._UI_STORE_VERSION
    assert seed["state"]["surface"] == "plugin:preview-fixture:main"
    assert seed["state"]["rightPanel"] == "plugin:preview-fixture:side"
    assert seed["state"]["railOrder"]["left"] == ["chat", "plugin:preview-fixture:main"]
    assert seed["state"]["railOrder"]["right"] == ["work", "plugin:preview-fixture:side"]


def test_layout_seed_honours_an_explicit_view_to_dock_map():
    """The caller can mount any view in any dock — the cadview freeze shape is one view on the
    stage and ANOTHER in the right panel at once. Unknown view ids fall back (never a dead key)."""
    desc = preview.build_preview_descriptor(load_manifest(FIXTURE))
    seed = preview.layout_seed_payload(desc, {"surface": "side", "right": "main"})
    assert seed["state"]["surface"] == "plugin:preview-fixture:side"
    assert seed["state"]["rightPanel"] == "plugin:preview-fixture:main"

    fallback = preview.layout_seed_payload(desc, {"surface": "does-not-exist"})
    # A bad id resolves to None → the default first-rail view, not a broken surface key.
    assert fallback["state"]["surface"] == "plugin:preview-fixture:main"


def test_write_layout_seed_file_targets_the_slug_suffixed_storage_key(tmp_path):
    """The seed file carries the exact localStorage {key, value} the mock injects: the layout
    lives under `protoagent.ui:<slug>` (uiStore.ts per-agent key), value a JSON-encoded blob."""
    desc = preview.build_preview_descriptor(load_manifest(FIXTURE))
    out = tmp_path / "layout.json"
    written = preview.write_layout_seed_file(out, desc, None, "preview-fixture")

    assert written["key"] == "protoagent.ui:preview-fixture"
    assert json.loads(written["value"]) == preview.layout_seed_payload(desc, None)
    assert json.loads(out.read_text()) == written
    # The host console (no slug) uses the un-suffixed key.
    assert preview._storage_key("") == "protoagent.ui"


def test_locate_console_bundle_names_the_build_command_when_absent(tmp_path, monkeypatch):
    """The bundle is never built silently (a multi-minute Node step) — a missing dist raises
    with the exact `npm run build` command to run."""
    monkeypatch.setattr(preview, "console_dist_dir", lambda: tmp_path / "dist")
    with pytest.raises(preview.ConsoleBundleMissing) as exc:
        preview.locate_console_bundle()
    assert "npm run build" in str(exc.value)
    assert str(tmp_path / "dist") in str(exc.value)
