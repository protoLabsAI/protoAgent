from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.web import mount_ds_plugin_kit, mount_react_app


def test_mount_react_app_serves_index_assets_and_fallback(tmp_path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    (dist / "protolabs-icon-outline.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

    app = FastAPI()
    assert mount_react_app(app, dist)
    client = TestClient(app)

    assert client.get("/app").text == "<div id='root'></div>"
    assert client.get("/app/runtime").text == "<div id='root'></div>"
    assert client.get("/app/protolabs-icon-outline.svg").text == "<svg></svg>"
    assert client.get("/app/assets/app.js").text == "console.log('ok')"


def test_mount_react_app_noops_when_dist_is_missing(tmp_path) -> None:
    app = FastAPI()

    assert mount_react_app(app, tmp_path / "missing") is False
    assert TestClient(app).get("/app").status_code == 404


def test_mount_ds_plugin_kit_serves_css_js_independent_of_the_spa(tmp_path) -> None:
    # No index.html / SPA — proves the kit is served independently of the console,
    # which is exactly the `--ui none` fleet-member case: a member serves plugin
    # view pages that need the kit but never mounts the SPA (version-coherence Axis 3).
    dist = tmp_path / "dist"
    (dist / "_ds").mkdir(parents=True)
    (dist / "_ds" / "plugin-kit.css").write_text(":root{--pl-color-bg:#0a0a0c}", encoding="utf-8")
    (dist / "_ds" / "plugin-kit.js").write_text("export const x=1", encoding="utf-8")

    app = FastAPI()
    assert mount_ds_plugin_kit(app, dist)
    client = TestClient(app)

    css = client.get("/_ds/plugin-kit.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"] and "--pl-color-bg" in css.text
    js = client.get("/_ds/plugin-kit.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/app").status_code == 404  # the kit function never mounts the console SPA


def test_mount_ds_plugin_kit_noops_without_the_build_artifact(tmp_path) -> None:
    app = FastAPI()
    assert mount_ds_plugin_kit(app, tmp_path / "missing") is False
    assert TestClient(app).get("/_ds/plugin-kit.css").status_code == 404


def test_mount_react_app_no_longer_serves_the_kit(tmp_path) -> None:
    # The kit moved OUT of mount_react_app (it's now tier-independent — see
    # mount_ds_plugin_kit). mount_react_app mounts only the SPA; this guards against
    # silently re-coupling them (which would re-break members).
    dist = tmp_path / "dist"
    (dist / "_ds").mkdir(parents=True)
    (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    (dist / "_ds" / "plugin-kit.css").write_text(":root{}", encoding="utf-8")

    app = FastAPI()
    assert mount_react_app(app, dist)
    client = TestClient(app)
    assert client.get("/app").status_code == 200
    assert client.get("/_ds/plugin-kit.css").status_code == 404  # NOT served by the SPA mount
