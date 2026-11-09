"""The plugin browser client (plugins/agent_browser/client.py) — host-free, with a scripted CLI.

Every test drives ``BrowserClient`` through a fake ``subprocess.Popen`` that answers per argv, so
what's asserted is the argv the client builds (launch flags on every open, never a URL-less open,
explicit wait timeouts, tab ownership) and how each CLI outcome maps to a typed ``Result`` kind.
The real-browser counterpart is test_agent_browser_client_live.py.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from graph.plugins.testkit import FakeRegistry, load_plugin

_PKG = load_plugin(Path(__file__).resolve().parents[1] / "plugins" / "agent_browser", "agent_browser")
client = importlib.import_module(f"{_PKG.__name__}.client")
runner = importlib.import_module(f"{_PKG.__name__}.runner")
walls = importlib.import_module(f"{_PKG.__name__}.walls")
# Module level, with runner: a later load_plugin() by another test module replaces the package in
# sys.modules, and a tools copy from THAT load would key its guard off a different runner.
tools = importlib.import_module(f"{_PKG.__name__}.tools")


class _Proc:
    def __init__(self, argv, out=b"", err=b"", rc=0, hang=False):
        self.argv, self.pid = list(argv), None
        self.stdin = io.BytesIO()
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(err)
        self._rc, self._hang, self.killed, self.returncode = rc, hang, False, None

    def wait(self, timeout=None):
        if self._hang and not self.killed:
            raise subprocess.TimeoutExpired(self.argv[0], timeout)
        self.returncode = -9 if self.killed else self._rc
        return self.returncode

    def kill(self):
        self.killed = True


def _ok(data) -> bytes:
    return json.dumps({"success": True, "data": data, "error": None}).encode()


def _fail(msg) -> bytes:
    return json.dumps({"success": False, "data": None, "error": msg}).encode()


class CLI:
    """Scripted CLI: ``answer(argv) -> (out, err, rc[, hang])``. Records argv + env + stdin."""

    def __init__(self, answer):
        self.answer, self.calls, self.envs, self.stdins = answer, [], [], []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv[1:]))
        self.envs.append(kw.get("env") or {})
        got = self.answer(list(argv[1:]))
        out, err, rc, *rest = (*got, False) if len(got) == 3 else got
        p = _Proc(argv, out if isinstance(out, bytes) else out.encode(),
                  err if isinstance(err, bytes) else err.encode(), rc, hang=bool(rest and rest[0]))
        orig_close = p.stdin.close

        def _close():
            self.stdins.append(p.stdin.getvalue().decode())
            orig_close()
        p.stdin.close = _close
        return p


def _verb(argv):
    """The command words, flags and their values dropped (``open``, ``tab new``, ``eval``…)."""
    words, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--profile", "--args", "--user-agent", "--label", "--timeout", "--text", "--url", "--load", "--fn"):
            skip = True
            continue
        if not a.startswith("-"):
            words.append(a)
    return words


def _plain_page(argv):
    v = _verb(argv)
    if v[:1] == ["open"]:
        return _ok({"url": v[1], "title": "Page"}), "", 0
    if v[:1] == ["eval"]:
        probe = {"url": "https://shop.example/s?q=x", "title": "Results", "text": "lots " * 400,
                 "text_len": 3000, "links": 80, "frames": [], "markers": {}, "status": 200}
        return _ok({"origin": "x", "result": json.dumps(probe)}), "", 0
    return _ok({}), "", 0


@pytest.fixture
def make(monkeypatch):
    def _make(answer=_plain_page, **opts):
        cli = CLI(answer)
        monkeypatch.setattr(runner.subprocess, "Popen", cli)
        host = client._Host({"binary": "ab", "cli_autofetch": False})
        opts.setdefault("session", "shop")
        return client.BrowserClient(host=host, **opts), cli
    return _make


# ── the service ──────────────────────────────────────────────────────────────────────────────


def test_register_offers_the_client_service():
    reg = FakeRegistry(config={"binary": "/nonexistent/agent-browser", "cli_autofetch": False},
                       plugin_id="agent_browser")
    _PKG.register(reg)
    make = reg.services["agent_browser.client"]
    b = make(session="shop", own_tab="shoptab")
    assert isinstance(b, make.BrowserClient)
    assert b.session.endswith("-shop") and b.session.startswith("pa-")


def test_the_factory_reads_cores_config_live(monkeypatch):
    cfg = {"binary": "ab1", "cli_autofetch": False}
    b = client.make_factory(lambda: cfg)(session="shop")
    assert b._host.binary == "ab1"
    cfg["binary"] = "ab2"
    cfg["max_response_bytes"] = 1234
    assert (b._host.binary, b._host.max_bytes) == ("ab2", 1234)


@pytest.mark.parametrize("bad", [dict(session=""), dict(session="--headed"), dict(session="a/b"),
                                 dict(session="x" * 25), dict(session="ok", own_tab="has space"),
                                 dict(session="ok", browser_args=["--a,--b"]),
                                 dict(session="ok", browser_args=["nodash"]),
                                 dict(session="ok", profile="--headed")])
def test_invalid_options_are_a_construction_error(bad):
    with pytest.raises(ValueError):
        client.BrowserClient(**bad)


def test_instance_scoped_by_default_and_verbatim_when_asked():
    assert client.BrowserClient(session="ebay").session.startswith("pa-")
    assert client.BrowserClient(session="ebay", instance_scoped=False).session == "ebay"


# ── open ─────────────────────────────────────────────────────────────────────────────────────


def test_open_carries_the_launch_flags_and_the_session_rides_the_env(make):
    b, cli = make(instance_scoped=False, profile="/p/prof", headed=True,
                  browser_args=["--disable-blink-features=AutomationControlled"])
    r = b.open("https://shop.example/s?q=x")
    assert r.ok and r.value == {"url": "https://shop.example/s?q=x", "title": "Page"}
    assert cli.calls[0] == ["--json", "open", "https://shop.example/s?q=x", "--headed", "--profile", "/p/prof",
                            "--args", "--disable-blink-features=AutomationControlled"]
    assert cli.envs[0]["AGENT_BROWSER_SESSION"] == "shop"
    assert "--session" not in cli.calls[0]


def test_open_adds_no_launch_options_of_its_own(make):
    """No stealth/anti-throttle defaults: a plugin gets exactly the options it asked for."""
    b, cli = make()
    b.open("https://shop.example/")
    assert cli.calls[0] == ["--json", "open", "https://shop.example/"]


def test_open_never_runs_a_url_less_open(make):
    b, cli = make(headed=True, profile="/p")
    r = b.open("")
    assert r.kind == "bad_argument" and "temp profile" in r.error
    assert cli.calls == []


def test_an_option_shaped_url_is_refused_before_any_subprocess(make):
    b, cli = make()
    r = b.open("--headed")
    assert r.kind == "bad_argument" and cli.calls == []


def test_open_reports_a_wall_typed(make, monkeypatch):
    monkeypatch.setattr(client, "_WALL_RECHECK_S", 0)
    cf = {"url": "https://shop.example/s", "title": "Just a moment...", "text": "Checking", "text_len": 40,
          "links": 0, "frames": [], "markers": {"cf": True}, "status": 403}

    def answer(argv):
        if _verb(argv)[:1] == ["eval"]:
            return _ok({"result": json.dumps(cf)}), "", 0
        return _plain_page(argv)
    b, cli = make(answer)
    r = b.open("https://shop.example/s")
    assert r.kind == "wall" and r.wall.kind == "cloudflare" and r.wall.transient
    assert "Do not try to solve it" in r.error and not r.error.startswith("Error:")
    assert sum(1 for c in cli.calls if _verb(c)[:1] == ["eval"]) == 3  # re-probed twice first
    assert r.to_dict()["wall"]["label"] == "a Cloudflare bot check"


def test_a_timed_out_open_recovers_once_in_a_fresh_own_tab(make):
    state = {"opens": 0}

    def answer(argv):
        v = _verb(argv)
        if v[:1] == ["open"]:
            state["opens"] += 1
            return (b"", b"", 0, True) if state["opens"] == 1 else _plain_page(argv)
        if v == ["tab", "list"]:
            return _ok({"tabs": []}), "", 0
        return _plain_page(argv)
    b, cli = make(answer, own_tab="shoptab", nav_timeout_s=5)
    r = b.open("https://shop.example/s?q=x")
    assert r.ok, r
    verbs = [_verb(c) for c in cli.calls]
    # switch → open (hangs) → panels closed, OUR tab replaced → switch → open
    assert verbs[:2] == [["tab", "shoptab"], ["open", "https://shop.example/s?q=x"]]
    assert ["tab", "close", "shoptab"] in verbs and ["tab", "new"] in verbs
    assert verbs.count(["open", "https://shop.example/s?q=x"]) == 2


def test_a_second_timeout_is_typed_and_says_it_is_not_a_wall(make):
    def answer(argv):
        if _verb(argv)[:1] == ["open"]:
            return b"", b"", 0, True
        return _ok({"tabs": []}), "", 0
    b, _ = make(answer, own_tab="shoptab", nav_timeout_s=5)
    r = b.open("https://shop.example/")
    assert r.kind == "timeout" and "NOT a bot wall" in r.error and "twice" in r.error
    with pytest.raises(b.Error) as exc:
        r.raise_for_error()
    assert exc.value.kind == "timeout"


def test_the_cli_giving_up_after_its_ipc_retries_is_a_timeout(make):
    def answer(argv):
        if _verb(argv)[:1] == ["open"]:
            return b"", b"\xe2\x9c\x97 Resource temporarily unavailable (os error 35) (after 5 retries - daemon may be busy or unresponsive)", 1
        return _plain_page(argv)
    b, _ = make(answer)
    assert b.open("https://shop.example/", recover=False).kind == "timeout"


def test_a_panel_that_steals_the_tab_is_closed_and_the_open_retried(make):
    state = {"opens": 0}
    panel = {"tabId": "t9", "type": "webview", "url": "https://gemini.google.com/glic"}
    page = {"tabId": "t1", "type": "page", "url": "file:///Users/me/notes.html"}

    def answer(argv):
        v = _verb(argv)
        if v[:1] == ["open"]:
            state["opens"] += 1
            if state["opens"] == 1:
                return _fail("net::ERR_BLOCKED_BY_CLIENT"), "", 1
        if v == ["tab", "list"]:
            return _ok({"tabs": [page, panel]}), "", 0
        return _plain_page(argv)
    b, cli = make(answer, own_tab="shoptab")
    assert b.open("https://shop.example/").ok
    verbs = [_verb(c) for c in cli.calls]
    assert ["tab", "close", "t9"] in verbs
    assert ["tab", "close", "t1"] not in verbs  # an operator's page is never closed


def test_a_wedged_own_tab_is_replaced(make):
    def answer(argv):
        v = _verb(argv)
        if v == ["tab", "shoptab"]:
            return b"", b"\xe2\x9c\x97 tab did not respond", 1
        if v == ["tab", "new"] and not any(_verb(c) == ["tab", "close", "shoptab"] for c in cli_ref[0].calls):
            return b"", b"\xe2\x9c\x97 Label `shoptab` is already used by another tab", 1
        return _plain_page(argv)
    cli_ref = []
    b, cli = make(answer, own_tab="shoptab")
    cli_ref.append(cli)
    assert b.focus_tab().value == "recreated"


def test_a_missing_cli_is_unavailable_not_a_crash(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file")
    monkeypatch.setattr(runner.subprocess, "Popen", boom)
    b = client.BrowserClient(session="shop", host=client._Host({"binary": "/nonexistent/agent-browser",
                                                                 "cli_autofetch": False}))
    r = b.open("https://shop.example/")
    assert r.kind == "unavailable" and "isn't installed" in r.error


def test_a_profile_held_by_another_browser_is_named(make):
    def answer(argv):
        return b"", b"Failed to create a ProcessSingleton for your profile directory", 1
    b, _ = make(answer, profile="/p/prof")
    r = b.open("https://shop.example/", check_walls=False, recover=False)
    assert r.kind == "cli_error" and "already in use" in r.error


def test_navigations_are_paced(make, monkeypatch):
    slept = []
    monkeypatch.setattr(client.time, "sleep", lambda s: slept.append(s))
    b, _ = make(min_interval_s=1.5)
    b.open("https://shop.example/", check_walls=False)
    b.open("https://shop.example/2", check_walls=False)
    assert slept and 0 < slept[-1] <= 1.5


# ── wait / eval / read ───────────────────────────────────────────────────────────────────────


def test_wait_always_sends_an_explicit_timeout_below_the_ipc_read_timeout(make):
    b, cli = make()
    b.wait(selector="li.card", timeout_s=99)
    assert cli.calls[-1] == ["--json", "wait", "li.card"]
    assert cli.envs[-1]["AGENT_BROWSER_DEFAULT_TIMEOUT"] == "25000"
    b.wait(text="Results")
    assert cli.calls[-1] == ["--json", "wait", "--text", "Results", "--timeout", "10000"]


def test_a_wait_that_does_not_happen_is_a_typed_timeout(make):
    b, _ = make(lambda argv: (_fail("Wait timed out after 10000ms"), "", 1))
    r = b.wait(selector="li.card")
    assert r.kind == "timeout" and "li.card" in r.error


@pytest.mark.parametrize("kw", [{}, {"selector": "a", "text": "b"}, {"selector": "@e3"},
                                {"url": "*/a*b"}, {"load": "idle"}, {"selector": "--headed"}])
def test_wait_refuses_a_malformed_condition(make, kw):
    b, cli = make()
    assert b.wait(**kw).kind == "bad_argument" and cli.calls == []


def test_eval_json_unwraps_the_envelope_and_a_stringified_result(make):
    b, cli = make(lambda argv: (_ok({"origin": "x", "result": json.dumps({"rows": [1, 2]})}), "", 0))
    r = b.eval_json("JSON.stringify({rows:[1,2]})")
    assert r.ok and r.value == {"rows": [1, 2]}
    assert cli.calls[-1] == ["--json", "eval", "--stdin"]
    assert cli.stdins[-1] == "JSON.stringify({rows:[1,2]})"  # stdin, never argv


def test_eval_json_keeps_a_plain_string_and_types_a_thrown_script(make):
    b, _ = make(lambda argv: (_ok({"result": "hello"}), "", 0))
    assert b.eval_json("'hello'").value == "hello"
    b, _ = make(lambda argv: (_fail("Evaluation error: Error: boom"), "", 1))
    r = b.eval_json("throw new Error('boom')")
    assert r.kind == "script_error" and "boom" in r.error


def test_get_text_is_truncated_at_the_cap_not_failed(make, monkeypatch):
    big = ("line of page text\n" * 20_000).encode()
    b, _ = make(lambda argv: (big, b"", 0))
    monkeypatch.setattr(type(b._host), "max_bytes", property(lambda self: 1000))
    r = b.get_text("main")
    assert r.ok and r.truncated and len(r.text) <= 1000 and r.text.endswith("line of page text")


def test_snapshot_defaults_to_interactive_compact(make):
    b, cli = make(lambda argv: (b"- button @e1", b"", 0))
    assert b.snapshot(scope="form", depth=3).text == "- button @e1"
    assert cli.calls[-1] == ["snapshot", "-i", "-c", "-s", "form", "-d", "3"]


def test_check_wall_on_the_current_page(make):
    login = {"url": "https://shop.example/login?next=/s", "title": "Sign in", "text": "Sign in",
             "text_len": 100, "links": 3, "frames": [], "markers": {}, "status": 200,
             "password_fields": 1, "other_fields": 1}
    b, _ = make(lambda argv: (_ok({"result": json.dumps(login)}), "", 0))
    r = b.check_wall("https://shop.example/s")
    assert r.kind == "wall" and r.wall.kind == "signin"


# ── the shared session guard ─────────────────────────────────────────────────────────────────


def test_a_client_waits_for_a_tool_holding_the_same_session():
    b = client.BrowserClient(session="shared-x", instance_scoped=False)
    g = runner.guard("shared-x")
    tool_owner = object()
    assert g.try_acquire(tool_owner)
    entered = threading.Event()

    def run():
        with b.hold():
            entered.set()
    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)
    assert not entered.is_set()  # blocked behind the tool
    g.release(tool_owner)
    t.join(2)
    assert entered.is_set()


def test_the_guard_is_reentrant_for_its_owner_only():
    g = runner.SessionGuard()
    assert g.try_acquire("a") and g.try_acquire("a") and not g.try_acquire("b")
    g.release("b")  # never releases someone else's hold
    assert g.held
    g.release("a")
    g.release("a")
    assert not g.held and g.try_acquire("b")


async def test_a_browser_tool_waits_for_a_client_on_the_same_session(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_SESSION", "shared-y")
    monkeypatch.setattr(tools.subprocess, "Popen", CLI(lambda argv: (b"https://x/", b"", 0)))
    t = {x.name: x for x in tools.get_browser_tools({"binary": "ab", "cli_autofetch": False})}
    g, owner = runner.guard("shared-y"), ("client", 1)
    g.acquire(owner)
    task = asyncio.ensure_future(t["browser_back"].ainvoke({}))
    await asyncio.sleep(0.2)
    assert not task.done()
    g.release(owner)
    assert await asyncio.wait_for(task, 5)
