"""``POST /api/fs/open-in-editor`` — the console's host-editor fallback for "Open in editor".

Driven through a real FastAPI app over a real temp project; the launch is faked at
``subprocess.Popen`` so the shared ``open_in_host_editor`` helper (fence, secret deny, argv)
runs for real.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tools.fs_tools as fs
from graph.config import LangGraphConfig
from operator_api.browse_routes import _same_machine, register_browse_routes
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive

_needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")

LOCAL = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def _unwired_host_config(monkeypatch):
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "config", None)


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("one\ntwo\n")
    (root / ".env").write_text("API_KEY=hunter2\n")
    (tmp_path / "outside.txt").write_text("outside\n")
    return root


class _FakePopen:
    calls: list[list[str]] = []

    def __init__(self, argv, **kwargs):
        _FakePopen.calls.append(argv)

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def launches(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(fs.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)
    return _FakePopen.calls


def _client(monkeypatch, proj: Path, editor: str = "zed", client=LOCAL) -> TestClient:
    cfg = LangGraphConfig(filesystem_editor_command=editor, filesystem_projects=[{"name": "repo", "path": str(proj)}])
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    app = FastAPI()
    register_browse_routes(app)
    return TestClient(app, client=client)


def _post(c: TestClient, headers: dict | None = None, **body):
    return c.post("/api/fs/open-in-editor", json={"project": "repo", "path": "src/app.py", **body}, headers=headers)


def test_opens_the_fenced_file_at_the_line(monkeypatch, proj, launches):
    r = _post(_client(monkeypatch, proj), line=2)
    assert r.status_code == 200 and r.json() == {"ok": True, "editor": "zed"}
    assert launches == [["/usr/bin/zed", f"{(proj / 'src' / 'app.py').resolve()}:2"]]


def test_no_editor_command_is_404_disabled(monkeypatch, proj, launches):
    r = _post(_client(monkeypatch, proj, editor=""))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "disabled"
    assert launches == []


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "~/x", "src/../../outside.txt"])
def test_fence_escape_is_400_bad_path(monkeypatch, proj, launches, path):
    r = _post(_client(monkeypatch, proj), path=path)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path", path
    assert launches == []


def test_secret_like_file_is_403_denied_before_existence(monkeypatch, proj, launches):
    c = _client(monkeypatch, proj)
    for path in (".env", "missing/.env"):  # a missing secret-like name is denied too — no oracle
        r = _post(c, path=path)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "denied", path
    assert launches == []


@_needs_symlinks
def test_symlink_to_a_secret_is_denied(monkeypatch, proj, launches):
    (proj / "notes.txt").symlink_to(proj / ".env")
    r = _post(_client(monkeypatch, proj), path="notes.txt")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "denied"
    assert launches == []


def test_missing_dir_and_bad_line(monkeypatch, proj, launches):
    c = _client(monkeypatch, proj)
    assert _post(c, path="src/nope.py").json()["detail"]["code"] == "not_found"
    assert _post(c, path="src").json()["detail"]["code"] == "not_a_file"
    for line in (0, -1, "3", True, 1.5):
        assert _post(c, line=line).json()["detail"]["code"] == "bad_line", line
    assert _post(c, project=3).json()["detail"]["code"] == "bad_request"
    assert launches == []


def test_launch_failure_is_502(monkeypatch, proj):
    monkeypatch.setattr(fs.shutil, "which", lambda name: None)
    r = _post(_client(monkeypatch, proj))
    assert r.status_code == 502 and r.json()["detail"]["code"] == "launch_failed"
    assert "not found on PATH" in r.json()["detail"]["reason"]


def test_refused_unless_the_console_is_on_this_machine(monkeypatch, proj, launches):
    remote = _client(monkeypatch, proj, client=("192.168.1.20", 50000))
    assert _post(remote).json()["detail"]["code"] == "not_local"
    local = _client(monkeypatch, proj)
    # A LAN browser reaching a local member through a fleet hub arrives over loopback,
    # but carries its own Origin.
    assert _post(local, headers={"Origin": "http://192.168.1.5:7870"}).json()["detail"]["code"] == "not_local"
    assert _post(local, headers={"Origin": "http://localhost:7870"}).status_code == 200
    assert _post(local, headers={"Origin": "tauri://localhost"}).status_code == 200
    assert len(launches) == 2


def test_same_machine_predicate():
    assert _same_machine("127.0.0.1", None)
    assert _same_machine("::1", "http://127.0.0.1:7871")
    assert _same_machine("127.0.0.2", "http://tauri.localhost")
    assert not _same_machine("10.0.0.3", None)
    assert not _same_machine(None, None)
    assert not _same_machine("testclient", None)
    assert not _same_machine("127.0.0.1", "https://evil.example")


def test_runtime_status_reports_editor_command_configured_never_the_command():
    from operator_api.runtime import build_runtime_status

    def status(**kw):
        return build_runtime_status(config=LangGraphConfig(**kw), setup_complete=True, graph_loaded=True)

    assert status()["editor_command_configured"] is False
    on = status(filesystem_editor_command="code -g")
    assert on["editor_command_configured"] is True
    assert "code -g" not in repr(on)
    assert status(filesystem_editor_command="'unbalanced")["editor_command_configured"] is False
    assert status(filesystem_editor_command="zed", filesystem_enabled=False)["editor_command_configured"] is False
    assert (
        build_runtime_status(config=None, setup_complete=True, graph_loaded=False)["editor_command_configured"] is False
    )
