"""Tests for the agent_browser plugin — the tool subprocess wrappers (arg-building +
graceful error degradation + the byte cap), the fenced captures, the setup-gap preflight,
the interactive panel routes, the CDP bridge's pure brains, the launch-flag builder,
``register()`` wiring, and manifest/catalog coherence.

agent_browser is bundled into core under ``plugins/agent_browser/`` (#3451, superseding
the retired ``agent-browser-plugin`` repo), so ``ROOT`` anchors there off the repo root
rather than the test's parent dir — the same shape as ``tests/test_artifact_plugin.py``.

This is the standalone repo's whole suite, ported, minus two groups that could not come
with it, plus what only became possible once the plugin lives with the host it runs on:

* the repo's ``tests/test_docs.py`` guarded its OWN ``PROTO.md``/``CLAUDE.md``/``AGENTS.md``
  pointer files, which core owns and the import dropped; the equivalent in-tree guards (the
  bundled README, the docs guide, the catalog rows, the tool count in the reference) replace
  them below;
* the ten tests for #21's ``/panel/dash`` signed-cookie gate are replaced by guards that the
  gate is GONE and by the host-side check that shows why it was unsound (a view path is a
  PREFIX exemption, so the public ``/panel`` sibling served the same bytes anyway);
* new: the setup-gap preflight, the capture fence, ``browser_pdf``, the skill/workflow
  tool-name sweep, and one test that drives the REAL CLI when it's on PATH — the drift the
  standalone CI could never catch, because it mocked the binary entirely.

Host-free where the source was host-free: ``subprocess`` is mocked, so no agent-browser
binary and no real browser are needed (except the explicitly-skipped live test).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest
import yaml

from graph.plugins.testkit import FakeRegistry, load_plugin

pytestmark = pytest.mark.platform_sensitive

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "agent_browser"

# The retired repo the bundled manifest supersedes. Its last TAG was v0.6.4 and its main
# branch carried 0.6.5; GitHub Releases stopped at v0.5.1. The bundled version must stay
# strictly above all of them: a copy that loses its plugins.lock row becomes untracked,
# and an untracked copy that isn't older than the bundled one wins (#1574).
RETIRED_REPO = "https://github.com/protoLabsAI/agent-browser-plugin"
LAST_STANDALONE_VERSION = (0, 6, 5)

EXPECTED_TOOLS = {
    "browser_open", "browser_back", "browser_forward", "browser_reload",
    "browser_snapshot", "browser_get_text", "browser_get_html", "browser_get_value",
    "browser_form_read",
    "browser_click", "browser_fill", "browser_type", "browser_select", "browser_upload",
    "browser_press", "browser_hover",
    "browser_eval", "browser_screenshot", "browser_pdf", "browser_close",
}


# ── loading: the plugin as the host loads it (relative imports resolve) ──────────


_PKG = load_plugin(ROOT, "agent_browser")


def _mod(name: str):
    return importlib.import_module(f"{_PKG.__name__}.{name}")


bp = _mod("browser_panel")
bs = _mod("browser_stream")
preflight = _mod("preflight")
rt = _mod("runtime")
storage = _mod("storage")
tools = _mod("tools")
forms = _mod("forms")
cli_fetch = _mod("cli_fetch")
chrome_install = _mod("chrome_install")


@pytest.fixture(autouse=True)
def _no_real_cli_fetch(monkeypatch, tmp_path):
    """Every test here runs with an EMPTY, private CLI cache and no network: a missing CLI
    must never make a unit test download the real one (first use fetches it), and a CLI a
    developer already fetched must never make a "missing CLI" test pass or fail by accident.
    The real download is exercised by tests/test_agent_browser_cli_fetch.py, opt-in."""
    monkeypatch.setenv(cli_fetch.ENV_CLI_DIR, str(tmp_path / "ab-cli-cache"))

    def _offline(url, timeout):
        raise OSError("network disabled in unit tests")

    monkeypatch.setattr(cli_fetch, "_urllib_download", _offline)
    monkeypatch.setattr(cli_fetch, "_egress_check", lambda url: None)  # it resolves DNS otherwise
    cli_fetch.reset_state()
    chrome_install.reset_state()
    monkeypatch.setitem(rt._CHROME, "major", 0)
    yield
    cli_fetch.reset_state()
    chrome_install.reset_state()


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))


def _toolmap(cfg=None, **kw):
    return {t.name: t for t in tools.get_browser_tools(cfg or {}, **kw)}


# ── a subprocess.Popen stand-in for the tool wrappers ────────────────────────────
# _run() streams the child's pipes through drain threads under a byte cap, so the tool
# tests mock Popen (not run): BytesIO pipes yield the canned bytes, wait()/kill() drive
# the timeout + reap paths.


class _StdinSink(io.BytesIO):
    """A writable stdin pipe stand-in whose bytes survive `close()` — _run's feeder thread
    writes the script then closes the pipe, and the test still needs to read what arrived."""

    def close(self):
        pass  # keep the buffer readable via getvalue() after the run


class _FakeProc:
    """Minimal Popen: BytesIO pipes + wait/kill, enough for _run's drain loop."""

    def __init__(self, argv, out=b"", err=b"", rc=0, timeout=False):
        self._argv = list(argv)
        self.stdin = _StdinSink()
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self._rc = rc
        self._timeout = timeout  # make wait(timeout=…) raise until killed
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        if self._timeout and timeout is not None and not self.killed:
            raise subprocess.TimeoutExpired(cmd=self._argv[0], timeout=timeout)
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._rc
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def poll(self):
        return self.returncode


def fake_popen(out=b"", err=b"", rc=0, timeout=False, record=None, procs=None):
    """A subprocess.Popen stand-in: records argv, returns a _FakeProc whose pipes yield
    the canned bytes. Swallows the stdout=/stderr= PIPE kwargs the wrapper passes."""
    if isinstance(out, str):
        out = out.encode()
    if isinstance(err, str):
        err = err.encode()

    def _popen(argv, **kw):
        if record is not None:
            record.append(list(argv))
        p = _FakeProc(argv, out=out, err=err, rc=rc, timeout=timeout)
        if procs is not None:
            procs.append(p)
        return p

    return _popen


def fake_run(rc=0, stdout="ok", stderr="", record=None):
    """A subprocess.run stand-in: records the argv and returns a canned CompletedProcess."""

    def _run(args, **kw):
        if record is not None:
            record.append(list(args))
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    return _run


# ── the tools: arg-building ──────────────────────────────────────────────────────


async def test_open_passes_url_and_curated_launch_flags(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="OPENED", record=rec))
    # headless so argv is clean (headed injects anti-throttle --args; covered below)
    t = _toolmap({"binary": "ab", "allowed_domains": "x.com", "max_output": 500})
    out = await t["browser_open"].ainvoke({"url": "https://x.com"})
    assert "OPENED" in out
    assert rec[-1] == ["ab", "--allowed-domains", "x.com", "--max-output", "500", "open", "https://x.com"]


async def test_open_blank_url_omits_it(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    await _toolmap({"binary": "ab"})["browser_open"].ainvoke({})
    assert rec[-1] == ["ab", "open"]


async def test_action_tools_pass_refs(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    t = _toolmap({"binary": "ab"})
    await t["browser_click"].ainvoke({"selector": "@e2"})
    assert rec[-1] == ["ab", "click", "@e2"]
    await t["browser_fill"].ainvoke({"selector": "#q", "text": "hi there"})
    assert rec[-1] == ["ab", "fill", "#q", "hi there"]
    await t["browser_snapshot"].ainvoke({})
    assert rec[-1] == ["ab", "snapshot"]


def test_all_20_tools_present():
    names = set(_toolmap())
    assert names == EXPECTED_TOOLS
    # 16 standalone + browser_pdf (#3451) + browser_form_read (#4032 A1) + browser_select (#4032 A2)
    #   + browser_upload (#4032 A3)
    assert len(names) == 20
    assert "browser_dashboard" not in names  # the dashboard tool is gone (full switchover)


def test_every_tool_has_a_docstring_the_model_can_act_on():
    for name, t in _toolmap().items():
        assert t.description and len(t.description) >= 20, name


# ── the tools: graceful error degradation (a failed action informs, never crashes) ──


async def test_missing_binary_returns_install_hint(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(tools.subprocess, "Popen", boom)
    out = await _toolmap({"binary": "nope"})["browser_snapshot"].ainvoke({})
    assert "not on PATH" in out and "npm i -g agent-browser" in out


async def test_timeout_returns_readable_error_and_reaps_child(monkeypatch):
    procs = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(timeout=True, procs=procs))
    out = await _toolmap({"binary": "ab", "timeout_s": 1})["browser_snapshot"].ainvoke({})
    assert "timed out" in out
    assert procs[0].killed  # child terminated + reaped on timeout — never a zombie


async def test_nonzero_exit_surfaces_stderr(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(rc=2, err="boom"))
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke({"selector": "@e9"})
    assert out.startswith("Error:") and "boom" in out


# ── the tools: aggregate stdout+stderr byte cap (memory + context safety) ──────────


async def test_output_within_cap_is_unchanged(monkeypatch):
    # under the cap → behavior identical to before: raw stdout, stripped.
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="hello world\n"))
    out = await _toolmap({"binary": "ab", "max_response_bytes": 100})["browser_get_text"].ainvoke({"selector": "body"})
    assert out == "hello world"


async def test_output_over_cap_is_truncated_with_diagnostic(monkeypatch):
    procs = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=b"x" * 5000, procs=procs))
    t = _toolmap({"binary": "ab", "max_response_bytes": 100})
    out = await t["browser_get_text"].ainvoke({"selector": "body"})
    assert out == "Error: output exceeded 100 bytes (truncated)"
    assert procs[0].killed  # overflow kills the child cleanly


async def test_aggregate_stdout_plus_stderr_is_bounded(monkeypatch):
    # neither stream alone exceeds the cap, but together they do → still bounded.
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=b"a" * 60, err=b"b" * 60))
    out = await _toolmap({"binary": "ab", "max_response_bytes": 100})["browser_snapshot"].ainvoke({})
    assert out == "Error: output exceeded 100 bytes (truncated)"


async def test_configured_cap_overrides_default(monkeypatch):
    # a small configured cap trips where the 200KB default would not.
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=b"y" * 1000))
    out = await _toolmap({"binary": "ab", "max_response_bytes": 10})["browser_get_html"].ainvoke({})
    assert out == "Error: output exceeded 10 bytes (truncated)"


async def test_default_cap_is_200kb_when_unconfigured(monkeypatch):
    # no max_response_bytes key → 200000 default applies; 250KB overflows, and the
    # diagnostic names the default limit.
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=b"z" * 250_000))
    out = await _toolmap({"binary": "ab"})["browser_get_text"].ainvoke({"selector": "body"})
    assert out == "Error: output exceeded 200000 bytes (truncated)"


async def test_output_at_the_cap_is_not_truncated(monkeypatch):
    # exactly the cap is allowed through (only strictly-larger output overflows).
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=b"q" * 50))
    out = await _toolmap({"binary": "ab", "max_response_bytes": 50})["browser_get_text"].ainvoke({"selector": "body"})
    assert out == "q" * 50


# ── #3451 (a): the setup gap — a missing CLI is an operator banner, not a log line ──


def _probe_env(monkeypatch, *, which=None, chrome="pass", chrome_msg="Chrome 149 at /x",
               version="agent-browser 0.27.1", doctor_rc=0, doctor_out=None):
    """Point ``preflight`` at a synthetic CLI: ``which`` is what PATH resolution
    returns (None = missing), and ``doctor --json`` answers with one Chrome check."""
    monkeypatch.setattr(preflight.shutil, "which", lambda name: which)
    payload = doctor_out
    if payload is None:
        payload = json.dumps({"checks": [
            {"category": "Environment", "id": "env.version", "status": "pass", "message": "…"},
            {"category": "Chrome", "id": "chrome.installed", "status": chrome, "message": chrome_msg},
        ]})

    def _run(args, **kw):
        if args[1:2] == ["--version"]:
            return types.SimpleNamespace(returncode=0, stdout=version, stderr="")
        if args[1:2] == ["doctor"]:
            assert "--quick" in args and "--offline" in args  # no live launch, no network
            return types.SimpleNamespace(returncode=doctor_rc, stdout=payload, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", _run)


def test_preflight_reports_a_gap_when_the_cli_is_missing(monkeypatch):
    _probe_env(monkeypatch, which=None)
    reg = FakeRegistry({"binary": "agent-browser"}, plugin_id="agent_browser", plugin_dir=ROOT)
    probe = preflight.report(reg, reg.config)
    assert probe.cli_ok is False
    assert preflight.CLI_GAP in reg.setup_gaps
    msg = reg.setup_gaps[preflight.CLI_GAP]
    assert "isn't on PATH" in msg and "npm i -g agent-browser" in msg
    assert len(msg) <= 300  # the host truncates past MAX_MESSAGE_CHARS
    # a missing CLI says nothing about Chrome — one gap, not two
    assert preflight.CHROME_GAP not in reg.setup_gaps


def test_the_cli_gap_carries_a_config_action_the_host_actually_keeps(monkeypatch):
    """The projectBoard trap: a plugin that guesses ``actions=`` loses its Configure
    button silently. Assert the SINGULAR kwarg and that the host's real sanitizer keeps
    the action — not just that the fake captured something."""
    from graph.plugins import setup_gaps

    _probe_env(monkeypatch, which=None)
    reg = FakeRegistry({}, plugin_id="agent_browser", plugin_dir=ROOT)
    preflight.report(reg, reg.config)
    actions = reg.setup_gap_actions[preflight.CLI_GAP]
    # the Download button first, then the config fallback — both host-allowlisted kinds
    assert [a["kind"] for a in actions] == ["plugin_setup", "plugin_config"]
    assert all(a["kind"] in setup_gaps.ACTION_KINDS for a in actions)

    setup_gaps.reset()
    try:
        setup_gaps.report("agent_browser", preflight.CLI_GAP, "cli missing", label="Agent Browser", action=actions)
        [gap] = setup_gaps.active()
        # survived sanitizing WITH their labels, step and highlighted field — the real contract
        assert gap["actions"] == [
            {"kind": "plugin_setup", "target": "agent_browser", "step": "download-cli",
             "label": "Download agent-browser"},
            {"kind": "plugin_config", "target": "agent_browser", "label": "Set the CLI path", "fields": ["binary"]}]
        assert setup_gaps.warnings() == ["Agent Browser: cli missing"]
    finally:
        setup_gaps.reset()


def test_preflight_reports_chrome_separately_from_the_cli(monkeypatch):
    _probe_env(monkeypatch, which="/usr/local/bin/agent-browser", chrome="fail",
               chrome_msg="No Chrome install found")
    reg = FakeRegistry({}, plugin_id="agent_browser", plugin_dir=ROOT)
    probe = preflight.report(reg, reg.config)
    assert probe.cli_ok is True and probe.chrome == "missing"
    assert preflight.CLI_GAP not in reg.setup_gaps          # the CLI is fine
    chrome_msg = reg.setup_gaps[preflight.CHROME_GAP]
    assert "agent-browser install" in chrome_msg and "No Chrome install found" in chrome_msg
    # the Chrome fix is a CLI command — now a button that runs it (a `plugin_setup` step)
    assert reg.setup_gap_actions[preflight.CHROME_GAP] == {
        "kind": "plugin_setup", "step": preflight.STEP_INSTALL_CHROME, "label": "Install Chrome"}


def test_preflight_clears_both_gaps_when_everything_resolves(monkeypatch):
    _probe_env(monkeypatch, which="/opt/ab", chrome="pass")
    reg = FakeRegistry({}, plugin_id="agent_browser", plugin_dir=ROOT)
    probe = preflight.report(reg, reg.config)
    assert (probe.cli_ok, probe.chrome, probe.cli_version) == (True, "ok", "agent-browser 0.27.1")
    assert reg.setup_gaps == {}   # reporting None is what makes the banner self-heal


def test_preflight_gap_self_heals_across_probes(monkeypatch):
    reg = FakeRegistry({}, plugin_id="agent_browser", plugin_dir=ROOT)
    _probe_env(monkeypatch, which=None)
    preflight.report(reg, reg.config)
    assert preflight.CLI_GAP in reg.setup_gaps
    _probe_env(monkeypatch, which="/opt/ab")          # operator installed it
    preflight.report(reg, reg.config)
    assert reg.setup_gaps == {}                        # banner gone, no restart


@pytest.mark.parametrize("doctor", ["not json at all", json.dumps({"checks": []}), ""])
def test_an_unreadable_doctor_never_invents_a_chrome_gap(monkeypatch, doctor):
    """An older CLI (no ``--json``), a crash, or a check set without the Chrome id →
    ``unknown``. A false banner about the operator's browser is worse than none."""
    _probe_env(monkeypatch, which="/opt/ab", doctor_out=doctor, doctor_rc=2)
    reg = FakeRegistry({}, plugin_id="agent_browser", plugin_dir=ROOT)
    probe = preflight.report(reg, reg.config)
    assert probe.chrome == "unknown"
    assert reg.setup_gaps == {}


def test_a_wedged_binary_cannot_stall_boot_past_the_preflight_budget(monkeypatch):
    """The preflight runs inside register(), i.e. at boot. Two serial probes at 15 s each
    let a hung binary stall boot ~30 s. The budget is now TOTAL across both calls. Simulated
    with a clock the wedged binary advances by its full allowance on every call."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(preflight.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/opt/ab")
    given = []

    def wedged(args, **kw):
        given.append(kw["timeout"])
        clock["t"] += kw["timeout"]   # burns every second it was allowed
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kw["timeout"])

    monkeypatch.setattr(preflight.subprocess, "run", wedged)
    probe = preflight.probe({})
    assert probe.cli_ok and probe.chrome == "unknown"          # degraded, never raised
    assert sum(given) <= preflight.PREFLIGHT_BUDGET_S + 1e-9, given
    assert preflight.PREFLIGHT_BUDGET_S <= 10                   # a boot-time budget, not a leash


def test_preflight_never_raises_when_the_probe_explodes(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/opt/ab")

    def boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(preflight.subprocess, "run", boom)
    probe = preflight.probe({})                                  # degraded, never raised…
    # …and an OSError from STARTING the binary is a real gap, not "healthy": a CLI that
    # can't be exec'd is as useless as a missing one (#3451 round 3)
    # cli_path is the RESOLVED location, so it is platform-native (`\opt\ab` on Windows):
    # compare paths, not strings. The operator's configured value is `binary`, and THAT one
    # is echoed verbatim — see the next test.
    assert Path(probe.cli_path) == Path("/opt/ab") and probe.cli_ok is False and probe.chrome == "unknown"
    assert "exec format error" in probe.cli_error
    assert "can't be started" in preflight.hint(probe)


@pytest.mark.parametrize("configured", ["C:/Tools/agent-browser.exe", r"C:\Tools\agent-browser.exe",
                                        "~/bin/agent browser"])
def test_the_operators_configured_binary_is_echoed_verbatim(monkeypatch, configured):
    """The one path the plugin must NOT normalise is the operator's own `binary` setting:
    the banner quotes it back so they can recognise — and fix — exactly what they typed.
    Only the RESOLVED location (`cli_path`) is platform-native (a Windows CI failure, #3451)."""
    _probe_env(monkeypatch, which=None)
    reg = FakeRegistry({"binary": configured}, plugin_id="agent_browser", plugin_dir=ROOT)
    probe = preflight.report(reg, reg.config)
    assert probe.binary == configured
    assert repr(configured) in reg.setup_gaps[preflight.CLI_GAP]


def test_preflight_resolves_an_operator_pinned_absolute_path(monkeypatch, tmp_path):
    """The desktop case: the Tauri shell may not see an nvm CLI on PATH, so an operator
    pins a full path. ``which`` misses that; the fallback must not."""
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    pinned = tmp_path / "bin" / "agent-browser"
    pinned.parent.mkdir()
    pinned.write_text("#!/bin/sh\n", encoding="utf-8")
    pinned.chmod(0o755)
    monkeypatch.setattr(preflight.subprocess, "run",
                        lambda args, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert preflight.resolve_binary(str(pinned)) == str(pinned.resolve())
    assert preflight.resolve_binary(str(tmp_path / "bin" / "nope")) == ""
    assert preflight.resolve_binary("") == ""


def test_preflight_tolerates_a_host_without_the_seam(monkeypatch):
    """A plugin that also runs on an older host guards with getattr — so a registry with
    no ``report_setup_gap`` must still probe cleanly."""
    _probe_env(monkeypatch, which=None)

    class OldHost:
        config: dict = {}

    probe = preflight.report(OldHost(), {})
    assert probe.cli_ok is False


async def test_a_tool_run_refreshes_the_gap_when_the_cli_vanishes(monkeypatch):
    """Boot-time reporting can't catch a CLI uninstalled mid-session, so the wrapper
    re-reports on FileNotFoundError — and clears it on the next success."""
    calls = []

    def boom(argv, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(tools.subprocess, "Popen", boom)
    t = _toolmap({"binary": "no-such-agent-browser"}, refresh_gaps=lambda: calls.append("refresh"))
    out = await t["browser_snapshot"].ainvoke({})
    assert calls == ["refresh"] and "setup banner" in out
    await t["browser_snapshot"].ainvoke({})
    assert calls == ["refresh"]                         # a failing LOOP probes once, not per call

    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="tree"))
    assert await t["browser_snapshot"].ainvoke({}) == "tree"
    assert calls == ["refresh", "refresh"]              # cleared on the first success
    await t["browser_snapshot"].ainvoke({})
    assert calls == ["refresh", "refresh"]              # …and not re-run every call


async def test_a_nonzero_exit_also_refreshes_the_gap_so_chrome_can_self_heal(monkeypatch):
    """The CLI-present-but-Chrome-missing case never raises FileNotFoundError — it exits
    non-zero. Keying the re-probe only on FileNotFoundError left that banner stuck up
    forever (it could only be cleared by a fresh `register()`), while the README and the
    guide both promised it self-heals."""
    calls = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(rc=1, err="no Chrome installation found"))
    t = _toolmap({"binary": "ab"}, refresh_gaps=lambda: calls.append("refresh"))
    out = await t["browser_open"].ainvoke({"url": "https://x.com"})
    assert out.startswith("Error:") and calls == ["refresh"]

    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok"))   # operator ran the install
    await t["browser_open"].ainvoke({"url": "https://x.com"})
    assert calls == ["refresh", "refresh"]              # the Chrome banner clears on the next call


async def test_a_steady_state_success_never_re_probes(monkeypatch):
    calls = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok"))
    t = _toolmap({"binary": "ab"}, refresh_gaps=lambda: calls.append("refresh"))
    for _ in range(3):
        await t["browser_snapshot"].ainvoke({})
    assert calls == []   # nothing was ever wrong — no preflight subprocesses per call


async def test_a_banner_raised_at_boot_clears_on_the_first_good_call(monkeypatch):
    """Boot found no Chrome, the operator ran `agent-browser install`, calls succeed. The
    tools used to start out assuming healthy, so with no FAILED call first nothing ever
    re-probed and the banner stayed up for the life of the process."""
    calls = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok"))
    t = _toolmap({"binary": "ab"}, refresh_gaps=lambda: calls.append("refresh"), start_gap=True)
    await t["browser_snapshot"].ainvoke({})
    assert calls == ["refresh"]
    await t["browser_snapshot"].ainvoke({})
    assert calls == ["refresh"]   # cleared: steady state again


@pytest.mark.parametrize("boot", ["cli", "chrome"])
async def test_register_seeds_the_tools_with_the_boot_gap_end_to_end(monkeypatch, boot):
    """The same bug through the real wiring: register() with a broken setup raises the
    banner; the operator fixes it; the next successful call clears it with no restart."""
    if boot == "cli":
        _probe_env(monkeypatch, which=None)
    else:
        _probe_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome")
    reg = _registry({"binary": "ab"})
    _PKG.register(reg)
    assert reg.setup_gaps, "boot should have raised a banner"
    _probe_env(monkeypatch, which="/opt/ab", chrome="pass")         # fixed before any call
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok"))
    tool = next(x for x in reg.tools if x.name == "browser_snapshot")
    assert await tool.ainvoke({}) == "ok"
    assert reg.setup_gaps == {}                                   # cleared, no restart


async def test_routine_failures_do_not_re_probe_on_every_call(monkeypatch):
    """A missed click exits non-zero just like a setup problem. With the probe saying the
    setup is fine, 20 alternating failures and successes must not run 20 preflights."""
    calls = []
    healthy = preflight.Probe(binary="ab", cli_path="/opt/ab", chrome="ok")
    state = {"n": 0}

    def alternating(argv, **kw):
        state["n"] += 1
        return _FakeProc(argv, out=b"ok", err=b"no element", rc=state["n"] % 2)

    monkeypatch.setattr(tools.subprocess, "Popen", alternating)
    t = _toolmap({"binary": "ab"}, refresh_gaps=lambda: calls.append("r") or healthy)
    for _ in range(20):
        await t["browser_click"].ainvoke({"selector": "@e9"})
    assert calls == ["r"]   # one look, then the probe's "fine" verdict holds


async def test_a_vanished_cli_bypasses_the_failure_rate_limit(monkeypatch):
    calls = []
    healthy = preflight.Probe(binary="ab", cli_path="/opt/ab", chrome="ok")
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(rc=1, err="no element"))
    t = _toolmap({"binary": "ab"}, refresh_gaps=lambda: calls.append("r") or healthy)
    await t["browser_click"].ainvoke({"selector": "@e9"})         # routine failure: one look
    assert calls == ["r"]

    def boom(argv, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(tools.subprocess, "Popen", boom)
    await t["browser_click"].ainvoke({"selector": "@e9"})         # inside the window, but unambiguous
    assert calls == ["r", "r"]


async def test_a_failing_gap_refresh_never_breaks_the_tool(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError()

    def explode():
        raise RuntimeError("registry gone")

    monkeypatch.setattr(tools.subprocess, "Popen", boom)
    out = await _toolmap({"binary": "no-such-agent-browser"}, refresh_gaps=explode)["browser_click"].ainvoke({"selector": "x"})
    assert "not on PATH" in out


@pytest.mark.parametrize("bad", [-1, 0, "", "nope", None])
async def test_a_garbage_response_cap_falls_back_instead_of_bricking_every_command(monkeypatch, bad):
    """`max_response_bytes: -1` made EVERY command fail "output exceeded -1 bytes" —
    a config typo that silently disables the whole toolset."""
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="hello"))
    out = await _toolmap({"binary": "ab", "max_response_bytes": bad})["browser_snapshot"].ainvoke({})
    assert out == "hello"


# ── #3451 (c): the capture fence — `filesystem: scoped` is now enforced ────────────


def test_capture_root_is_this_plugin_s_own_instance_store():
    from infra.paths import instance_paths

    root = storage.capture_root()
    assert root == instance_paths().store("agent_browser") / "captures"
    assert root.is_dir()   # created on demand


def test_a_bare_filename_lands_in_the_fence():
    p = storage.resolve_capture_path("home.png", default_name="page.png")
    assert p.parent == storage.capture_root().resolve() and p.name == "home.png"


def test_a_blank_path_uses_the_default_name():
    assert storage.resolve_capture_path("", default_name="page.pdf").name == "page.pdf"
    assert storage.resolve_capture_path(None, default_name="page.pdf").name == "page.pdf"


def test_a_relative_subdirectory_is_created_inside_the_fence():
    p = storage.resolve_capture_path("runs/2026/shot.png", default_name="page.png")
    root = storage.capture_root().resolve()
    assert p.is_relative_to(root) and p.parent.is_dir()


@pytest.mark.parametrize("bad", [
    "../escape.png",
    "../../../../etc/passwd",
    "a/../../escape.png",
    "/etc/passwd",
    "~/.ssh/authorized_keys",
    ".",
])
def test_traversal_and_absolute_paths_are_refused(bad):
    with pytest.raises(ValueError) as e:
        storage.resolve_capture_path(bad, default_name="page.png")
    assert "refusing to write outside" in str(e.value)


def test_a_symlink_pointing_out_of_the_fence_is_refused(tmp_path):
    root = storage.capture_root()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    with pytest.raises(ValueError):
        storage.resolve_capture_path("escape/shot.png", default_name="page.png")


def test_an_absolute_path_already_inside_the_fence_is_accepted():
    """So an agent can re-use a path a previous call handed back."""
    first = storage.resolve_capture_path("shot.png", default_name="page.png")
    assert storage.resolve_capture_path(str(first), default_name="page.png") == first


def _writing_popen(data=b"bytes", record=None, rc=0, url="https://example.test/", content="1"):
    """A scripted CLI. `get url` answers `url` (a page is open); `screenshot` / `pdf`
    WRITE `data` to the requested path (None = write nothing — even on a failing run, to
    model a partial write) and exit `rc`. So the post-run checks see what a real run leaves."""
    def _popen(argv, **kw):
        if record is not None:
            record.append(list(argv))
        if argv[1:3] == ["get", "url"]:
            return _FakeProc(argv, out=url.encode())
        if argv[1] == "eval":   # the page-has-content check
            return _FakeProc(argv, out=content.encode())
        if argv[1] in ("screenshot", "pdf"):
            if data is not None:
                Path(argv[2]).write_bytes(data)
            return _FakeProc(argv, out=b"(ok)" if rc == 0 else b"", err=b"" if rc == 0 else b"boom", rc=rc)
        return _FakeProc(argv, out=b"ok")

    return _popen


async def test_screenshot_passes_the_fenced_path_to_the_cli(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_screenshot"].ainvoke({"path": "shot.png"})
    root = storage.capture_root().resolve()
    assert rec[-1][:2] == ["ab", "screenshot"]
    written = Path(rec[-1][2])     # the CLI writes a short temp name beside the target…
    assert written.parent == root and written.suffix == ".png" and written.name.startswith(".")
    assert (root / "shot.png").is_file() and not written.exists()   # …swapped into place
    assert str(root / "shot.png") in out   # the absolute path, for save_file_artifact


async def test_screenshot_refuses_an_escaping_path_without_running_the_cli(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_screenshot"].ainvoke({"path": "/tmp/x.png"})
    assert out.startswith("Error:") and "refusing to write outside" in out
    assert rec == []   # the fence runs BEFORE the subprocess — Chrome never sees the path


async def test_a_failed_capture_command_is_not_reported_as_saved(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(rc=1, err="no page open"))
    out = await _toolmap({"binary": "ab"})["browser_screenshot"].ainvoke({"path": "shot.png"})
    assert out.startswith("Error:") and "Saved to" not in out


# The CLI can exit 0 having written nothing — "Saved to" must mean bytes on disk.


async def test_a_silent_no_write_is_reported_as_an_error_not_a_save(monkeypatch):
    """Exit 0, no file. Reporting success sent the agent to save_file_artifact, which
    answered "No file at … write the file first" — a dead end two tools from the cause."""
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="(ok)"))   # writes nothing
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "silent.pdf"})
    assert out.startswith("Error:") and "wrote no file" in out
    assert "Saved to" not in out and "browser_open" in out
    assert not (storage.capture_root().resolve() / "silent.pdf").exists()


async def test_a_zero_byte_capture_is_refused_and_cleaned_up(monkeypatch):
    """Worse than nothing: stored, then downloaded as a broken PDF."""
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b""))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "empty.pdf"})
    assert out.startswith("Error:") and "an empty file" in out
    assert not (storage.capture_root().resolve() / "empty.pdf").exists()   # never swapped in
    assert not [p for p in storage.capture_root().iterdir() if p.name.startswith(".")]   # temp dropped


async def test_a_failed_run_does_not_delete_a_pre_existing_file(monkeypatch):
    """Cleanup removes the partial file this call created — never one that was already
    there (an earlier good capture the agent may still be holding a path to)."""
    keep = storage.capture_root().resolve() / "keep.pdf"
    keep.write_bytes(b"%PDF-1.4 previous")
    # a page IS open, and the failed run even leaves partial bytes at the target
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"partial", rc=1))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "keep.pdf"})
    assert out.startswith("Error:") and keep.read_bytes() == b"%PDF-1.4 previous"
    assert not [p for p in keep.parent.iterdir() if p.name.startswith(".")]   # no temp left behind


async def test_a_re_export_that_writes_nothing_is_not_passed_off_as_the_old_file(monkeypatch):
    """The resume flow re-exports to the same name. A run that exits 0 but writes nothing
    used to leave the OLD file in place — non-empty, so indistinguishable — and the tool
    said "Saved to …/resume.pdf" over last time's page."""
    target = storage.capture_root().resolve() / "resume.pdf"
    target.write_bytes(b"%PDF-1.4 OLD-PAGE")
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=None))   # exit 0, writes nothing
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "resume.pdf"})
    assert out.startswith("Error:") and "wrote no file" in out and "Saved to" not in out
    assert target.read_bytes() == b"%PDF-1.4 OLD-PAGE"          # the previous capture survives
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]


async def test_a_successful_re_export_replaces_the_old_file(monkeypatch):
    target = storage.capture_root().resolve() / "resume.pdf"
    target.write_bytes(b"%PDF-1.4 OLD-PAGE")
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"%PDF-1.4 NEW-PAGE"))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "resume.pdf"})
    assert "Saved to" in out and target.read_bytes() == b"%PDF-1.4 NEW-PAGE"
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".")]   # no temp left behind


async def test_printing_an_empty_blank_page_is_refused_before_printing(monkeypatch):
    """With no page, the real CLI exits 0 and writes a blank ~860-byte PDF of about:blank,
    which passes any size check — so an EMPTY blank page is refused up front."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(url="about:blank", content="0", record=rec))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "blank.pdf"})
    assert out.startswith("Error:") and "blank" in out
    # the way through for an agent that has HTML but no URL
    assert "browser_open" in out and "data:text/html" in out and "file://" in out and "browser_eval" in out
    assert [a[1] for a in rec] == ["get", "eval"]                # never reached `pdf`
    assert not (storage.capture_root().resolve() / "blank.pdf").exists()


async def test_a_blank_page_the_agent_wrote_content_into_is_printed(monkeypatch):
    """The misfire: open a blank page, write a report into it with browser_eval, print it.
    The URL is still about:blank, but the page is not empty — so it must print."""
    monkeypatch.setattr(tools.subprocess, "Popen",
                        _writing_popen(url="about:blank", content="1", data=b"%PDF report"))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "report.pdf"})
    assert "Saved to" in out
    assert (storage.capture_root().resolve() / "report.pdf").read_bytes() == b"%PDF report"


async def test_prune_never_deletes_the_capture_it_just_saved(monkeypatch):
    """A capture bigger than the whole retention budget used to prune ITSELF, and the tool
    still said "Saved to"."""
    monkeypatch.setattr(storage, "MAX_CAPTURE_BYTES", 10)
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"x" * 100))
    out = await _toolmap({"binary": "ab"})["browser_screenshot"].ainvoke({"path": "huge.png"})
    assert "Saved to" in out and (storage.capture_root().resolve() / "huge.png").is_file()


async def test_an_oversized_capture_warns_about_the_artifact_limit(monkeypatch):
    """A 40 MB PDF writes fine but save_file_artifact refuses it by default — say so here,
    where the agent can still act on it."""
    big = b"x" * (storage.ARTIFACT_BLOB_LIMIT_BYTES + 1024)
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=big))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "big.pdf"})
    assert "Saved to" in out and "max_blob_kb" in out and "25 MB limit" in out


async def test_captures_are_pruned_to_the_retention_budget(monkeypatch):
    root = storage.capture_root().resolve()
    for i in range(6):
        (root / f"old{i}.png").write_bytes(b"x")
    monkeypatch.setattr(storage, "MAX_CAPTURE_FILES", 3)
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"new"))
    out = await _toolmap({"binary": "ab"})["browser_screenshot"].ainvoke({"path": "fresh.png"})
    assert "Saved to" in out
    remaining = sorted(p.name for p in root.iterdir() if p.is_file())
    assert len(remaining) == 3 and "fresh.png" in remaining   # oldest-first, newest kept


def test_pruning_never_raises_on_an_unreadable_store(monkeypatch):
    monkeypatch.setattr(storage, "capture_root", lambda: (_ for _ in ()).throw(OSError("gone")))
    assert storage.prune_captures() == 0


def test_an_unnamed_capture_gets_a_unique_filename():
    """Concurrent `browser_pdf()` calls all defaulted to `page.pdf` and clobbered each
    other — the second caller handed save_file_artifact the first caller's page."""
    names = {storage.unique_default_name("page.pdf") for _ in range(200)}
    assert len(names) == 200
    for n in names:
        assert n.startswith("page-") and n.endswith(".pdf")


# ── #3451 (d): browser_pdf — the HTML→PDF capability, fenced like screenshots ──────


async def test_pdf_wraps_the_cli_pdf_command(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"%PDF-1.4", record=rec))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "resume.pdf"})
    assert rec[-1][:2] == ["ab", "pdf"]
    assert (storage.capture_root().resolve() / "resume.pdf").read_bytes() == b"%PDF-1.4"
    assert "Saved to" in out


async def test_pdf_defaults_to_a_collision_free_filename(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"%PDF", record=rec))
    t = _toolmap({"binary": "ab"})
    outs = [await t["browser_pdf"].ainvoke({}), await t["browser_pdf"].ainvoke({})]
    first, second = (Path(o.split("Saved to ", 1)[1].splitlines()[0]).name for o in outs)
    assert first != second, "two unnamed captures must not clobber each other"
    for name in (first, second):
        assert re.fullmatch(r"page-\d{8}-\d{6}-\d+-[0-9a-f]{6}\.pdf", name), name


async def test_pdf_is_fenced_exactly_like_screenshot(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "../../out.pdf"})
    assert out.startswith("Error:") and "refusing to write outside" in out
    assert rec == []


# ── a model-supplied operand may not look like a CLI option ──────────────────────
# Verified against the real 0.27.1 (live tests below): the CLI scans the WHOLE argv for
# options it recognises — `fill '#q' '--help'` prints help and fills NOTHING, and
# `--headed` / `--allow-file-access` would take effect on a first launch — but passes
# everything else through untouched (`-5`, `-$50.00`, `- buy milk`, `-`). So the guard is
# the option grammar (a dash, then a letter), not "any leading dash".


@pytest.mark.parametrize(("name", "args"), [
    ("browser_open", {"url": "--auto-connect"}),
    ("browser_click", {"selector": "--help"}),
    ("browser_fill", {"selector": "--help", "text": "hi"}),
    ("browser_fill", {"selector": "#q", "text": "--allow-file-access"}),
    ("browser_type", {"selector": "#q", "text": "-x"}),
    ("browser_press", {"key": "--help"}),
    ("browser_hover", {"selector": "-a"}),
    ("browser_get_text", {"selector": "--help"}),
    ("browser_get_html", {"selector": "--help"}),
    ("browser_get_value", {"selector": "--help"}),
    ("browser_eval", {"expression": "--help"}),
    ("browser_fill", {"selector": "#q", "text": "--headed"}),
    ("browser_type", {"selector": "#q", "text": "-h"}),
    ("browser_select", {"field": "--help", "option_text": "x"}),
    ("browser_select", {"field": "#c", "option_text": "--allow-file-access"}),
])
async def test_an_operand_that_reads_as_a_flag_is_refused_before_the_subprocess(monkeypatch, name, args):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})[name].ainvoke(args)
    assert out.startswith("Error:") and "looks like a command-line option" in out
    assert rec == [], "the refusal must happen before the CLI runs"


@pytest.mark.parametrize(("name", "args", "argv_tail"), [
    ("browser_fill", {"selector": "#amount", "text": "-5"}, ["fill", "#amount", "-5"]),
    ("browser_fill", {"selector": "#amount", "text": "-$50.00"}, ["fill", "#amount", "-$50.00"]),
    ("browser_type", {"selector": "#todo", "text": "- buy milk"}, ["type", "#todo", "- buy milk"]),
    ("browser_fill", {"selector": "#q", "text": "-"}, ["fill", "#q", "-"]),
    ("browser_fill", {"selector": "#q", "text": "--"}, ["fill", "#q", "--"]),
    ("browser_fill", {"selector": "@e2", "text": "a -5% drop"}, ["fill", "@e2", "a -5% drop"]),
    ("browser_press", {"key": "-"}, ["press", "-"]),
    ("browser_press", {"key": "Control+a"}, ["press", "Control+a"]),
    # browser_eval's dash-value pass-through is covered by the stdin tests below (the
    # expression rides stdin now, not argv, so there is no argv tail to assert here).
])
async def test_dash_values_the_cli_does_not_swallow_pass_through(monkeypatch, name, args, argv_tail):
    """Negative amounts (finance, merchantAgent), list bullets and the minus key — all of
    which the first version of the guard refused, though the CLI handles them fine."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok", record=rec))
    out = await _toolmap({"binary": "ab"})[name].ainvoke(args)
    assert not out.startswith("Error:"), out
    assert rec[-1] == ["ab", *argv_tail]


@pytest.mark.parametrize(("name", "args", "hint"), [
    ("browser_fill", {"selector": "#q", "text": "--foo"}, "browser_eval"),
    ("browser_press", {"key": "-a"}, "Minus"),
    ("browser_eval", {"expression": "-a"}, "(-a)"),
])
async def test_a_refusal_says_how_to_do_it_anyway(monkeypatch, name, args, hint):
    """Flag-shaped text the CLI happens not to know (`--foo`) is refused on purpose — the
    rule is the grammar, so an option upstream adds tomorrow is covered today. The message
    has to give the way through."""
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok"))
    out = await _toolmap({"binary": "ab"})[name].ainvoke(args)
    assert out.startswith("Error:") and hint in out


# ── #3689: browser_eval sends the script over stdin, never as an argv item ─────────
# A script as an argv item overruns Windows' 32,767-char command-line cap after quoting
# (CreateProcess WinError 206) before the CLI runs — design-system-plugin's 32,439-char
# SITE_PROBE_JS built a 33,429-char command line. The fix: `agent-browser eval --stdin`
# with the script on stdin, always, no size threshold.


async def test_eval_sends_the_script_over_stdin_not_argv(monkeypatch):
    rec, procs = [], []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="42", record=rec, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_eval"].ainvoke({"expression": "40 + 2"})
    assert out == "42"
    assert rec[-1] == ["ab", "eval", "--stdin"]           # the script is NOT an argv item…
    assert procs[-1].stdin.getvalue() == b"40 + 2"        # …it arrives on stdin


async def test_a_100k_eval_script_stays_under_the_windows_command_line_limit(monkeypatch):
    """The bug shape, pinned: for a 100,000-char script the issued command line must stay
    well under Windows' 32,767-char CreateProcess limit — which it can only do if the
    script rides stdin, not argv."""
    rec, procs = [], []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok", record=rec, procs=procs))
    script = "1;" + "a".ljust(100_000, "a")               # 100,002 non-flag-shaped chars
    await _toolmap({"binary": "ab"})["browser_eval"].ainvoke({"expression": script})
    argv = rec[-1]
    assert argv == ["ab", "eval", "--stdin"] and script not in argv    # never on the command line
    assert len(subprocess.list2cmdline(argv)) < 32_767               # Windows CreateProcess cap
    assert procs[-1].stdin.getvalue() == script.encode()             # delivered via stdin


async def test_eval_dash_values_pass_the_guard_and_ride_stdin(monkeypatch):
    """The dash-value pass-through for eval (`-1`, `(-1) + 2`) that used to be an argv-tail
    assertion: still accepted by bad_operand, now delivered on stdin."""
    procs = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="-1", procs=procs))
    t = _toolmap({"binary": "ab"})
    for expr in ("-1", "(-1) + 2"):
        out = await t["browser_eval"].ainvoke({"expression": expr})
        assert not out.startswith("Error:"), out
        assert procs[-1].stdin.getvalue() == expr.encode()


async def test_the_capture_content_probe_uses_stdin(monkeypatch):
    """The page-has-content probe capture runs on an about:blank page takes the same
    Windows-safe path: `eval --stdin` with its JS on stdin, not in argv."""
    seen = []

    def _popen(argv, **kw):
        if argv[1:3] == ["get", "url"]:
            return _FakeProc(argv, out=b"about:blank")
        p = _FakeProc(argv, out=b"0")            # eval → page empty → capture refused up front
        if argv[1] == "eval":
            seen.append((list(argv), p))
        return p

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "blank.pdf"})
    assert out.startswith("Error:") and "blank" in out
    [(argv, proc)] = seen
    assert argv == ["ab", "eval", "--stdin"]
    assert proc.stdin.getvalue() == tools._PAGE_HAS_CONTENT_JS.encode()


def test_every_pdf_surface_says_the_output_is_us_letter():
    """`agent-browser pdf` has no paper-size option and ignores CSS `@page size` (pinned
    against the real binary below). An agent that promises A4 hands the user the wrong
    paper, so the tool, the skill and the guide all have to say Letter."""
    assert "US Letter" in _toolmap()["browser_pdf"].description
    assert "US Letter" in _skill_text()
    guide = (REPO / "docs" / "guides" / "browser-automation.md").read_text(encoding="utf-8")
    assert "always US Letter" in guide


def test_pdf_tells_the_model_about_the_artifact_handoff():
    """The reason browser_pdf exists: print a page, then hand the file to the artifact
    plugin. If the docstring stops saying so, the capability is undiscoverable."""
    desc = _toolmap()["browser_pdf"].description
    assert "save_file_artifact" in desc and "PDF" in desc


# ── #4032: label-located fields + browser_form_read (forms.py) ─────────────────────
# Host-free: the in-page JS never runs here — the tool tests mock Popen and assert on the
# stdin script + the parse of canned eval output; the label-matching RANKING is pure Python
# (forms.match_fields) and is exercised directly.


# A canned `abEnumerate` payload — the shape the in-page JS prints — covering every kind. The
# radio group is TWO per-option entries (what the JS now emits so each option is independently
# addressable); the read renderer folds them back into one radio-group row.
_FORM_FIELDS = [
    {"idx": 0, "label": "First name", "labels": ["First name", "first_name"], "kind": "text",
     "name": "first_name", "id": "fn", "required": True, "value": "Ada", "selector": '[data-ab-field="0"]'},
    {"idx": 1, "label": "Email", "labels": ["Email", "email"], "kind": "email",
     "name": "email", "id": "em", "required": True, "value": "ada@x.io", "selector": '[data-ab-field="1"]'},
    {"idx": 2, "label": "Country", "labels": ["Country"], "kind": "combobox",
     "name": "country", "id": "cty", "required": False, "value": "United States", "selector": '[data-ab-field="2"]'},
    {"idx": 3, "label": "Role", "labels": ["Role"], "kind": "native-select",
     "name": "role", "id": "role", "required": False, "value": "Engineer",
     "options": ["", "Engineer", "Manager"], "selector": '[data-ab-field="3"]'},
    {"idx": 4, "label": "Junior", "labels": ["Junior", "Seniority", "sen"], "kind": "radio-group",
     "name": "sen", "id": "sen_j", "required": True, "value": "Junior", "selector": '[data-ab-field="4"]',
     "group": "sen", "groupLabel": "Seniority", "optionLabel": "Junior", "checked": False},
    {"idx": 5, "label": "Senior", "labels": ["Senior", "Seniority", "sen"], "kind": "radio-group",
     "name": "sen", "id": "sen_s", "required": True, "value": "Senior", "selector": '[data-ab-field="5"]',
     "group": "sen", "groupLabel": "Seniority", "optionLabel": "Senior", "checked": True},
    {"idx": 6, "label": "Résumé", "labels": ["Résumé", "resume"], "kind": "file",
     "name": "resume", "id": "rz", "required": False, "value": "cv.pdf", "selector": '[data-ab-field="6"]'},
    {"idx": 7, "label": "Subscribe", "labels": ["Subscribe", "subscribe"], "kind": "checkbox",
     "name": "sub", "id": "sb", "required": False, "value": True, "selector": '[data-ab-field="7"]'},
]


def _field(label, labels=None, kind="text", name="", selector=None, **kw):
    d = {"label": label, "labels": labels if labels is not None else [label], "kind": kind,
         "name": name, "selector": selector or f"[sel-{label}]"}
    d.update(kw)
    return d


# ── forms.classify / normalize — pure string routing ──────────────────────────────


@pytest.mark.parametrize(("field", "kind"), [
    ("@e5", "ref"), ("@e123", "ref"), ("  @e9 ", "ref"),
    ("#email", "css"), (".form-control", "css"), ("[name='email']", "css"),
    # a combinator routes to CSS only when it ANCHORS real compound chains on both sides —
    # `div`/`input`/`a`/`b`/`li` are HTML tags, so these are genuine selectors.
    ("div > input", "css"), ("a + b", "css"), ("li ~ a", "css"), ("input+label", "css"),
    (".parent > .child", "css"), ("input > .foo", "css"),
    # tag-led selectors that worked before this plugin learned labels must still reach the CLI
    # as CSS — not be mistaken for a label the form has no field for (#4032 review).
    ("button[type=submit]", "css"), ("input[name='email']", "css"), ("textarea", "css"),
    ("select#country", "css"), ("form input", "css"), ("input:checked", "css"), ("*", "css"),
    # a space inside an attribute value must not fracture the selector into "label words"
    ('input[aria-label="First name"]', "css"),
    ("Email", "label"), ("First name", "label"), ("Résumé", "label"), ("  Email  ", "label"),
    # a combinator CHARACTER inside a visible label must NOT route to CSS off a bare substring —
    # the Greenhouse tech-job fields the PR exists to fill ("C++ experience") were being passed
    # to the CLI as broken selectors (#4032 review, blocking). An unanchored combinator (no real
    # compound on one side, or a non-tag word beside it) stays a LABEL.
    ("C++ experience", "label"), ("C++", "label"), ("Years of C++", "label"),
    ("Rust > Go preference", "label"), ("~5 years", "label"), ("Pros + Cons", "label"),
    ("x ~ y", "label"),   # `x`/`y` are not HTML tags → not a real sibling selector
    # labels that happen to collide with a tag name stay LABELS (tag match is case-sensitive,
    # so a capitalised word is never a bare type selector) — the regression the review caught.
    ("Address", "label"), ("Time", "label"), ("Select one", "label"), ("Full name", "label"),
])
def test_classify_routes_refs_css_and_labels(field, kind):
    assert forms.classify(field) == kind
    assert forms.is_ref(field) == (kind == "ref")
    assert forms.is_css(field) == (kind == "css")


def test_normalize_collapses_whitespace_strips_asterisks_and_lowercases():
    assert forms.normalize("  Email   Address * ") == "email address"
    assert forms.normalize("First\nName*") == "first name"
    assert forms.normalize("") == ""


# ── forms.match_fields — the label ranking (exact > prefix > substring; never guess) ──


def test_match_exact_beats_prefix_and_resolves_uniquely():
    fields = [_field("Email address"), _field("Email", selector="[email]")]
    m = forms.match_fields("Email", fields)
    assert m.ok and m.selector == "[email]" and m.label == "Email"


def test_match_prefix_beats_substring():
    fields = [_field("Please enter your email"), _field("Email address", selector="[addr]")]
    m = forms.match_fields("Email", fields)
    assert m.ok and m.selector == "[addr]"   # prefix (tier 1) wins over substring (tier 2)


def test_match_falls_back_to_non_primary_label_sources():
    # a react-styled field with no visible <label>, matched via its `name`/placeholder
    fields = [_field("", labels=["email"], name="email", selector="[byname]")]
    m = forms.match_fields("Email", fields)
    assert m.ok and m.selector == "[byname]"


def test_required_marker_asterisk_is_ignored_when_matching():
    m = forms.match_fields("Email", [_field("Email *", labels=["Email *"], selector="[e]")])
    assert m.ok and m.selector == "[e]"


def test_match_ambiguous_at_the_best_tier_is_an_error_that_names_them():
    fields = [_field("Email", kind="email", name="a"), _field("Email", kind="text", name="b")]
    m = forms.match_fields("Email", fields)
    assert not m.ok and m.error.startswith("Error:")
    assert "2 fields" in m.error and "name=a" in m.error and "name=b" in m.error
    assert m.selector == ""   # SHALL NOT act on any


def test_match_zero_is_an_error_listing_the_closest_candidates():
    fields = [_field("First name"), _field("Last name"), _field("Phone number")]
    m = forms.match_fields("email", fields)
    assert not m.ok and m.error.startswith("Error:")
    assert m.candidates and len(m.candidates) <= 5


def test_closest_labels_ranks_by_similarity_and_caps_at_five():
    fields = [_field(x) for x in ["Email", "E-mail address", "Emergency contact",
                                  "First name", "Last name", "Phone"]]
    close = forms.closest_labels(fields, "email", n=5)
    assert len(close) == 5 and close[0] == "Email"   # the nearest label first


# ── forms.match_fields — a radio group is enumerated per option, so each is addressable ──
# #4032 review: collapsing a group to its first radio meant a later option could not be
# clicked by its own text, and the group's legend silently acted on the first member. Each
# option is now its own descriptor, and the legend rides every member's labels.
_RADIO_OPTIONS = [
    {"label": "Junior", "labels": ["Junior", "Seniority", "sen"], "kind": "radio-group",
     "name": "sen", "selector": '[data-ab-field="0"]', "group": "sen", "groupLabel": "Seniority",
     "optionLabel": "Junior", "checked": False},
    {"label": "Senior", "labels": ["Senior", "Seniority", "sen"], "kind": "radio-group",
     "name": "sen", "selector": '[data-ab-field="1"]', "group": "sen", "groupLabel": "Seniority",
     "optionLabel": "Senior", "checked": True},
]


@pytest.mark.parametrize(("option", "selector"), [
    ("Senior", '[data-ab-field="1"]'),   # a LATER option resolves to its own radio, not the first
    ("Junior", '[data-ab-field="0"]'),
])
def test_match_resolves_a_specific_radio_option_by_its_own_label(option, selector):
    m = forms.match_fields(option, _RADIO_OPTIONS)
    assert m.ok and m.selector == selector and m.label == option


def test_match_by_the_radio_group_legend_is_ambiguous_not_a_silent_first_pick():
    """Addressing the group by its legend matches every option (the legend rides each
    member's labels), so it is an ambiguous error listing them — never a silent act on the
    first radio."""
    m = forms.match_fields("Seniority", _RADIO_OPTIONS)
    assert not m.ok and m.selector == ""
    assert "2 fields" in m.error and "Junior" in m.error and "Senior" in m.error


# ── forms.resolve_js / parse_resolve / resolve_target — the reusable locator ───────


def test_resolve_js_passes_refs_and_css_through_without_walking_the_dom():
    for field in ("@e7", "#email", ".foo", "div > input"):
        js = forms.resolve_js(field)
        assert "mode:'pass'" in js and json.dumps(field.strip()) in js
        assert "abEnumerate" not in js   # a pass-through never enumerates


def test_resolve_js_for_a_label_enumerates_the_page():
    js = forms.resolve_js("Email")
    assert "abEnumerate(document)" in js and "mode:'enumerate'" in js


@pytest.mark.parametrize("field", ["C++ experience", "Years of C++", "Rust > Go preference"])
def test_resolve_js_treats_combinator_bearing_labels_as_labels_not_css(field):
    """#4032 review (blocking): a label that merely CONTAINS a combinator char (`C++`, `>`)
    must enumerate the page and rank by label — never be shipped to the CLI as a selector that
    matches nothing. The previous substring check routed `"C++ experience"` to CSS."""
    js = forms.resolve_js(field)
    assert "abEnumerate(document)" in js and "mode:'enumerate'" in js
    assert "mode:'pass'" not in js   # NOT a CSS/ref pass-through


def test_resolve_target_round_trips_a_ref_pass_through_unchanged():
    m = forms.resolve_target("@e3", forms.parse_resolve(json.dumps({"mode": "pass", "target": "@e3"})))
    assert m.ok and m.selector == "@e3"   # r1: the ref reaches the CLI unchanged


def test_resolve_target_resolves_a_label_against_enumerated_fields():
    out = json.dumps({"mode": "enumerate", "fields": [
        {"label": "Email", "labels": ["Email"], "selector": "[s]", "kind": "email"}]})
    m = forms.resolve_target("Email", forms.parse_resolve(out))
    assert m.ok and m.selector == "[s]" and m.kind == "email"


def test_parse_resolve_never_raises_on_unreadable_output():
    m = forms.resolve_target("Email", forms.parse_resolve("<<not json>>"))
    assert not m.ok and m.error.startswith("Error:")


# ── the action tools address a field by label (resolver wired into the CLI wrappers) ──
# The #4032-review gap: browser_form_read's docstring says the other form tools address by
# the labels it returns, but fill/click/type/hover/get_value passed the arg raw to the CLI,
# so a label was a selector failure — the wasted-round loop this PR targets. They now route
# through the shared forms resolver.


def _resolving_popen(fields, record=None, procs=None, action_out=b"(ok)"):
    """A scripted CLI: an `eval --stdin` answers the label resolver with an enumerate payload
    of `fields`; every other verb returns `action_out`. So a label action makes TWO calls —
    the in-page resolve, then the act — and the test can read both."""
    payload = json.dumps({"mode": "enumerate", "fields": fields}).encode()

    def _popen(argv, **kw):
        if record is not None:
            record.append(list(argv))
        p = _FakeProc(argv, out=payload if argv[1:2] == ["eval"] else action_out)
        if procs is not None:
            procs.append(p)
        return p

    return _popen


async def test_fill_resolves_a_label_in_the_page_then_acts(monkeypatch):
    """r2: the label is resolved FRESH on this call (an eval), with no reliance on a prior
    snapshot, and the CLI fills the element the resolver returned."""
    rec, procs = [], []
    fields = [
        {"label": "First name", "labels": ["First name"], "selector": '[data-ab-field="0"]', "kind": "text"},
        {"label": "Email", "labels": ["Email"], "selector": '[data-ab-field="1"]', "kind": "email"},
    ]
    monkeypatch.setattr(tools.subprocess, "Popen", _resolving_popen(fields, record=rec, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_fill"].ainvoke({"selector": "Email", "text": "ada@x.io"})
    assert not out.startswith("Error:"), out
    assert rec[0] == ["ab", "eval", "--stdin"]                       # r7: resolve rides stdin
    assert "abEnumerate" in procs[0].stdin.getvalue().decode()       # the shared forms resolver
    assert rec[1] == ["ab", "fill", '[data-ab-field="1"]', "ada@x.io"]   # then act on the match


async def test_click_and_get_value_resolve_labels_too(monkeypatch):
    fields = [{"label": "Subscribe", "labels": ["Subscribe"], "selector": '[data-ab-field="0"]', "kind": "checkbox"}]
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _resolving_popen(fields, record=rec))
    t = _toolmap({"binary": "ab"})
    await t["browser_click"].ainvoke({"selector": "Subscribe"})
    assert rec[-1] == ["ab", "click", '[data-ab-field="0"]']
    await t["browser_get_value"].ainvoke({"selector": "Subscribe"})
    assert rec[-1] == ["ab", "get", "value", '[data-ab-field="0"]']


async def test_click_addresses_a_specific_radio_option_not_the_group_first_member(monkeypatch):
    """#4032 review: clicking a later radio option by its own text hits THAT option; clicking
    the whole group by its legend is refused as ambiguous, never a silent act on the first."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _resolving_popen(_RADIO_OPTIONS, record=rec))
    t = _toolmap({"binary": "ab"})
    out = await t["browser_click"].ainvoke({"selector": "Senior"})
    assert not out.startswith("Error:"), out
    assert rec[-1] == ["ab", "click", '[data-ab-field="1"]']        # the Senior radio, not Junior
    rec.clear()
    out = await t["browser_click"].ainvoke({"selector": "Seniority"})
    assert out.startswith("Error:") and "Junior" in out and "Senior" in out
    assert rec == [["ab", "eval", "--stdin"]]                        # resolved, then refused — no click


@pytest.mark.parametrize("name", ["browser_click", "browser_fill", "browser_type", "browser_hover", "browser_get_value"])
async def test_action_tools_still_pass_refs_and_css_straight_through(monkeypatch, name):
    """r1 / back-compat: a `@eN` ref or a CSS selector reaches the CLI unchanged, with NO
    in-page resolve eval — the pre-label behaviour, preserved exactly."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="(ok)", record=rec))
    args = {"selector": "@e2"}
    if name in ("browser_fill", "browser_type"):
        args["text"] = "hi"
    await _toolmap({"binary": "ab"})[name].ainvoke(args)
    assert rec[-1][1] != "eval"                       # a ref never triggers an in-page resolve
    assert "@e2" in rec[-1]
    # a CSS selector takes the same straight-through path
    rec.clear()
    args["selector"] = "#q"
    await _toolmap({"binary": "ab"})[name].ainvoke(args)
    assert rec[-1][1] != "eval" and "#q" in rec[-1]


@pytest.mark.parametrize("name", ["browser_click", "browser_fill", "browser_type", "browser_hover", "browser_get_value"])
@pytest.mark.parametrize("sel", ["button[type=submit]", "input[name='email']", "textarea",
                                 "select#country", "form input"])
async def test_tag_led_css_selectors_reach_the_cli_unchanged(monkeypatch, name, sel):
    """#4032 review regression: a selector that leads with a tag name (`button[type=submit]`,
    `textarea`, `form input`, …) is a CSS selector that worked before this plugin learned
    labels. It must still go straight to the CLI — not be treated as a label the form has no
    field for and fail 'no form field matches'."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="(ok)", record=rec))
    args = {"selector": sel}
    if name in ("browser_fill", "browser_type"):
        args["text"] = "hi"
    out = await _toolmap({"binary": "ab"})[name].ainvoke(args)
    assert not out.startswith("Error:"), out
    assert rec[-1][1] != "eval" and sel in rec[-1]   # no in-page resolve; the selector reaches the CLI


async def test_a_label_matching_no_field_is_an_error_and_never_acts(monkeypatch):
    """r3: zero matches → an Error naming the closest candidates, and the CLI never acts."""
    fields = [{"label": "First name", "labels": ["First name"], "selector": '[data-ab-field="0"]', "kind": "text"},
              {"label": "Last name", "labels": ["Last name"], "selector": '[data-ab-field="1"]', "kind": "text"}]
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _resolving_popen(fields, record=rec))
    out = await _toolmap({"binary": "ab"})["browser_fill"].ainvoke({"selector": "Email", "text": "x"})
    assert out.startswith("Error:") and "no form field matches" in out
    assert rec == [["ab", "eval", "--stdin"]]        # resolved, then stopped — never filled


async def test_a_label_matching_two_fields_at_the_best_tier_refuses_to_act(monkeypatch):
    """r4: more than one match at the best tier → an Error listing them, and SHALL NOT act."""
    fields = [{"label": "Email", "labels": ["Email"], "selector": '[data-ab-field="0"]', "kind": "email", "name": "a"},
              {"label": "Email", "labels": ["Email"], "selector": '[data-ab-field="1"]', "kind": "text", "name": "b"}]
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _resolving_popen(fields, record=rec))
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke({"selector": "Email"})
    assert out.startswith("Error:") and "2 fields" in out and "name=a" in out and "name=b" in out
    assert rec == [["ab", "eval", "--stdin"]]        # never clicked either one


async def test_a_flag_shaped_label_is_refused_before_any_resolve_eval(monkeypatch):
    """r8 / the argv guard still fires FIRST: a flag-shaped selector never reaches even the
    in-page resolver subprocess."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_fill"].ainvoke({"selector": "--headed", "text": "x"})
    assert out.startswith("Error:") and "looks like a command-line option" in out
    assert rec == []


# ── #4032 A4: browser_click can fall back to a JS-dispatched click ─────────────────
# On Greenhouse the résumé "Enter manually" button opened its textarea only after a
# JS-dispatched click; a normal CLI click exited 0 and NOTHING happened. `js_fallback=True`
# fingerprints the page, does the CLI click, re-fingerprints, and dispatches an in-page
# click ONLY when nothing moved. Host-free: Popen is mocked, so the fingerprint/click evals
# return canned bytes and the test reads the issued argv + stdin.


def _click_fallback_popen(fingerprints, record=None, procs=None, click_out=b"(ok)",
                          jsclick=b'{"ok":true}'):
    """A scripted CLI for ``browser_click(js_fallback=True)`` against a CSS selector (no label
    resolve eval): the Nth `eval --stdin` returns ``fingerprints[N]`` while any remain, then
    the js-click eval returns ``jsclick``; the `click` verb returns ``click_out``. So the call
    order is fingerprint-before (eval), click, fingerprint-after (eval), then — only when the
    two fingerprints agree — the js-click (eval)."""
    state = {"evals": 0}

    def _popen(argv, **kw):
        if record is not None:
            record.append(list(argv))
        if argv[1:2] == ["eval"]:
            i = state["evals"]
            state["evals"] += 1
            out = fingerprints[i] if i < len(fingerprints) else jsclick
        else:
            out = click_out
        p = _FakeProc(argv, out=out if isinstance(out, bytes) else out.encode())
        if procs is not None:
            procs.append(p)
        return p

    return _popen


async def test_click_without_js_fallback_issues_no_fingerprint_eval(monkeypatch):
    """r1: the default path is byte-for-byte the old one — a single CLI click, no fingerprint
    evals, for a CSS selector just as for a `@eN` ref."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="(ok)", record=rec))
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke({"selector": "#go"})
    assert out == "(ok)" and rec == [["ab", "click", "#go"]]
    # explicit js_fallback=False is identical
    rec.clear()
    await _toolmap({"binary": "ab"})["browser_click"].ainvoke({"selector": "#go", "js_fallback": False})
    assert rec == [["ab", "click", "#go"]]


async def test_js_fallback_dispatches_an_in_page_click_when_nothing_changed(monkeypatch):
    """r2: the fingerprint is unchanged after the CLI click, so the tool dispatches a bubbling
    in-page click on the resolved element and says `(JS fallback)`."""
    rec, procs = [], []
    fp = b'{"n":100,"ae":"BODY#.:","exp":"false"}'
    monkeypatch.setattr(tools.subprocess, "Popen",
                        _click_fallback_popen([fp, fp], record=rec, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "#enter-manually", "js_fallback": True})
    # fingerprint-before, click, fingerprint-after, THEN the fallback js-click
    assert [a[1] for a in rec] == ["eval", "click", "eval", "eval"]
    assert out == "Clicked #enter-manually (JS fallback)"
    # the fallback eval dispatches a bubbling click on the SAME selector, never a keypress
    js = procs[-1].stdin.getvalue().decode()
    assert "querySelector" in js and "mousedown" in js and "mouseup" in js and "el.click()" in js
    assert json.dumps("#enter-manually") in js and "press" not in js.lower()


async def test_js_fallback_does_not_dispatch_when_the_click_changed_the_page(monkeypatch):
    """r3: the fingerprint moved (the textarea opened, element count grew), so the click
    worked — the tool must NOT dispatch a second click."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _click_fallback_popen(
        [b'{"n":100,"ae":"A","exp":"false"}', b'{"n":140,"ae":"TEXTAREA","exp":"true"}'], record=rec))
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "#enter-manually", "js_fallback": True})
    # fingerprint-before, click, fingerprint-after — and then STOP (no third eval)
    assert [a[1] for a in rec] == ["eval", "click", "eval"]
    assert out == "(ok)" and "JS fallback" not in out


async def test_js_fallback_suppressed_when_a_fingerprint_cannot_be_read(monkeypatch):
    """A doubt is never a second click: if either fingerprint comes back as an Error, the CLI
    result stands and no in-page click is dispatched."""
    rec = []
    seen = {"evals": 0}

    def _popen(argv, **kw):
        rec.append(list(argv))
        if argv[1:2] == ["eval"]:
            seen["evals"] += 1
            if seen["evals"] == 2:
                return _FakeProc(argv, out=b"", err=b"page gone", rc=1)   # fingerprint-after fails
            return _FakeProc(argv, out=b'{"n":100,"ae":"","exp":""}')
        return _FakeProc(argv, out=b"(ok)")

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "#x", "js_fallback": True})
    assert [a[1] for a in rec] == ["eval", "click", "eval"]   # no fallback js-click
    assert out == "(ok)"


async def test_js_fallback_does_not_fire_when_the_cli_click_itself_errors(monkeypatch):
    """A CLI click that exits non-zero is a real failure to surface — not a case for the JS
    fallback, which exists for a click that 'succeeds' yet does nothing."""
    rec = []

    def _popen(argv, **kw):
        rec.append(list(argv))
        if argv[1:2] == ["eval"]:
            return _FakeProc(argv, out=b'{"n":1,"ae":"","exp":""}')
        return _FakeProc(argv, out=b"", err=b"no such element", rc=2)   # the click fails

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "#missing", "js_fallback": True})
    assert out.startswith("Error:") and "no such element" in out
    assert [a[1] for a in rec] == ["eval", "click"]   # fingerprint-before, click, then stop


async def test_js_fallback_resolves_a_label_through_the_shared_locator(monkeypatch):
    """r4: with js_fallback a LABEL is still resolved through the bd-12mo.1 locator — the
    resolve eval enumerates, and the fingerprint/click/fallback all act on the resolved
    `[data-ab-field]` selector, never a stale ref."""
    rec, procs = [], []
    fields = [{"label": "Enter manually", "labels": ["Enter manually"],
               "selector": '[data-ab-field="3"]', "kind": "other"}]
    enum = json.dumps({"mode": "enumerate", "fields": fields}).encode()
    fp = b'{"n":80,"ae":"","exp":"false"}'
    state = {"evals": 0}

    def _popen(argv, **kw):
        rec.append(list(argv))
        if argv[1:2] == ["eval"]:
            i = state["evals"]
            state["evals"] += 1
            out = enum if i == 0 else (fp if i in (1, 2) else b'{"ok":true}')
            p = _FakeProc(argv, out=out)
        else:
            p = _FakeProc(argv, out=b"(ok)")
        procs.append(p)
        return p

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "Enter manually", "js_fallback": True})
    # resolve(eval) → fingerprint(eval) → click → fingerprint(eval) → js-click(eval)
    assert [a[1] for a in rec] == ["eval", "eval", "click", "eval", "eval"]
    assert "abEnumerate" in procs[0].stdin.getvalue().decode()        # the shared resolver
    assert rec[2] == ["ab", "click", '[data-ab-field="3"]']           # clicked the resolved selector
    assert json.dumps('[data-ab-field="3"]') in procs[-1].stdin.getvalue().decode()
    assert out == "Clicked Enter manually (JS fallback)"              # reported by the model's label


async def test_js_fallback_refuses_a_ref_without_running_the_cli(monkeypatch):
    """#4032 review: with `js_fallback` a `@ref` was silently a no-op — the fallback dispatches
    the click via an in-page document.querySelector where "@e5" is invalid CSS and throws, the
    throw was swallowed as 'not-found', and the CLI's ordinary success text still stood, so the
    model believed a fallback ran that didn't. A ref is now refused up front (as browser_select
    / browser_upload / browser_form_read refuse refs), before ANY subprocess."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="(ok)", record=rec))
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "@e5", "js_fallback": True})
    assert out.startswith("Error:") and "@ref" in out
    assert rec == []   # a ref can't drive an in-page click — refuse before the CLI runs
    # but the SAME ref still clicks fine on the plain CLI path (js_fallback off), unchanged
    rec.clear()
    out = await _toolmap({"binary": "ab"})["browser_click"].ainvoke(
        {"selector": "@e5", "js_fallback": False})
    assert out == "(ok)" and rec == [["ab", "click", "@e5"]]


def test_fingerprint_js_and_js_click_js_ride_stdin_and_carry_the_selector():
    """The two fallback scripts are built host-free: each embeds the selector as a JSON literal
    and is shaped for `eval --stdin` (#3689)."""
    fp = forms.fingerprint_js('[data-ab-field="2"]')
    assert json.dumps('[data-ab-field="2"]') in fp and "getElementsByTagName" in fp
    assert "activeElement" in fp and "aria-expanded" in fp
    click = forms.js_click_js("#go")
    assert json.dumps("#go") in click and "mousedown" in click and "el.click()" in click


@pytest.mark.parametrize(("before", "after", "changed"), [
    ('{"n":1}', '{"n":1}', False),            # identical → nothing moved
    ('{"n":1}', '{"n":2}', True),             # element count grew → the click did something
    ("Error: boom", '{"n":1}', True),         # unreadable → treated as changed (suppress fallback)
    ('{"n":1}', "", True),                    # empty → suppress
])
def test_fingerprint_changed_is_strict_and_fails_safe(before, after, changed):
    assert forms.fingerprint_changed(before, after) is changed


def test_render_js_click_reports_the_fallback_or_defers_to_the_cli():
    assert forms.render_js_click('{"ok":true}', "#go", "(ok)") == "Clicked #go (JS fallback)"
    # element gone from the page by dispatch time (not-found) → the CLI's own result stands
    assert forms.render_js_click('{"ok":false,"reason":"not-found"}', "#go", "(ok)") == "(ok)"
    assert forms.render_js_click("<<garbage>>", "#go", "(ok)") == "(ok)"


# ── browser_form_read — the tool (canned eval output + stdin script) ───────────────


async def test_form_read_returns_public_json_in_document_order(monkeypatch):
    payload = json.dumps({"ok": True, "fields": _FORM_FIELDS})
    rec, procs = [], []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=payload, record=rec, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({})
    # r7: the script rides eval --stdin, never argv
    assert rec[-1] == ["ab", "eval", "--stdin"]
    assert "abEnumerate" in procs[-1].stdin.getvalue().decode()
    data = json.loads(out)
    assert [f["label"] for f in data] == ["First name", "Email", "Country", "Role",
                                          "Seniority", "Résumé", "Subscribe"]   # r5: document order
    # the internal addressing keys never surface
    assert all("labels" not in f and "selector" not in f and "idx" not in f for f in data)
    first = data[0]
    assert set(first) == {"label", "kind", "name", "id", "required", "value"}
    assert first["required"] is True and first["kind"] == "text" and first["name"] == "first_name"


async def test_form_read_reports_committed_values_and_options(monkeypatch):
    payload = json.dumps({"ok": True, "fields": _FORM_FIELDS})
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=payload))
    data = json.loads(await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({}))
    by_kind = {f["kind"]: f for f in data}
    # r6: a combobox reports its committed selection, and omits options (not in the DOM)
    assert by_kind["combobox"]["value"] == "United States" and "options" not in by_kind["combobox"]
    assert by_kind["native-select"]["options"] == ["", "Engineer", "Manager"]
    assert by_kind["native-select"]["value"] == "Engineer"
    assert by_kind["radio-group"]["options"] == ["Junior", "Senior"] and by_kind["radio-group"]["value"] == "Senior"
    assert by_kind["file"]["value"] == "cv.pdf"
    assert by_kind["checkbox"]["value"] is True


def test_render_folds_radio_options_into_one_group_at_the_first_members_position():
    """The per-option radio descriptors `abEnumerate` emits collapse to ONE radio-group row
    for the read view: `options` is the member labels, `value` the checked one, `required` true
    if any member is, placed where the first member appeared."""
    fields = [
        {"label": "Size", "labels": ["Size"], "kind": "text", "name": "size", "id": "",
         "required": False, "value": ""},
        {"label": "Red", "kind": "radio-group", "name": "color", "group": "color",
         "groupLabel": "Colour", "optionLabel": "Red", "checked": False, "required": True,
         "selector": "[a]"},
        {"label": "Green", "kind": "radio-group", "name": "color", "group": "color",
         "groupLabel": "Colour", "optionLabel": "Green", "checked": True, "required": False,
         "selector": "[b]"},
    ]
    out = json.loads(forms.render_form_read(json.dumps({"ok": True, "fields": fields})))
    assert [f["label"] for f in out] == ["Size", "Colour"]       # folded, at the group's slot
    grp = out[1]
    assert grp["kind"] == "radio-group" and grp["options"] == ["Red", "Green"]
    assert grp["value"] == "Green" and grp["required"] is True   # checked option; any-required
    assert set(grp) == {"label", "kind", "name", "id", "required", "value", "options"}


def test_form_read_js_extracts_the_committed_combobox_value_not_typed_text():
    """r6 is produced in-page; the script must read the rendered single-value, and must
    skip a combobox's inner search input so half-typed text is never surfaced as a field."""
    js = forms.read_form_js("")
    assert "single-value" in js and "singleValue" in js   # the committed selection
    assert "abInCombo" in js and "select__control" in js   # the inner input is skipped


# The enumeration JS is DOM logic, so the combobox de-duplication is exercised against a real
# DOM (jsdom, from the console workspace's node_modules) — the only host-free way to prove it.
NODE = shutil.which("node")

_REACT_SELECT_HTML = """
<form>
  <div class="field">
    <label for="rs-country-input">Country</label>
    <div class="select__control">
      <div class="select__value-container">
        <div class="select__single-value">United States</div>
        <div class="select__input-container">
          <input id="rs-country-input" role="combobox" name="country" value="typed-but-not-chosen"/>
        </div>
      </div>
      <div class="select__indicators"><span class="select__indicator">v</span></div>
    </div>
  </div>
  <label for="plain">Email</label>
  <input id="plain" type="email" name="email" value="ada@x.io"/>
</form>
"""


def _run_enumerate_js(html: str):
    """Run forms._JS_LIB's ``abEnumerate`` against a real DOM and return the field list."""
    if not NODE:
        pytest.skip("node not on PATH")
    probe = subprocess.run([NODE, "-e", "require.resolve('jsdom')"], cwd=REPO,
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("jsdom not installed (run npm ci in apps/web or the repo root)")
    harness = (
        "const { JSDOM } = require('jsdom');\n"
        "const dom = new JSDOM(" + json.dumps(html) + ");\n"
        "global.window = dom.window; global.document = dom.window.document; global.CSS = dom.window.CSS;\n"
        + forms._JS_LIB + "\n"
        "console.log(JSON.stringify(abEnumerate(document)));\n"
    )
    out = subprocess.run([NODE, "-e", harness], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_enumerate_collapses_a_react_select_combobox_to_one_labelled_field():
    """#4032-review correctness bug: the react-select inner search input also carries
    role="combobox", so the dropdown enumerated TWICE — an unlabelled entry with the real
    value and a labelled entry whose value was always ''. It must be ONE field, labelled
    (from the inner input) AND carrying the committed selection."""
    fields = _run_enumerate_js(_REACT_SELECT_HTML)
    assert [f["kind"] for f in fields] == ["combobox", "email"]   # document order, no twin
    combos = [f for f in fields if f["kind"] == "combobox"]
    assert len(combos) == 1                                       # not two
    c = combos[0]
    assert c["label"] == "Country"                               # labelled (merged from inner)
    assert c["value"] == "United States"                        # committed selection, not typed
    assert c["name"] == "country" and c["id"] == "rs-country-input"   # inner identity adopted
    assert "" not in [f["label"] for f in fields]               # no phantom unlabelled entry


def test_enumerate_keeps_a_bare_aria_combobox_as_a_single_field():
    """A plain ARIA combobox (no react-select container) is still one root — the fix only
    folds the inner input of a `.select__control`, nothing else."""
    html = """<form>
      <label for="cb">State</label>
      <div id="cb" role="combobox" aria-expanded="false">California</div>
    </form>"""
    fields = _run_enumerate_js(html)
    assert len(fields) == 1 and fields[0]["kind"] == "combobox"
    assert fields[0]["label"] == "State" and fields[0]["value"] == "California"


def test_enumerate_file_value_falls_back_to_the_filename_chip():
    """#4032 bug 3: a file input the widget RE-RENDERED empty still shows the filename as a chip
    in its field container — form_read reports that filename (controls stripped), so a verify-fill
    sees the attached résumé after the re-render instead of a blank file field."""
    html = """<form>
      <div id="resume_block">
        <label for="resume">Resume/CV</label>
        <input id="resume" type="file" name="resume"/>
        <span class="file-chip"><span class="file-chip__name">grace.pdf</span><button type="button">x</button></span>
      </div>
    </form>"""
    fields = _run_enumerate_js(html)
    f = [x for x in fields if x.get("id") == "resume"][0]
    assert f["kind"] == "file" and f["value"] == "grace.pdf"


_RADIO_HTML = """
<form>
  <fieldset>
    <legend>Seniority *</legend>
    <label><input type="radio" name="sen" value="jr"/> Junior</label>
    <label><input type="radio" name="sen" value="sr" checked/> Senior</label>
  </fieldset>
  <label for="e">Email</label>
  <input id="e" type="email" name="email"/>
</form>
"""


def test_enumerate_lists_every_radio_option_as_its_own_addressable_field():
    """#4032 review: each radio is enumerated and tagged separately (so a later option is
    clickable by its own text), the group legend rides every member's labels (so addressing
    the group hits every option), and the read renderer folds the members into one row."""
    fields = _run_enumerate_js(_RADIO_HTML)
    radios = [f for f in fields if f["kind"] == "radio-group"]
    assert len(radios) == 2                                        # one entry PER option, not collapsed
    assert [r["label"] for r in radios] == ["Junior", "Senior"]
    assert radios[0]["selector"] != radios[1]["selector"]         # each independently addressable
    assert all(r["selector"].startswith('[data-ab-field="') for r in radios)
    assert all("Seniority" in r["labels"] for r in radios)        # legend (asterisk stripped) on each
    assert radios[1]["checked"] is True and radios[1]["optionLabel"] == "Senior"
    # the read renderer folds the two options back into one radio-group row
    folded = [f for f in forms.collapse_radio_groups(fields) if f["kind"] == "radio-group"]
    assert len(folded) == 1
    assert folded[0]["label"] == "Seniority" and folded[0]["options"] == ["Junior", "Senior"]
    assert folded[0]["value"] == "Senior"


async def test_form_read_scope_css_is_embedded_as_a_query_root(monkeypatch):
    procs = []
    monkeypatch.setattr(tools.subprocess, "Popen",
                        fake_popen(out=json.dumps({"ok": True, "fields": []}), procs=procs))
    await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({"scope": "#application"})
    script = procs[-1].stdin.getvalue().decode()
    assert 'abScopeRoot("#application", true)' in script


async def test_form_read_scope_label_is_passed_as_a_container_name(monkeypatch):
    procs = []
    monkeypatch.setattr(tools.subprocess, "Popen",
                        fake_popen(out=json.dumps({"ok": True, "fields": []}), procs=procs))
    await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({"scope": "Work history"})
    assert 'abScopeRoot("Work history", false)' in procs[-1].stdin.getvalue().decode()


async def test_form_read_refuses_a_ref_scope_without_running_the_cli(monkeypatch):
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({"scope": "@e5"})
    assert out.startswith("Error:") and "@ref" in out
    assert rec == []   # a ref can't be resolved in an eval — refuse before the subprocess


async def test_form_read_reports_an_unmatched_scope(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen",
                        fake_popen(out=json.dumps({"ok": False, "error": "scope-not-found"})))
    out = await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({"scope": "#nope"})
    assert out.startswith("Error:") and "#nope" in out


async def test_form_read_degrades_on_unreadable_output_instead_of_raising(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="not json at all"))
    out = await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({})
    assert out.startswith("Error:")   # r8: no new tool raises


async def test_form_read_surfaces_an_eval_error(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(rc=1, err="no page open"))
    out = await _toolmap({"binary": "ab"})["browser_form_read"].ainvoke({})
    assert out.startswith("Error:") and "no page open" in out


def test_form_read_is_a_registered_tool_with_a_usable_docstring():
    t = _toolmap()["browser_form_read"]
    assert "browser_form_read" in EXPECTED_TOOLS
    assert t.description and len(t.description) >= 20


# ── #4032 A2: browser_select — native / react-select / intl-tel-input, with read-back ──
# Host-free like the form-read tests: the in-page engine never runs here. The branch-dispatch
# tests assert the single `eval --stdin` script carries each widget's marker (detection is
# in-page); the tool tests mock Popen and parse canned select-engine output (success /
# mismatch / ambiguous / no-option / not-found); the match + render logic is pure Python.


def _select_popen(result, fields=None, record=None, procs=None):
    """A scripted CLI for browser_select. The FIRST `eval --stdin` (the bd-12mo.1 label
    resolver) answers with an enumerate payload of `fields`; the SECOND (the select engine)
    answers with `result` — so a LABEL select makes two evals and the test can read both.
    A CSS/ref field skips the resolver, so there is only the one select eval."""
    enum = json.dumps({"mode": "enumerate", "fields": fields or []}).encode()
    res = result if isinstance(result, str) else json.dumps(result)
    res = res.encode() if isinstance(res, str) else res
    state = {"evals": 0}

    def _popen(argv, **kw):
        if record is not None:
            record.append(list(argv))
        out = b"(ok)"
        if argv[1:2] == ["eval"]:
            out = enum if state["evals"] == 0 else res
            state["evals"] += 1
        p = _FakeProc(argv, out=out)
        if procs is not None:
            procs.append(p)
        return p

    return _popen


async def _select(monkeypatch, result, field="#country", option_text="United States",
                  record=None, procs=None):
    """Drive browser_select against a CSS field (no resolver eval) with a canned engine result."""
    payload = result if isinstance(result, str) else json.dumps(result)
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out=payload, record=record, procs=procs))
    return await _toolmap({"binary": "ab"})["browser_select"].ainvoke(
        {"field": field, "option_text": option_text})


def test_select_js_dispatches_to_each_widget_branch_in_one_script():
    """r1/r2/r3: ONE `eval` script detects the widget in-page and dispatches. Assert every
    branch's marker is present, the locator + option ride as JSON literals, and the combobox
    is cleared before typing — never appended to (the 'YeYess' bug)."""
    js = forms.select_js('[data-ab-field="2"]', "United States")
    assert json.dumps('[data-ab-field="2"]') in js and json.dumps("United States") in js
    assert "abSelectKind" in js
    # native <select>: set the option and fire BUBBLING input + change
    assert "native-select" in js and "el.options" in js
    assert "abFire(el, 'input')" in js and "abFire(el, 'change')" in js and "bubbles:true" in js
    # react-select combobox: role=option menu, the select__/-container/aria-autocomplete signature
    assert 'role="option"' in js and "select__control" in js
    assert "aria-autocomplete" in js and '-container' in js
    # intl-tel-input: the country list + the selected-flag read-back
    assert "iti__country" in js and "iti__country-name" in js
    assert "selected-flag" in js and ("title" in js and "aria-label" in js)


def test_select_combobox_clears_before_typing_and_commits_by_click_never_enter():
    """r2, by construction: the input is cleared (`abSetNativeValue(input, '')`) before the
    filter is typed, and the option is COMMITTED BY CLICK (`abClickOption`). The only keyboard
    event is an ArrowDown to OPEN the menu (#4032 fix round) — Enter is never pressed, so the
    highlighted-wrong-option commit (#4032) cannot happen."""
    js = forms.select_js("#country", "United States")
    assert "abSetNativeValue(input, '')" in js            # clear first, never append
    assert "abClickOption" in js                           # commit by clicking the option element
    # ArrowDown is an OPEN signal, not a commit; Enter/keypress is never dispatched to commit.
    assert "ArrowDown" in js and "keypress" not in js
    assert "'Enter'" not in js and '"Enter"' not in js


def _run_select_lib_js(html: str, expr: str):
    """Evaluate `expr` against a real DOM (jsdom) with forms._JS_LIB + _SELECT_LIB loaded, and
    return the JSON it produces — the host-free way to prove the in-page option scoping."""
    if not NODE:
        pytest.skip("node not on PATH")
    probe = subprocess.run([NODE, "-e", "require.resolve('jsdom')"], cwd=REPO,
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("jsdom not installed (run npm ci in apps/web or the repo root)")
    harness = (
        "const { JSDOM } = require('jsdom');\n"
        "const dom = new JSDOM(" + json.dumps(html) + ");\n"
        "global.window = dom.window; global.document = dom.window.document; global.CSS = dom.window.CSS;\n"
        + forms._JS_LIB + forms._SELECT_LIB + "\n"
        "console.log(JSON.stringify((function(){ return " + expr + "; })()));\n"
    )
    out = subprocess.run([NODE, "-e", harness], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# A react-select control whose menu is wired by `aria-controls`, plus ANOTHER widget's
# listbox already open on the page (the #4032 wrong country). The scoped scan must ignore it.
_SCOPED_COMBO_ARIA_HTML = """
<form>
  <ul role="listbox" id="other-menu"><li role="option">Afghanistan</li></ul>
  <div class="field">
    <label for="rs-country">Country</label>
    <div class="select__control">
      <div class="select__value-container">
        <div class="select__input-container">
          <input id="rs-country" role="combobox" name="country" aria-controls="rs-menu"/>
        </div>
      </div>
    </div>
    <div class="select__menu" id="rs-menu" role="listbox">
      <div role="option">United States</div>
      <div role="option">United Kingdom</div>
    </div>
  </div>
</form>
"""

# A react-select control with NO aria wiring: the menu is a sibling of the control inside a
# `-container` wrapper. The fallback must scope to that wrapper — never the whole document.
_SCOPED_COMBO_CONTAINER_HTML = """
<form>
  <ul role="listbox" id="other-menu"><li role="option">Afghanistan</li></ul>
  <div class="my-select-container">
    <label for="cc">Country</label>
    <div class="select__control"><input id="cc" role="combobox" name="country"/></div>
    <div class="select__menu" role="listbox">
      <div role="option">Canada</div>
      <div role="option">Cambodia</div>
    </div>
  </div>
</form>
"""

_COMBO_OPTIONS_EXPR = (
    "abComboOptions("
    "document.querySelector('.select__control').querySelector('[role=combobox]'),"
    "document.querySelector('.select__control'))"
    ".map(function(o){ return o.textContent.trim(); })"
)


@pytest.mark.parametrize(("html", "expected"), [
    (_SCOPED_COMBO_ARIA_HTML, ["United States", "United Kingdom"]),
    (_SCOPED_COMBO_CONTAINER_HTML, ["Canada", "Cambodia"]),
])
def test_combobox_options_are_scoped_to_this_controls_menu(html, expected):
    """#4032 review: the combobox branch read `[role="option"]` from the WHOLE document, and
    the poll returns on its first non-empty read — so a menu already open elsewhere (here an
    'Afghanistan' option) could be matched and clicked, changing another field. Options must
    come only from THIS control's menu (via aria-controls, else its own container wrapper)."""
    opts = _run_select_lib_js(html, _COMBO_OPTIONS_EXPR)
    assert opts == expected
    assert "Afghanistan" not in opts     # the other widget's open menu is never seen


async def test_select_native_success_reads_back_and_reports(monkeypatch):
    """r1: a successful native selection reports the committed read-back value and label."""
    rec, procs = [], []
    out = await _select(monkeypatch,
                        {"ok": True, "kind": "native-select", "label": "Role",
                         "chosen": "Engineer", "committed": "Engineer"},
                        field="#role", option_text="Engineer", record=rec, procs=procs)
    assert out == 'Selected "Engineer" in Role'
    assert rec[-1] == ["ab", "eval", "--stdin"]            # r6: the engine rides eval --stdin
    assert "abSelectKind" in procs[-1].stdin.getvalue().decode()


async def test_select_reuses_the_bd12mo1_label_locator_before_the_engine(monkeypatch):
    """r6: a LABEL is resolved FRESH by the shared bd-12mo.1 resolver (an enumerate eval), and
    the resolved `[data-ab-field]` selector is what the select engine then acts on — two evals,
    both on stdin, no second locator."""
    rec, procs = [], []
    fields = [{"label": "Country", "labels": ["Country"], "selector": '[data-ab-field="0"]',
               "kind": "combobox"}]
    monkeypatch.setattr(tools.subprocess, "Popen",
                        _select_popen({"ok": True, "kind": "combobox", "label": "Country",
                                       "chosen": "United States", "committed": "United States"},
                                      fields=fields, record=rec, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_select"].ainvoke(
        {"field": "Country", "option_text": "United States"})
    assert out == 'Selected "United States" in Country'
    assert rec == [["ab", "eval", "--stdin"], ["ab", "eval", "--stdin"]]
    assert "abEnumerate" in procs[0].stdin.getvalue().decode()        # the resolver
    select_script = procs[1].stdin.getvalue().decode()
    assert "abSelectKind" in select_script                            # the engine
    assert json.dumps('[data-ab-field="0"]') in select_script         # on the RESOLVED selector


async def test_select_mismatch_on_read_back_is_a_hard_error(monkeypatch):
    """r5: the field reading back a different value than was chosen is an Error naming both —
    never a success. This is the guard against committing the wrong option silently (#4032)."""
    out = await _select(monkeypatch,
                        {"ok": False, "reason": "mismatch", "kind": "combobox", "label": "Visa",
                         "wanted": "No", "actual": "Yes, Ireland Highly Skilled Worker Visa"},
                        field="#visa", option_text="No")
    assert out.startswith("Error:") and "Selected" not in out
    assert "Visa" in out and "No" in out
    assert "Yes, Ireland Highly Skilled Worker Visa" in out
    assert "reads" in out and "after selecting" in out


async def test_select_no_matching_option_lists_the_available_ones(monkeypatch):
    """r4: zero matches → an Error listing the available options, and nothing is committed."""
    out = await _select(monkeypatch,
                        {"ok": False, "reason": "no-option", "kind": "native-select",
                         "label": "Role", "options": ["Engineer", "Manager", "Designer"]},
                        field="#role", option_text="Astronaut")
    assert out.startswith("Error:") and "Astronaut" in out
    assert "Engineer" in out and "Manager" in out and "Designer" in out
    assert "Selected" not in out


async def test_select_ambiguous_option_is_an_error_not_a_guess(monkeypatch):
    """r4: more than one candidate at the winning tier → an Error (never a silent pick)."""
    out = await _select(monkeypatch,
                        {"ok": False, "reason": "ambiguous", "kind": "combobox", "label": "Country",
                         "options": ["United States", "United States Minor Outlying Islands"]},
                        field="#country", option_text="United")
    assert out.startswith("Error:") and "more than one" in out
    assert "United States" in out and "Selected" not in out


def test_select_render_caps_the_listed_options_at_ten():
    """r4: the available-options listing is bounded at 10, however long the real list."""
    opts = [f"Country {i}" for i in range(25)]
    out = forms.render_select(
        json.dumps({"ok": False, "reason": "no-option", "label": "Country", "options": opts}),
        "#country", "Nowhere")
    assert out.startswith("Error:")
    assert "Country 0" in out and "Country 9" in out
    assert "Country 10" not in out and "Country 24" not in out


async def test_select_not_found_points_at_form_read(monkeypatch):
    """A locator that matches no element in the page is a clear Error, never a success."""
    out = await _select(monkeypatch, {"ok": False, "reason": "not-found"},
                        field="#missing", option_text="x")
    assert out.startswith("Error:") and "browser_form_read" in out and "Selected" not in out


async def test_select_refuses_a_ref_field_without_running_the_cli(monkeypatch):
    """#4032 review: browser_select claimed `@ref` support, but it sets the widget via an
    in-page `document.querySelector`, where "@e5" is invalid CSS and throws — the error was
    swallowed as 'not-found', so EVERY ref-addressed select failed. A ref is now refused up
    front (as browser_form_read does), before any subprocess."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_select"].ainvoke(
        {"field": "@e5", "option_text": "United States"})
    assert out.startswith("Error:") and "@ref" in out
    assert rec == []   # a ref can't be resolved in an eval — refuse before the CLI runs


async def test_select_on_a_non_choice_field_is_refused(monkeypatch):
    """A plain text field isn't a choice widget — say so rather than pretend to select."""
    out = await _select(monkeypatch, {"ok": False, "reason": "unsupported", "kind": "other",
                                      "label": "First name"},
                        field="#first", option_text="x")
    assert out.startswith("Error:") and "browser_fill" in out


async def test_select_degrades_on_unreadable_output_instead_of_raising(monkeypatch):
    out = await _select(monkeypatch, "not json at all", field="#country", option_text="x")
    assert out.startswith("Error:")   # never raises


async def test_select_surfaces_an_eval_error(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(rc=1, err="no page open"))
    out = await _toolmap({"binary": "ab"})["browser_select"].ainvoke(
        {"field": "#country", "option_text": "x"})
    assert out.startswith("Error:") and "no page open" in out


async def test_select_phone_country_is_the_iti_branch_and_fill_follows_it(monkeypatch):
    """r3 + the phone helper: selecting the COUNTRY drives the intl-tel-input branch (click a
    country entry, read back the selected flag), and because that branch never writes the tel
    input's value, a following browser_fill of the national number reaches the tel input
    unchanged. The docstring documents the country-before-number order."""
    # the iti branch of the engine must not type into the tel input — else the number a later
    # browser_fill enters would be clobbered (and the order in the docstring would be a lie).
    iti_src = forms._SELECT_LIB.split("async function abIti")[1].split("\nfunction ")[0]
    assert "abSetNativeValue" not in iti_src and ".value" not in iti_src

    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen",
                        fake_popen(out=json.dumps({"ok": True, "kind": "iti", "label": "Phone",
                                                   "chosen": "United States",
                                                   "committed": "United States"}), record=rec))
    t = _toolmap({"binary": "ab"})
    picked = await t["browser_select"].ainvoke({"field": "#phone", "option_text": "United States"})
    assert picked == 'Selected "United States" in Phone'
    # then the number is filled into the SAME tel input, verbatim
    filled = await t["browser_fill"].ainvoke({"selector": "#phone", "text": "2015550123"})
    assert not filled.startswith("Error:")
    assert rec[-1] == ["ab", "fill", "#phone", "2015550123"]

    desc = t["browser_select"].description.lower()
    assert "phone" in desc and "country" in desc and "before" in desc and "fill" in desc


def test_select_is_a_registered_tool_with_a_usable_docstring():
    t = _toolmap()["browser_select"]
    assert "browser_select" in EXPECTED_TOOLS
    assert t.description and len(t.description) >= 20


# ── #4032 A3: browser_upload — attach a fenced file to a file input, with read-back ──
# Host-free like the other form tools: the in-page engine never runs here. The fence is pure
# Python (storage.resolve_upload_path) and exercised directly; the tool tests mock Popen and
# assert argv + the canned read-back; the in-page file-input RESOLUTION (the Attach-button
# container climb) is proven against a real DOM (jsdom) where it is the only host-free way.


# ── storage.resolve_upload_path — the capture fence, in reverse ────────────────────


def test_resolve_upload_path_accepts_a_file_inside_the_fence():
    root = storage.capture_root().resolve()
    f = root / "cv.pdf"
    f.write_bytes(b"%PDF-1.4 resume")
    assert storage.resolve_upload_path("cv.pdf") == f
    assert storage.resolve_upload_path(str(f)) == f   # an absolute path already inside the fence


def test_resolve_upload_path_accepts_a_relative_subdirectory_file():
    root = storage.capture_root().resolve()
    sub = root / "out"
    sub.mkdir()
    (sub / "resume.pdf").write_bytes(b"%PDF")
    assert storage.resolve_upload_path("out/resume.pdf") == (sub / "resume.pdf").resolve()


@pytest.mark.parametrize("bad", [
    "/etc/passwd",
    "../../escape.pdf",
    "a/../../escape.pdf",
    "~/.ssh/id_rsa",
    "",
    ".",
])
def test_resolve_upload_path_refuses_paths_outside_the_fence(bad):
    with pytest.raises(ValueError):
        storage.resolve_upload_path(bad)


def test_resolve_upload_path_requires_an_existing_non_empty_file():
    root = storage.capture_root().resolve()
    (root / "empty.pdf").write_bytes(b"")
    with pytest.raises(ValueError) as e_empty:
        storage.resolve_upload_path("empty.pdf")      # inside the fence, but zero bytes
    assert "empty" in str(e_empty.value)
    with pytest.raises(ValueError):
        storage.resolve_upload_path("missing.pdf")    # inside the fence, but absent


def test_resolve_upload_path_refuses_a_symlink_escaping_the_fence(tmp_path):
    root = storage.capture_root().resolve()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF secret")
    link = root / "linked.pdf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    with pytest.raises(ValueError):
        storage.resolve_upload_path("linked.pdf")     # resolves (and exists) OUTSIDE the root


# ── the tool: fence refusals run BEFORE any subprocess ─────────────────────────────


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../escape.pdf", "~/.ssh/id_rsa", ""])
async def test_upload_refuses_a_path_outside_the_fence_without_running_the_cli(monkeypatch, bad):
    """r3/r4: a path that escapes the fence (or is blank) is an Error, and the CLI never runs —
    the fence is resolved before any subprocess, exactly like a capture."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": bad})
    assert out.startswith("Error:")
    assert rec == []   # nothing reached agent-browser


async def test_upload_refuses_a_symlink_escape_without_running_the_cli(monkeypatch, tmp_path):
    """r3: a symlink INSIDE the fence that points out resolves to a file elsewhere — refused
    before the CLI, so a planted link can't exfiltrate its target."""
    root = storage.capture_root().resolve()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF secret")
    try:
        (root / "linked.pdf").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "linked.pdf"})
    assert out.startswith("Error:") and "outside" in out
    assert rec == []


async def test_upload_refuses_a_missing_or_empty_file_without_running_the_cli(monkeypatch):
    """r4: a file that is missing, or present but empty, is an Error before the upload."""
    root = storage.capture_root().resolve()
    (root / "empty.pdf").write_bytes(b"")
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    t = _toolmap({"binary": "ab"})
    out_missing = await t["browser_upload"].ainvoke({"field": "#resume", "file_path": "nope.pdf"})
    out_empty = await t["browser_upload"].ainvoke({"field": "#resume", "file_path": "empty.pdf"})
    assert out_missing.startswith("Error:") and out_empty.startswith("Error:")
    assert rec == []   # neither reached the CLI


async def test_upload_refuses_a_ref_field_without_running_the_cli(monkeypatch):
    """A @ref can't be resolved in the page (where the input is tagged), so it's refused up
    front — as browser_select / browser_form_read do — before any subprocess."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF")
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "@e5", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "@ref" in out
    assert rec == []


# ── the tool: the happy path + the read-back verify ────────────────────────────────


def _upload_popen(locate, verify, record=None, procs=None, upload_out=b"(ok)"):
    """A scripted CLI for browser_upload against a CSS field (no resolver eval): the FIRST
    `eval --stdin` answers the locate/tag step with `locate`, the SECOND answers the read-back
    verify with `verify`, and `upload` returns `upload_out`."""
    loc = locate if isinstance(locate, (bytes, bytearray)) else json.dumps(locate).encode()
    ver = verify if isinstance(verify, (bytes, bytearray)) else json.dumps(verify).encode()
    state = {"evals": 0}

    def _popen(argv, **kw):
        if record is not None:
            record.append(list(argv))
        out = b"(ok)"
        if argv[1:2] == ["eval"]:
            out = loc if state["evals"] == 0 else ver
            state["evals"] += 1
        elif argv[1:2] == ["upload"]:
            out = upload_out
        p = _FakeProc(argv, out=out)
        if procs is not None:
            procs.append(p)
        return p

    return _popen


async def test_upload_attaches_a_fenced_file_and_verifies_the_readback(monkeypatch):
    """r1: a CSS-addressed file input is tagged in-page, the CLI uploads the fenced ABSOLUTE
    path to that stable selector, and success is reported only after reading the attached
    filename back and confirming it matches."""
    root = storage.capture_root().resolve()
    resume = root / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    rec, procs = [], []
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": True, "selector": '[data-pa-upload="abc123"]', "label": "Résumé", "name": "resume"},
        {"ok": True, "name": "cv.pdf", "error": ""},
        record=rec, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out == "Uploaded cv.pdf to Résumé"
    # locate(eval) → upload → verify(eval): a CSS field skips the label resolver
    assert [a[1] for a in rec] == ["eval", "upload", "eval"]
    # the CLI uploads the STABLE tagged selector + the fenced ABSOLUTE path — never a ref
    assert rec[1] == ["ab", "upload", '[data-pa-upload="abc123"]', str(resume)]
    # both scripts ride stdin (#3689): the locate tags with data-pa-upload, the verify reads files[0]
    locate_script = procs[0].stdin.getvalue().decode()
    verify_script = procs[2].stdin.getvalue().decode()
    assert "data-pa-upload" in locate_script
    assert "abc123" in verify_script and "files" in verify_script


async def test_upload_resolves_a_label_through_the_bd12mo1_locator_first(monkeypatch):
    """r1/r2: a LABEL is resolved FRESH by the shared bd-12mo.1 resolver (an enumerate eval),
    and the resolved `[data-ab-field]` selector is what the locate/tag step then acts on —
    resolve, locate, upload, verify, all on stdin."""
    root = storage.capture_root().resolve()
    resume = root / "cv.pdf"
    resume.write_bytes(b"%PDF resume")
    fields = [{"label": "Résumé", "labels": ["Résumé"], "selector": '[data-ab-field="6"]', "kind": "file"}]
    enum = json.dumps({"mode": "enumerate", "fields": fields}).encode()
    locate = json.dumps({"ok": True, "selector": '[data-pa-upload="n"]', "label": "Résumé"}).encode()
    verify = json.dumps({"ok": True, "name": "cv.pdf", "error": ""}).encode()
    rec, procs = [], []
    state = {"evals": 0}

    def _popen(argv, **kw):
        rec.append(list(argv))
        out = b"(ok)"
        if argv[1:2] == ["eval"]:
            out = (enum, locate, verify)[state["evals"]]
            state["evals"] += 1
        p = _FakeProc(argv, out=out)
        procs.append(p)
        return p

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "Résumé", "file_path": "cv.pdf"})
    assert out == "Uploaded cv.pdf to Résumé"
    assert [a[1] for a in rec] == ["eval", "eval", "upload", "eval"]
    assert "abEnumerate" in procs[0].stdin.getvalue().decode()           # the resolver runs first
    assert json.dumps('[data-ab-field="6"]') in procs[1].stdin.getvalue().decode()  # tagged the match
    assert rec[2] == ["ab", "upload", '[data-pa-upload="n"]', str(resume)]


async def test_upload_marks_the_file_in_flight_so_a_prune_cannot_take_it(monkeypatch):
    """r6: while the upload runs, the source file is protected from capture pruning — even a
    prune with a zero budget fired mid-upload leaves it in place."""
    root = storage.capture_root().resolve()
    resume = root / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4 resume content")
    locate = json.dumps({"ok": True, "selector": '[data-pa-upload="x"]', "label": "Résumé"}).encode()
    verify = json.dumps({"ok": True, "name": "cv.pdf", "error": ""}).encode()
    seen, state = {}, {"evals": 0}

    def _popen(argv, **kw):
        out = b"(ok)"
        if argv[1:2] == ["eval"]:
            out = locate if state["evals"] == 0 else verify
            state["evals"] += 1
        elif argv[1:2] == ["upload"]:
            # a prune that would otherwise delete every capture fires mid-upload
            storage.prune_captures(max_files=0, max_bytes=0)
            seen["survived"] = resume.is_file()
        return _FakeProc(argv, out=out)

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out == "Uploaded cv.pdf to Résumé"
    assert seen["survived"] is True   # the in-flight source was shielded from the prune


# ── the tool: verify failures and locate errors are hard errors ────────────────────


async def test_upload_readback_mismatch_is_a_hard_error(monkeypatch):
    """r5: the input reading back a DIFFERENT filename than was uploaded is an Error naming
    both — never a success. This is the guard against a silent non-attach."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF resume")
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": True, "selector": '[data-pa-upload="x"]', "label": "Résumé"},
        {"ok": True, "name": "stale-old-file.pdf", "error": ""}))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "Uploaded" not in out
    assert "stale-old-file.pdf" in out and "cv.pdf" in out


async def test_upload_empty_readback_is_a_hard_error_and_surfaces_validation(monkeypatch):
    """r5: an empty read-back (nothing attached) is an Error, and any field validation text is
    surfaced so the agent learns why."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF resume")
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": True, "selector": '[data-pa-upload="x"]', "label": "Résumé"},
        {"ok": True, "name": "", "error": "Résumé is required"}))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "nothing is attached" in out
    assert "Résumé is required" in out


# ── #4032 bug 3: the attach survives the widget RE-RENDERING its file input ─────────


async def test_upload_confirms_via_the_displayed_filename_after_a_rerender(monkeypatch):
    """bug 3 (mocked CLI): the widget re-rendered its input (the nonce node is gone, the fresh
    input reads empty), but the field still SHOWS the filename — the tool confirms the attach via
    that displayed filename, instead of the old 'could not be found to verify' error."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF resume")
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": True, "selector": '[data-pa-upload="x"]', "label": "Résumé",
         "id": "resume", "name": "resume", "container": "#resume_block"},
        {"ok": True, "name": "", "displayed": True, "error": ""}))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out == "Uploaded cv.pdf to Résumé (verified via the field's displayed filename)"


async def test_upload_verify_script_carries_the_id_name_and_container(monkeypatch):
    """The verify eval is handed the input's id/name and the container selector locate found, so a
    re-render that drops the nonce can still re-find the input (and read its displayed chip)."""
    root = storage.capture_root().resolve()
    resume = root / "cv.pdf"
    resume.write_bytes(b"%PDF resume")
    procs = []
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": True, "selector": '[data-pa-upload="n"]', "label": "Résumé",
         "id": "resume", "name": "resume", "container": "#resume_block"},
        {"ok": True, "name": "cv.pdf", "error": ""}, procs=procs))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out == "Uploaded cv.pdf to Résumé"
    verify_script = procs[-1].stdin.getvalue().decode()
    assert '"resume"' in verify_script            # the input's id + name, to re-find it fresh
    assert "resume_block" in verify_script        # the field container, for the displayed-text check
    assert "cv.pdf" in verify_script              # the basename to look for in the container


def test_render_upload_confirms_via_the_displayed_filename():
    """bug 3: input re-rendered empty (name='') but the field shows the filename (displayed=true)
    → success naming the evidence used."""
    out = forms.render_upload(json.dumps({"ok": True, "name": "", "displayed": True, "error": ""}),
                              "#resume", "cv.pdf", label="Résumé")
    assert out == "Uploaded cv.pdf to Résumé (verified via the field's displayed filename)"


def test_render_upload_prefers_a_read_back_name_over_the_display():
    """(a)/(b): a re-found input that actually carries the file wins — the success line does NOT
    claim the displayed-filename evidence."""
    out = forms.render_upload(json.dumps({"ok": True, "name": "cv.pdf", "displayed": True, "error": ""}),
                              "#resume", "cv.pdf", label="Résumé")
    assert out == "Uploaded cv.pdf to Résumé"


def test_render_upload_empty_and_not_displayed_is_a_hard_error():
    """r4: no attached file AND no displayed filename is a hard error — never a silent success —
    and any field validation text is surfaced."""
    out = forms.render_upload(
        json.dumps({"ok": True, "name": "", "displayed": False, "error": "The file could not be attached"}),
        "#transcript", "cv.pdf", label="Transcript")
    assert out.startswith("Error:") and "nothing is attached" in out
    assert "could not be attached" in out


async def test_upload_no_file_input_in_container_is_an_error(monkeypatch):
    """r2 (negative): the located element isn't a file input and its container has none — a
    clear Error, and the CLI never uploads."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF")
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": False, "reason": "no-file-input", "label": "Cover letter"},
        {"ok": True}, record=rec))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#cover", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "file input" in out
    assert not any(a[1:2] == ["upload"] for a in rec)   # located, then stopped — never uploaded


async def test_upload_multiple_file_inputs_in_container_is_an_error(monkeypatch):
    """r2 (negative): more than one file input in the container is ambiguous, never a guess."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF")
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen(
        {"ok": False, "reason": "multiple", "label": "Documents", "count": 2},
        {"ok": True}, record=rec))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#docs", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "ambiguous" in out and "2" in out
    assert not any(a[1:2] == ["upload"] for a in rec)


async def test_upload_surfaces_a_cli_upload_error(monkeypatch):
    """A non-zero `upload` exit (e.g. the element vanished) is surfaced, not swallowed."""
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF")

    def _popen(argv, **kw):
        if argv[1:2] == ["eval"]:
            return _FakeProc(argv, out=json.dumps(
                {"ok": True, "selector": '[data-pa-upload="x"]', "label": "Résumé"}).encode())
        if argv[1:2] == ["upload"]:
            return _FakeProc(argv, out=b"", err=b"no element", rc=1)
        return _FakeProc(argv, out=b"(ok)")

    monkeypatch.setattr(tools.subprocess, "Popen", _popen)
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "no element" in out


async def test_upload_degrades_on_unreadable_locate_output_instead_of_raising(monkeypatch):
    root = storage.capture_root().resolve()
    (root / "cv.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(tools.subprocess, "Popen", _upload_popen("not json at all", {"ok": True}))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "#resume", "file_path": "cv.pdf"})
    assert out.startswith("Error:")   # never raises


async def test_upload_flag_shaped_field_is_refused_before_anything(monkeypatch):
    """The argv guard fires first: a flag-shaped field never reaches the fence or the CLI."""
    rec = []
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(record=rec))
    out = await _toolmap({"binary": "ab"})["browser_upload"].ainvoke(
        {"field": "--headed", "file_path": "cv.pdf"})
    assert out.startswith("Error:") and "looks like a command-line option" in out
    assert rec == []


# ── the in-page resolution (a real DOM via jsdom) — the Attach-button container climb ──


def _run_upload_js(html: str, selector: str, nonce: str = "n1"):
    """Eval `forms.upload_js(selector, nonce)` against a real DOM (jsdom) and return the JSON
    it produces — the only host-free way to prove the file-input container resolution."""
    if not NODE:
        pytest.skip("node not on PATH")
    probe = subprocess.run([NODE, "-e", "require.resolve('jsdom')"], cwd=REPO,
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("jsdom not installed (run npm ci in apps/web or the repo root)")
    harness = (
        "const { JSDOM } = require('jsdom');\n"
        "const dom = new JSDOM(" + json.dumps(html) + ");\n"
        "global.window = dom.window; global.document = dom.window.document; global.CSS = dom.window.CSS;\n"
        "const script = " + json.dumps(forms.upload_js(selector, nonce)) + ";\n"
        "console.log((0, eval)(script));\n"   # upload_js returns a JSON string
    )
    out = subprocess.run([NODE, "-e", harness], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_ATTACH_BUTTON_HTML = """
<form>
  <div class="field" id="resume-field">
    <label>Resume/CV</label>
    <button type="button">Attach</button>
    <input type="file" name="resume" style="display:none"/>
  </div>
</form>
"""


def test_upload_js_uses_the_single_file_input_hidden_behind_an_attach_button():
    """r2: the located element is the field wrapper (the real input hides behind an "Attach"
    button, as Greenhouse/Ashby do). The driver climbs to the container and tags its single
    file input, so the CLI gets a stable `[data-pa-upload]` selector for it."""
    res = _run_upload_js(_ATTACH_BUTTON_HTML, "#resume-field", nonce="n1")
    assert res["ok"] is True
    assert res["selector"] == '[data-pa-upload="n1"]'
    assert res["name"] == "resume"


def test_upload_js_tags_a_file_input_addressed_directly():
    html = '<form><label for="r">Résumé</label><input id="r" type="file" name="resume"/></form>'
    res = _run_upload_js(html, "#r", nonce="z9")
    assert res["ok"] is True and res["selector"] == '[data-pa-upload="z9"]' and res["name"] == "resume"


def test_upload_js_refuses_more_than_one_file_input_in_the_container():
    html = ('<form><div id="box">'
            '<input type="file" name="a"/><input type="file" name="b"/>'
            '</div></form>')
    res = _run_upload_js(html, "#box", nonce="z")
    assert res["ok"] is False and res["reason"] == "multiple" and res["count"] == 2


def test_upload_js_refuses_when_the_container_has_no_file_input():
    html = '<form><div id="box"><input type="text" name="a"/></div></form>'
    res = _run_upload_js(html, "#box", nonce="z")
    assert res["ok"] is False and res["reason"] == "no-file-input"


def test_upload_js_reports_not_found_for_an_absent_selector():
    res = _run_upload_js("<form></form>", "#nope", nonce="z")
    assert res["ok"] is False and res["reason"] == "not-found"


def test_upload_js_also_returns_the_input_id_and_a_container_selector():
    """bug 3: locate also returns the input's id/name and a stable container selector, so the
    verify step can re-find the input (and read its filename chip) after the widget re-renders and
    the nonce tag rides off with the discarded node."""
    res = _run_upload_js(_ATTACH_BUTTON_HTML, "#resume-field", nonce="n1")
    assert res["ok"] is True and res["name"] == "resume"
    assert res["container"] == "#resume-field"        # the nearest ancestor carrying an id


def test_upload_js_resolves_a_bare_or_hashed_id_file_input_directly():
    """r2: a `#id` OR a bare id that names an input[type=file] resolves to THAT input directly —
    no container climb — even when it is hidden behind an "Attach" button."""
    html = ('<form><div id="wrap"><button type="button">Attach</button>'
            '<input id="resume" type="file" name="resume" style="display:none"/></div></form>')
    for sel in ("#resume", "resume"):
        res = _run_upload_js(html, sel, nonce="z")
        assert res["ok"] is True, (sel, res)
        assert res["id"] == "resume" and res["name"] == "resume", (sel, res)
        assert res["container"] == "#wrap", (sel, res)


_RERENDER_HTML = """
<form>
  <div id="resume_block">
    <label for="resume">Resume/CV</label>
    <input id="resume" type="file" name="resume"/>
    <span class="file-chip"><span class="file-chip__name">live_resume.pdf</span><button type="button">x</button></span>
  </div>
</form>
"""


def _run_upload_verify_js(html, selector, input_id="", input_name="", container="", basename=""):
    """Eval `forms.upload_verify_js(...)` against a real DOM (jsdom) and return the JSON it
    produces — the host-free way to prove the re-render-tolerant read-back (nonce gone, re-find by
    id/name, read the container's filename chip)."""
    if not NODE:
        pytest.skip("node not on PATH")
    probe = subprocess.run([NODE, "-e", "require.resolve('jsdom')"], cwd=REPO,
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("jsdom not installed (run npm ci in apps/web or the repo root)")
    harness = (
        "const { JSDOM } = require('jsdom');\n"
        "const dom = new JSDOM(" + json.dumps(html) + ");\n"
        "global.window = dom.window; global.document = dom.window.document; global.CSS = dom.window.CSS;\n"
        "const script = " + json.dumps(
            forms.upload_verify_js(selector, input_id, input_name, container, basename)) + ";\n"
        "console.log((0, eval)(script));\n"
    )
    out = subprocess.run([NODE, "-e", harness], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_upload_verify_js_confirms_via_the_chip_after_a_rerender():
    """bug 3: the nonce node is gone (re-rendered away) and the fresh input is empty, but the field
    container still SHOWS the filename as a chip — verify reports displayed=true (the chip's remove
    button stripped), so the attach is confirmed rather than reported missing."""
    res = _run_upload_verify_js(_RERENDER_HTML, '[data-pa-upload="gone"]',
                                input_id="resume", input_name="resume",
                                container="#resume_block", basename="live_resume.pdf")
    assert res["ok"] is True
    assert res["name"] == ""              # the re-rendered input carries no files
    assert res["displayed"] is True       # but the field shows the filename
    assert res["error"] == ""


def test_upload_verify_js_surfaces_a_rejection_message_with_no_filename():
    """r4: a rejecting widget clears the input and shows a validation message — verify returns an
    empty name, displayed=false, and surfaces the message."""
    html = ('<form><div id="box"><label for="t">Transcript</label>'
            '<input id="t" type="file" name="t"/>'
            '<div class="field-error" role="alert">The file could not be attached</div></div></form>')
    res = _run_upload_verify_js(html, '[data-pa-upload="gone"]', input_id="t", input_name="t",
                                container="#box", basename="rejected.pdf")
    assert res["ok"] is True and res["name"] == "" and res["displayed"] is False
    assert "could not be attached" in res["error"]


def test_upload_is_a_registered_tool_with_a_usable_docstring():
    t = _toolmap()["browser_upload"]
    assert "browser_upload" in EXPECTED_TOOLS
    assert t.description and len(t.description) >= 20


# ── register() wiring ────────────────────────────────────────────────────────────


def _registry(cfg=None):
    return FakeRegistry(cfg or {}, plugin_id="agent_browser", plugin_dir=ROOT)


def test_register_wires_tools_and_panel_routers(monkeypatch):
    _probe_env(monkeypatch, which="/opt/ab")
    reg = _registry()
    _PKG.register(reg)
    assert {t.name for t in reg.tools} == EXPECTED_TOOLS
    prefixes = [p for p, _ in reg.routers]
    assert None in prefixes  # the panel PAGE (host default prefix /plugins/agent_browser)
    assert "/api/plugins/agent_browser" in prefixes  # gated data routes
    assert reg.surfaces == []  # no dashboard lifecycle surface anymore
    assert reg.skill_dirs == [] and reg.workflow_dirs == []  # auto-discovered (ADR 0027)


def test_register_preflights_so_a_broken_setup_is_visible_before_the_first_call(monkeypatch):
    _probe_env(monkeypatch, which=None)
    reg = _registry()
    _PKG.register(reg)
    assert preflight.CLI_GAP in reg.setup_gaps          # the banner is up at load time
    assert {t.name for t in reg.tools} == EXPECTED_TOOLS  # …and the tools still register


def test_register_survives_a_preflight_that_raises(monkeypatch):
    monkeypatch.setattr(preflight, "report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    reg = _registry()
    _PKG.register(reg)
    assert {t.name for t in reg.tools} == EXPECTED_TOOLS


# ── the plugin is discovered, and loads only when enabled ─────────────────────────


def test_the_real_loader_finds_the_bundled_plugin_and_honours_enabled_false(monkeypatch):
    from graph.config import LangGraphConfig
    from graph.plugins import loader

    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [REPO / "plugins"])
    off = loader.load_plugins(LangGraphConfig(plugins_enabled=[]))
    assert not any(t.name.startswith("browser_") for t in off.tools)   # ships disabled

    _probe_env(monkeypatch, which="/opt/ab")
    on = loader.load_plugins(LangGraphConfig(plugins_enabled=["agent_browser"]))
    assert EXPECTED_TOOLS <= {t.name for t in on.tools}
    assert "/plugins/agent_browser/panel" in on.public_paths   # the view page is public chrome


# ── manifest / catalog coherence + settings ──────────────────────────────────────


def test_manifest_identity_and_defaults():
    m = _manifest()
    assert m["id"] == "agent_browser" and m["enabled"] is False
    assert m["config_section"] == "agent_browser"
    assert m["views"][0]["path"] == "/plugins/agent_browser/panel"
    assert m["capabilities"]["network"] == ["*"]
    assert m["capabilities"]["filesystem"] == "scoped"


def test_the_folder_is_named_after_the_plugin_id():
    assert ROOT.name == _manifest()["id"]


def test_bundled_version_is_above_every_standalone_release():
    version = tuple(int(p) for p in str(_manifest()["version"]).split("."))
    assert version > LAST_STANDALONE_VERSION, (
        f"bundled agent_browser {version} must be above the retired repo's last version "
        f"{LAST_STANDALONE_VERSION} — an untracked copy that isn't older than the bundled "
        f"one wins (#1574)"
    )


def test_manifest_supersedes_the_retired_repo():
    from graph.plugins.manifest import canonical_source, load_manifest

    m = load_manifest(ROOT)
    assert m is not None
    assert [canonical_source(u) for u in m.supersedes] == [canonical_source(RETIRED_REPO)]


def test_the_import_dropped_the_standalone_repo_scaffolding():
    """Core owns ruff/pytest config, CI, and the agent-instruction files; a bundled copy
    carries none of them, and `repository:`/`min_protoagent_version` are meaningless for a
    plugin that ships WITH the host (a stale floor could only refuse the copy that came
    with the release)."""
    m = _manifest()
    assert "repository" not in m and "min_protoagent_version" not in m
    for gone in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", ".gitignore",
                 "PROTO.md", "CLAUDE.md", "AGENTS.md", "CHANGELOG.md", ".github", ".beads",
                 ".ruff_cache", "tests", "conftest.py"):
        assert not (ROOT / gone).exists(), f"{gone} should not be vendored"
    # the repo's tests/ came across as tests/test_agent_browser_plugin.py, which runs in
    # the host suite — a vendored tests/ dir would be collected twice and drift
    assert (REPO / "tests" / "test_agent_browser_plugin.py").is_file()


def test_settings_fields_are_valid_and_back_real_config():
    m = _manifest()
    by_key = {f["key"]: f for f in m["settings"]}
    assert by_key["headed"]["type"] == "bool" and by_key["timeout_s"]["type"] == "number"
    # the switchover dropped these knobs entirely:
    assert "panel_mode" not in by_key and "dashboard_port" not in by_key
    assert "panel_mode" not in m["config"] and "manage_dashboard" not in m["config"]
    # every settings key has a declared default in config:
    assert set(by_key) <= set(m["config"])
    # and nothing the manifest never declares is read as a config key (the require_auth trap)
    assert "require_auth" not in m["config"] and "require_auth" not in by_key


def test_max_response_bytes_default_is_declared():
    m = _manifest()
    assert m["config"]["max_response_bytes"] == 200000  # the wrapper's default cap
    by_key = {f["key"]: f for f in m["settings"]}
    assert by_key["max_response_bytes"]["type"] == "number"  # operator-editable knob


def test_no_pip_dependencies_are_declared():
    """The only external requirement is the CLI on PATH; `websockets` and `fastapi` come
    from the host (a uvicorn[standard] extra)."""
    assert "requires_pip" not in _manifest()


# ── the panel routes (page / ticket / WS gating / nav) ───────────────────────────


def _app(cfg=None):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(bp.build_panel_router(cfg or {}), prefix="/plugins/agent_browser")
    app.include_router(bp.build_panel_data_router(cfg or {}), prefix="/api/plugins/agent_browser")
    return app


def test_panel_page_wires_canvas_stream_and_input():
    from fastapi.testclient import TestClient

    html = TestClient(_app({})).get("/plugins/agent_browser/panel").text
    assert "/_ds/plugin-kit.css" in html  # DS kit
    assert 'location.pathname.split("/plugins/")[0]' in html  # slug-aware base
    assert 'id="cv"' in html and "createImageBitmap" in html  # the canvas + frame painting
    assert "/api/plugins/agent_browser/stream-ticket" in html  # mint a ticket (gated)
    assert "/api/plugins/agent_browser/stream" in html  # the WS stream
    assert 'u.protocol==="https:" ? "wss:" : "ws:"' in html  # http→ws upgrade
    assert 'send({t:"mouse"' in html and 'send({t:"key"' in html  # input forwarding
    assert "ResizeObserver" in html and 'send({t:"resize"' in html  # responsive viewport tracking
    assert "object-fit:contain" in html  # no distortion during resize
    assert "visibilitychange" in html and 'send({t:"refresh"' in html  # refresh when re-shown
    assert "/api/plugins/agent_browser/nav" in html and "kit.apiFetch" in html  # nav via gated route
    assert "startBrowser" in html and 'const HOME="";' in html  # empty-state Start; blank home default
    # the removed dashboard-embed / screenshot modes leave no trace:
    assert "/api/plugins/agent_browser/shot" not in html
    assert 'id="f"' not in html and "Open the console locally" not in html


def test_panel_home_url_is_injected_safely():
    from fastapi.testclient import TestClient

    # a configured homepage lands as a JS string literal the Start button + auto-open use
    html = TestClient(_app({"home_url": "https://example.com"})).get("/plugins/agent_browser/panel").text
    assert 'const HOME="https://example.com";' in html
    assert "__HOME_URL__" not in html  # placeholder fully interpolated
    # a </script>-injection attempt is escaped: the quote is JSON-escaped and the `<`
    # becomes \u003c, so it neither breaks the JS string nor closes the inline script.
    evil = TestClient(_app({"home_url": '"</script>'})).get("/plugins/agent_browser/panel").text
    assert 'const HOME="\\"\\u003c/script>";' in evil


def test_a_server_note_is_escaped_before_it_reaches_innerhtml():
    """The panel's empty state renders a server note as HTML (it carries a Start button),
    and that note now includes setup text and filesystem paths from the CLI — so it goes
    through an escaper."""
    from fastapi.testclient import TestClient

    html = TestClient(_app({})).get("/plugins/agent_browser/panel").text
    assert "function esc(" in html
    assert "esc(note)" in html   # the note is escaped, not concatenated raw


def test_stream_ticket_route_mints_a_ticket():
    from fastapi.testclient import TestClient

    body = TestClient(_app()).post("/api/plugins/agent_browser/stream-ticket").json()
    assert isinstance(body.get("ticket"), str) and len(body["ticket"]) > 10


def test_stream_ws_rejects_a_bad_ticket():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    c = TestClient(_app())
    # no valid ticket → handler closes (1008) before accept → connect raises.
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/api/plugins/agent_browser/stream?ticket=nope"):
            pass


def test_stream_ws_accepts_valid_ticket_then_reports_no_page(monkeypatch):
    from fastapi.testclient import TestClient

    # resolve returns no page → the handler accepts, sends an error frame, and closes
    # (exercises the ticket gate + accept path without a real browser/CDP).
    monkeypatch.setattr(bp.browser_stream, "resolve_page_target",
                        lambda binary, timeout: (None, "no page open"))
    c = TestClient(_app())
    ticket = c.post("/api/plugins/agent_browser/stream-ticket").json()["ticket"]
    with c.websocket_connect(f"/api/plugins/agent_browser/stream?ticket={ticket}") as ws:
        assert ws.receive_json() == {"t": "error", "msg": "no page open"}


def test_stream_ws_turns_a_missing_binary_into_the_setup_sentence(monkeypatch):
    """The panel used to show the raw ``'agent-browser' not on PATH`` — the same dead end
    the tools gave the model. It now says what to do, matching the operator banner."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(bp.browser_stream, "resolve_page_target",
                        lambda binary, timeout: (None, "'agent-browser' not on PATH"))
    _probe_env(monkeypatch, which=None)
    c = TestClient(_app())
    ticket = c.post("/api/plugins/agent_browser/stream-ticket").json()["ticket"]
    with c.websocket_connect(f"/api/plugins/agent_browser/stream?ticket={ticket}") as ws:
        msg = ws.receive_json()["msg"]
    assert "npm i -g agent-browser" in msg and "binary" in msg


def test_stream_ticket_is_single_use():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    c = TestClient(_app())
    ticket = c.post("/api/plugins/agent_browser/stream-ticket").json()["ticket"]
    assert bp.browser_stream.consume_ticket(ticket) is True   # burn it directly
    with pytest.raises(WebSocketDisconnect):                  # replay is rejected
        with c.websocket_connect(f"/api/plugins/agent_browser/stream?ticket={ticket}"):
            pass


def test_nav_route_validates(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(bp.subprocess, "run", fake_run(record=[]))
    c = TestClient(_app())
    assert c.post("/api/plugins/agent_browser/nav", json={"action": "bogus"}).json()["ok"] is False
    assert c.post("/api/plugins/agent_browser/nav", json={"action": "open"}).json()["error"] == "url required"
    assert c.post("/api/plugins/agent_browser/nav", json={"action": "reload"}).json()["ok"] is True


def test_nav_open_applies_launch_flags(monkeypatch):
    from fastapi.testclient import TestClient

    rec = []
    monkeypatch.setattr(bp.subprocess, "run", fake_run(record=rec))
    # argv[0] is the RESOLVED CLI now; with none resolvable (and no first-use download) it is
    # the configured name, whatever the developer's own PATH holds.
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    # a session started from the panel gets the same headed/stealth setup as the agent's
    c = TestClient(_app({"headed": True, "stealth": True, "cli_autofetch": False}))
    c.post("/api/plugins/agent_browser/nav", json={"action": "open", "url": "https://x.com"})
    argv = rec[-1]
    assert argv[-2:] == ["open", "https://x.com"]
    assert "--headed" in argv and "--args" in argv  # launch flags applied on open
    # back/forward/reload don't relaunch, so they carry no flags
    c.post("/api/plugins/agent_browser/nav", json={"action": "reload"})
    assert rec[-1] == ["agent-browser", "reload"]


def test_nav_reports_the_setup_sentence_when_the_cli_is_missing(monkeypatch):
    from fastapi.testclient import TestClient

    # One patch for both call sites: `bp` and `preflight` share the `subprocess` module
    # object, so the nav command must raise while the preflight's own probes still answer.
    _probe_env(monkeypatch, which=None)
    probe_run = preflight.subprocess.run

    def _run(args, **kw):
        if args[1:2] in (["--version"], ["doctor"]):
            return probe_run(args, **kw)
        raise FileNotFoundError()

    monkeypatch.setattr(bp.subprocess, "run", _run)
    body = TestClient(_app()).post("/api/plugins/agent_browser/nav", json={"action": "reload"}).json()
    assert body["ok"] is False and "npm i -g agent-browser" in body["error"]


def test_the_panel_actually_renders_a_failed_nav_instead_of_swallowing_it():
    """The route builds a good sentence; the page used to drop it on the floor
    (`try{ await apiFetch(…) }catch(_){}` never reads the body, and a 200 throws nothing),
    so the operator clicked Go and watched nothing happen."""
    from fastapi.testclient import TestClient

    html = TestClient(_app({})).get("/plugins/agent_browser/panel").text
    assert "b.ok===false" in html and "showStart(b.error)" in html
    # …but never over a LIVE page: `back` with no history exits non-zero while the page is
    # still showing, so a connected panel toasts instead of claiming "No page open"
    assert "if(connected){ toast(b.error); }" in html and 'id="toast"' in html


# ── #3451 (b): the /panel/dash cookie gate is GONE, and here is why ───────────────


def test_the_dash_alias_and_its_cookie_gate_are_not_vendored():
    """#21's gate keyed off ``cfg["require_auth"]``, which nothing ever set, so it could
    never fire. Vendoring a dead gate would leave an operator believing the panel is
    protected when it is not."""
    from fastapi.testclient import TestClient

    c = TestClient(_app({"require_auth": True}))   # the key that used to arm it
    assert c.get("/plugins/agent_browser/panel/dash").status_code == 404
    assert c.post("/api/plugins/agent_browser/dash-session").status_code == 404
    # and the page no longer makes the two extra round-trips per connect
    assert "dash-session" not in TestClient(_app()).get("/plugins/agent_browser/panel").text


def test_a_view_path_exemption_is_a_prefix_so_the_page_was_never_the_boundary():
    """The host-side fact that made the cookie gate unsound: a manifest view path is
    auto-exempted from the bearer gate as a PREFIX, so ``/panel/dash`` was exempt exactly
    like ``/panel`` — and both served byte-identical HTML. Only core can check this, which
    is why the standalone repo couldn't."""
    from a2a_impl import auth
    from graph.plugins.manifest import load_manifest

    m = load_manifest(ROOT)
    assert "/plugins/agent_browser/panel" in m.public_paths
    auth.set_public_prefixes(m.public_paths)
    try:
        assert auth._is_public("/plugins/agent_browser/panel") is True
        assert auth._is_public("/plugins/agent_browser/panel/dash") is True    # the same door
        assert auth._is_public("/api/plugins/agent_browser/stream-ticket") is False
        assert auth._is_public("/api/plugins/agent_browser/nav") is False
    finally:
        auth.set_public_prefixes([])


def test_the_capability_routes_are_the_ones_under_api():
    """What actually gates the browser: every data/action route lives under
    ``/api/plugins/agent_browser`` (operator bearer), and only the WS is self-gated."""
    router = bp.build_panel_data_router({})
    paths = {r.path for r in router.routes}
    assert paths == {"/stream-ticket", "/stream", "/nav"}
    page = bp.build_panel_router({})
    assert {r.path for r in page.routes} == {"/panel"}


# ── the CDP bridge: pure, host-free brains ───────────────────────────────────────


def test_http_base_from_ws_url():
    assert bs._http_base_from_ws(
        "ws://127.0.0.1:52886/devtools/browser/4580bed9") == "http://127.0.0.1:52886"


def _t(type_, url, ws="ws://x"):
    return {"type": type_, "url": url, "webSocketDebuggerUrl": ws}


def test_pick_page_prefers_current_url():
    targets = [_t("page", "https://a.com", "ws://a"), _t("page", "https://b.com", "ws://b")]
    assert bs.pick_page_target(targets, "https://b.com") == "ws://b"


def test_pick_page_falls_back_to_first_real_page():
    targets = [_t("page", "chrome://newtab/", "ws://newtab"), _t("page", "https://real.com", "ws://real")]
    # no current-url match → skip chrome:// surfaces, take the first real page.
    assert bs.pick_page_target(targets, "https://gone.com") == "ws://real"


def test_pick_page_skips_non_page_and_missing_ws():
    targets = [
        {"type": "service_worker", "url": "x", "webSocketDebuggerUrl": "ws://sw"},
        {"type": "page", "url": "https://c.com"},  # no ws url → not streamable
        _t("page", "https://d.com", "ws://d"),
    ]
    assert bs.pick_page_target(targets, "") == "ws://d"


def test_pick_page_none_when_no_pages():
    assert bs.pick_page_target([{"type": "iframe", "url": "x", "webSocketDebuggerUrl": "ws://i"}], "") is None
    assert bs.pick_page_target([], "") is None


def test_mouse_down_maps_to_pressed_with_button_and_count():
    method, p = bs.input_to_cdp(
        {"t": "mouse", "action": "down", "x": 10, "y": 20, "button": "left", "clickCount": 2, "buttons": 1})
    assert method == "Input.dispatchMouseEvent"
    assert p["type"] == "mousePressed" and p["x"] == 10.0 and p["y"] == 20.0
    assert p["button"] == "left" and p["clickCount"] == 2 and p["buttons"] == 1


def test_mouse_move_has_no_button_or_clickcount():
    _, p = bs.input_to_cdp({"t": "mouse", "action": "move", "x": 5, "y": 6})
    assert p["type"] == "mouseMoved"
    assert "button" not in p and "clickCount" not in p


def test_mouse_up_maps_to_released():
    _, p = bs.input_to_cdp({"t": "mouse", "action": "up", "x": 1, "y": 2, "button": "left"})
    assert p["type"] == "mouseReleased"


def test_wheel_maps_to_mousewheel_deltas():
    method, p = bs.input_to_cdp({"t": "wheel", "x": 3, "y": 4, "dx": 0, "dy": 120})
    assert method == "Input.dispatchMouseEvent" and p["type"] == "mouseWheel"
    assert p["deltaX"] == 0.0 and p["deltaY"] == 120.0


def test_key_down_with_text_carries_text_and_vkey():
    method, p = bs.input_to_cdp(
        {"t": "key", "action": "down", "key": "a", "code": "KeyA", "text": "a", "keyCode": 65})
    assert method == "Input.dispatchKeyEvent" and p["type"] == "keyDown"
    assert p["text"] == "a" and p["key"] == "a" and p["code"] == "KeyA"
    assert p["windowsVirtualKeyCode"] == 65 and p["nativeVirtualKeyCode"] == 65


def test_key_up_omits_text():
    _, p = bs.input_to_cdp({"t": "key", "action": "up", "key": "a", "code": "KeyA", "text": "a"})
    assert p["type"] == "keyUp" and "text" not in p


def test_modifiers_bitmask():
    _, p = bs.input_to_cdp({"t": "mouse", "action": "move", "x": 0, "y": 0, "ctrl": True, "shift": True})
    assert p["modifiers"] == (2 | 8)  # Ctrl=2, Shift=8


def test_unknown_messages_return_none():
    assert bs.input_to_cdp({"t": "bogus"}) is None
    assert bs.input_to_cdp({"t": "mouse", "action": "wat"}) is None
    assert bs.input_to_cdp({"t": "key", "action": "wat"}) is None


def test_resolve_returns_note_when_no_session(monkeypatch):
    def no_url(args, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no session")

    monkeypatch.setattr(bs.subprocess, "run", no_url)
    ws, note = bs.resolve_page_target("ab")
    assert ws is None and "no session" in note


def test_resolve_finds_active_page(monkeypatch):
    def _run(args, **kw):
        if args[1:] == ["get", "cdp-url"]:
            return types.SimpleNamespace(returncode=0, stdout="ws://127.0.0.1:9/devtools/browser/x\n", stderr="")
        if args[1:] == ["get", "url"]:
            return types.SimpleNamespace(returncode=0, stdout="https://ex.com\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bs.subprocess, "run", _run)
    listing = b'[{"type":"page","url":"https://ex.com","webSocketDebuggerUrl":"ws://127.0.0.1:9/devtools/page/P"}]'
    monkeypatch.setattr(bs.urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(listing))
    ws, note = bs.resolve_page_target("ab")
    assert ws == "ws://127.0.0.1:9/devtools/page/P" and note == ""


def test_ticket_mint_then_consume_is_single_use():
    t = bs.mint_ticket()
    assert bs.consume_ticket(t) is True    # first use validates
    assert bs.consume_ticket(t) is False   # replay is burned


def test_consume_rejects_unknown_and_empty():
    assert bs.consume_ticket("never-minted") is False
    assert bs.consume_ticket("") is False


def test_ticket_expires_after_ttl(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(bs.time, "monotonic", lambda: clock["t"])
    t = bs.mint_ticket()
    clock["t"] += bs._TICKET_TTL + 1     # advance past the TTL
    assert bs.consume_ticket(t) is False


def test_viewport_metrics_normal_hidpi():
    cw, ch, scale, mw, mh = bs.viewport_metrics(800, 1000, 2)
    assert (cw, ch, scale) == (800, 1000, 2.0)
    assert (mw, mh) == (1600, 2000)  # frame = css × dpr


def test_viewport_metrics_clamps_giant_dock():
    cw, ch, scale, mw, mh = bs.viewport_metrics(5000, 5000, 3)
    assert (cw, ch, scale) == (2048, 2048, 2.0)  # css ≤2048, dpr ≤2
    assert (mw, mh) == (2560, 2560)  # frame long side capped at 2560


def test_viewport_metrics_floors_degenerate():
    assert bs.viewport_metrics(0, 0, 0) == (1, 1, 1.0, 1, 1)
    # sub-1 dpr floors to 1.0 (never upscale-blur by pretending lo-dpi)
    assert bs.viewport_metrics(640, 480, 0.5)[2] == 1.0


# ── the launch-flag builder (incl. the anti-detection layer, vendored UNCHANGED) ──


def test_default_is_empty():
    assert rt.launch_flags({}) == []
    assert rt.launch_flags(None) == []


def test_curated_flags_stable_order():
    # headless keeps the argv clean (headed injects anti-throttle --args, tested separately)
    f = rt.launch_flags({"profile": "P", "device": "iPhone 16 Pro",
                         "allowed_domains": "x.com", "confirm_actions": "nav", "max_output": 500})
    assert f == ["--profile", "P", "--device", "iPhone 16 Pro",
                 "--allowed-domains", "x.com", "--confirm-actions", "nav", "--max-output", "500"]


def test_headed_injects_anti_throttle_args():
    f = rt.launch_flags({"headed": True})
    assert f[0] == "--headed" and f[1] == "--args"
    aset = set(f[2].split(","))
    assert {"--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling"} <= aset  # keep a headed window rendering unfocused


def test_headless_stays_clean():
    assert rt.launch_flags({"allowed_domains": "x.com"}) == ["--allowed-domains", "x.com"]  # no --args


def test_stealth_headless_adds_automation_arg_and_real_ua():
    f = rt.launch_flags({"stealth": True})  # headless by default
    assert f[f.index("--user-agent") + 1].startswith("Mozilla/5.0")
    assert "HeadlessChrome" not in f[f.index("--user-agent") + 1]
    assert "--disable-blink-features=AutomationControlled" in f[f.index("--args") + 1]


def test_stealth_headed_skips_ua_but_keeps_automation_arg():
    f = rt.launch_flags({"stealth": True, "headed": True})
    assert "--user-agent" not in f  # a headed browser already reports a real UA
    assert "--disable-blink-features=AutomationControlled" in f[f.index("--args") + 1]


def test_explicit_ua_wins_and_browser_args_merge():
    f = rt.launch_flags({"stealth": True, "user_agent": "UA/1", "browser_args": "--foo, --bar"})
    assert f[f.index("--user-agent") + 1] == "UA/1"  # explicit override beats the stealth default
    args = f[f.index("--args") + 1].split(",")
    assert "--foo" in args and "--bar" in args
    assert "--disable-blink-features=AutomationControlled" in args  # merged, not duplicated
    assert args.count("--disable-blink-features=AutomationControlled") == 1


def test_browser_args_without_stealth_passes_through():
    f = rt.launch_flags({"browser_args": "--mute-audio"})
    assert f == ["--args", "--mute-audio"]
    assert "--user-agent" not in f  # no stealth → no UA injection


def test_the_stealth_surface_is_intact_and_unedited():
    """The anti-detection / UA-spoofing options were vendored VERBATIM pending a
    drop-or-keep ruling (#3451 "Decision needed"). The ruling (2026-09-12): stealth SHIPS
    with core and stays OFF by default; when on, the spoofed UA claims the installed
    Chrome's real version instead of a hard-coded one (tests/test_agent_browser_cli_fetch.py
    pins that). This pins the rest of the surface — the three config keys, their settings
    rows, the off default, and the two runtime.py mechanisms — so a later edit is a
    deliberate answer, not a drive-by."""
    m = _manifest()
    assert {"stealth", "user_agent", "browser_args"} <= set(m["config"])
    assert m["config"]["stealth"] is False and m["config"]["user_agent"] == ""
    assert {"stealth", "user_agent", "browser_args"} <= {f["key"] for f in m["settings"]}
    source = (ROOT / "runtime.py").read_text(encoding="utf-8")
    assert "_STEALTH_UA" in source and "AutomationControlled" in source
    assert rt._STEALTH_UA.startswith("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")


# ── the skill + workflows reference tools that actually exist ─────────────────────


def _in_tree_tool_names() -> set[str]:
    """Every ``@tool`` name defined anywhere in the tree (AST, so nothing is imported).

    The cross-repo drift this closes: the skill named ``browser_screenshot`` and nothing
    could check the tool still existed. It sweeps the whole tree, not just this plugin, so
    a skill may legitimately point at a core/other-plugin tool (``save_file_artifact``).
    """
    names: set[str] = set()
    for base in (REPO / "plugins", REPO / "tools", REPO / "graph"):
        for path in base.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    label = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                    if label != "tool":
                        continue
                    args = dec.args if isinstance(dec, ast.Call) else []
                    explicit = next(
                        (a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)), None
                    )
                    names.add(explicit or node.name)
    return names


def _skill_text() -> str:
    return (ROOT / "skills" / "web-browse" / "SKILL.md").read_text(encoding="utf-8")


def _workflow_docs() -> list[tuple[str, dict]]:
    return [(p.name, yaml.safe_load(p.read_text(encoding="utf-8")))
            for p in sorted((ROOT / "workflows").glob("*.yaml"))]


def test_the_sweep_actually_finds_this_plugin_s_tools():
    """If extraction ever finds nothing, the guard is broken — not the skill."""
    assert EXPECTED_TOOLS <= _in_tree_tool_names()


def test_every_tool_the_skill_declares_exists():
    from graph.skills.loader import parse_skill_md

    skill = parse_skill_md(ROOT / "skills" / "web-browse" / "SKILL.md")
    assert skill is not None and skill.name == "web-browse"
    declared = set(skill.tools_used or [])
    assert declared, "the web-browse skill should declare its tools"
    assert declared <= EXPECTED_TOOLS, sorted(declared - EXPECTED_TOOLS)
    assert "browser_pdf" in declared   # the new capability is discoverable


def test_the_skill_frontmatter_lists_the_form_filling_tools():
    """#4032 A4: the web-browse skill now teaches form filling, so its advisory `tools:` list
    must carry the form tools the Filling forms section relies on — or the model can't see
    them as part of the skill."""
    from graph.skills.loader import parse_skill_md

    declared = set(parse_skill_md(ROOT / "skills" / "web-browse" / "SKILL.md").tools_used or [])
    assert {"browser_form_read", "browser_select", "browser_upload"} <= declared


def test_the_skill_has_a_filling_forms_section_with_the_form_doctrine():
    """r5: the Filling forms doctrine is present — read first, address by label, select for
    dropdowns, country-before-number, upload from a browser_pdf, read-back diff, and never
    submitting (or solving a captcha/login) without the operator."""
    text = _skill_text()
    assert "## Filling forms" in text
    assert "browser_form_read" in text and "browser_select" in text and "browser_upload" in text
    low = text.lower()
    assert "label" in low and "@en" in low                       # address by label, not @eN
    assert "never" in low and "type" in low and "enter" in low   # never type-and-Enter for selects
    assert "country" in low and "number" in low                  # country before the number
    assert "browser_pdf" in text                                 # upload a file made with browser_pdf
    assert "js_fallback" in low                                  # the click fallback is taught
    assert "submit" in low and ("captcha" in low or "login" in low)   # consent + human steps


def test_every_backticked_identifier_in_the_skill_is_a_real_tool():
    """A `` `name_like_this` `` in a skill reads to the model as a tool it can call, so
    each one must resolve. Mutation-checked: adding `` `totally_fake_tool` `` fails this."""
    in_tree = _in_tree_tool_names()
    cited = {m for m in re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", _skill_text())}
    assert cited, "expected the skill to cite tools in backticks"
    assert cited <= in_tree, sorted(cited - in_tree)


def test_every_tool_the_workflows_name_exists():
    in_tree = _in_tree_tool_names()
    docs = _workflow_docs()
    assert len(docs) == 2, [n for n, _ in docs]
    for name, doc in docs:
        text = yaml.safe_dump(doc)
        cited = {m for m in re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", text)}
        assert cited, f"{name} names no tools"
        assert cited <= in_tree, (name, sorted(cited - in_tree))


def test_the_workflows_are_loader_valid_recipes():
    from plugins.workflows.registry import WorkflowRegistry

    reg = WorkflowRegistry([str(ROOT / "workflows")])
    assert {r["name"] for r in reg.list()} == {"Browse & Extract", "Fill Form"}
    assert all(r["steps"] for r in reg.list())


# ── docs / catalog coherence (the in-tree replacement for the repo's test_docs.py) ──


def test_the_bundled_plugin_ships_a_readme_that_names_the_setup_and_the_fence():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "npm i -g agent-browser" in readme          # the one external requirement
    assert "setup gap" in readme                        # (a)
    assert "fence" in readme                            # (c)
    assert "browser_pdf" in readme                      # (d)
    for f in ("tools.py", "browser_panel.py", "browser_stream.py", "runtime.py",
              "storage.py", "preflight.py", "__init__.py"):
        assert f in readme, f"the README should list {f}"


def test_the_docs_guide_exists_and_is_in_the_sidebar():
    guide = REPO / "docs" / "guides" / "browser-automation.md"
    assert guide.is_file()
    text = guide.read_text(encoding="utf-8")
    assert "agent-browser install" in text and "browser_pdf" in text
    sidebar = (REPO / "docs" / ".vitepress" / "config.mts").read_text(encoding="utf-8")
    assert "/guides/browser-automation" in sidebar


def test_the_plugin_directory_row_is_bundled_and_the_catalog_is_regenerated():
    directory = yaml.safe_load((REPO / "config" / "plugin-directory.yaml").read_text(encoding="utf-8"))
    row = next(r for r in directory["plugins"] if r["id"] == "agent_browser")
    assert row["bundled"] is True
    assert "repo" not in row      # a bundled row's source link is derived from the tree
    catalog = json.loads((REPO / "config" / "plugin-catalog.json").read_text(encoding="utf-8"))
    entry = next(p for p in catalog["plugins"] if p["id"] == "agent_browser")
    assert RETIRED_REPO not in json.dumps(entry)   # no install-from-the-retired-repo link


def test_the_readme_and_tool_reference_list_the_bundled_plugin():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "[`agent_browser`](./plugins/agent_browser/)" in readme
    reference = (REPO / "docs" / "reference" / "starter-tools.md").read_text(encoding="utf-8")
    assert "browser_pdf" in reference and "agent_browser" in reference


# ── the live CLI: the drift a mocked suite can never catch ───────────────────────


CLI = shutil.which("agent-browser")
needs_cli = pytest.mark.skipif(CLI is None, reason="the agent-browser CLI is not on PATH")


@needs_cli
def test_the_real_cli_still_has_every_subcommand_and_flag_the_plugin_sends():
    """Upstream is a separately-released native binary; the standalone CI mocked it
    entirely, so a renamed subcommand would have shipped green. Read its own help."""
    help_text = subprocess.run([CLI, "--help"], capture_output=True, text=True, timeout=60).stdout
    for verb in ("open", "back", "forward", "reload", "snapshot", "click", "fill", "type",
                 "press", "hover", "eval", "screenshot", "pdf", "upload", "close"):
        assert re.search(rf"^\s+{re.escape(verb)}\b", help_text, re.M), f"CLI lost `{verb}`"
    # `get <what>` is documented as its own group, and the plugin sends text/html/value
    assert "agent-browser get <what>" in help_text
    assert re.search(r"^\s+text, html, value\b", help_text, re.M), "CLI changed `get` subjects"
    for flag in ("--headed", "--profile", "--device", "--allowed-domains", "--confirm-actions",
                 "--max-output", "--user-agent", "--args"):
        assert flag in help_text, f"CLI lost {flag} (runtime.launch_flags would fail)"


@needs_cli
def test_the_real_cli_really_does_eat_a_leading_dash_operand():
    """The premise of the `-` guard, checked against the binary rather than assumed: the
    CLI scans the WHOLE argv for options, so a third-positional `--help` is consumed as
    one, and there is no `--` end-of-options escape. If upstream ever fixes that, this
    test is where we find out (and the guard can relax)."""
    def run(*args):
        env = {**os.environ, "AGENT_BROWSER_SESSION": f"protoagent-argv-{os.getpid()}"}
        return subprocess.run([CLI, *args], capture_output=True, text=True, timeout=60, env=env).stdout

    assert "Usage: agent-browser fill" in run("fill", "#q", "--help"), "no longer eats a 3rd positional"
    assert "Usage: agent-browser press" in run("press", "--help")
    assert "Usage: agent-browser open" in run("open", "--", "--help"), "`--` is now honoured?"


@needs_cli
async def test_the_real_cli_prints_us_letter_and_ignores_css_page_size(monkeypatch, tmp_path):
    """Checked against the binary, not assumed: an `@page { size: A4 }` page prints at
    612x792 pt (Letter), not 595x842 (A4). If this starts failing with 595x842, upstream
    now honours `@page` — update browser_pdf's docstring, the skill and the guide, which
    all say Letter."""
    pypdf = pytest.importorskip("pypdf")
    monkeypatch.setenv("AGENT_BROWSER_SESSION", f"protoagent-pagesize-{os.getpid()}")
    page = tmp_path / "a4.html"
    page.write_text("<!doctype html><style>@page { size: A4; margin: 0 }</style><h1>A4 probe</h1>",
                    encoding="utf-8")
    t = _toolmap({"binary": "agent-browser", "timeout_s": 120})
    try:
        opened = await t["browser_open"].ainvoke({"url": page.as_uri()})
        if opened.startswith("Error:"):
            pytest.skip(f"no browser available here: {opened[:120]}")
        out = await t["browser_pdf"].ainvoke({"path": "a4-probe.pdf"})
        assert not out.startswith("Error:"), out
        box = pypdf.PdfReader(storage.capture_root().resolve() / "a4-probe.pdf").pages[0].mediabox
        assert (round(float(box.width)), round(float(box.height))) == (612, 792)
    finally:
        await t["browser_close"].ainvoke({})


@needs_cli
async def test_the_real_cli_takes_dash_values_the_guard_lets_through(monkeypatch):
    """The other half of the guard's premise, through the TOOLS against the binary: what
    the guard allows really is entered verbatim (the first guard refused all of these)."""
    monkeypatch.setenv("AGENT_BROWSER_SESSION", f"protoagent-dash-{os.getpid()}")
    t = _toolmap({"binary": "agent-browser", "timeout_s": 120})
    try:
        opened = await t["browser_open"].ainvoke({"url": "data:text/html,<input id=q>"})
        if opened.startswith("Error:"):
            pytest.skip(f"no browser available here: {opened[:120]}")
        for value in ("-5", "-$50.00", "- buy milk", "-", "--"):
            filled = await t["browser_fill"].ainvoke({"selector": "#q", "text": value})
            assert not filled.startswith("Error:"), (value, filled)
            assert await t["browser_get_value"].ainvoke({"selector": "#q"}) == value
        assert await t["browser_eval"].ainvoke({"expression": "-1"}) == "-1"
    finally:
        await t["browser_close"].ainvoke({})


@needs_cli
async def test_the_real_cli_reports_about_blank_when_no_page_is_open(monkeypatch):
    """The premise of the no-page check: with nothing open, `get url` answers about:blank
    (it doesn't fail), and printing that would "succeed" with a blank PDF."""
    monkeypatch.setenv("AGENT_BROWSER_SESSION", f"protoagent-nopage-{os.getpid()}")
    t = _toolmap({"binary": "agent-browser", "timeout_s": 120})
    try:
        out = await t["browser_pdf"].ainvoke({"path": "nopage.pdf"})
        assert out.startswith("Error:") and "the page is blank" in out, out
        assert not (storage.capture_root().resolve() / "nopage.pdf").exists()
    finally:
        await t["browser_close"].ainvoke({})


@needs_cli
def test_the_real_preflight_reads_the_real_doctor():
    """``chrome.installed`` is the check id the Chrome gap keys on — pin that it is still
    the CLI's own contract, not a shape we invented."""
    probe = preflight.probe({"binary": "agent-browser"})
    assert probe.cli_ok and probe.cli_path == str(Path(CLI))
    assert probe.cli_version.startswith("agent-browser ")
    assert probe.chrome in ("ok", "missing"), "doctor --json stopped answering chrome.installed"


@needs_cli
async def test_the_real_cli_prints_a_real_pdf_into_the_fence(monkeypatch):
    """End-to-end for the new tool against the actual binary: launch an ISOLATED session
    (never the default one a live agent may be driving), print a small data: page, and
    check the bytes are a PDF in the fenced directory."""
    monkeypatch.setenv("AGENT_BROWSER_SESSION", f"protoagent-tests-{os.getpid()}")
    t = _toolmap({"binary": "agent-browser", "timeout_s": 120})
    try:
        opened = await t["browser_open"].ainvoke({"url": "data:text/html,<h1>protoAgent pdf probe</h1>"})
        if opened.startswith("Error:"):
            pytest.skip(f"no browser available here: {opened[:120]}")
        out = await t["browser_pdf"].ainvoke({"path": "live.pdf"})
        assert not out.startswith("Error:"), out
        written = storage.capture_root().resolve() / "live.pdf"
        assert written.is_file() and written.read_bytes()[:4] == b"%PDF"
        assert str(written) in out
    finally:
        await t["browser_close"].ainvoke({})


# ── round 3: a capture never moves the previous file (the reviewer's M1-M4, permanent) ──
# b790c1a8 "parked" an existing target as `.name.<hex>.prev` before a re-export and put it
# back on failure. Anything between park and restore — a cancelled turn, a kill -9, a
# concurrent export to the same name, another capture's prune — lost the user's last good
# export. The fix never moves the old file: the CLI writes a temp name beside it and a good
# run swaps that into place with one os.replace. These drive a REAL subprocess (a stateful
# fake CLI) through the real tools, and each fails on b790c1a8.

posix_only = pytest.mark.skipif(os.name == "nt", reason="drives an executable #! fake CLI (POSIX exec)")

_FAKE_CLI = '''#!{python} -S
import os, sys, time, pathlib
ST = pathlib.Path({state!r})


def read(name, default=""):
    try:
        return (ST / name).read_text()
    except OSError:
        return default


a = sys.argv[1:]
verb = a[0] if a else ""
if verb == "--version":
    print("agent-browser 0.27.1"); sys.exit(0)
if verb == "doctor":
    print('{{"checks":[{{"id":"chrome.installed","status":"pass","message":"ok"}}]}}'); sys.exit(0)
if verb == "get" and a[1:2] == ["url"]:
    print(read("url", "https://example.test/")); sys.exit(0)
if verb == "eval":
    print(read("eval", "1")); sys.exit(0)
if verb in ("pdf", "screenshot"):
    out = pathlib.Path(a[1])
    mode = read("mode_" + verb) or read("mode", "write")
    if mode.startswith("first-"):
        try:
            (ST / "lock_first").mkdir()
            mode = mode[len("first-"):].split("-else-")[0]
        except FileExistsError:
            mode = "write:B-BYTES"
    (ST / ("started_" + verb)).write_text(str(os.getpid()))
    try:
        if mode == "nothing":
            pass
        elif mode == "slownothing":
            time.sleep(float(read("slow", "1.0")))
        elif mode == "slowfail":
            time.sleep(float(read("slow", "1.0")))
            sys.stderr.write("renderer crashed\\n")
            sys.exit(1)
        else:
            tag = mode.split(":", 1)[1] if mode.startswith("write:") else read("tag", "PAGE")
            out.write_bytes(b"%PDF-1.4 " + tag.encode())
    finally:
        (ST / ("done_" + verb)).write_text("1")
    sys.exit(0)
print("ok")
'''


class _FakeCLI:
    """A stateful stand-in for the agent-browser binary: a real executable the tools exec,
    steered through files in `state/` (mode, tag, timing) and leaving `started_*` / `done_*`
    markers so a test can act at a precise moment in a capture."""

    def __init__(self, root: Path):
        self.state = root / "state"
        self.state.mkdir(parents=True)
        self.path = root / "agent-browser"
        self.path.write_text(_FAKE_CLI.format(python=sys.executable, state=str(self.state)), encoding="utf-8")
        self.path.chmod(0o755)

    def set(self, name: str, value) -> None:
        (self.state / name).write_text(str(value), encoding="utf-8")

    def clear(self, *names: str) -> None:
        for name in names:
            p = self.state / name
            if p.is_dir():
                p.rmdir()
            else:
                p.unlink(missing_ok=True)

    async def reached(self, marker: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not (self.state / marker).exists():
            if time.monotonic() > deadline:
                raise AssertionError(f"the fake CLI never wrote {marker!r}")
            await asyncio.sleep(0.02)

    def tools(self):
        return _toolmap({"binary": str(self.path), "timeout_s": 30})


@pytest.fixture
def fake_cli(tmp_path):
    return _FakeCLI(tmp_path / "fake-cli")


def _hidden(root: Path) -> list[str]:
    """Dot-files in the capture dir: parked copies (old) or temps (new) left behind."""
    return sorted(p.name for p in root.iterdir() if p.name.startswith("."))


@posix_only
async def test_m1_a_cancelled_re_export_never_loses_the_last_good_file(fake_cli):
    """M1: the turn is cancelled while the CLI is rendering. b790c1a8 had already moved
    resume.pdf aside, so it was gone — surviving only as a hidden `.prev` the next export
    neither restored nor removed."""
    t = fake_cli.tools()
    root = storage.capture_root().resolve()
    fake_cli.set("tag", "LAST-GOOD")
    assert "Saved to" in await t["browser_pdf"].ainvoke({"path": "resume.pdf"})
    fake_cli.clear("started_pdf", "done_pdf")
    fake_cli.set("mode", "slownothing")
    task = asyncio.create_task(t["browser_pdf"].ainvoke({"path": "resume.pdf"}))
    await fake_cli.reached("started_pdf")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (root / "resume.pdf").read_bytes() == b"%PDF-1.4 LAST-GOOD"   # right where it was
    await fake_cli.reached("done_pdf")                                    # let the orphan finish
    fake_cli.set("mode", "write")
    fake_cli.set("tag", "NEXT")
    assert "Saved to" in await t["browser_pdf"].ainvoke({"path": "resume.pdf"})
    assert (root / "resume.pdf").read_bytes() == b"%PDF-1.4 NEXT"
    assert _hidden(root) == []


@posix_only
async def test_m2_a_kill_9_mid_capture_never_loses_the_last_good_file(fake_cli, tmp_path, monkeypatch):
    """M2: the whole process is SIGKILLed mid-capture — no finally, no cleanup of any kind.
    Driven in a real child process that shares this test's instance root."""
    from infra.paths import reset_instance_paths

    home = tmp_path / "instance-home"
    # PROTOAGENT_HOME is TERMINAL: parent and child resolve the SAME root and neither can
    # ever reach the real ~/.protoagent (conftest's isolation is in-process only).
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    reset_instance_paths()
    root = storage.capture_root().resolve()
    assert root.is_relative_to(home.resolve())
    t = fake_cli.tools()
    fake_cli.set("tag", "LAST-GOOD")
    assert "Saved to" in await t["browser_pdf"].ainvoke({"path": "resume.pdf"})
    fake_cli.clear("started_pdf", "done_pdf")
    fake_cli.set("mode", "slownothing")
    fake_cli.set("slow", "3")
    seen = tmp_path / "child-capture-root.txt"
    child = textwrap.dedent(f"""
        import asyncio, importlib, pathlib, sys
        sys.path.insert(0, {str(REPO)!r})
        from graph.plugins.testkit import load_plugin
        pkg = load_plugin({str(ROOT)!r}, "agent_browser")
        storage = importlib.import_module(pkg.__name__ + ".storage")
        tools = importlib.import_module(pkg.__name__ + ".tools")
        pathlib.Path({str(seen)!r}).write_text(str(storage.capture_root().resolve()))
        T = {{t.name: t for t in tools.get_browser_tools({{"binary": {str(fake_cli.path)!r}}})}}
        asyncio.run(T["browser_pdf"].ainvoke({{"path": "resume.pdf"}}))
    """)
    proc = subprocess.Popen([sys.executable, "-c", child], cwd=str(REPO), env=dict(os.environ))
    try:
        await fake_cli.reached("started_pdf", timeout=90)   # the child is mid-capture…
        proc.kill()                                           # …and dies without cleanup
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert seen.read_text() == str(root)                     # the child used THIS instance
    assert (root / "resume.pdf").read_bytes() == b"%PDF-1.4 LAST-GOOD"
    await fake_cli.reached("done_pdf", timeout=30)
    fake_cli.set("mode", "write")
    fake_cli.set("tag", "NEXT")
    assert "Saved to" in await t["browser_pdf"].ainvoke({"path": "resume.pdf"})
    assert (root / "resume.pdf").read_bytes() == b"%PDF-1.4 NEXT"
    assert _hidden(root) == []


@posix_only
async def test_m3_a_failing_concurrent_export_never_deletes_the_winners_file(fake_cli):
    """M3: two exports to one name; the slow one fails. b790c1a8's failure handling
    deleted the FAST call's just-reported file and restored the old one."""
    t = fake_cli.tools()
    root = storage.capture_root().resolve()
    fake_cli.set("tag", "OLD-GOOD")
    await t["browser_pdf"].ainvoke({"path": "report.pdf"})
    fake_cli.clear("started_pdf", "lock_first")
    fake_cli.set("mode", "first-slowfail-else-write")
    a = asyncio.create_task(t["browser_pdf"].ainvoke({"path": "report.pdf"}))
    await fake_cli.reached("started_pdf")                         # A is rendering (and will fail)
    rb = await t["browser_pdf"].ainvoke({"path": "report.pdf"})   # B finishes first
    ra = await a
    assert "Saved to" in rb and ra.startswith("Error:"), (ra, rb)
    assert (root / "report.pdf").read_bytes() == b"%PDF-1.4 B-BYTES"   # B's output stands
    assert _hidden(root) == []


@posix_only
async def test_m3b_a_concurrent_export_that_wrote_nothing_cannot_claim_the_others_bytes(fake_cli):
    """M3b: as M3, but the slow call exits 0 having written NOTHING — b790c1a8 then saw the
    other call's file at the target and reported "Saved to" for bytes it never wrote."""
    t = fake_cli.tools()
    root = storage.capture_root().resolve()
    fake_cli.set("tag", "OLD-GOOD")
    await t["browser_pdf"].ainvoke({"path": "report.pdf"})
    fake_cli.clear("started_pdf", "lock_first")
    fake_cli.set("mode", "first-slownothing-else-write")
    a = asyncio.create_task(t["browser_pdf"].ainvoke({"path": "report.pdf"}))
    await fake_cli.reached("started_pdf")
    rb = await t["browser_pdf"].ainvoke({"path": "report.pdf"})
    ra = await a
    assert "Saved to" in rb
    assert ra.startswith("Error:") and "wrote no file" in ra, ra
    assert (root / "report.pdf").read_bytes() == b"%PDF-1.4 B-BYTES"
    assert _hidden(root) == []


@posix_only
async def test_m4_another_captures_prune_never_takes_a_file_mid_re_export(fake_cli, monkeypatch):
    """M4: at the retention cap (a long-running instance lives there), another capture's
    prune runs while resume.pdf is being re-exported. b790c1a8's parked copy kept the old
    mtime, so it was the oldest file and was pruned first; the failed re-export's restore
    then hit FileNotFoundError and the last good export was gone."""
    t = fake_cli.tools()
    root = storage.capture_root().resolve()
    fake_cli.set("tag", "LAST-GOOD-RESUME")
    await t["browser_pdf"].ainvoke({"path": "resume.pdf"})
    day_ago = time.time() - 86400
    os.utime(root / "resume.pdf", (day_ago, day_ago))            # yesterday: the OLDEST capture
    for i in range(2):
        await t["browser_screenshot"].ainvoke({"path": f"shot{i}.png"})
    monkeypatch.setattr(storage, "MAX_CAPTURE_FILES", 3)
    fake_cli.clear("started_pdf")
    fake_cli.set("mode_pdf", "slowfail")
    fake_cli.set("mode_screenshot", "write")
    a = asyncio.create_task(t["browser_pdf"].ainvoke({"path": "resume.pdf"}))   # will fail
    await fake_cli.reached("started_pdf")
    assert "Saved to" in await t["browser_screenshot"].ainvoke({"path": "shot2.png"})   # prunes now
    assert (await a).startswith("Error:")
    assert (root / "resume.pdf").read_bytes() == b"%PDF-1.4 LAST-GOOD-RESUME"
    assert _hidden(root) == []


@posix_only
async def test_a_name_near_the_filesystem_limit_can_be_re_exported(fake_cli):
    """The parked copy was named `.<name>.<hex>.prev` — 15 chars longer than the target —
    so a ~240-char name exported once and could then never be re-exported."""
    t = fake_cli.tools()
    root = storage.capture_root().resolve()
    name = "r" * 240 + ".pdf"
    fake_cli.set("tag", "ONE")
    assert "Saved to" in await t["browser_pdf"].ainvoke({"path": name})
    fake_cli.set("tag", "TWO")
    out = await t["browser_pdf"].ainvoke({"path": name})
    assert "Saved to" in out, out
    assert (root / name).read_bytes() == b"%PDF-1.4 TWO"


def test_temp_names_are_short_fixed_and_keep_the_extension():
    temp = storage.temp_path_for(Path("/x") / ("r" * 250 + ".pdf"))
    assert temp.parent == Path("/x") and temp.suffix == ".pdf" and storage.is_temp(temp)
    assert len(temp.name) <= 32                      # never embeds the (long) stem


async def test_orphaned_temps_are_swept_and_live_ones_are_left_alone(monkeypatch):
    """A cancelled or killed capture can leave a temp once its CLI finishes writing; the next
    prune sweeps it once it's clearly abandoned. A YOUNG temp is someone's capture in
    progress — never deleted, and not counted against the budget."""
    root = storage.capture_root().resolve()
    stale = root / f"{storage.TEMP_PREFIX}{'0' * 16}.pdf"
    stale.write_bytes(b"orphan")
    old = time.time() - storage.STALE_TEMP_S - 60
    os.utime(stale, (old, old))
    live = root / f"{storage.TEMP_PREFIX}{'1' * 16}.pdf"
    live.write_bytes(b"in flight")
    monkeypatch.setattr(storage, "MAX_CAPTURE_FILES", 1)
    monkeypatch.setattr(tools.subprocess, "Popen", _writing_popen(data=b"%PDF new"))
    assert "Saved to" in await _toolmap({"binary": "ab"})["browser_pdf"].ainvoke({"path": "new.pdf"})
    assert not stale.exists() and live.exists() and (root / "new.pdf").is_file()


# ── a binary that exists but can't be started is not "missing" ────────────────────


def _unstartable(tmp_path: Path) -> Path:
    """The npm launcher shape: a script whose interpreter isn't there."""
    shim = tmp_path / "agent-browser"
    shim.write_text("#!/nonexistent/interpreter/node\nconsole.log('x')\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim


@posix_only
async def test_a_binary_that_exists_but_cannot_start_is_not_called_missing(tmp_path):
    """With no node on the host's PATH the kernel refuses the npm launcher with the SAME
    FileNotFoundError as a missing binary. "Install it" is the wrong advice — and the probe
    used to call it healthy."""
    shim = _unstartable(tmp_path)
    out = await _toolmap({"binary": str(shim)})["browser_snapshot"].ainvoke({})
    assert out.startswith("Error:") and "could not be started" in out and "not on PATH" not in out
    probe = preflight.probe({"binary": str(shim)})
    assert probe.cli_path and probe.cli_ok is False and probe.cli_error
    assert "can't be started" in preflight.hint(probe)


@posix_only
async def test_an_unstartable_binary_raises_the_banner_at_boot_and_stops_re_probing(tmp_path, monkeypatch):
    """Before: no banner ever, and since the probe kept saying "healthy", every failing call
    re-probed (a FileNotFoundError bypasses the rate limit)."""
    shim = _unstartable(tmp_path)
    probes = []
    real_report = preflight.report
    monkeypatch.setattr(preflight, "report", lambda *a, **k: probes.append(1) or real_report(*a, **k))
    reg = _registry({"binary": str(shim)})
    _PKG.register(reg)
    assert preflight.CLI_GAP in reg.setup_gaps and "can't be started" in reg.setup_gaps[preflight.CLI_GAP]
    probes.clear()
    tool = next(x for x in reg.tools if x.name == "browser_snapshot")
    for _ in range(5):
        assert "could not be started" in await tool.ainvoke({})
    assert probes == []                     # the banner is up: no re-probe per call


@needs_cli
async def test_the_real_cli_prints_html_written_into_a_blank_page(monkeypatch):
    """The no-page check's misfire, against the binary: open a blank page, write a report
    into it with browser_eval, print it. The URL is still about:blank; the page isn't empty."""
    pypdf = pytest.importorskip("pypdf")
    monkeypatch.setenv("AGENT_BROWSER_SESSION", f"protoagent-blankhtml-{os.getpid()}")
    t = _toolmap({"binary": "agent-browser", "timeout_s": 120})
    try:
        opened = await t["browser_open"].ainvoke({})
        if opened.startswith("Error:"):
            pytest.skip(f"no browser available here: {opened[:120]}")
        await t["browser_eval"].ainvoke({"expression": "document.body.innerHTML = '<h1>Quarterly report</h1>'"})
        out = await t["browser_pdf"].ainvoke({"path": "written.pdf"})
        assert "Saved to" in out, out
        text = pypdf.PdfReader(storage.capture_root().resolve() / "written.pdf").pages[0].extract_text()
        assert "Quarterly report" in text
    finally:
        await t["browser_close"].ainvoke({})


# ── CodeRabbit review threads (#3451): each of these fails on 3024f8f8 ─────────────


@pytest.mark.parametrize("url", ["--profile=/tmp/x", "--headed", "-h"])
def test_nav_refuses_a_url_that_reads_as_a_cli_option(monkeypatch, url):
    """CWE-88. The /nav url lands in the CLI's argv, and the CLI reads options anywhere in
    it. The tools had the guard; the panel's route didn't. The bearer gate limits WHO can
    call /nav — it doesn't make the value safe."""
    from fastapi.testclient import TestClient

    rec = []
    monkeypatch.setattr(bp.subprocess, "run", fake_run(record=rec))
    body = TestClient(_app()).post("/api/plugins/agent_browser/nav", json={"action": "open", "url": url}).json()
    assert body["ok"] is False and "command-line option" in body["error"]
    assert rec == [], "refused before the CLI runs"


def test_nav_uses_the_same_grammar_not_any_leading_dash(monkeypatch):
    from fastapi.testclient import TestClient

    rec = []
    monkeypatch.setattr(bp.subprocess, "run", fake_run(record=rec))
    body = TestClient(_app()).post("/api/plugins/agent_browser/nav", json={"action": "open", "url": "-1"}).json()
    assert body["ok"] is True and rec[-1][-2:] == ["open", "-1"]
    assert bp.bad_operand is rt.bad_operand is tools.bad_operand   # ONE guard, not two copies


def test_whitespace_only_cdp_output_returns_a_note_not_an_exception(monkeypatch):
    monkeypatch.setattr(bs.subprocess, "run",
                        lambda args, **kw: types.SimpleNamespace(returncode=0, stdout="\n", stderr=""))
    ws, note = bs.resolve_page_target("ab")
    assert ws is None and note          # the documented (None, note) — never IndexError


def test_the_panel_stream_reports_a_blank_cdp_answer_instead_of_dropping_the_socket(monkeypatch):
    """resolve_page_target runs before the WS route's try block, so its IndexError used to
    reach the client as a closed socket instead of the {"t": "error"} note."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(bs.subprocess, "run",
                        lambda args, **kw: types.SimpleNamespace(returncode=0, stdout="\n", stderr=""))
    c = TestClient(_app())
    ticket = c.post("/api/plugins/agent_browser/stream-ticket").json()["ticket"]
    with c.websocket_connect(f"/api/plugins/agent_browser/stream?ticket={ticket}") as ws:
        msg = ws.receive_json()
    assert msg["t"] == "error" and msg["msg"]


def test_a_whitespace_only_version_line_does_not_skip_the_chrome_probe(monkeypatch):
    """`--version` printing a bare newline raised IndexError in _cli_version; probe()
    swallowed it and never asked about Chrome."""
    _probe_env(monkeypatch, which="/opt/ab", version="\n", chrome="pass")
    probe = preflight.probe({})
    assert probe.cli_ok and probe.cli_version == "" and probe.chrome == "ok"


def test_every_browser_tool_the_skill_body_uses_is_in_its_tools_list():
    """`tools:` is the skill's advisory list of what it relies on; the body tells the model
    to use browser_eval (typing flag-shaped text, writing HTML into a blank page), and the
    list omitted it."""
    from graph.skills.loader import parse_skill_md

    declared = set(parse_skill_md(ROOT / "skills" / "web-browse" / "SKILL.md").tools_used)
    used = set(re.findall(r"`(browser_[a-z_]+)`", _skill_text()))
    assert used and used <= declared, sorted(used - declared)


@pytest.mark.parametrize("bad", ["1m", "soon", "", None, [1]])
async def test_a_non_numeric_timeout_never_breaks_the_tools(monkeypatch, bad):
    """`type: number` validates Settings edits only; a hand-written langgraph-config.yaml
    reaches the plugin raw. `timeout_s: "1m"` raised inside get_browser_tools."""
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen(out="ok"))
    t = _toolmap({"binary": "ab", "timeout_s": bad})
    assert set(t) == EXPECTED_TOOLS
    assert await t["browser_snapshot"].ainvoke({}) == "ok"


def test_register_still_contributes_everything_with_garbage_numbers(monkeypatch):
    """The real consequence: register() swallowed that ValueError, so the agent silently had
    NO browser tools and no panel."""
    _probe_env(monkeypatch, which="/opt/ab")
    reg = _registry({"timeout_s": "1m", "max_output": "lots", "stream_quality": "high"})
    _PKG.register(reg)
    assert {t.name for t in reg.tools} == EXPECTED_TOOLS
    assert len(reg.routers) == 2


def test_a_non_numeric_max_output_never_breaks_launch_flags():
    assert rt.launch_flags({"max_output": "lots"}) == []
    assert rt.launch_flags({"max_output": "250"}) == ["--max-output", "250"]


def test_the_panel_router_builds_with_garbage_numbers():
    router = bp.build_panel_data_router({"timeout_s": "soon", "stream_quality": "high"})
    assert {r.path for r in router.routes} == {"/stream-ticket", "/stream", "/nav"}


@pytest.mark.parametrize(("raw", "expected"), [(0, 60.0), (-5, 60.0), ("2.5", 2.5), (None, 60.0)])
def test_a_timeout_must_be_a_positive_number(raw, expected):
    """A 0 s timeout would time every command out; blank means the default."""
    assert rt.number({"timeout_s": raw}, "timeout_s", 60.0, positive=True) == expected


def test_default_names_are_collision_free_under_a_burst():
    """Two random bytes after a one-second timestamp: a burst of unnamed captures inside one
    second repeated names (~190 collisions expected in 5000). A per-process counter makes it
    impossible by construction."""
    names = [storage.unique_default_name("page.pdf") for _ in range(5000)]
    assert len(set(names)) == len(names)
    for n in names[:3]:
        assert re.fullmatch(r"page-\d{8}-\d{6}-\d+-[0-9a-f]{6}\.pdf", n), n


# C8 — the drain threads, fixed here rather than as a follow-up.


def _reap(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@posix_only
async def test_a_timed_out_cli_whose_children_hold_the_pipes_cannot_pin_a_worker(tmp_path):
    """The reviewer's reproducer: the CLI backgrounds `sleep 300`s that inherit our pipes,
    then hangs. The timeout killed only the direct child, the sleeps kept the pipes open, and
    the unbounded join pinned an asyncio.to_thread worker for five minutes. The CLI now runs
    in its own session and a timeout kills the whole group."""
    pids = tmp_path / "pids"
    cli = tmp_path / "hangs"
    cli.write_text(f"#!/bin/sh\nsleep 300 &\necho $! >> '{pids}'\nsleep 300 &\necho $! >> '{pids}'\nwait\n",
                   encoding="utf-8")
    cli.chmod(0o755)
    t = _toolmap({"binary": str(cli), "timeout_s": 1})
    started = time.monotonic()
    recorded: list[int] = []
    try:
        out = await asyncio.wait_for(t["browser_snapshot"].ainvoke({}), timeout=20)
        assert "timed out" in out and time.monotonic() - started < 15
        recorded = [int(x) for x in pids.read_text().split()]
        deadline = time.monotonic() + 5
        while True:                                   # the group kill reached the sleeps too
            alive = []
            for pid in recorded:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            if not alive or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        assert alive == [], f"descendants survived the timeout: {alive}"
    finally:
        _reap(recorded or ([int(x) for x in pids.read_text().split()] if pids.exists() else []))


@posix_only
async def test_a_cli_that_exits_but_leaves_a_descendant_holding_the_pipes_still_returns(tmp_path, monkeypatch):
    """The other half: a descendant that left the process group (its own session) holds our
    pipes after the CLI exits 0. There's nothing to kill, so the bounded join is the
    backstop — the call returns what the CLI wrote instead of waiting on the stray process."""
    monkeypatch.setattr(tools, "_JOIN_TIMEOUT_S", 0.5, raising=False)
    pidfile = tmp_path / "grandchild.pid"
    cli = tmp_path / "leaks"
    cli.write_text(
        f"#!{sys.executable} -S\n"
        "import pathlib, subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-S', '-c', 'import time; time.sleep(300)'], start_new_session=True)\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(g.pid))\n"
        "print('hello')\n",
        encoding="utf-8")
    cli.chmod(0o755)
    t = _toolmap({"binary": str(cli), "timeout_s": 30})
    try:
        out = await asyncio.wait_for(t["browser_snapshot"].ainvoke({}), timeout=20)
        assert out == "hello"
    finally:
        _reap([int(pidfile.read_text())] if pidfile.exists() else [])
