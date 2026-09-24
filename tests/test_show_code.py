"""``show_code`` — the fs tool that points the operator's code pane at a line range
(ADR 0112), emitting a validated ``code-ref`` component."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from graph.components import encode_component, extract_component, strip_component, validate_component_props
from tools.fs_tools import build_fs_tools


@pytest.fixture(autouse=True)
def _unwired_host_config(monkeypatch):
    """The fs registry prefers the LIVE ``HOST.config`` seam over the config it was given;
    pin it unwired so a test elsewhere that wired it can't swap this module's projects."""
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


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "router.py").write_bytes(b"".join(f"line {i}\n".encode() for i in range(1, 11)))
    (root / ".env").write_bytes(b"TOKEN=hunter2\n")
    (root / "img.png").write_bytes(b"\x89PNG\x00\x00")
    (tmp_path / "outside.txt").write_bytes(b"x\n")
    return root


def _show(root: Path, write: bool = False):
    cfg = _Cfg(filesystem_projects=[{"name": "repo", "path": str(root), "write": write}])
    return {t.name: t for t in build_fs_tools(cfg)}["show_code"]


def test_bound_with_the_fs_tools_even_read_only(proj):
    assert _show(proj, write=False) is not None


def test_emits_code_ref_with_text_prefix(proj):
    out = _show(proj).invoke(
        {"project": "repo", "path": "src/router.py", "line": 3, "end_line": 5, "note": "the retry reset"}
    )
    assert out.startswith("Showing repo/src/router.py:3-5 to the operator.")
    comp = extract_component(out)
    assert comp == {
        "component": "code-ref",
        "props": {"project": "repo", "path": "src/router.py", "line": 3, "end_line": 5, "note": "the retry reset"},
    }
    assert strip_component(out) == "Showing repo/src/router.py:3-5 to the operator."


def test_end_line_defaults_and_clamps(proj):
    t = _show(proj)
    one = extract_component(t.invoke({"project": "repo", "path": "src/router.py", "line": 4}))
    assert (one["props"]["line"], one["props"]["end_line"]) == (4, 4)
    clamped = extract_component(t.invoke({"project": "repo", "path": "src/router.py", "line": 8, "end_line": 500}))
    assert clamped["props"]["end_line"] == 10


def test_normalises_path(proj):
    out = _show(proj).invoke({"project": "repo", "path": "./src/../src/router.py", "line": 1})
    assert extract_component(out)["props"]["path"] == "src/router.py"


@pytest.mark.parametrize(
    "args,needle",
    [
        ({"path": ".env", "line": 1}, "secret"),
        ({"path": "keys/id_rsa", "line": 1}, "secret"),
        ({"path": "img.png", "line": 1}, "binary"),
        ({"path": "src/router.py", "line": 0}, "out of range"),
        ({"path": "src/router.py", "line": 11}, "out of range"),
        ({"path": "src/router.py", "line": 5, "end_line": 4}, "before line"),
        ({"path": "src/nope.py", "line": 1}, "no such file"),
        ({"path": "src", "line": 1}, "no such file"),
        ({"path": "../outside.txt", "line": 1}, "escapes"),
        ({"path": "/etc/passwd", "line": 1}, "relative"),
        ({"path": "src/router.py", "line": 1, "note": "x" * 281}, "281 chars"),
    ],
)
def test_refusals_emit_no_component(proj, args, needle):
    out = _show(proj).invoke({"project": "repo", **args})
    assert out.startswith("Error:"), out
    assert needle in out
    assert extract_component(out) is None
    assert "hunter2" not in out


def test_unknown_project(proj):
    out = _show(proj).invoke({"project": "nope", "path": "src/router.py", "line": 1})
    assert out.startswith("Error:") and "unknown project" in out


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_to_secret_refused(proj):
    (proj / "innocent.txt").symlink_to(proj / ".env")
    out = _show(proj).invoke({"project": "repo", "path": "innocent.txt", "line": 1})
    assert out.startswith("Error:") and "secret" in out


# ── the component frame rides server/chat.py's existing extraction path ──────


async def test_chat_stream_lifts_the_code_ref_frame(proj, monkeypatch):
    """Unit-level: feed a real show_code ToolMessage through server/chat.py's
    ``_run_turn_stream`` on_tool_end branch (a stub graph that emits just that one
    event) and assert it yields a ("component", …) frame and a clean tool card."""
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from server.chat import _run_turn_stream

    out = _show(proj).invoke({"project": "repo", "path": "src/router.py", "line": 2, "note": "why"})
    msg = ToolMessage(content=out, tool_call_id="call-1", name="show_code")

    class _OneEventGraph:
        async def astream_events(self, *_a, **_k):
            yield {"event": "on_tool_end", "name": "show_code", "run_id": "r1", "data": {"output": msg}}

        async def aget_state(self, *_a, **_k):
            return None

    monkeypatch.setattr(rs.STATE, "graph", _OneEventGraph(), raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)

    frames = []
    try:
        async for kind, payload in _run_turn_stream("hi", "sc1", {"configurable": {"thread_id": "sc1"}}):
            frames.append((kind, payload))
            if kind == "tool_end":
                break
    except Exception as exc:  # noqa: BLE001 — only the frames before tool_end matter here
        raise AssertionError(f"stream failed before tool_end: {exc!r}; frames={frames}") from exc
    comp = next(p for k, p in frames if k == "component")
    assert comp["component"] == "code-ref"
    assert comp["props"] == {"project": "repo", "path": "src/router.py", "line": 2, "end_line": 2, "note": "why"}
    end = next(p for k, p in frames if k == "tool_end")
    assert end["output"] == "Showing repo/src/router.py:2 to the operator."
    assert [k for k, _ in frames].index("component") < [k for k, _ in frames].index("tool_end")


# ── prop validation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "props",
    [
        {"project": "repo", "path": "a.py", "line": 0, "end_line": 1, "note": ""},
        {"project": "repo", "path": "a.py", "line": 2, "end_line": 1, "note": ""},
        {"project": "repo", "path": "a.py", "line": True, "end_line": 1, "note": ""},
        {"project": "repo", "path": "a.py", "line": "1", "end_line": 1, "note": ""},
        {"project": "repo", "path": "a.py", "line": 1, "end_line": 1, "note": "x" * 281},
        {"project": "", "path": "a.py", "line": 1, "end_line": 1, "note": ""},
        {"project": "repo", "path": 5, "line": 1, "end_line": 1, "note": ""},
        {"project": "repo", "path": "a.py", "line": 1, "end_line": 1, "note": "", "text": "smuggled content"},
    ],
)
def test_invalid_code_ref_props_are_dropped(props):
    assert validate_component_props("code-ref", props) is not None
    assert extract_component("x " + encode_component("code-ref", props)) is None


def test_valid_code_ref_props():
    props = {"project": "repo", "path": "a.py", "line": 1, "end_line": 3, "note": "why"}
    assert validate_component_props("code-ref", props) is None
    assert validate_component_props("table", {"anything": object()}) is None  # ADR 0051 widgets unchanged


async def test_show_component_refuses_code_ref():
    from tools.lg_tools import get_all_tools

    tool = {t.name: t for t in get_all_tools()}["show_component"]
    out = await tool.ainvoke(
        {"component": "code-ref", "props": {"project": "r", "path": "a", "line": 1, "end_line": 1}}
    )
    assert out.startswith("Error:") and "show_code" in out
    assert extract_component(out) is None
