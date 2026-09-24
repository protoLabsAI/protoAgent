"""`open_in_editor` — the config-gated fs tool that pops a fenced file open in the
operator's desktop editor (``filesystem.editor_command``)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import tools.fs_tools as fs
from tools.fs_tools import _editor_argv, build_fs_tools


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
def proj(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "router.py").write_text("a\nb\nc\n")
    (tmp_path / "outside.txt").write_text("secret")
    return root


def _cfg(root: Path, editor: str = "zed", write: bool = False) -> _Cfg:
    return _Cfg(
        filesystem_editor_command=editor, filesystem_projects=[{"name": "repo", "path": str(root), "write": write}]
    )


def _tools(cfg):
    return {t.name: t for t in build_fs_tools(cfg)}


class _FakePopen:
    calls: list[dict] = []

    def __init__(self, argv, **kwargs):
        _FakePopen.calls.append({"argv": argv, **kwargs})
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_launch(monkeypatch):
    """Popen + which stubbed: records argv, never starts a real editor."""
    _FakePopen.calls = []
    monkeypatch.setattr(fs.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)
    return _FakePopen.calls


# ── binding ───────────────────────────────────────────────────────────────────


def test_unbound_by_default(proj):
    assert "open_in_editor" not in _tools(_cfg(proj, editor=""))


def test_unbound_on_whitespace_or_unparseable_command(proj):
    assert "open_in_editor" not in _tools(_cfg(proj, editor="   "))
    assert "open_in_editor" not in _tools(_cfg(proj, editor='zed "unterminated'))


def test_bound_when_configured(proj):
    assert "open_in_editor" in _tools(_cfg(proj))


def test_config_default_is_empty_and_parses():
    from graph.config import LangGraphConfig

    assert LangGraphConfig().filesystem_editor_command == ""
    cfg = LangGraphConfig.from_dict({"filesystem": {"editor_command": "  code -g  "}})
    assert cfg.filesystem_editor_command == "code -g"


# ── fence + existence ─────────────────────────────────────────────────────────


def test_fence_escape_refused(proj, fake_launch):
    t = _tools(_cfg(proj))["open_in_editor"]
    for bad in ["../outside.txt", "/etc/passwd", "~/x", "src/../../outside.txt"]:
        out = t.invoke({"project": "repo", "path": bad})
        assert out.startswith("Error:"), bad
    assert fake_launch == []  # nothing launched for any escape


def test_unknown_project_refused(proj, fake_launch):
    out = _tools(_cfg(proj))["open_in_editor"].invoke({"project": "nope", "path": "src/router.py"})
    assert "unknown project" in out
    assert fake_launch == []


def test_missing_file_refused(proj, fake_launch):
    out = _tools(_cfg(proj))["open_in_editor"].invoke({"project": "repo", "path": "src/nope.py"})
    assert out == "Error: no such file: src/nope.py"
    assert fake_launch == []


def test_bad_line_refused(proj, fake_launch):
    t = _tools(_cfg(proj))["open_in_editor"]
    assert t.invoke({"project": "repo", "path": "src/router.py", "line": 0}).startswith("Error:")
    assert t.invoke({"project": "repo", "path": "src", "line": 3}).startswith("Error:")  # line on a dir
    assert fake_launch == []


def test_directory_without_line_refused(proj, fake_launch):
    """A directory is not a file even when no `line` is given — nothing launches."""
    out = _tools(_cfg(proj))["open_in_editor"].invoke({"project": "repo", "path": "src"})
    assert out.startswith("Error: not a file: src")
    assert fake_launch == []


def test_missing_binary(proj, monkeypatch):
    monkeypatch.setattr(fs.shutil, "which", lambda name: None)
    out = _tools(_cfg(proj))["open_in_editor"].invoke({"project": "repo", "path": "src/router.py"})
    assert out == "Error: editor command 'zed' not found on PATH."


def test_nonzero_exit_reported(proj, monkeypatch):
    class _Failing(_FakePopen):
        def wait(self, timeout=None):
            return 2

    monkeypatch.setattr(fs.shutil, "which", lambda name: "/usr/local/bin/zed")
    monkeypatch.setattr(fs.subprocess, "Popen", _Failing)
    out = _tools(_cfg(proj))["open_in_editor"].invoke({"project": "repo", "path": "src/router.py"})
    assert out == "Error: editor command 'zed' exited with status 2."


def test_still_running_editor_is_success(proj, monkeypatch):
    class _Slow(_FakePopen):
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("zed", timeout)
            return 0

    monkeypatch.setattr(fs.shutil, "which", lambda name: "/usr/local/bin/zed")
    monkeypatch.setattr(fs.subprocess, "Popen", _Slow)
    out = _tools(_cfg(proj))["open_in_editor"].invoke({"project": "repo", "path": "src/router.py"})
    assert out == "Opened repo/src/router.py in zed."


# ── argv shape ────────────────────────────────────────────────────────────────


def test_argv_shape_and_detached_launch(proj, fake_launch):
    t = _tools(_cfg(proj, editor="code -g"))["open_in_editor"]
    out = t.invoke({"project": "repo", "path": "src/router.py", "line": 2})
    assert out == "Opened repo/src/router.py:2 in code."
    (call,) = fake_launch
    target = (proj / "src" / "router.py").resolve()
    assert call["argv"] == ["/usr/local/bin/code", "-g", f"{target}:2"]
    assert call["stdout"] is subprocess.DEVNULL and call["stderr"] is subprocess.DEVNULL
    assert call["stdin"] is subprocess.DEVNULL
    assert call.get("start_new_session") or call.get("creationflags")  # detached


def test_works_in_read_only_project(proj, fake_launch):
    out = _tools(_cfg(proj, write=False))["open_in_editor"].invoke({"project": "repo", "path": "src/router.py"})
    assert out.startswith("Opened")
    assert fake_launch[0]["argv"][-1] == str((proj / "src" / "router.py").resolve())


def test_editor_argv_line_formatting(tmp_path):
    target = (tmp_path / "f.py").resolve()
    assert _editor_argv("zed", target) == ["zed", str(target)]
    assert _editor_argv("zed", target, 42) == ["zed", f"{target}:42"]
    assert _editor_argv("cursor -g", target, 7) == ["cursor", "-g", f"{target}:7"]


def test_editor_argv_model_text_is_one_element(tmp_path):
    """A hostile-looking filename stays inside the single target element."""
    target = (tmp_path / "--wait x.py").resolve()
    argv = _editor_argv("zed", target, 1)
    assert argv == ["zed", f"{target}:1"]
    assert not argv[-1].startswith("-")


def test_editor_argv_refuses_relative_target():
    with pytest.raises(ValueError):
        _editor_argv("zed", Path("-rf"))
    with pytest.raises(ValueError):
        _editor_argv("", Path("/abs/x"))


# ── operator-MCP classification ───────────────────────────────────────────────


def test_not_in_operator_mcp_read_only_profile():
    """A side effect on the operator's desktop is not a read — it must never ride the
    ``read-only`` operator-MCP profile."""
    from runtime.operator_mcp_tools import _READ_ONLY_TOOLS

    assert "open_in_editor" not in _READ_ONLY_TOOLS


# ── Windows launch rules ──────────────────────────────────────────────────────


def test_windows_quoted_exe_path_is_unquoted(proj, monkeypatch):
    """shlex(posix=False) keeps the quotes around a spaced exe path; they must not reach
    `shutil.which` (else the editor reads as missing) or the argv."""
    monkeypatch.setattr(fs, "_is_windows", lambda: True)
    exe = r"C:\Program Files\Zed\zed.exe"
    seen: list[str] = []

    def _which(name):
        seen.append(name)
        return name if name == exe else None

    monkeypatch.setattr(fs.shutil, "which", _which)
    _FakePopen.calls = []
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)
    tools = _tools(_cfg(proj, editor=f'"{exe}" -g'))
    out = tools["open_in_editor"].invoke({"project": "repo", "path": "src/router.py", "line": 2})
    assert out == f"Opened repo/src/router.py:2 in {exe}."
    assert seen == [exe]
    (call,) = _FakePopen.calls
    assert call["argv"][:2] == [exe, "-g"]


def test_windows_split_strips_quotes_per_token(monkeypatch):
    monkeypatch.setattr(fs, "_is_windows", lambda: True)
    assert fs._split_editor_command(r'"C:\Program Files\Code\Code.exe" "-g"') == [
        r"C:\Program Files\Code\Code.exe",
        "-g",
    ]
    assert fs._split_editor_command(r"C:\Tools\zed.exe") == [r"C:\Tools\zed.exe"]


@pytest.mark.parametrize(
    "launcher", [r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd", r"C:\tools\ed.BAT"]
)
def test_windows_batch_launcher_refused(proj, monkeypatch, launcher):
    """`code` on Windows is `code.cmd`; Popen would run it via cmd.exe, and a managed-project
    file named `a&calc&b.py` would inject. Refuse batch launchers outright."""
    monkeypatch.setattr(fs, "_is_windows", lambda: True)
    monkeypatch.setattr(fs.shutil, "which", lambda name: launcher)
    _FakePopen.calls = []
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)
    (proj / "a&calc&b.py").write_text("x")
    out = _tools(_cfg(proj, editor="code -g"))["open_in_editor"].invoke({"project": "repo", "path": "a&calc&b.py"})
    assert out.startswith("Error: editor command 'code' resolves to a batch launcher")
    assert "Code.exe" in out and "filesystem.editor_command" in out
    assert _FakePopen.calls == []


def test_batch_suffix_allowed_off_windows(proj, monkeypatch):
    """The cmd.exe hazard is Windows-only — a `.cmd`-named script elsewhere is exec'd directly."""
    monkeypatch.setattr(fs, "_is_windows", lambda: False)
    monkeypatch.setattr(fs.shutil, "which", lambda name: "/opt/bin/ed.cmd")
    _FakePopen.calls = []
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)
    out = _tools(_cfg(proj, editor="ed.cmd"))["open_in_editor"].invoke({"project": "repo", "path": "src/router.py"})
    assert out.startswith("Opened")
