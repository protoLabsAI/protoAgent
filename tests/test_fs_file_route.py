"""``GET /api/fs/file`` — the console code pane's fenced, capped file read (ADR 0112).

Driven through a real FastAPI app over a real temp project; the fence is the same
``live_project_registry(cfg).resolve`` chokepoint ``read_file`` uses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from operator_api.browse_routes import register_browse_routes
from runtime.state import STATE
from tools import fs_view
from tools.fs_view import LINE_CUT_MARKER, MAX_LINE_CHARS, guess_language, read_window

_needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")


@pytest.fixture(autouse=True)
def _unwired_host_config(monkeypatch):
    """The fs registry prefers the LIVE ``HOST.config`` seam over the config it was given;
    pin it unwired so a test elsewhere that wired it can't swap this module's projects."""
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "config", None)


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_bytes(b"one\ntwo\nthree\n")
    (root / ".env").write_bytes(b"API_KEY=hunter2\n")
    (root / ".env.example").write_bytes(b"API_KEY=\n")
    (tmp_path / "outside.txt").write_bytes(b"outside the fence\n")
    return root


@pytest.fixture
def client(monkeypatch, proj):
    cfg = LangGraphConfig(filesystem_projects=[{"name": "repo", "path": str(proj)}])
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    app = FastAPI()
    register_browse_routes(app)
    return TestClient(app)


def _get(client, path, **params):
    return client.get("/api/fs/file", params={"project": "repo", "path": path, **params})


def test_reads_whole_small_file(client):
    r = _get(client, "src/app.py")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "project": "repo",
        "path": "src/app.py",
        "size": 14,
        "line_count": 3,
        "start": 1,
        "end": 3,
        "truncated": False,
        "language": "py",
        "binary": False,
        "text": "one\ntwo\nthree\n",
    }


def test_window_by_start_end(client):
    body = _get(client, "src/app.py", start=2, end=2).json()
    assert (body["start"], body["end"], body["text"], body["truncated"]) == (2, 2, "two\n", False)
    body = _get(client, "src/app.py", start=2, end=99).json()
    assert (body["end"], body["text"], body["truncated"]) == (3, "two\nthree\n", False)


def test_start_past_eof_is_bad_range(client):
    r = _get(client, "src/app.py", start=9)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_range"
    r = _get(client, "src/app.py", start=3, end=2)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_range"


def test_empty_file(client, proj):
    (proj / "empty.txt").write_bytes(b"")
    body = _get(client, "empty.txt").json()
    assert (body["line_count"], body["start"], body["end"], body["text"], body["truncated"]) == (0, 1, 0, "", False)


def test_crlf_preserved(client, proj):
    (proj / "win.txt").write_bytes(b"a\r\nb\r\nc")
    body = _get(client, "win.txt").json()
    assert body["text"] == "a\r\nb\r\nc"
    assert body["line_count"] == 3
    # A lone \r is NOT a line break (editors count \n) — unlike read_file's splitlines.
    (proj / "cr.txt").write_bytes(b"a\rb\n")
    assert _get(client, "cr.txt").json()["line_count"] == 1


@pytest.mark.parametrize("bad", ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "~/x"])
def test_fence_escape_is_bad_path(client, bad):
    r = _get(client, bad)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_path"
    assert "outside the fence" not in r.text


@_needs_symlinks
def test_symlink_out_of_root_is_bad_path(client, proj, tmp_path):
    (proj / "sneaky.txt").symlink_to(tmp_path / "outside.txt")
    r = _get(client, "sneaky.txt")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"
    assert "outside the fence" not in r.text


@_needs_symlinks
def test_symlink_to_a_secret_inside_root_is_denied(client, proj):
    (proj / "notes.txt").symlink_to(proj / ".env")
    r = _get(client, "notes.txt")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "denied"
    assert "hunter2" not in r.text


def test_unknown_project_is_bad_path(client):
    r = client.get("/api/fs/file", params={"project": "nope", "path": "src/app.py"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"


def test_secret_denied_even_when_missing(client):
    for rel in [".env", "deploy/id_rsa", "nope/.env.local"]:
        r = _get(client, rel)
        assert r.status_code == 403, rel
        assert r.json()["detail"]["code"] == "denied"
        assert "reason" in r.json()["detail"]
    assert "hunter2" not in _get(client, ".env").text


def test_env_example_is_allowed(client):
    r = _get(client, ".env.example")
    assert r.status_code == 200 and r.json()["language"] == "dotenv"


def test_missing_is_404_and_directory_is_400(client):
    r = _get(client, "src/nope.py")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    r = _get(client, "src")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "not_a_file"


def test_binary_returns_metadata_only(client, proj):
    data = b"\x89PNG\r\n\x1a\n\x00\x00\x00binary"
    (proj / "logo.png").write_bytes(data)
    body = _get(client, "logo.png").json()
    assert body["binary"] is True and body["text"] is None
    assert body["size"] == len(data) and body["path"] == "logo.png"


def test_filesystem_disabled_means_no_projects(monkeypatch, proj):
    cfg = LangGraphConfig(filesystem_enabled=False, filesystem_projects=[{"name": "repo", "path": str(proj)}])
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    app = FastAPI()
    register_browse_routes(app)
    r = TestClient(app).get("/api/fs/file", params={"project": "repo", "path": "src/app.py"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"


# ── caps ──────────────────────────────────────────────────────────────────────


def test_long_line_is_cut_with_marker(client, proj):
    (proj / "min.js").write_bytes(b"x" * (MAX_LINE_CHARS * 10) + b"\r\nnext\n")
    body = _get(client, "min.js").json()
    first, second = body["text"].split("\r\n", 1)
    assert first == "x" * MAX_LINE_CHARS + LINE_CUT_MARKER
    assert second == "next\n"
    assert body["truncated"] is True and body["line_count"] == 2


def test_line_count_cap_and_paging(client, proj, monkeypatch):
    monkeypatch.setattr(fs_view, "MAX_LINES", 10)
    (proj / "big.txt").write_bytes(b"".join(f"L{i}\n".encode() for i in range(1, 26)))
    body = _get(client, "big.txt").json()
    assert (body["start"], body["end"], body["line_count"], body["truncated"]) == (1, 10, 25, True)
    assert body["text"].startswith("L1\n") and body["text"].endswith("L10\n")
    page = _get(client, "big.txt", start=body["end"] + 1).json()
    assert (page["start"], page["end"], page["truncated"]) == (11, 20, True)
    last = _get(client, "big.txt", start=21).json()
    assert (last["end"], last["truncated"]) == (25, False)
    # Explicitly asking for more than the cap is truncated too.
    assert _get(client, "big.txt", start=1, end=25).json()["truncated"] is True


def test_byte_cap_ends_on_a_line_and_pages(client, proj, monkeypatch):
    monkeypatch.setattr(fs_view, "MAX_RESPONSE_BYTES", 100)
    (proj / "wide.txt").write_bytes(b"".join((f"{i:02d}" + "y" * 37 + "\n").encode() for i in range(10)))  # 40B lines
    body = _get(client, "wide.txt").json()
    assert (body["end"], body["truncated"], len(body["text"])) == (2, True, 80)
    assert _get(client, "wide.txt", start=3).json()["start"] == 3


def test_huge_single_line_still_returns_one_line(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_view, "MAX_RESPONSE_BYTES", 10)
    p = tmp_path / "f"
    p.write_bytes(b"z" * 50 + b"\nsecond\n")
    win = read_window(p)
    assert win.end == 1 and win.text == "z" * 50 + "\n" and win.truncated


def test_chunk_boundaries_do_not_split_lines_or_crlf(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_view, "_CHUNK", 3)
    p = tmp_path / "f"
    p.write_bytes(b"ab\r\ncdef\r\ng\n\nh")
    win = read_window(p)
    assert win.text == "ab\r\ncdef\r\ng\n\nh" and win.line_count == 5
    assert read_window(p, 2, 3).text == "cdef\r\ng\n"


def test_invalid_utf8_is_replaced_not_fatal(client, proj):
    (proj / "latin.txt").write_bytes(b"caf\xe9\n")
    assert _get(client, "latin.txt").json()["text"] == "caf�\n"


@pytest.mark.parametrize(
    "name,lang",
    [
        ("a.ts", "ts"),
        ("a.tsx", "tsx"),
        ("x/y.PY", "py"),
        ("Dockerfile", "docker"),
        ("Makefile", "make"),
        ("README.md", "md"),
        ("c.yml", "yaml"),
        ("noext", "text"),
        (".gitignore", "gitignore"),
        ("weird.zzz", "text"),
    ],
)
def test_guess_language(name, lang):
    assert guess_language(name) == lang


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_fifo_is_not_a_file_and_never_blocks(client, proj):
    """Opening a FIFO for read blocks until a writer appears — the route must refuse it
    without ever opening it (a worker thread stuck forever per request)."""
    os.mkfifo(proj / "pipe")
    r = _get(client, "pipe")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "not_a_file"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs")
def test_open_regular_refuses_a_fifo_without_blocking(tmp_path):
    """The race CodeRabbit flagged: even if a caller's is_file() check passed and the path
    was then swapped for a FIFO, the open itself must neither block nor read it."""
    from tools.fs_view import NotARegularFile, open_regular

    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(NotARegularFile):
        open_regular(tmp_path / "pipe")


@_needs_symlinks
def test_open_regular_refuses_a_final_symlink(tmp_path):
    from tools.fs_view import open_regular

    (tmp_path / "real").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OSError):
        open_regular(tmp_path / "link")


@pytest.mark.parametrize("params", [{"start": "abc"}, {"end": "x"}, {"start": "1.5"}, {"start": "1", "end": "two"}])
def test_non_numeric_range_is_bad_range_not_422(client, params):
    r = _get(client, "src/app.py", **params)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_range" and "reason" in r.json()["detail"]


def test_blank_range_params_mean_defaults(client):
    body = _get(client, "src/app.py", start="", end="").json()
    assert (body["start"], body["end"]) == (1, 3)
