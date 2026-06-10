from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.web import mount_react_app


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


def test_serves_plugin_kit_same_origin_when_present(tmp_path) -> None:
    dist = tmp_path / "dist"
    (dist / "_ds").mkdir(parents=True)
    (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    (dist / "_ds" / "plugin-kit.css").write_text(":root{--pl-color-bg:#0a0a0c}", encoding="utf-8")

    app = FastAPI()
    assert mount_react_app(app, dist)
    res = TestClient(app).get("/_ds/plugin-kit.css")

    assert res.status_code == 200
    assert "text/css" in res.headers["content-type"]
    assert "--pl-color-bg" in res.text


def test_plugin_kit_route_absent_without_the_build_artifact(tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")

    app = FastAPI()
    assert mount_react_app(app, dist)
    # No dist/_ds/plugin-kit.css → the root route is never registered.
    assert TestClient(app).get("/_ds/plugin-kit.css").status_code == 404
