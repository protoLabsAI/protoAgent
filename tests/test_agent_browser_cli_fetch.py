"""agent_browser's pinned CLI download (``cli_fetch``), the Chrome install step, the
setup-gap buttons that run them, and the stealth UA derived from the installed Chrome.

Unit tests fake the network (a downloader callable / a patched ``_urllib_download``) and the
CLI (``subprocess.run`` / ``Popen``). ONE opt-in test downloads the REAL pinned asset for
this platform, verifies it and runs ``--version``:

    PROTOAGENT_NETWORK_TESTS=1 python -m pytest tests/test_agent_browser_cli_fetch.py -k real
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest
import yaml

from graph.plugins import setup_gaps
from graph.plugins.testkit import FakeRegistry, load_plugin

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "agent_browser"

_PKG = load_plugin(ROOT, "agent_browser")


def _mod(name: str):
    return importlib.import_module(f"{_PKG.__name__}.{name}")


cli_fetch = _mod("cli_fetch")
chrome_install = _mod("chrome_install")
preflight = _mod("preflight")
rt = _mod("runtime")
setup_steps = _mod("setup_steps")
tools = _mod("tools")

# The real network seams, kept for the one opt-in test; every other test runs offline.
_REAL_DOWNLOAD = cli_fetch._urllib_download
_REAL_EGRESS = cli_fetch._egress_check
_NETWORK = os.environ.get("PROTOAGENT_NETWORK_TESTS", "").strip().lower() in ("1", "true", "yes")

PAYLOAD = b"\x7fELF fake agent-browser build\n" * 128


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """A private, empty CLI cache; no network; clean fetch / install / gap state."""
    monkeypatch.setenv(cli_fetch.ENV_CLI_DIR, str(tmp_path / "cli-cache"))

    def _offline(url, timeout):
        raise OSError("network disabled in unit tests")

    monkeypatch.setattr(cli_fetch, "_urllib_download", _offline)
    monkeypatch.setattr(cli_fetch, "_egress_check", lambda url: None)
    cli_fetch.reset_state()
    chrome_install.reset_state()
    monkeypatch.setitem(rt._CHROME, "major", 0)
    setup_gaps.reset()
    yield
    cli_fetch.reset_state()
    chrome_install.reset_state()
    setup_gaps.reset()


def _spec(payload: bytes = PAYLOAD, platform: str = "linux-x64"):
    asset = cli_fetch.ASSETS[platform][0]
    return cli_fetch.FetchSpec(cli_fetch.CLI_VERSION, platform, asset,
                               cli_fetch.RELEASE_URL.format(version=cli_fetch.CLI_VERSION, asset=asset),
                               _sha(payload))


def _pin_fake(monkeypatch, payload: bytes = PAYLOAD) -> str:
    """Pin THIS host's asset to ``payload`` so ensure_cli / installed_path accept it."""
    key = cli_fetch.platform_key()
    if key is None:
        pytest.skip("upstream publishes no agent-browser build for this test host")
    monkeypatch.setitem(cli_fetch.ASSETS, key, (cli_fetch.ASSETS[key][0], _sha(payload)))
    return key


def _cli_env(monkeypatch, *, which=None, chrome="pass",
             chrome_msg="Google Chrome for Testing 151.0.7900.12 at /x/chrome", installs=None, install_rc=0,
             install_err=""):
    """A synthetic CLI behind ``subprocess.run`` (shared by preflight and chrome_install):
    ``--version``, ``doctor --json`` (one Chrome check, whose status an ``install`` flips to
    pass when it succeeds), and ``install``."""
    monkeypatch.setattr(preflight.shutil, "which", lambda name: which)
    state = {"chrome": chrome, "msg": chrome_msg}

    def _run(args, **kw):
        verb = args[1:2]
        if verb == ["--version"]:
            return types.SimpleNamespace(returncode=0, stdout="agent-browser 0.27.1", stderr="")
        if verb == ["doctor"]:
            payload = json.dumps({"checks": [{"id": "chrome.installed", "status": state["chrome"],
                                              "message": state["msg"]}]})
            return types.SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if verb == ["install"]:
            if installs is not None:
                installs.append(list(args))
            if install_rc == 0:
                state["chrome"] = "pass"
                state["msg"] = "Google Chrome for Testing 151.0.7900.12 at /x/chrome-151.0.7900.12/chrome"
                return types.SimpleNamespace(returncode=0, stdout="✓ Chrome 151.0.7900.12 installed successfully",
                                             stderr="")
            return types.SimpleNamespace(returncode=install_rc, stdout="", stderr=install_err)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", _run)
    return state


def _registry(cfg=None):
    return FakeRegistry(cfg or {}, plugin_id="agent_browser", plugin_dir=ROOT)


def _host_actions(raw) -> list[dict]:
    """What the REAL host keeps of an action after sanitizing it."""
    setup_gaps.reset()
    setup_gaps.report("agent_browser", "k", "m", label="Agent Browser", action=raw)
    [gap] = setup_gaps.active()
    setup_gaps.reset()
    return gap.get("actions", [])


# ── the pin ───────────────────────────────────────────────────────────────────────


def test_every_upstream_asset_is_pinned_with_its_own_sha256():
    assert set(cli_fetch.ASSETS) == {"darwin-arm64", "darwin-x64", "linux-x64", "linux-arm64",
                                     "linux-musl-x64", "linux-musl-arm64", "win32-x64"}
    for key, (asset, sha) in cli_fetch.ASSETS.items():
        assert asset == f"agent-browser-{key}" + (".exe" if key.startswith("win32") else "")
        assert re.fullmatch(r"[0-9a-f]{64}", sha), key
    assert len({sha for _, sha in cli_fetch.ASSETS.values()}) == len(cli_fetch.ASSETS)


def test_the_pin_is_the_version_the_plugin_is_verified_against():
    """0.27.1 is the release the argv guard and the doctor-JSON reading were checked on
    (runtime.bad_operand's docstring) — move it deliberately, never to "latest"."""
    assert cli_fetch.CLI_VERSION == "0.27.1"
    assert "verified on 0.27.1" in (ROOT / "runtime.py").read_text(encoding="utf-8")
    assert cli_fetch.fetch_spec("darwin-arm64").url == (
        "https://github.com/vercel-labs/agent-browser/releases/download/v0.27.1/agent-browser-darwin-arm64")


@pytest.mark.parametrize("system,machine,musl,key", [
    ("Darwin", "arm64", False, "darwin-arm64"),
    ("Darwin", "x86_64", False, "darwin-x64"),
    ("Linux", "x86_64", False, "linux-x64"),
    ("Linux", "aarch64", False, "linux-arm64"),
    ("Linux", "x86_64", True, "linux-musl-x64"),
    ("Linux", "aarch64", True, "linux-musl-arm64"),
    ("Windows", "AMD64", False, "win32-x64"),
    ("Windows", "ARM64", False, None),
    ("FreeBSD", "amd64", False, None),
    ("Linux", "riscv64", False, None),
])
def test_platform_keys_follow_upstreams_asset_naming(system, machine, musl, key):
    assert cli_fetch.platform_key(system, machine, musl=musl) == key


def test_windows_gets_an_exe_and_the_path_is_keyed_by_version(tmp_path):
    assert cli_fetch.fetched_path("win32-x64", base=tmp_path) == tmp_path / "0.27.1" / "agent-browser.exe"
    assert cli_fetch.fetched_path("linux-x64", base=tmp_path) == tmp_path / "0.27.1" / "agent-browser"


def test_the_cache_is_the_hosts_box_tier_cache_dir(monkeypatch):
    from infra.paths import instance_paths

    monkeypatch.delenv(cli_fetch.ENV_CLI_DIR)
    assert cli_fetch.cache_root() == Path(instance_paths().cache_dir) / "agent-browser"


# ── install: verified BEFORE written, atomic ─────────────────────────────────────


def test_install_verifies_then_installs_atomically(tmp_path):
    dest = tmp_path / "0.27.1" / "agent-browser"
    assert cli_fetch.install(_spec(), dest, downloader=lambda url, timeout: PAYLOAD) == dest
    assert dest.read_bytes() == PAYLOAD
    if os.name != "nt":
        assert os.access(dest, os.X_OK)
    assert [p.name for p in dest.parent.iterdir()] == ["agent-browser"]  # no temp left behind


def test_a_flipped_byte_is_refused_and_nothing_is_installed(tmp_path):
    corrupt = bytearray(PAYLOAD)
    corrupt[len(corrupt) // 2] ^= 0x01
    dest = tmp_path / "0.27.1" / "agent-browser"
    with pytest.raises(cli_fetch.ChecksumError, match="sha256 mismatch"):
        cli_fetch.install(_spec(), dest, downloader=lambda url, timeout: bytes(corrupt))
    assert not dest.exists()
    assert not dest.parent.exists() or list(dest.parent.iterdir()) == []


def test_a_corrupt_download_never_touches_a_good_install(tmp_path):
    dest = tmp_path / "0.27.1" / "agent-browser"
    cli_fetch.install(_spec(), dest, downloader=lambda url, timeout: PAYLOAD)
    with pytest.raises(cli_fetch.ChecksumError):
        cli_fetch.install(_spec(), dest, downloader=lambda url, timeout: PAYLOAD[:-1] + b"X")
    assert dest.read_bytes() == PAYLOAD
    assert [p.name for p in dest.parent.iterdir()] == ["agent-browser"]


def test_a_failure_after_the_temp_is_written_leaves_no_temp(monkeypatch, tmp_path):
    def disk_full(tmp, dest, sha256, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cli_fetch, "_replace", disk_full)
    dest = tmp_path / "0.27.1" / "agent-browser"
    with pytest.raises(OSError, match="disk full"):
        cli_fetch.install(_spec(), dest, downloader=lambda url, timeout: PAYLOAD)
    assert list(dest.parent.iterdir()) == []


def test_an_in_use_target_is_retried_with_backoff_then_replaced(tmp_path):
    """Windows: os.replace over a running binary raises PermissionError."""
    tmp, dest = tmp_path / ".agent-browser-x.part", tmp_path / "agent-browser"
    tmp.write_bytes(PAYLOAD)
    attempts, slept = [], []

    def flaky(a, b):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "in use")
        os.replace(a, b)

    cli_fetch._replace(str(tmp), dest, _sha(PAYLOAD), sleep=slept.append, replace=flaky)
    assert dest.read_bytes() == PAYLOAD and len(attempts) == 3 and slept == [0.25, 0.5]


def test_a_target_stuck_in_use_is_accepted_only_when_it_is_already_the_pinned_build(tmp_path):
    tmp, dest = tmp_path / ".agent-browser-x.part", tmp_path / "agent-browser"
    slept = []

    def never(a, b):
        raise PermissionError(13, "in use")

    dest.write_bytes(PAYLOAD)                  # another instance installed the same bytes
    tmp.write_bytes(PAYLOAD)
    cli_fetch._replace(str(tmp), dest, _sha(PAYLOAD), sleep=slept.append, replace=never)
    assert not tmp.exists() and dest.read_bytes() == PAYLOAD
    assert sum(slept) < 10                     # bounded, not a spin

    dest.write_bytes(b"an older build")        # something ELSE is stuck in place
    tmp.write_bytes(PAYLOAD)
    with pytest.raises(PermissionError, match="in use"):
        cli_fetch._replace(str(tmp), dest, _sha(PAYLOAD), sleep=lambda s: None, replace=never)
    assert dest.read_bytes() == b"an older build"


def test_an_egress_block_stops_the_download_before_it_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_fetch, "_egress_check", lambda url: "Error: github.com is not allowlisted")
    called = []
    with pytest.raises(PermissionError, match="egress blocked"):
        cli_fetch.install(_spec(), tmp_path / "ab", downloader=lambda url, timeout: called.append(url) or PAYLOAD)
    assert called == []


@pytest.mark.parametrize("url", [
    "http://release-assets.githubusercontent.com/x",      # not https
    "https://evil.example/x",
    "https://githubusercontent.com.evil.example/x",       # suffix spoof
])
def test_a_redirect_off_githubusercontent_is_refused(url):
    with pytest.raises(PermissionError):
        cli_fetch.check_redirect_target(url)


def test_a_redirect_to_the_release_asset_host_is_allowed():
    cli_fetch.check_redirect_target("https://release-assets.githubusercontent.com/github-production-release-asset/1")


def test_an_oversized_or_non_bytes_download_is_refused(monkeypatch, tmp_path):
    with pytest.raises(TypeError):
        cli_fetch.install(_spec(), tmp_path / "ab", downloader=lambda url, timeout: "not bytes")
    monkeypatch.setattr(cli_fetch, "MAX_ASSET_BYTES", 10)
    with pytest.raises(ValueError, match="larger than"):
        cli_fetch.install(_spec(), tmp_path / "ab", downloader=lambda url, timeout: PAYLOAD)
    assert not (tmp_path / "ab").exists()


def test_only_old_temps_are_swept_because_the_cache_is_shared(tmp_path):
    now = time.time()
    old, young, other = tmp_path / ".agent-browser-old.part", tmp_path / ".agent-browser-new.part", tmp_path / "x"
    for f in (old, young, other):
        f.write_bytes(b"x")
    os.utime(old, (now - 3600, now - 3600))
    cli_fetch._sweep_stale_temps(tmp_path, now=now)
    assert not old.exists() and young.exists() and other.exists()


# ── ensure_cli: once, joined, and never retried behind the operator's back ───────


def test_first_ensure_downloads_once_then_resolves_from_the_cache(monkeypatch):
    _pin_fake(monkeypatch)
    calls, done = [], []

    def dl(url, timeout):
        calls.append(url)
        return PAYLOAD

    st = cli_fetch.ensure_cli(background=False, downloader=dl, on_done=lambda: done.append(1))
    assert st["state"] == "done" and Path(st["path"]) == cli_fetch.fetched_path() and done == [1]
    assert cli_fetch.installed_path() == st["path"]
    assert cli_fetch.ensure_cli(background=False, downloader=dl)["state"] == "done"
    assert len(calls) == 1


def test_a_download_in_flight_is_joined_not_repeated(monkeypatch):
    _pin_fake(monkeypatch)
    gate, calls, out = threading.Event(), [], []

    def slow(url, timeout):
        calls.append(url)
        gate.wait(5)
        return PAYLOAD

    assert cli_fetch.ensure_cli(background=True, downloader=slow)["state"] == "fetching"
    joiner = threading.Thread(target=lambda: out.append(cli_fetch.ensure_cli(background=False, wait=5,
                                                                               downloader=slow)))
    joiner.start()
    time.sleep(0.05)
    gate.set()
    joiner.join(5)
    assert out and out[0]["state"] == "done" and len(calls) == 1


def test_a_failed_download_is_not_retried_automatically_but_is_on_the_button(monkeypatch):
    _pin_fake(monkeypatch)
    calls = []

    def bad(url, timeout):
        calls.append(url)
        raise OSError("connection reset")

    st = cli_fetch.ensure_cli(background=False, downloader=bad)
    assert st["state"] == "failed" and "connection reset" in st["error"]
    assert cli_fetch.ensure_cli(background=False, downloader=bad)["state"] == "failed"
    assert len(calls) == 1                                           # the tool path didn't retry
    assert cli_fetch.ensure_cli(background=False, force=True,
                                downloader=lambda url, timeout: PAYLOAD)["state"] == "done"


def test_a_tampered_cached_binary_is_not_resolved(monkeypatch):
    _pin_fake(monkeypatch)
    cli_fetch.ensure_cli(background=False, downloader=lambda url, timeout: PAYLOAD)
    path = Path(cli_fetch.installed_path())
    path.write_bytes(b"not the pinned build")
    assert cli_fetch.installed_path() == ""
    assert preflight.locate("agent-browser") == ("", "") or preflight.locate("agent-browser")[1] == "path"


def test_an_unsupported_platform_is_a_clear_state_not_a_crash(monkeypatch):
    monkeypatch.setattr(cli_fetch, "platform_key", lambda *a, **k: None)
    st = cli_fetch.ensure_cli(background=False)
    assert st["state"] == "unsupported" and "publishes no build for this platform" in st["error"]
    assert cli_fetch.installed_path() == ""


# ── resolution: PATH wins; the download stands in only for the stock name ─────────


def test_path_wins_over_the_fetched_cli_and_a_custom_name_never_falls_back(monkeypatch):
    _pin_fake(monkeypatch)
    cli_fetch.ensure_cli(background=False, downloader=lambda url, timeout: PAYLOAD)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/local/bin/agent-browser")
    path, source = preflight.locate("agent-browser")
    assert Path(path) == Path("/usr/local/bin/agent-browser") and source == "path"
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    assert preflight.locate("agent-browser") == (cli_fetch.installed_path(), "fetched")
    assert preflight.locate("ab") == ("", "")


def test_the_probe_runs_the_fetched_cli(monkeypatch):
    _pin_fake(monkeypatch)
    cli_fetch.ensure_cli(background=False, downloader=lambda url, timeout: PAYLOAD)
    _cli_env(monkeypatch, which=None)
    probe = preflight.probe({})
    assert probe.cli_ok and probe.source == "fetched" and Path(probe.cli_path) == cli_fetch.fetched_path()


# ── the CLI gap: a Download button the host keeps ────────────────────────────────


def test_a_missing_cli_offers_download_and_configure_buttons_the_host_keeps(monkeypatch):
    _cli_env(monkeypatch, which=None)
    reg = _registry()
    preflight.report(reg, {})
    msg = reg.setup_gaps[preflight.CLI_GAP]
    assert "v0.27.1" in msg and "checksum-verified" in msg and "npm i -g agent-browser" in msg
    assert len(msg) <= setup_gaps.MAX_MESSAGE_CHARS
    assert _host_actions(reg.setup_gap_actions[preflight.CLI_GAP]) == [
        {"kind": "plugin_setup", "target": "agent_browser", "step": "download-cli", "label": "Download agent-browser"},
        {"kind": "plugin_config", "target": "agent_browser", "label": "Set the CLI path", "fields": ["binary"]},
    ]


def test_while_downloading_the_banner_has_no_button_and_a_failure_offers_retry(monkeypatch):
    _pin_fake(monkeypatch)
    _cli_env(monkeypatch, which=None)
    holder = cli_fetch._slot()
    holder.state.update(state="fetching", started=time.time())
    reg = _registry()
    preflight.report(reg, {})
    assert "downloading the agent-browser CLI v0.27.1" in reg.setup_gaps[preflight.CLI_GAP]
    assert preflight.CLI_GAP not in reg.setup_gap_actions             # nothing to double-click

    holder.state.update(state="failed", error="OSError: connection reset by peer")
    preflight.report(reg, {})
    msg = reg.setup_gaps[preflight.CLI_GAP]
    assert "failed" in msg and "connection reset by peer" in msg and len(msg) <= setup_gaps.MAX_MESSAGE_CHARS
    retry, configure = reg.setup_gap_actions[preflight.CLI_GAP]
    assert retry == {"kind": "plugin_setup", "step": "download-cli", "label": "Retry download"}
    assert configure["kind"] == "plugin_config"


def test_a_custom_binary_is_never_offered_the_download(monkeypatch):
    _cli_env(monkeypatch, which=None)
    reg = _registry({"binary": "ab"})
    preflight.report(reg, reg.config)
    assert reg.setup_gap_actions[preflight.CLI_GAP]["kind"] == "plugin_config"


def test_an_unsupported_platform_gets_a_clear_gap_and_no_download_button(monkeypatch):
    monkeypatch.setattr(cli_fetch, "platform_key", lambda *a, **k: None)
    _cli_env(monkeypatch, which=None)
    reg = _registry()
    preflight.report(reg, {})
    msg = reg.setup_gaps[preflight.CLI_GAP]
    assert "publishes no build for this platform" in msg and "npm i -g agent-browser" in msg
    assert reg.setup_gap_actions[preflight.CLI_GAP]["kind"] == "plugin_config"


# ── the Chrome gap: an Install Chrome button ─────────────────────────────────────


def test_no_chrome_offers_an_install_button_the_host_keeps(monkeypatch):
    _cli_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome binary found")
    reg = _registry()
    preflight.report(reg, {})
    assert "No Chrome binary found" in reg.setup_gaps[preflight.CHROME_GAP]
    assert _host_actions(reg.setup_gap_actions[preflight.CHROME_GAP]) == [
        {"kind": "plugin_setup", "target": "agent_browser", "step": "install-chrome", "label": "Install Chrome"}]


def test_linux_arm64_is_told_to_use_chromium_not_given_a_button_that_must_fail(monkeypatch):
    monkeypatch.setattr(chrome_install, "supported", lambda *a, **k: False)
    _cli_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome binary found")
    reg = _registry()
    preflight.report(reg, {})
    assert "Linux ARM64" in reg.setup_gaps[preflight.CHROME_GAP]
    assert preflight.CHROME_GAP not in reg.setup_gap_actions


def test_chrome_install_is_supported_everywhere_but_linux_arm64():
    assert chrome_install.supported("Linux", "aarch64") is False
    assert all(chrome_install.supported(s, m) for s, m in [("Linux", "x86_64"), ("Darwin", "arm64"),
                                                            ("Windows", "AMD64")])


# ── the steps behind the buttons ─────────────────────────────────────────────────


def test_register_registers_both_steps(monkeypatch):
    _cli_env(monkeypatch, which=None)
    reg = _registry()
    _PKG.register(reg)
    assert set(reg.setup_steps) == {"download-cli", "install-chrome"}
    assert reg.setup_gap_actions[preflight.CLI_GAP][0]["step"] in reg.setup_steps


def test_the_download_button_fetches_in_the_background_and_the_banner_moves_on(monkeypatch):
    _pin_fake(monkeypatch)
    monkeypatch.setattr(cli_fetch, "_urllib_download", lambda url, timeout: PAYLOAD)
    _cli_env(monkeypatch, which=None, chrome="fail", chrome_msg="No Chrome binary found")
    reg = _registry()
    _PKG.register(reg)
    assert preflight.CLI_GAP in reg.setup_gaps
    res = reg.setup_steps["download-cli"]()
    assert res["ok"] is True
    assert cli_fetch._slot().idle.wait(5)
    assert cli_fetch.fetch_state()["state"] == "done"
    assert preflight.CLI_GAP not in reg.setup_gaps                      # the CLI banner cleared…
    assert reg.setup_gap_actions[preflight.CHROME_GAP]["step"] == "install-chrome"  # …and Chrome is next


def test_the_download_button_is_a_no_op_when_a_cli_is_on_path(monkeypatch):
    _cli_env(monkeypatch, which="/opt/ab")
    calls = []
    monkeypatch.setattr(cli_fetch, "_urllib_download", lambda url, timeout: calls.append(url) or PAYLOAD)
    res = setup_steps.download_cli({}, refresh=None)
    assert res["ok"] is True and "already on PATH" in res["message"] and calls == []


def test_the_download_button_refuses_when_binary_names_something_else(monkeypatch):
    res = setup_steps.download_cli({"binary": "/opt/custom/ab"}, refresh=None)
    assert res["ok"] is False and "`binary` setting" in res["message"]


def test_the_install_chrome_button_runs_the_resolved_cli_and_the_gap_clears(monkeypatch):
    installs = []
    _cli_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome binary found", installs=installs)
    reg = _registry()
    _PKG.register(reg)
    assert preflight.CHROME_GAP in reg.setup_gaps
    res = reg.setup_steps["install-chrome"]()
    assert res["ok"] is True
    assert chrome_install._slot().idle.wait(5)
    assert [Path(a[0]) for a in installs] == [Path("/opt/ab")] and [a[1:] for a in installs] == [["install"]]
    assert chrome_install.state()["state"] == "done"
    assert reg.setup_gaps == {}
    # …and the re-probe noted the Chrome it installed, so stealth claims THAT version
    assert "Chrome/151.0.0.0 " in rt.stealth_user_agent()


def test_a_failed_chrome_install_puts_the_clis_error_and_a_retry_on_the_banner(monkeypatch):
    _cli_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome binary found", install_rc=1,
             install_err="\x1b[31m✗\x1b[0m Failed to download Chrome: connection timed out")
    reg = _registry()
    _PKG.register(reg)
    reg.setup_steps["install-chrome"]()
    assert chrome_install._slot().idle.wait(5)
    msg = reg.setup_gaps[preflight.CHROME_GAP]
    assert "connection timed out" in msg and "\x1b" not in msg
    assert reg.setup_gap_actions[preflight.CHROME_GAP]["label"] == "Retry Chrome install"


def test_installing_shows_progress_without_a_button(monkeypatch):
    _cli_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome binary found")
    chrome_install._slot().state.update(state="installing", started=time.time())
    reg = _registry()
    preflight.report(reg, {})
    assert "installing Chrome for Testing" in reg.setup_gaps[preflight.CHROME_GAP]
    assert preflight.CHROME_GAP not in reg.setup_gap_actions


def test_install_chrome_without_a_cli_says_so(monkeypatch):
    _cli_env(monkeypatch, which=None)
    res = setup_steps.install_chrome({}, refresh=None)
    assert res["ok"] is False and "CLI first" in res["message"]


def test_a_second_install_click_joins_the_first(monkeypatch):
    gate, runs = threading.Event(), []

    def slow_run(args, **kw):
        runs.append(args)
        gate.wait(5)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(chrome_install.subprocess, "run", slow_run)
    assert chrome_install.start("/opt/ab")["state"] == "installing"
    assert chrome_install.start("/opt/ab")["state"] == "installing"
    gate.set()
    assert chrome_install._slot().idle.wait(5)
    assert len(runs) == 1


# ── first use: the tools fetch the CLI (never Chrome) ─────────────────────────────


class _Proc:
    def __init__(self, out=b"ok"):
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(b"")
        self.returncode, self.pid = None, None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        pass


async def test_first_use_downloads_the_cli_then_runs_it(monkeypatch):
    _pin_fake(monkeypatch)
    monkeypatch.setattr(cli_fetch, "_urllib_download", lambda url, timeout: PAYLOAD)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    argv = []
    monkeypatch.setattr(tools.subprocess, "Popen", lambda args, **kw: argv.append(list(args)) or _Proc())
    out = await {t.name: t for t in tools.get_browser_tools({})}["browser_snapshot"].ainvoke({})
    assert out == "ok"
    assert Path(argv[0][0]) == cli_fetch.fetched_path() and argv[0][1:] == ["snapshot"]


async def test_first_use_with_autofetch_off_never_downloads(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(cli_fetch, "_urllib_download", lambda url, timeout: calls.append(url) or PAYLOAD)

    def missing(args, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(tools.subprocess, "Popen", missing)
    out = await {t.name: t for t in tools.get_browser_tools({"cli_autofetch": "false"})}["browser_snapshot"].ainvoke({})
    assert "not on PATH" in out and calls == []


async def test_a_failed_first_use_download_is_explained_in_the_tool_result(monkeypatch):
    _pin_fake(monkeypatch)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    def missing(args, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(tools.subprocess, "Popen", missing)
    out = await {t.name: t for t in tools.get_browser_tools({})}["browser_snapshot"].ainvoke({})
    assert "downloading it failed" in out and "network disabled" in out and "Retry" in out


async def test_no_tool_call_ever_installs_chrome(monkeypatch):
    """Chrome is the operator's call: a tool call with no Chrome fails and raises the
    banner; it never runs `agent-browser install`."""
    _cli_env(monkeypatch, which="/opt/ab", chrome="fail", chrome_msg="No Chrome binary found")
    spawned = []

    def popen(args, **kw):
        spawned.append(list(args))
        p = _Proc(b"")
        p.stderr = io.BytesIO(b"Chrome not found")
        p.wait = lambda timeout=None: setattr(p, "returncode", 1) or 1
        return p

    monkeypatch.setattr(tools.subprocess, "Popen", popen)
    out = await {t.name: t for t in tools.get_browser_tools({})}["browser_open"].ainvoke({"url": "https://x.com"})
    assert out.startswith("Error:")
    assert not any("install" in a for a in spawned) and chrome_install.state()["state"] == "idle"


# ── stealth: OFF by default; when on, it claims the INSTALLED Chrome ─────────────


def test_stealth_stays_off_by_default():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["config"]["stealth"] is False
    assert "--user-agent" not in rt.launch_flags({})


def test_the_stealth_ua_claims_the_installed_chromes_version(monkeypatch):
    _cli_env(monkeypatch, which="/opt/ab", chrome_msg="Google Chrome for Testing 151.0.7900.12 at /x/chrome")
    assert preflight.probe({}).chrome_version == "151.0.7900.12"
    flags = rt.launch_flags({"stealth": True})
    ua = flags[flags.index("--user-agent") + 1]
    # Chrome's own reduced-UA form: the major, then zeros — what a real Chrome 151 sends
    assert "Chrome/151.0.0.0 Safari/537.36" in ua and "HeadlessChrome" not in ua and "149" not in ua


def test_the_stealth_ua_falls_back_when_the_version_is_unknown(monkeypatch):
    _cli_env(monkeypatch, which="/opt/ab", chrome_msg="Chrome at /opt/chrome (version unknown)")
    assert preflight.probe({}).chrome_version == ""
    flags = rt.launch_flags({"stealth": True})
    assert flags[flags.index("--user-agent") + 1] == rt._STEALTH_UA
    assert "Chrome/149.0.0.0" in rt._STEALTH_UA


@pytest.mark.parametrize("text", ["", "abc", "1.2.3.4", "5000.1.2.3", None])
def test_an_implausible_chrome_version_never_lands_in_the_ua(text):
    assert rt.note_chrome_version(text) == 0
    assert "Chrome/149.0.0.0" in rt.stealth_user_agent()


def test_the_version_is_read_from_doctors_real_message_shape():
    msg = ("Google Chrome for Testing 149.0.7827.55 at /Users/me/.agent-browser/browsers/"
           "chrome-149.0.7827.55/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    assert preflight.chrome_version_of(msg) == "149.0.7827.55"


# ── the real thing (opt-in) ──────────────────────────────────────────────────────


@pytest.mark.skipif(not _NETWORK, reason="downloads the real pinned agent-browser release — "
                                         "set PROTOAGENT_NETWORK_TESTS=1 to run it")
def test_real_download_of_the_pinned_asset_verifies_runs_and_refuses_a_flipped_byte(monkeypatch, tmp_path):
    spec = cli_fetch.fetch_spec()
    if spec is None:
        pytest.skip("upstream publishes no agent-browser build for this host")
    monkeypatch.setattr(cli_fetch, "_egress_check", _REAL_EGRESS)
    fetched: list[bytes] = []

    def real(url, timeout):
        data = _REAL_DOWNLOAD(url, timeout)
        fetched.append(data)
        return data

    st = cli_fetch.ensure_cli(background=False, downloader=real)
    assert st["state"] == "done", st
    path = Path(st["path"])
    assert path == cli_fetch.fetched_path() and _sha(path.read_bytes()) == spec.sha256
    p = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout.strip() == f"agent-browser {cli_fetch.CLI_VERSION}", p

    # The same real bytes with ONE bit flipped are refused, and nothing is left behind.
    corrupt = bytearray(fetched[0])
    corrupt[len(corrupt) // 2] ^= 0x01
    dest = tmp_path / "flipped" / cli_fetch.binary_name()
    with pytest.raises(cli_fetch.ChecksumError):
        cli_fetch.install(spec, dest, downloader=lambda url, timeout: bytes(corrupt))
    assert not dest.exists() and (not dest.parent.exists() or list(dest.parent.iterdir()) == [])
