"""One line numbering across every line-addressed fs surface (ADR 0112).

A line ends at ``\\n`` — what an editor's gutter counts. ``read_file``/``search_files`` used
``str.splitlines`` (which also breaks on ``\\f``, a lone ``\\r``, ``\\x0b``, ``\\x1c``-``\\x1e``,
``\\x85``, ``\\u2028``/``\\u2029``) while the code pane split on ``\\n``: a file with one form
feed made ``search_files`` report ``file:4`` for a match on line 3, so the console opened the
wrong row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from operator_api.browse_routes import register_browse_routes
from runtime.state import STATE
from tools.fs_tools import build_fs_tools
from tools.fs_view import split_lines


@pytest.fixture(autouse=True)
def _unwired_host_config(monkeypatch):
    from graph.plugins.host import HOST

    monkeypatch.setattr(HOST, "config", None)


@dataclass
class _Cfg:
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = False
    filesystem_run_requires_approval: bool = True
    filesystem_bypass_allowed: bool = True
    filesystem_editor_command: str = ""
    filesystem_projects: list = field(default_factory=list)
    tools_memoize_reads_enabled: bool = False


# Each case: file bytes, and the \n-line (1-based) the word NEEDLE is on.
_CASES = {
    "formfeed": (b"one\ntwo\n\x0cthree NEEDLE\nfour\n", 3),
    "lone_cr": (b"one\rtwo\nthree NEEDLE\n", 2),
    "u2028": ("one two\nNEEDLE here\n".encode(), 2),
    "x85_vt_fs": ("a\x85b\x0bc\x1cd\nNEEDLE\n".encode(), 2),
    "crlf": (b"one\r\ntwo\r\nNEEDLE\r\n", 3),
}


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for name, (data, _) in _CASES.items():
        (root / f"{name}.txt").write_bytes(data)
    return root


def _tools(root):
    return {t.name: t for t in build_fs_tools(_Cfg(filesystem_projects=[{"name": "repo", "path": str(root)}]))}


def _pane(monkeypatch, root) -> TestClient:
    monkeypatch.setattr(
        STATE, "graph_config", LangGraphConfig(filesystem_projects=[{"name": "repo", "path": str(root)}]), raising=False
    )
    app = FastAPI()
    register_browse_routes(app)
    return TestClient(app)


@pytest.mark.parametrize("name", list(_CASES))
def test_search_read_pane_and_show_code_agree(proj, monkeypatch, name):
    data, want = _CASES[name]
    tools = _tools(proj)
    hit = tools["search_files"].invoke({"project": "repo", "query": "NEEDLE"})
    assert f"{name}.txt:{want}:" in hit, hit

    # read_file(offset=N, limit=1) is that very line.
    one = tools["read_file"].invoke({"project": "repo", "path": f"{name}.txt", "offset": want, "limit": 1})
    assert "NEEDLE" in one.split("\n… (")[0]

    # The pane: same line count, same row.
    body = _pane(monkeypatch, proj).get(
        "/api/fs/file", params={"project": "repo", "path": f"{name}.txt", "start": want, "end": want}
    ).json()
    assert "NEEDLE" in body["text"]
    assert body["line_count"] == data.count(b"\n")

    # show_code echoes the same line back.
    out = tools["show_code"].invoke({"project": "repo", "path": f"{name}.txt", "line": want})
    assert f"L{want}: `" in out and "NEEDLE" in out.split("\n")[0]


def test_read_file_whole_file_is_verbatim_and_counts_newlines(proj):
    tools = _tools(proj)
    out = tools["read_file"].invoke({"project": "repo", "path": "formfeed.txt", "offset": 2, "limit": 10})
    # Paging past line 1 reports the \n-count total (4), not splitlines' 5.
    assert out.startswith("two\n\x0cthree NEEDLE\nfour\n")
    assert "of 4" in out or out.endswith("four\n")


def test_crlf_keeps_its_endings_and_is_one_line(proj):
    out = _tools(proj)["read_file"].invoke({"project": "repo", "path": "crlf.txt", "offset": 2, "limit": 1})
    assert out.startswith("two\r\n")


def test_search_context_lines_have_no_stray_cr(proj):
    out = _tools(proj)["search_files"].invoke({"project": "repo", "query": "NEEDLE", "context_lines": 1})
    assert "crlf.txt-2- two\n" in out + "\n"
    # A CRLF line's \r is its ending, not content (lone_cr.txt's inner \r IS content).
    assert not any(ln.startswith("crlf.txt") and ln.endswith("\r") for ln in out.split("\n"))


@pytest.mark.parametrize(
    "text,keep,want",
    [
        ("", False, []),
        ("a", False, ["a"]),
        ("a\n", False, ["a"]),
        ("a\n\n", False, ["a", ""]),
        ("a\r\nb", False, ["a", "b"]),
        ("a\r\nb", True, ["a\r\n", "b"]),
        ("a\x0cb\rc\n", True, ["a\x0cb\rc\n"]),
        ("a b\n", False, ["a b"]),
    ],
)
def test_split_lines(text, keep, want):
    assert split_lines(text, keepends=keep) == want
