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
    for page in ("/app", "/app/runtime", "/app/index.html"):  # the page itself may only be framed by its own origin
        assert client.get(page).headers["content-security-policy"] == "frame-ancestors 'self'"
        assert client.get(page).headers["x-frame-options"] == "SAMEORIGIN"
    assert client.get("/app/protolabs-icon-outline.svg").text == "<svg></svg>"
    assert client.get("/app/assets/app.js").text == "console.log('ok')"


def _csp_client(tmp_path, monkeypatch, mode, *, policy=True, public_url=""):
    import json

    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<div id='root'></div>", encoding="utf-8")
    if policy:
        doc = {"directives": {"script-src": ["'self'", "'sha256-abc='"], "img-src": ["'self'"], "connect-src": ["'self'"], "frame-ancestors": ["'self'"]}}
        (dist / "csp.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("PROTOAGENT_CONSOLE_CSP", mode)
    monkeypatch.setenv("A2A_PUBLIC_URL", public_url)
    app = FastAPI()
    assert mount_react_app(app, dist)
    return TestClient(app)


def test_console_csp_is_reported_by_default_enforced_on_request_and_framing_always_holds(tmp_path, monkeypatch) -> None:
    h = _csp_client(tmp_path, monkeypatch, "").get("/app").headers
    assert h["content-security-policy"] == "frame-ancestors 'self'"  # framing: enforced regardless
    assert h["content-security-policy-report-only"] == (
        "script-src 'self' 'sha256-abc='; img-src 'self'; connect-src 'self'; frame-ancestors 'self'; report-uri /csp-report"
    )
    h = _csp_client(tmp_path / "e", monkeypatch, "enforce", public_url="https://agent.example/agents/ava").get("/app/x").headers
    assert h["content-security-policy"] == (
        "script-src 'self' 'sha256-abc='; img-src 'self' https://agent.example; connect-src 'self' https://agent.example; frame-ancestors 'self'"
    )
    assert "content-security-policy-report-only" not in h
    for mode, policy in (("off", True), ("enforce", False)):  # off, or a build with no policy: framing only
        h = _csp_client(tmp_path / f"{mode}{policy}", monkeypatch, mode, policy=policy).get("/app").headers
        assert h["content-security-policy"] == "frame-ancestors 'self'" and "content-security-policy-report-only" not in h


def test_csp_reports_are_logged_once_bounded_and_without_queries(tmp_path, monkeypatch, caplog) -> None:
    import operator_api.web as web
    from a2a_impl.auth import _is_public

    assert _is_public("/csp-report")  # a browser sends reports without credentials
    monkeypatch.setattr(web, "_REPORT_SEEN", set())
    c = _csp_client(tmp_path, monkeypatch, "report")
    report = {"csp-report": {"violated-directive": "img-src", "blocked-uri": "https://x.example/p.png?d=SECRET\n", "document-uri": "https://a/app/"}}
    with caplog.at_level("WARNING", logger="operator_api.web"):
        assert c.post("/csp-report", json=report).status_code == 204
        assert c.post("/csp-report", json=report).status_code == 204  # the same pair: logged once
        api = [{"type": "csp-violation", "body": {"effectiveDirective": "script-src-elem", "blockedURL": "inline"}}]
        c.post("/csp-report", json=api)
        c.post("/csp-report", content=b"{" + b" " * 20000 + b"}")  # oversized: ignored
    lines = [r.getMessage() for r in caplog.records if "[console-csp]" in r.getMessage()]
    assert len(lines) == 2 and "SECRET" not in lines[0] and "blocked-uri=https://x.example/p.png" in lines[0]
    assert "effective-directive=script-src-elem" in lines[1]


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
