"""agent_browser perception budget: the cheaper snapshot, browser_wait, dotted-id selectors,
stale-ref guidance and screenshot options — argv construction and validation, host-free.

The overflow-truncation tests live with the other byte-cap tests in
``tests/test_agent_browser_plugin.py``; the real-browser checks are in
``tests/test_agent_browser_perception_live.py``.
"""

from __future__ import annotations

import importlib
import io
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "agent_browser"
_PKG = load_plugin(ROOT, "agent_browser")
tools = importlib.import_module(f"{_PKG.__name__}.tools")
forms = importlib.import_module(f"{_PKG.__name__}.forms")
cli_fetch = importlib.import_module(f"{_PKG.__name__}.cli_fetch")

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture(autouse=True)
def _offline_cli_cache(monkeypatch, tmp_path):
    # Never download a real CLI from a unit test (first use would fetch it).
    monkeypatch.setenv(cli_fetch.ENV_CLI_DIR, str(tmp_path / "ab-cli-cache"))
    monkeypatch.setattr(cli_fetch, "_urllib_download",
                        lambda url, timeout: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(cli_fetch, "_egress_check", lambda url: None)
    cli_fetch.reset_state()
    yield
    cli_fetch.reset_state()


class _Sink(io.BytesIO):
    def close(self):  # keep what the feeder wrote readable after it closes stdin
        pass


class _Proc:
    def __init__(self, out=b"", err=b"", rc=0):
        self.stdin = _Sink()
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(err)
        self._rc, self.returncode, self.pid = rc, None, None

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.returncode = -9


class _CLI:
    """A scripted CLI: records (argv, env, stdin) per call; `answer(argv)` -> (out, err, rc)."""

    def __init__(self, answer=None):
        self.calls, self._answer = [], answer or (lambda argv: ("ok", "", 0))

    def __call__(self, argv, **kw):
        out, err, rc = self._answer(list(argv[1:]))
        p = _Proc(out.encode() if isinstance(out, str) else out, err.encode(), rc)
        self.calls.append((list(argv[1:]), kw.get("env"), p))
        return p

    @property
    def argvs(self):
        return [a for a, _, _ in self.calls]


def _tools(monkeypatch, answer=None, **cfg):
    cli = _CLI(answer)
    monkeypatch.setattr(tools.subprocess, "Popen", cli)
    return {t.name: t for t in tools.get_browser_tools({"binary": "ab", **cfg})}, cli


# ── browser_snapshot ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("args,argv", [
    ({}, ["snapshot", "-i", "-c"]),
    ({"full": True}, ["snapshot"]),
    ({"scope": "#main"}, ["snapshot", "-i", "-c", "-s", "#main"]),
    ({"depth": 4}, ["snapshot", "-i", "-c", "-d", "4"]),
    ({"full": True, "scope": "form", "depth": 2}, ["snapshot", "-s", "form", "-d", "2"]),
])
async def test_snapshot_argv(monkeypatch, args, argv):
    t, cli = _tools(monkeypatch)
    await t["browser_snapshot"].ainvoke(args)
    assert cli.argvs == [argv]


async def test_snapshot_refuses_a_flag_shaped_scope_and_a_negative_depth(monkeypatch):
    t, cli = _tools(monkeypatch)
    assert (await t["browser_snapshot"].ainvoke({"scope": "--headed"})).startswith("Error:")
    assert (await t["browser_snapshot"].ainvoke({"depth": -1})).startswith("Error:")
    assert cli.calls == []


@pytest.mark.parametrize("tool,args", [
    ("browser_get_text", {"selector": " --headed"}),
    ("browser_get_html", {"selector": "\t--allow-file-access"}),
    ("browser_snapshot", {"scope": "  --headed"}),
    ("browser_wait", {"selector": " --headed"}),
    ("browser_wait", {"url": "*--allow-file-access"}),
    ("browser_wait", {"url": " **--headed* "}),
])
async def test_a_flag_hidden_behind_whitespace_or_a_glob_is_still_refused(monkeypatch, tool, args):
    # QA panel (#4922): operands were stripped AFTER the option guard ran, so " --headed" or
    # "*--allow-file-access" passed the guard and reached argv as a bare flag (CWE-88).
    t, cli = _tools(monkeypatch)
    out = await t[tool].ainvoke(args)
    assert out.startswith("Error:") and "command-line option" in out, out
    assert not any(a and a[0] in ("get", "snapshot", "wait") for a in cli.argvs)


def test_snapshot_docstring_says_to_read_text_with_get_text():
    t = {x.name: x for x in tools.get_browser_tools({})}
    assert "browser_get_text" in t["browser_snapshot"].description


# ── browser_wait ─────────────────────────────────────────────────────────────────


def _waits(cli):
    return [(a, env) for a, env, _ in cli.calls if a[0] == "wait"]


@pytest.mark.parametrize("args,argv", [
    ({"selector": "#done"}, ["wait", "#done"]),
    ({"text": "Thanks for applying"}, ["wait", "--text", "Thanks for applying", "--timeout", "25000"]),
    ({"url": "/confirmation"}, ["wait", "--url", "/confirmation"]),
    ({"url": "**/confirmation"}, ["wait", "--url", "/confirmation"]),  # edge globs stripped
    ({"load": "networkidle"}, ["wait", "--load", "networkidle"]),
    ({"js": "!document.querySelector('.spinner')"}, ["wait", "--fn", "!document.querySelector('.spinner')"]),
])
async def test_wait_argv_and_explicit_timeout(monkeypatch, args, argv):
    t, cli = _tools(monkeypatch)
    out = await t["browser_wait"].ainvoke(args)
    assert out == "ok"
    (got, env), = _waits(cli)
    assert got == argv
    # every wait carries an explicit timeout (the daemon's 30 s default == the IPC timeout)
    assert env["AGENT_BROWSER_DEFAULT_TIMEOUT"] == "25000"
    # the daemon is made to exist first, under the normal env, so the timeout env can't
    # become the default of a freshly launched daemon
    first_argv, first_env = cli.calls[0][0], cli.calls[0][1]
    assert first_argv == ["get", "url"] and "AGENT_BROWSER_DEFAULT_TIMEOUT" not in first_env
    # env_extra is layered over the per-instance session env, not over a bare os.environ
    assert env["AGENT_BROWSER_SESSION"] == first_env["AGENT_BROWSER_SESSION"] != "default"


@pytest.mark.parametrize("timeout_s,cfg,ms", [
    (3, {}, "3000"),
    (999, {}, "25000"),                 # capped at the CLI-safe max
    (0, {"timeout_s": 10}, "8000"),     # below the plugin's subprocess timeout
    (0.01, {}, "100"),
])
async def test_wait_timeout_is_bounded(monkeypatch, timeout_s, cfg, ms):
    t, cli = _tools(monkeypatch, **cfg)
    await t["browser_wait"].ainvoke({"text": "x", "timeout_s": timeout_s})
    (argv, env), = _waits(cli)
    assert argv[-1] == ms and env["AGENT_BROWSER_DEFAULT_TIMEOUT"] == ms


@pytest.mark.parametrize("args,needle", [
    ({}, "exactly ONE"),
    ({"selector": "#a", "text": "b"}, "exactly ONE"),
    ({"selector": "@e3"}, "not a @ref"),
    ({"selector": "1500"}, "fixed sleep"),
    ({"load": "complete"}, "load must be one of"),
    ({"url": "/a/*/b"}, "no wildcards"),
    ({"url": "***"}, "no wildcards"),
    ({"text": "x", "timeout_s": -1}, "positive"),
    # the argv option guard covers every condition value
    ({"text": "--headed"}, "looks like a command-line option"),
    ({"selector": "-h"}, "looks like a command-line option"),
    ({"url": "--allow-file-access"}, "looks like a command-line option"),
    ({"load": "--profile"}, "looks like a command-line option"),
    ({"js": "-x"}, "looks like a command-line option"),
])
async def test_wait_validation_runs_before_any_subprocess(monkeypatch, args, needle):
    t, cli = _tools(monkeypatch)
    out = await t["browser_wait"].ainvoke(args)
    assert out.startswith("Error:") and needle in out, out
    assert cli.calls == []


async def test_a_wait_that_times_out_says_what_to_do(monkeypatch):
    def answer(argv):
        if argv[0] == "wait":
            return "", "✗ Wait timed out after 3000ms", 1
        return "https://x.test/", "", 0

    t, _ = _tools(monkeypatch, answer)
    out = await t["browser_wait"].ainvoke({"selector": "#done", "timeout_s": 3})
    assert out.startswith("Error: waited 3s for selector='#done'") and "browser_snapshot" in out


async def test_a_wait_when_the_page_cannot_be_read_returns_that_error(monkeypatch):
    t, cli = _tools(monkeypatch, lambda argv: ("", "✗ Browser not launched", 1))
    out = await t["browser_wait"].ainvoke({"text": "x"})
    assert out.startswith("Error:") and "Browser not launched" in out
    assert not _waits(cli)


# ── dotted / CSS-special ids ─────────────────────────────────────────────────────


@pytest.mark.parametrize("sel,target", [
    ("#input-candidate.name-3", "input-candidate.name-3"),
    ("#a:b", "a:b"),
    ("#x[0]", "x[0]"),
    ("#3col", "3col"),
    ("  #a.b  ", "a.b"),
    ("#plain-id_1", ""),          # a plain id needs no probe
    ("#a .b", ""),                # descendant chain — a real selector
    ("#a > b", ""),
    ("#a, #b", ""),
    ('#a[x="1"]', ""),
    (".cls", ""),
    ("@e3", ""),
    ("#", ""),
])
def test_id_probe_target(sel, target):
    assert forms.id_probe_target(sel) == target


def test_id_attr_selector_escapes_quotes_and_backslashes():
    assert forms.id_attr_selector("input-candidate.name-3") == '[id="input-candidate.name-3"]'
    assert forms.id_attr_selector('a"b\\c') == '[id="a\\"b\\\\c"]'


@pytest.mark.parametrize("out,mode", [('"id"', "id"), ("css", "css"), ('"none"', "none"),
                                      ("", "none"), ("garbage", "none")])
def test_parse_id_probe(out, mode):
    assert forms.parse_id_probe(out) == mode


def _probe_answer(mode):
    def answer(argv):
        if argv[:2] == ["eval", "--stdin"]:
            return f'"{mode}"', "", 0
        return "ok", "", 0
    return answer


async def test_fill_rewrites_a_dotted_id_that_only_matches_as_an_exact_id(monkeypatch):
    t, cli = _tools(monkeypatch, _probe_answer("id"))
    out = await t["browser_fill"].ainvoke({"selector": "#input-candidate.name-3", "text": "Ada"})
    assert out == "ok"
    assert cli.argvs[-1] == ["fill", '[id="input-candidate.name-3"]', "Ada"]
    probe_js = cli.calls[0][2].stdin.getvalue().decode()
    assert "getElementById(\"input-candidate.name-3\")" in probe_js


async def test_a_dotted_selector_that_matches_as_css_is_left_alone(monkeypatch):
    t, cli = _tools(monkeypatch, _probe_answer("css"))
    await t["browser_click"].ainvoke({"selector": "#cart.open"})
    assert cli.argvs[-1] == ["click", "#cart.open"]


async def test_a_plain_id_costs_no_probe(monkeypatch):
    t, cli = _tools(monkeypatch)
    await t["browser_fill"].ainvoke({"selector": "#email", "text": "a@b.c"})
    assert cli.argvs == [["fill", "#email", "a@b.c"]]


@pytest.mark.parametrize("tool,args,argv", [
    ("browser_get_text", {"selector": "#a.b"}, ["get", "text", '[id="a.b"]']),
    ("browser_get_html", {"selector": "#a.b"}, ["get", "html", '[id="a.b"]']),
    ("browser_get_value", {"selector": "#a.b"}, ["get", "value", '[id="a.b"]']),
    ("browser_snapshot", {"scope": "#a.b"}, ["snapshot", "-i", "-c", "-s", '[id="a.b"]']),
    ("browser_wait", {"selector": "#a.b"}, ["wait", '[id="a.b"]']),
])
async def test_every_css_operand_gets_the_dotted_id_fix(monkeypatch, tool, args, argv):
    t, cli = _tools(monkeypatch, _probe_answer("id"))
    await t[tool].ainvoke(args)
    assert cli.argvs[-1] == argv


# ── stale refs ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("err,ref", [
    ("✗ Unknown ref: e4", "@e4"),
    ('✗ Could not locate element with role=button name=Submit', "that @ref"),
])
async def test_a_stale_ref_tells_the_model_what_to_do(monkeypatch, err, ref):
    t, _ = _tools(monkeypatch, lambda argv: ("", err, 1))
    out = await t["browser_click"].ainvoke({"selector": "@e4"})
    assert out.startswith(f"Error: {ref} is stale or unknown")
    assert "browser_snapshot" in out and "LABEL" in out


async def test_other_errors_pass_through(monkeypatch):
    t, _ = _tools(monkeypatch, lambda argv: ("", "✗ Element not found", 1))
    out = await t["browser_click"].ainvoke({"selector": "#nope"})
    assert "Element not found" in out and "stale" not in out


# ── browser_screenshot ───────────────────────────────────────────────────────────


def _screenshot_answer(argv):
    if argv[:2] == ["get", "url"]:
        return "https://x.test/", "", 0
    if "screenshot" in argv:
        Path(argv[-1]).write_bytes(b"\x89PNG fake")
        return "✓ Screenshot saved", "", 0
    return "ok", "", 0


@pytest.mark.parametrize("args,argv_head", [
    ({}, ["screenshot"]),
    ({"full_page": True}, ["screenshot", "--full"]),
    ({"annotate": True}, ["--annotate", "screenshot"]),
    ({"full_page": True, "annotate": True}, ["--annotate", "screenshot", "--full"]),
])
async def test_screenshot_options(monkeypatch, args, argv_head):
    t, cli = _tools(monkeypatch, _screenshot_answer)
    out = await t["browser_screenshot"].ainvoke(args)
    shot = cli.argvs[-1]
    assert shot[:-1] == argv_head and shot[-1].endswith(".png")
    # the vision attachment still rides the result
    from graph.multimodal import is_multimodal_result
    assert is_multimodal_result(out)
