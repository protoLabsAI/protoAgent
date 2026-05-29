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
