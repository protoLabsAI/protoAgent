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
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest
import yaml

from graph.plugins.testkit import FakeRegistry, load_plugin

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
    "browser_click", "browser_fill", "browser_type", "browser_press", "browser_hover",
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


def _manifest() -> dict:
    return yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))


def _toolmap(cfg=None, **kw):
    return {t.name: t for t in tools.get_browser_tools(cfg or {}, **kw)}


# ── a subprocess.Popen stand-in for the tool wrappers ────────────────────────────
# _run() streams the child's pipes through drain threads under a byte cap, so the tool
# tests mock Popen (not run): BytesIO pipes yield the canned bytes, wait()/kill() drive
# the timeout + reap paths.


class _FakeProc:
    """Minimal Popen: BytesIO pipes + wait/kill, enough for _run's drain loop."""

    def __init__(self, argv, out=b"", err=b"", rc=0, timeout=False):
        self._argv = list(argv)
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


def test_all_17_tools_present():
    names = set(_toolmap())
    assert names == EXPECTED_TOOLS
    assert len(names) == 17  # 16 from the standalone repo + browser_pdf (#3451)
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
    action = reg.setup_gap_actions[preflight.CLI_GAP]
    assert action["kind"] in setup_gaps.ACTION_KINDS and action["kind"] == "plugin_config"

    setup_gaps.reset()
    try:
        setup_gaps.report("agent_browser", preflight.CLI_GAP, "cli missing", label="Agent Browser", action=action)
        [gap] = setup_gaps.active()
        # survived sanitizing WITH its label and highlighted field — the real contract
        assert gap["actions"] == [{"kind": "plugin_config", "target": "agent_browser",
                                  "label": "Set the CLI path", "fields": ["binary"]}]
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
    # the Chrome fix is a CLI command, and ACTION_KINDS has no command kind — so no action
    assert preflight.CHROME_GAP not in reg.setup_gap_actions


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
    assert probe.cli_path == "/opt/ab" and probe.cli_ok is False and probe.chrome == "unknown"
    assert "exec format error" in probe.cli_error
    assert "can't be started" in preflight.hint(probe)


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
    names = {storage.unique_default_name("page.pdf") for _ in range(20)}
    assert len(names) == 20
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
        assert re.fullmatch(r"page-\d{8}-\d{6}-[0-9a-f]{4}\.pdf", name), name


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
    ("browser_eval", {"expression": "-1"}, ["eval", "-1"]),
    ("browser_eval", {"expression": "(-1) + 2"}, ["eval", "(-1) + 2"]),
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
    assert version == (0, 7, 0)


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
    # a session started from the panel gets the same headed/stealth setup as the agent's
    c = TestClient(_app({"headed": True, "stealth": True}))
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
    source = (ROOT / "browser_panel.py").read_text(encoding="utf-8")
    for gone in ("_dash_auth_required", "_DASH_COOKIE", "ab_session", "mint_dash_token",
                 "verify_dash_token", "ensureDashSession", "set_cookie", "import hmac"):
        assert gone not in source, f"{gone} should not be vendored"
    # the removal is EXPLAINED where the next reader will look, not just done
    assert "require_auth" in source and "could never fire" in source
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
    drop-or-keep ruling (#3451 "Decision needed"). This pins the exact surface — the three
    config keys, their settings rows, and the two runtime.py mechanisms — so a later edit
    is a deliberate answer to that question, not a drive-by. (It checks the surface that
    exists here; byte-identity with the source repo was confirmed by sha at import time,
    and nothing in-tree can re-verify that.)"""
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
                 "press", "hover", "eval", "screenshot", "pdf", "close"):
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
