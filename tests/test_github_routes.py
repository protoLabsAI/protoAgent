"""Console GitHub issue route (POST /api/github/issue, GET /api/github/config).

Thin layer over tools.gh_issue (covered in test_gh_issue); here we check the
HTTP contract + validation, with `gh` mocked.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.github_routes import register_github_routes

    app = FastAPI()
    register_github_routes(app)
    return TestClient(app)


_BODY = (
    "## Problem\nthe scroll wheel does nothing inside the delegate modal here\n\n"
    "## Steps to reproduce\nopen the modal and scroll the body region\n\n"
    "## Expected vs actual\nexpected scroll; nothing happens\n\n"
    "## Acceptance\nthe modal body scrolls on macOS and linux"
)


def test_config_reports_gh_availability(client):
    with patch("shutil.which", return_value="/usr/bin/gh"):
        r = client.get("/api/github/config")
    assert r.status_code == 200
    body = r.json()
    assert body["gh_available"] is True
    assert "repos" in body and isinstance(body["repos"], list)


def test_create_happy_path(client):
    url = "https://github.com/o/r/issues/7"
    with patch("tools.gh_issue.run_gh", return_value=(0, url, "")) as run:
        r = client.post("/api/github/issue", json={"title": "Scroll dead", "body": _BODY, "kind": "bug", "repo": "o/r"})
    j = r.json()
    assert j["ok"] and j["url"] == url
    assert "bug" in run.call_args.args[0]  # bug → label applied


def test_create_rejects_missing_sections(client):
    with patch("tools.gh_issue.run_gh") as run:
        r = client.post("/api/github/issue", json={"title": "x", "body": "too thin", "kind": "bug", "repo": "o/r"})
    run.assert_not_called()
    j = r.json()
    assert j["ok"] is False and j["missing"]


def test_create_requires_repo(client):
    r = client.post("/api/github/issue", json={"title": "x", "body": _BODY, "kind": "bug"})
    j = r.json()
    assert j["ok"] is False and "repo" in j["error"].lower()


def test_create_rejects_bad_repo(client):
    r = client.post("/api/github/issue", json={"title": "x", "body": _BODY, "kind": "bug", "repo": "nope"})
    assert r.json()["ok"] is False


def test_dry_run_does_not_call_gh(client):
    with patch("tools.gh_issue.run_gh") as run:
        r = client.post(
            "/api/github/issue",
            json={"title": "x", "body": _BODY, "kind": "bug", "repo": "o/r", "dry_run": True},
        )
    run.assert_not_called()
    assert r.json()["dry_run"] is True
