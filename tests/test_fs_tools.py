"""Tests for the fenced multi-project filesystem toolset (ADR 0007)."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field

import pytest

from tools.fs_tools import Project, ProjectRegistry, build_fs_tools

_LIST_COMMAND = "dir /b" if os.name == "nt" else "ls"


@dataclass
class _Cfg:
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = False
    filesystem_run_requires_approval: bool = True
    filesystem_bypass_allowed: bool = True
    filesystem_projects: list = field(default_factory=list)


@pytest.fixture
def workspace(tmp_path):
    a = tmp_path / "projA"
    (a / "src").mkdir(parents=True)
    (a / "src" / "main.py").write_text("print('hello')\nTODO: fix\n")
    (a / "README.md").write_text("# A")
    b = tmp_path / "projB"
    b.mkdir()
    (b / "notes.txt").write_text("read only")
    return tmp_path, a, b


# ── registry / fence ──────────────────────────────────────────────────────────


def test_registry_resolves_within_root(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a, write=True)])
    assert reg.resolve("a", "src/main.py") == a / "src" / "main.py"
    assert reg.resolve("a", ".") == a


def test_registry_rejects_escape(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a)])
    for bad in ["../etc/passwd", "../../x", "/etc/passwd", "~/secrets"]:
        with pytest.raises(ValueError):
            reg.resolve("a", bad)


def test_registry_unknown_project(workspace):
    _, a, _ = workspace
    reg = ProjectRegistry([Project("a", a)])
    with pytest.raises(ValueError, match="unknown project"):
        reg.resolve("nope", ".")


# ── build_fs_tools wiring ──────────────────────────────────────────────────────


def _tools(cfg):
    return {t.name: t for t in build_fs_tools(cfg)}


def test_no_tools_without_valid_projects():
    assert build_fs_tools(_Cfg(filesystem_projects=[])) == []
    # Nonexistent path → skipped → no tools.
    assert build_fs_tools(_Cfg(filesystem_projects=[{"name": "x", "path": "/nope/zzz"}])) == []


def test_all_folders_unusable_warns_with_paths(caplog):
    """Configured-but-all-unusable unbinds every fs tool. That's an operator
    mistake, so it logs at WARNING and names the offending folder; the inert default
    (nothing configured) stays quiet at INFO."""
    with caplog.at_level(logging.WARNING, logger="protoagent.fs"):
        assert build_fs_tools(_Cfg(filesystem_projects=[{"name": "x", "path": "/nope/zzz"}])) == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NOT bound" in m and "/nope/zzz" in m for m in msgs), msgs


def test_read_list_find_search(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert "hello" in t["read_file"].invoke({"project": "a", "path": "src/main.py"})
    assert "README.md" in t["list_dir"].invoke({"project": "a", "path": "."})
    assert "src/main.py" in t["find_files"].invoke({"project": "a", "pattern": "**/*.py"})
    hit = t["search_files"].invoke({"project": "a", "query": "TODO"})
    assert "main.py" in hit and "TODO" in hit


def test_read_file_escape_is_refused(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a)}]))
    out = t["read_file"].invoke({"project": "a", "path": "../projB/notes.txt"})
    assert out.startswith("Error:") and "escape" in out


def test_write_and_edit_in_rw_project(workspace):
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert "Created" in t["write_file"].invoke({"project": "a", "path": "new.txt", "content": "v1"})
    assert (a / "new.txt").read_text() == "v1"
    assert "Edited" in t["edit_file"].invoke({"project": "a", "path": "new.txt", "old": "v1", "new": "v2"})
    assert (a / "new.txt").read_text() == "v2"


def test_write_blocked_in_readonly_project(workspace):
    _, _, b = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "b", "path": str(b), "write": False}]))
    out = t["write_file"].invoke({"project": "b", "path": "x.txt", "content": "nope"})
    assert out.startswith("Error:") and "read-only" in out
    assert not (b / "x.txt").exists()


def test_edit_requires_unique_old(workspace):
    _, a, _ = workspace
    (a / "dup.txt").write_text("x\nx\n")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["edit_file"].invoke({"project": "a", "path": "dup.txt", "old": "x", "new": "y"})
    assert out.startswith("Error:") and "not unique" in out


# ── run_command gating ─────────────────────────────────────────────────────────


def test_run_command_absent_unless_allowed(workspace):
    _, a, _ = workspace
    base = {"name": "a", "path": str(a), "write": True}
    assert "run_command" not in _tools(_Cfg(filesystem_projects=[base], filesystem_allow_run=False))
    assert "run_command" in _tools(_Cfg(filesystem_projects=[base], filesystem_allow_run=True))


def test_run_command_executes_in_project_cwd(workspace):
    _, a, _ = workspace
    # Approval off here so the unit test exercises execution directly (the gate
    # calls interrupt(), which needs a graph runtime — covered separately).
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "README.md" in out


def test_run_command_runs_via_shell(workspace):
    """run_command goes through the platform shell, so native shell operators work."""
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "echo one && echo two"}))
    # Exact lines (not substrings): the old argv path would print the literal "one && echo two",
    # so this assertion specifically fails unless the && actually chained two commands.
    assert [line.rstrip() for line in out.splitlines()] == ["one", "two"]


def test_run_command_declined_returns_not_raises(workspace, monkeypatch):
    """A declined approval RETURNS a plain result — NOT a ToolException. A decline is
    the operator's deliberate choice, not a failure: raising stamped status="error"
    and the chat rendered an undismissable full-bleed red block. The result names the
    declined command and tells the model not to retry. interrupt() is stubbed to deny."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "declined by the operator" in out
    assert _LIST_COMMAND in out
    assert "Do not re-run" in out


def test_run_command_bypass_skips_approval(workspace, monkeypatch):
    """Bypass-permissions mode (per-turn metadata + host allows): run_command runs WITHOUT the
    approval gate. interrupt() is stubbed to DENY, so if the gate were reached the command would
    raise — a clean run proves it was skipped."""
    import langgraph.types
    from graph.middleware.request_context import request_metadata_scope

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
            filesystem_bypass_allowed=True,
        )
    )
    with request_metadata_scope({"bypass_permissions": True}):
        out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "README.md" in out


def test_run_command_bypass_forbidden_by_host_still_gates(workspace, monkeypatch):
    """When the host forbids bypass (filesystem_bypass_allowed=False), caller bypass metadata is
    IGNORED and the approval gate still fires — here stubbed to deny, so the command doesn't run
    and returns the decline result (the gate firing is the point; the decline handling is
    covered by test_run_command_declined_returns_not_raises)."""
    import langgraph.types
    from graph.middleware.request_context import request_metadata_scope

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
            filesystem_bypass_allowed=False,
        )
    )
    with request_metadata_scope({"bypass_permissions": True}):
        out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "ls"}))
    assert "declined by the operator" in out  # gate fired despite the bypass request


# ── no_delete fence mode + delete_file (ADR 0083 D5, #2012) ────────────────────


def test_registry_parses_no_delete(workspace):
    """A project's `no_delete: true` config key lands on the Project model."""
    _, a, b = workspace
    from tools.fs_tools import _registry_from_config

    reg = _registry_from_config(
        _Cfg(
            filesystem_projects=[
                {"name": "a", "path": str(a), "write": True, "no_delete": True},
                {"name": "b", "path": str(b), "write": True},
            ]
        )
    )
    assert reg.get("a").no_delete is True
    assert reg.get("b").no_delete is False


def test_list_projects_reports_three_modes(workspace):
    """list_projects labels each mode: ro / rw / rw-no-delete."""
    tmp, a, b = workspace
    c = tmp / "projC"
    c.mkdir()
    t = _tools(
        _Cfg(
            filesystem_projects=[
                {"name": "rw", "path": str(a), "write": True},
                {"name": "ro", "path": str(b)},  # write defaults false
                {"name": "nod", "path": str(c), "write": True, "no_delete": True},
            ]
        )
    )
    out = t["list_projects"].invoke({})
    assert "rw  [rw]" in out
    assert "ro  [ro]" in out
    assert "nod  [rw/no-delete]" in out


def test_delete_file_present_and_refused_when_read_only(workspace):
    """delete_file is always built (it self-gates); a read-only project refuses it."""
    _, _, b = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "b", "path": str(b)}]))  # write:false
    assert "delete_file" in t
    out = t["delete_file"].invoke({"project": "b", "path": "notes.txt"})
    assert "read-only" in out
    assert (b / "notes.txt").exists()  # untouched


def test_delete_file_refused_in_no_delete_project(workspace):
    """A read-write-no-delete project refuses delete_file even though writes are allowed."""
    _, a, _ = workspace
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True, "no_delete": True}]))
    out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "no_delete" in out
    assert (a / "README.md").exists()  # untouched


def test_delete_file_approved_removes(workspace, monkeypatch):
    """In a read-write project, an APPROVED delete removes the file. interrupt() → approve."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "approve")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    assert (a / "README.md").exists()
    out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "Deleted README.md" in out
    assert not (a / "README.md").exists()


def test_delete_file_declined_keeps_file(workspace, monkeypatch):
    """A DECLINED delete returns a plain decline (not a raise) and leaves the file."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "declined by the operator" in out
    assert "Do not retry" in out
    assert (a / "README.md").exists()  # kept


def test_delete_file_floor_not_bypassable(workspace, monkeypatch):
    """The permanent-delete floor ignores bypass-permissions: even with the /bypass toggle
    set (which skips run_command's gate), delete_file still asks — here stubbed to deny, so
    the file survives. This is the key difference from run_command."""
    import langgraph.types
    from graph.middleware.request_context import request_metadata_scope

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "denied")
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a), "write": True}],
            filesystem_bypass_allowed=True,
        )
    )
    with request_metadata_scope({"bypass_permissions": True}):
        out = t["delete_file"].invoke({"project": "a", "path": "README.md"})
    assert "declined by the operator" in out  # gate fired despite bypass
    assert (a / "README.md").exists()  # floor held


def test_delete_file_refuses_directory(workspace, monkeypatch):
    """delete_file removes a single file, never a directory tree."""
    import langgraph.types

    _, a, _ = workspace
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: "approve")
    t = _tools(_Cfg(filesystem_projects=[{"name": "a", "path": str(a), "write": True}]))
    out = t["delete_file"].invoke({"project": "a", "path": "src"})
    assert "is a directory" in out
    assert (a / "src").is_dir()  # untouched


# ── config round-trip ──────────────────────────────────────────────────────────


def test_config_parses_filesystem(tmp_path):
    from graph.config import LangGraphConfig

    p = tmp_path / "c.yaml"
    p.write_text(
        "filesystem:\n  enabled: true\n  allow_run: true\n  projects:\n    - {name: orbis, path: /tmp, write: false}\n"
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.filesystem_enabled is True
    assert cfg.filesystem_allow_run is True
    assert cfg.filesystem_projects[0]["name"] == "orbis"


def test_config_filesystem_default_on_fenced_workspace(tmp_path, monkeypatch):
    """Filesystem is ON by default (fenced to a workspace); run_command stays opt-in."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    assert cfg.filesystem_enabled is True
    # run_command is ON now (arbitrary argv, unsandboxed) but gated by HITL
    # approval by default — capable, not dangerous-by-default.
    assert cfg.filesystem_allow_run is True
    assert cfg.filesystem_run_requires_approval is True
    # No explicit projects → a single default `workspace` project, fenced + writable.
    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(tmp_path / "ws"))
    projects = cfg.effective_filesystem_projects(create=True)
    assert len(projects) == 1
    assert projects[0]["name"] == "workspace" and projects[0]["write"] is True
    assert (tmp_path / "ws").is_dir()  # created


def test_approved_accepts_known_shapes():
    from tools.fs_tools import _approved

    for yes in ("approve", "approved", "Yes", " OK ", True, {"approved": True}, {"decision": "approve"}):
        assert _approved(yes) is True, yes
    for no in ("deny", "denied", "no", "", False, {"approved": False}, {"decision": "deny"}, None):
        assert _approved(no) is False, no


def test_run_command_present_by_default_gated(tmp_path, monkeypatch):
    """Shell is on by default (allow_run) — run_command is built — and approval
    is required by default."""
    from graph.config import LangGraphConfig
    from tools.fs_tools import build_fs_tools

    monkeypatch.setenv("PROTOAGENT_WORKSPACE", str(tmp_path / "ws"))
    cfg = LangGraphConfig()  # defaults: enabled + allow_run + requires_approval
    names = {getattr(t, "name", "") for t in build_fs_tools(cfg)}
    assert "run_command" in names


def test_effective_projects_explicit_wins_and_disabled_is_empty(tmp_path):
    from graph.config import LangGraphConfig

    explicit = [{"name": "repo", "path": str(tmp_path), "write": False}]
    cfg = LangGraphConfig(filesystem_projects=explicit)
    assert cfg.effective_filesystem_projects() == explicit  # explicit registry wins
    off = LangGraphConfig(filesystem_enabled=False)
    assert off.effective_filesystem_projects() == []  # disabled → no projects


# ── run_command shell grammars (#2518) ─────────────────────────────────────────


def test_shell_argv_default_grammars():
    from tools.fs_tools import _platform_shell_argv

    argv, runner = _platform_shell_argv("echo hi", windows=False)
    assert argv == ["/bin/sh", "-c", "echo hi"]
    assert runner == "/bin/sh -c"
    argv, runner = _platform_shell_argv("dir", windows=True)
    assert argv[0].lower().endswith("cmd.exe")
    assert argv[1:] == ["/d", "/s", "/c", "dir"]
    assert "cmd.exe" in runner.lower()


def test_shell_argv_powershell_is_encoded_and_unicode_safe():
    """PowerShell rides -EncodedCommand (Base64 UTF-16LE): the exact approved text
    crosses the process boundary with no re-quoting, and non-ASCII survives."""
    import base64

    from tools.fs_tools import _platform_shell_argv

    cmd = "Set-Content -LiteralPath 'PA Windows Tool [café] 日本語.txt' -Value 'café'"
    argv, runner = _platform_shell_argv(cmd, "powershell", windows=True)
    assert argv[0] == "powershell.exe"
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    decoded = base64.b64decode(argv[4]).decode("utf-16-le")
    assert decoded.endswith(cmd)  # byte-exact inner command, no escaping layer
    assert "OutputEncoding" in decoded  # UTF-8 stdout preamble
    assert "powershell.exe" in runner and "EncodedCommand" in runner
    argv, _ = _platform_shell_argv(cmd, "powershell", windows=False)
    assert argv[0] == "pwsh"


def test_shell_argv_rejects_unknown_and_cross_platform():
    from tools.fs_tools import _platform_shell_argv

    with pytest.raises(ValueError, match="unknown shell"):
        _platform_shell_argv("x", "fish")
    with pytest.raises(ValueError, match="Windows-only"):
        _platform_shell_argv("x", "cmd", windows=False)
    with pytest.raises(ValueError, match="not available on Windows"):
        _platform_shell_argv("x", "sh", windows=True)


def test_run_command_unknown_shell_returns_error(workspace):
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": "ls", "shell": "fish"}))
    assert out.startswith("Error:") and "unknown shell" in out


def test_run_command_approval_detail_names_runner(workspace, monkeypatch):
    """The approval payload names the real executable chain, not just the inner
    command — approving PowerShell text that secretly ran under cmd.exe is the
    #2518 failure, so the operator must see the runner before approving."""
    import langgraph.types

    _, a, _ = workspace
    seen: dict = {}

    def fake_interrupt(payload):
        seen.update(payload)
        return "approve"

    monkeypatch.setattr(langgraph.types, "interrupt", fake_interrupt)
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=True,
        )
    )
    out = asyncio.run(t["run_command"].ainvoke({"project": "a", "command": _LIST_COMMAND}))
    assert "README.md" in out
    assert _LIST_COMMAND in seen["detail"]
    assert "runs via:" in seen["detail"]
    assert ("cmd.exe" in seen["detail"].lower()) or ("/bin/sh -c" in seen["detail"])


_PWSH = "powershell.exe" if os.name == "nt" else "pwsh"


@pytest.mark.skipif(shutil.which(_PWSH) is None, reason="PowerShell not installed")
def test_run_command_powershell_unicode_roundtrip(workspace):
    """#2518 acceptance: an ordinary Set-Content whose path and content carry
    spaces, brackets, accents, Japanese, a check mark, and a Greek letter works
    on the FIRST attempt — no [char] reconstruction, no cmd.exe reinterpretation."""
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a), "write": True}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    name = "PA Windows Tool [café] 日本語.txt"
    cmd = (
        f"Set-Content -LiteralPath '{name}' "
        "-Value @('PA-WINDOWS-TOOL-OK','café','日本語 ✓ Ω') -Encoding utf8"
    )
    out = asyncio.run(
        t["run_command"].ainvoke({"project": "a", "command": cmd, "shell": "powershell"})
    )
    assert not out.startswith("Error:"), out
    target = a / name
    assert target.exists()
    # utf-8-sig: Windows PowerShell 5.1 writes -Encoding utf8 WITH a BOM, pwsh without.
    text = target.read_bytes().decode("utf-8-sig")
    assert text.splitlines() == ["PA-WINDOWS-TOOL-OK", "café", "日本語 ✓ Ω"]


@pytest.mark.skipif(shutil.which(_PWSH) is None, reason="PowerShell not installed")
def test_run_command_powershell_stdout_unicode(workspace):
    """The UTF-8 output preamble holds: non-ASCII stdout decodes intact instead of
    arriving in the OEM code page as mojibake."""
    _, a, _ = workspace
    t = _tools(
        _Cfg(
            filesystem_projects=[{"name": "a", "path": str(a)}],
            filesystem_allow_run=True,
            filesystem_run_requires_approval=False,
        )
    )
    out = asyncio.run(
        t["run_command"].ainvoke(
            {"project": "a", "command": "Write-Output 'café ✓ 日本語'", "shell": "powershell"}
        )
    )
    assert "café ✓ 日本語" in out
