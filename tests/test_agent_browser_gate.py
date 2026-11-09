"""The browser action gate (plugins/agent_browser/gate.py) — policy, approval, and the tool hooks.

Policy tests are pure (no CLI). Tool tests drive the real tools over a scripted ``Popen`` that
answers each CLI command by its argv (and an ``eval``'s script by its stdin), so they prove the
argv the gate adds, and that an ungated install adds none.
"""

from __future__ import annotations

import importlib
import io
import json
import threading
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "agent_browser"
_PKG = load_plugin(ROOT, "agent_browser")
gate = importlib.import_module(f"{_PKG.__name__}.gate")
tools = importlib.import_module(f"{_PKG.__name__}.tools")
rt = importlib.import_module(f"{_PKG.__name__}.runtime")

PAGE = "https://boards.greenhouse.io/acme/jobs/123"


def T(**kw):
    base = {"found": True, "resolved": True, "page_url": PAGE}
    base.update(kw)
    return gate.Target(**base)


@pytest.fixture(autouse=True)
def _clean_policies():
    gate._POLICIES.clear()
    gate._PENDING.clear()
    yield
    gate._POLICIES.clear()
    gate._PENDING.clear()


# ── config ───────────────────────────────────────────────────────────────────────────────────


def test_parse_categories_default_off_all_and_mixed_words():
    assert gate.parse_categories(None) == {"submit"}  # key absent → the default
    assert gate.parse_categories("") == frozenset()  # explicitly emptied → off
    assert gate.parse_categories("all") == set(gate.CATEGORIES)
    assert gate.parse_categories("eval, upload") == {"eval", "upload"}
    assert gate.parse_categories("Submit,EVAL") == {"submit", "eval"}
    # only explicit off-words turn the gate off — nothing else
    for off in ("none", "off", "false", "no", "0", "disabled", "None, OFF"):
        assert gate.parse_categories(off) == frozenset(), off


def test_parse_categories_fails_closed_on_unknown_or_legacy_words():
    # A value made only of unrecognised words must NOT parse to "off" — that silently drops the
    # gate (including on real Submit buttons) for an operator who opted in. It fails closed to
    # the default instead.
    assert gate.parse_categories("click") == {"submit"}        # a legacy CLI verb → submit
    assert gate.parse_categories("nav") == {"submit"}          # a legacy verb with no category
    assert gate.parse_categories("navigate") == {"submit"}
    assert gate.parse_categories("sbumit") == {"submit"}       # a typo
    assert gate.parse_categories("navigate, fill") == {"submit"}
    # a mix of valid and invalid keeps the valid categories and still gates submit for the legacy verb
    assert gate.parse_categories("eval, click") == {"eval", "submit"}
    assert gate.parse_categories("click,navigate,eval") == {"submit", "eval"}
    # an unknown word next to a real category is dropped, not fatal, and never empties the gate
    assert gate.parse_categories("upload, bogus") == {"upload"}


def test_confirm_actions_is_no_longer_a_cli_flag():
    assert "--confirm-actions" not in rt.launch_flags({"confirm_actions": "eval,download"})


# ── policy: clicks ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", [
    T(tag="button", type="submit", name="Submit application", submits=True, form_method="post"),
    T(tag="button", type="submit", name="Send it", submits=True, form_method="post"),
    T(tag="input", type="submit", name="Submit", submits=True, form_method="post"),
    # an unlabelled / oddly-named control that still POSTs a form
    T(tag="button", type="submit", name="→", submits=True, form_method="post"),
    # no form at all, but the name commits something (a React checkout)
    T(tag="div", role="button", name="Place order"),
    T(tag="button", type="button", name="Pay now"),
    T(tag="button", type="button", name="Delete account"),
    T(tag="button", type="button", name="Complete your application"),
    T(tag="button", type="button", name="Finish"),
    # a ref the probe couldn't line up (iframe/overlay) — known only by text + type
    gate.fallback_target("Submit application", ""),
    gate.fallback_target("Continue to review", "submit"),
])
def test_submits_are_gated(target):
    v = gate.classify_click(target)
    assert v.categories == {"submit"}, (target, v)
    assert v.description


@pytest.mark.parametrize("target", [
    T(tag="a", name="Apply now", href=PAGE + "/apply"),
    T(tag="a", name="Apply", href=PAGE + "/apply"),
    T(tag="button", type="submit", name="Apply filters", submits=True, form_method="post"),
    T(tag="button", type="submit", name="Apply", submits=True, form_method="post"),
    T(tag="button", type="button", name="Easy Apply"),
    T(tag="button", type="submit", name="Search", submits=True, form_method="get", form_search=True),
    T(tag="button", type="submit", name="Submit search", submits=True, form_method="post", form_search=True),
    T(tag="button", type="submit", name="Go", submits=True, form_method="post"),
    T(tag="button", type="submit", name="Sign in", submits=True, form_method="post"),
    T(tag="button", type="submit", name="Next", submits=True, form_method="post"),
    T(tag="button", type="submit", name="Send code", submits=True, form_method="post"),
    T(tag="button", type="button", name="Accept all cookies"),
    T(tag="button", type="button", name="Confirm My Choices"),  # OneTrust's cookie banner
    # a GET form is a query, not a commitment
    T(tag="button", type="submit", name="Update", submits=True, form_method="get"),
    T(tag="button", type="button", name="Add another"),
    T(tag="input", name="Full name"),
    gate.fallback_target("Country", ""),
    gate.Target(found=False),
    None,
])
def test_non_submits_pass(target):
    assert gate.classify_click(target).categories == frozenset(), target


def test_download_link():
    v = gate.classify_click(T(tag="a", name="Report.pdf", href="https://x.test/r.pdf", download=True))
    assert v.categories == {"download"}


def test_description_is_plain_words():
    v = gate.classify_click(T(tag="button", type="submit", name="Submit application", submits=True,
                              form_method="post", form_action="https://api.greenhouse.io/apply"))
    assert v.description == ("Submit the form on boards.greenhouse.io — button “Submit application”, "
                             "sending it to api.greenhouse.io")


# ── policy: keys, typing, eval, upload ───────────────────────────────────────────────────────


def test_enter_in_a_form_field_submits_it_implicitly():
    field = T(tag="input", type="email", name="Email", implicit_submit=True, default_button="Submit application",
              form_method="post")
    v = gate.classify_press("Enter", field)
    assert v.categories == {"submit"}
    assert "Press Enter in the “Email” field, which submits the form on boards.greenhouse.io" in v.description
    assert gate.classify_press("Control+Enter", field).categories == {"submit"}
    assert gate.classify_press("Tab", field).categories == frozenset()
    # a search box's Enter is a search
    box = T(tag="input", type="search", name="Search jobs", implicit_submit=True, default_button="Search",
            form_method="get", form_search=True)
    assert gate.classify_press("Enter", box).categories == frozenset()
    # a textarea's Enter is a newline
    assert gate.classify_press("Enter", T(tag="textarea", multiline=True, implicit_submit=False)).categories == frozenset()


def test_enter_or_space_on_a_focused_submit_button():
    btn = T(tag="button", type="submit", name="Submit", submits=True, form_method="post")
    assert gate.classify_press("Enter", btn).categories == {"submit"}
    assert gate.classify_press("Space", btn).categories == {"submit"}
    assert gate.classify_press("Enter", T(tag="a", name="Apply now")).categories == frozenset()


def test_enter_or_space_in_an_unreadable_frame_is_gated():
    # Focus is inside an iframe/frame: the probe returns found=true, note='frame', with no tag or
    # form data. An Enter/Space there could submit an embedded form, so the gate fails closed.
    frame = gate.Target(found=True, resolved=False, note="frame", page_url=PAGE)
    assert gate.classify_press("Enter", frame).categories == {"submit"}
    assert gate.classify_press("Space", frame).categories == {"submit"}
    assert "frame" in gate.classify_press("Enter", frame).description.lower()
    # a non-activation key in a frame is still nothing to gate
    assert gate.classify_press("Tab", frame).categories == frozenset()
    # and typing a newline into a focused frame is an Enter, so it's gated too
    assert gate.classify_type("hi\n", frame).categories == {"submit"}


def test_parse_probe_keeps_the_frame_note_so_an_enter_there_is_gated():
    raw = json.dumps({"ok": True, "found": True, "resolved": False, "note": "frame", "page": PAGE})
    t = gate.parse_probe(raw, "focus")
    assert t.found and t.note == "frame" and not t.tag
    assert gate.classify_press("Enter", t).categories == {"submit"}


def test_activation_keys():
    assert gate.is_activation_key("Enter") and gate.is_activation_key("Return")
    assert gate.is_activation_key("Shift+Enter") and gate.is_activation_key(" ")
    assert not gate.is_activation_key("a") and not gate.is_activation_key("Control+a")
    assert not gate.is_activation_key("+")


def test_type_with_a_newline_is_an_enter():
    field = T(tag="input", type="text", name="Name", implicit_submit=True, default_button="Submit", form_method="post")
    assert gate.classify_type("Ada\n", field).categories == {"submit"}
    assert gate.classify_type("Ada", field).categories == frozenset()


def test_eval_categories():
    assert gate.classify_eval("document.title").categories == {"eval"}
    assert gate.classify_eval("document.forms[0].requestSubmit()").categories == {"eval", "submit"}
    assert gate.classify_eval("document.querySelector('#submit-app').click()").categories == {"eval", "submit"}
    assert gate.classify_eval("document.querySelector('.menu').click()").categories == {"eval"}
    assert gate.classify_eval("document.querySelector('.apply-btn').click()").categories == {"eval"}


def test_upload_description():
    v = gate.classify_upload("Resume", "cv.pdf", PAGE)
    assert v.categories == {"upload"}
    assert v.description == "Attach cv.pdf to the “Resume” field on boards.greenhouse.io"


# ── probe plumbing ───────────────────────────────────────────────────────────────────────────


def test_probe_js_embeds_values_as_json():
    js = gate.probe_js("css", selector="a[title=\"x\"]</script>")
    assert json.dumps("a[title=\"x\"]</script>") in js and "__SEL__" not in js and "__BOX__" not in js
    js = gate.probe_js("point", box={"x": 1, "y": 2, "width": 3, "height": 4})
    assert '{"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}' in js


def test_parse_probe_and_box():
    raw = json.dumps({"ok": True, "found": True, "resolved": True, "tag": "BUTTON", "type": "submit",
                      "name": "  Submit\n application ", "submits": True,
                      "form": {"action": "https://x/a", "method": "POST", "search": False}, "page": PAGE})
    t = gate.parse_probe(json.dumps(raw), "css")  # the CLI prints an eval's string JSON-quoted
    assert t.tag == "button" and t.name == "Submit application" and t.form_method == "post" and t.via == "css"
    assert gate.parse_probe("Error: boom") is None
    assert gate.parse_box('{"success":true,"data":{"height":21,"width":153,"x":72.4,"y":47}}') == {
        "x": 72.4, "y": 47.0, "width": 153.0, "height": 21.0}
    assert gate.parse_box("Error: Unknown ref: e9") is None


# ── decisions + plugin policies ──────────────────────────────────────────────────────────────

SUBMIT_V = gate.Verdict(frozenset({"submit"}), "it submits a form", "Submit the form on x — button “Submit”")
ACTION = {"tool": "browser_click", "verb": "click", "selector": "@e7", "key": "", "page_url": PAGE, "target": None}


def test_decide_core_only():
    assert gate.decide(frozenset({"submit"}), ACTION, SUBMIT_V).kind == gate.ASK
    assert gate.decide(frozenset(), ACTION, SUBMIT_V).kind == gate.ALLOW
    assert gate.decide(frozenset({"submit"}), ACTION, gate.Verdict()).kind == gate.ALLOW


def test_plugin_policy_can_allow_ask_or_deny_and_deny_wins():
    seen = []
    remove = gate.add_policy(lambda a: seen.append(a) or {"decision": "allow", "reason": "operator approved via plugin"})
    assert gate.decide(frozenset({"submit"}), ACTION, SUBMIT_V).kind == gate.ALLOW
    assert seen[0]["verdict"]["categories"] == ["submit"] and seen[0]["gated"] == ["submit"]
    gate.add_policy(lambda a: {"decision": "deny", "reason": "Blocked by careercoach submit gate"})
    d = gate.decide(frozenset({"submit"}), ACTION, SUBMIT_V)
    assert d.kind == gate.DENY and "careercoach" in d.reason
    remove()
    gate._POLICIES.clear()
    # a plugin can ask even where the operator gates nothing
    gate.add_policy(lambda a: {"decision": "ask"} if a["verdict"]["categories"] else None)
    assert gate.policy_categories() == {"submit"}
    assert gate.decide(frozenset(), ACTION, SUBMIT_V).kind == gate.ASK


def test_a_broken_policy_is_ignored():
    def boom(_a):
        raise RuntimeError("plugin bug")
    gate.add_policy(boom)
    assert gate.decide(frozenset({"submit"}), ACTION, SUBMIT_V).kind == gate.ASK


def test_describe_target_accepts_the_dict_plugins_get():
    t = T(tag="button", name="Submit application")
    assert gate.describe_target(t.to_dict()) == "button “Submit application” on boards.greenhouse.io"
    assert gate.describe_target({"found": False}) == "nothing identifiable"


def test_register_offers_the_gate_services():
    from graph.plugins.testkit import FakeRegistry
    reg = FakeRegistry(config={"binary": "/nonexistent/agent-browser", "cli_autofetch": False}, plugin_id="agent_browser")
    _PKG.register(reg)
    assert {"agent_browser.add_gate_policy", "agent_browser.describe_target"} <= set(reg.services)


# ── approval ─────────────────────────────────────────────────────────────────────────────────


def test_approved_is_default_deny():
    for yes in (True, "approve", "approved", "Allow", {"approved": True}, {"decision": "approve"}, {"choice": "approve"}):
        assert gate.approved(yes), yes
    for no in (False, "", "deny", "decline", None, {}, {"approved": False}, "[no interactive operator available] …"):
        assert not gate.approved(no), no


def _ctx(monkeypatch, configurable):
    import langgraph.config as lgc
    monkeypatch.setattr(lgc, "get_config", lambda: {"configurable": configurable})


def test_interrupt_context(monkeypatch):
    lead = {"thread_id": "T", "checkpoint_ns": "tools:abc"}
    _ctx(monkeypatch, lead)
    assert gate.interrupt_context({"operator_chat_turn": True}) == (True, "", "T|tools:abc")
    assert gate.interrupt_context({"operator_chat_turn": False})[1] == "unattended"  # scheduled / A2A / inbox
    assert gate.interrupt_context({})[1] == "unattended"
    assert gate.interrupt_context(None)[1] == "unattended"
    assert gate.interrupt_context({"operator_chat_turn": True, "subagent_fence": ["x"]})[1] == "subagent"
    _ctx(monkeypatch, {"thread_id": "T", "checkpoint_ns": "tools:abc|tools:def"})  # a nested (subagent) graph
    assert gate.interrupt_context({"operator_chat_turn": True})[1] == "subagent"


def test_interrupt_context_outside_a_graph():
    assert gate.interrupt_context({"operator_chat_turn": True})[1] == "no-graph"


def _decision():
    return gate.Decision(gate.ASK, "it submits a form", "Submit the form on x — button “Submit”", "submit")


def test_ask_approve_deny_no_operator_and_changed_page(monkeypatch):
    import langgraph.types as lgt
    monkeypatch.setattr(gate, "interrupt_context", lambda state: (True, "", "T|tools:1"))
    cards = []
    answer = {"v": "approve"}
    monkeypatch.setattr(lgt, "interrupt", lambda card: cards.append(card) or answer["v"])
    assert gate.ask(_decision(), {}, tool="browser_click", selector="@e7", page_url=PAGE) is None
    card = cards[-1]
    assert card["kind"] == "approval" and card["session_allow"] is False
    assert card["description"] == "Submit the form on x — button “Submit”"
    assert card["title"] == "Let the agent submit this?" and "browser_click @e7" in card["detail"]
    answer["v"] = "deny"
    out = gate.ask(_decision(), {}, tool="browser_click")
    assert out.startswith("Error: the operator declined") and "Don't retry" in out
    answer["v"] = "[no interactive operator available] This turn is running autonomously …"
    assert "no operator is available" in gate.ask(_decision(), {}, tool="browser_click")
    # the card shown on the first pass is what the resume is checked against
    answer["v"] = "approve"
    gate._PENDING["T|tools:1"] = ("Submit the form on x — button “Old”", 0.0 + 1e12)
    out = gate.ask(_decision(), {}, tool="browser_click")
    assert out.startswith("Error: the page changed while the approval card was open")


def test_ask_refuses_where_nobody_can_answer(monkeypatch):
    import langgraph.types as lgt
    monkeypatch.setattr(lgt, "interrupt", lambda card: pytest.fail("must not park a card nobody can answer"))
    monkeypatch.setattr(gate, "interrupt_context", lambda state: (False, "subagent", ""))
    out = gate.ask(_decision(), {}, tool="browser_click")
    assert out.startswith("Error: blocked by the browser action gate (submit)") and "subagent" in out


# ── the tools ────────────────────────────────────────────────────────────────────────────────


class _Sink(io.BytesIO):
    def __init__(self, done):
        super().__init__()
        self._done = done

    def close(self):
        self._done.set()


class _LazyOut:
    """stdout that answers once stdin is in (an eval's script arrives after Popen returns)."""

    def __init__(self, compute):
        self._compute, self._buf = compute, None

    def read1(self, n=-1):
        if self._buf is None:
            self._buf = io.BytesIO(self._compute())
        return self._buf.read(n)

    read = read1

    def close(self):
        pass


class _Proc:
    def __init__(self, argv, responder, record):
        self.args = list(argv)[1:]
        self._stdin_done = threading.Event()
        self.stdin = _Sink(self._stdin_done)
        self.stderr = io.BytesIO(b"")
        self.returncode = None
        self.pid = None

        def compute():
            if "--stdin" in self.args:
                self._stdin_done.wait(5)
            script = self.stdin.getvalue().decode()
            record.append((self.args, script))
            out, self._rc = responder(self.args, script)
            return out.encode()
        self.stdout = _LazyOut(compute)
        self._rc = 0

    def wait(self, timeout=None):
        # The drain thread computes the answer; wait for it like a real child would.
        for _ in range(500):
            if self.stdout._buf is not None:
                break
            threading.Event().wait(0.002)
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode


def probe_answer(**target):
    data = {"ok": True, "found": True, "resolved": True, "page": PAGE, **target}
    return json.dumps(json.dumps(data))


SUBMIT_BTN = {"tag": "button", "type": "submit", "name": "Submit application", "submits": True,
              "form": {"action": PAGE, "method": "post", "search": False}}
OTHER_BTN = {"tag": "button", "type": "button", "name": "Add another", "submits": False}


def _responder(target):
    def respond(args, script):
        if args[:3] == ["--json", "get", "box"]:
            return '{"success":true,"data":{"x":10,"y":10,"width":100,"height":20}}', 0
        if args[:1] == ["eval"] and "const MODE" in script:
            return probe_answer(**target), 0
        if args[:2] == ["get", "url"]:
            return PAGE, 0
        return "✓ Done", 0
    return respond


@pytest.fixture
def cli(monkeypatch, tmp_path):
    rec = []
    state = {"target": SUBMIT_BTN}
    monkeypatch.setattr(tools.preflight, "cli_for_run", lambda *a, **k: "agent-browser")
    monkeypatch.setattr(tools.subprocess, "Popen",
                        lambda argv, **kw: _Proc(argv, lambda a, s: _responder(state["target"])(a, s), rec))
    return rec, state


def _tool(cfg, name):
    return {t.name: t for t in tools.get_browser_tools(cfg)}[name]


def _verbs(rec):
    return [args for args, _ in rec]


async def test_ungated_install_makes_no_extra_cli_calls(cli):
    rec, _ = cli
    out = await _tool({"confirm_actions": ""}, "browser_click").ainvoke({"selector": "@e7"})
    assert out == "✓ Done" and _verbs(rec) == [["click", "@e7"]]
    rec.clear()
    await _tool({"confirm_actions": ""}, "browser_press").ainvoke({"key": "Enter"})
    await _tool({"confirm_actions": ""}, "browser_eval").ainvoke({"expression": "document.forms[0].submit()"})
    assert _verbs(rec) == [["press", "Enter"], ["eval", "--stdin"]]


async def test_default_gate_costs_nothing_for_non_activating_calls(cli):
    rec, _ = cli
    await _tool({}, "browser_press").ainvoke({"key": "Tab"})
    await _tool({}, "browser_eval").ainvoke({"expression": "document.title"})
    await _tool({}, "browser_type").ainvoke({"selector": "#q", "text": "hello"})
    assert _verbs(rec) == [["press", "Tab"], ["eval", "--stdin"], ["type", "#q", "hello"]]


async def test_ref_addressed_submit_click_is_gated_and_refused_without_an_operator(cli):
    rec, _ = cli
    out = await _tool({}, "browser_click").ainvoke({"selector": "@e42"})  # default config: submit gated
    assert out.startswith("Error: blocked by the browser action gate (submit)")
    assert "nobody can approve it" in out  # a direct call carries no operator_chat_turn stamp
    assert "button “Submit application”" in out and "boards.greenhouse.io" in out
    assert ["click", "@e42"] not in _verbs(rec)  # the click never ran
    assert _verbs(rec)[:2] == [["--json", "get", "box", "@e42"], ["eval", "--stdin"]]
    assert '"point"' in rec[1][1]  # the box-centre probe


async def test_non_submit_click_runs_after_one_probe(cli):
    rec, state = cli
    state["target"] = OTHER_BTN
    out = await _tool({}, "browser_click").ainvoke({"selector": "#addmore"})
    assert out == "✓ Done"
    assert _verbs(rec) == [["eval", "--stdin"], ["click", "#addmore"]]


async def test_approved_submit_runs_and_declined_does_not(cli, monkeypatch):
    import langgraph.types as lgt
    rec, _ = cli
    monkeypatch.setattr(gate, "interrupt_context", lambda state: (True, "", "T|tools:1"))
    answers = iter(["approve", "deny"])
    cards = []
    monkeypatch.setattr(lgt, "interrupt", lambda card: cards.append(card) or next(answers))
    click = _tool({}, "browser_click")
    assert await click.ainvoke({"selector": "@e42"}) == "✓ Done"
    assert _verbs(rec)[-1] == ["click", "@e42"]
    assert cards[0]["description"].startswith("Submit the form on boards.greenhouse.io — button “Submit application”")
    rec.clear()
    out = await click.ainvoke({"selector": "@e42"})
    assert out.startswith("Error: the operator declined") and ["click", "@e42"] not in _verbs(rec)


async def test_enter_press_on_a_form_field_is_gated(cli):
    rec, state = cli
    state["target"] = {"tag": "input", "type": "email", "name": "Email", "implicit_submit": True,
                       "default_button": "Submit application", "form": {"method": "post", "action": PAGE}}
    out = await _tool({}, "browser_press").ainvoke({"key": "Enter"})
    assert out.startswith("Error: blocked by the browser action gate (submit)")
    assert "Press Enter in the “Email” field" in out
    assert ["press", "Enter"] not in _verbs(rec)


async def test_eval_category_gates_every_eval_with_the_page_host(cli):
    rec, _ = cli
    out = await _tool({"confirm_actions": "eval"}, "browser_eval").ainvoke({"expression": "document.title"})
    assert out.startswith("Error: blocked by the browser action gate (eval)") and "boards.greenhouse.io" in out
    assert _verbs(rec) == [["get", "url"]]


async def test_a_plugin_policy_can_pre_approve(cli):
    rec, _ = cli
    seen = []
    gate.add_policy(lambda a: seen.append(a) or {"decision": "allow", "reason": "approved on the plugin's card"})
    assert await _tool({}, "browser_click").ainvoke({"selector": "@e42"}) == "✓ Done"
    assert seen[0]["target"]["name"] == "Submit application" and seen[0]["selector"] == "@e42"


async def test_a_plugin_policy_resolves_even_when_the_operator_gates_nothing(cli):
    rec, _ = cli
    gate.add_policy(lambda a: {"decision": "deny", "reason": "Blocked by careercoach submit gate: no grant"}
                    if "submit" in a["verdict"]["categories"] else None)
    out = await _tool({"confirm_actions": ""}, "browser_click").ainvoke({"selector": "@e42"})
    assert out.startswith("Error: Blocked by careercoach submit gate")


async def test_a_vanished_cli_costs_one_failed_probe_not_a_cascade(monkeypatch):
    attempts = []

    def boom(argv, **kw):
        attempts.append(list(argv)[1:])
        raise FileNotFoundError()
    monkeypatch.setattr(tools.preflight, "cli_for_run", lambda *a, **k: "agent-browser")
    monkeypatch.setattr(tools.subprocess, "Popen", boom)
    out = await _tool({}, "browser_click").ainvoke({"selector": "@e9"})
    assert out.startswith("Error:")
    assert attempts == [["--json", "get", "box", "@e9"], ["click", "@e9"]]


async def test_a_ref_the_probe_cannot_line_up_falls_back_to_the_cli_text(cli, monkeypatch):
    rec, state = cli
    state["target"] = {"found": False, "note": "frame"}

    def respond(args, script):
        if args[:2] == ["get", "text"]:
            return "Submit application", 0
        if args[:2] == ["get", "attr"]:
            return "", 0
        return _responder(state["target"])(args, script)
    monkeypatch.setattr(tools.subprocess, "Popen", lambda argv, **kw: _Proc(argv, respond, rec))
    out = await _tool({}, "browser_click").ainvoke({"selector": "@e4"})
    assert out.startswith("Error: blocked by the browser action gate (submit)") and "Submit application" in out
    assert _verbs(rec) == [["--json", "get", "box", "@e4"], ["eval", "--stdin"], ["get", "text", "@e4"],
                           ["get", "attr", "@e4", "type"]]


# ── through a real graph: the interrupt parks, the resume runs the click ────────────────────


class _ScriptedModel:
    """A chat model stand-in that emits a fixed list of AIMessages and accepts bind_tools."""

    @staticmethod
    def make(messages):
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class M(GenericFakeChatModel):
            def bind_tools(self, tools, **kw):
                return self
        return M(messages=iter(messages))


def _click_call(cid="c1"):
    from langchain_core.messages import AIMessage
    return AIMessage(content="", tool_calls=[{"name": "browser_click", "args": {"selector": "@e42"}, "id": cid}])


async def test_a_real_graph_parks_on_the_card_and_the_approval_runs_the_click(cli):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from graph.state import ProtoAgentState
    rec, _ = cli
    click = _tool({}, "browser_click")
    agent = create_agent(model=_ScriptedModel.make([_click_call(), AIMessage(content="done")]), tools=[click],
                         state_schema=ProtoAgentState, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "gate-T"}}
    first = await agent.ainvoke({"messages": [{"role": "user", "content": "submit it"}], "operator_chat_turn": True}, cfg)
    card = first["__interrupt__"][0].value
    assert card["kind"] == "approval" and "Submit application" in card["description"]
    assert ["click", "@e42"] not in _verbs(rec)  # parked BEFORE the click
    await agent.ainvoke(Command(resume="approve"), cfg)
    assert _verbs(rec)[-1] == ["click", "@e42"]  # the approval ran it, once
    assert _verbs(rec).count(["click", "@e42"]) == 1


async def test_a_subagent_graph_refuses_instead_of_parking(cli):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from graph.state import ProtoAgentState
    rec, _ = cli
    click = _tool({}, "browser_click")
    got = {}

    @tool
    async def task() -> str:
        """Delegate."""
        sub = create_agent(model=_ScriptedModel.make([_click_call("c2"), AIMessage(content="sub done")]),
                           tools=[click], state_schema=ProtoAgentState)
        # Even a stamped state can't make a nested graph's interrupt answerable.
        res = await sub.ainvoke({"messages": [{"role": "user", "content": "x"}], "operator_chat_turn": True})
        got["tool"] = next(m.content for m in res["messages"] if isinstance(m, ToolMessage))
        return "ok"

    lead = create_agent(model=_ScriptedModel.make([AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "c1"}]),
                                                    AIMessage(content="fin")]),
                        tools=[task], state_schema=ProtoAgentState, checkpointer=InMemorySaver())
    out = await lead.ainvoke({"messages": [{"role": "user", "content": "go"}], "operator_chat_turn": True},
                             {"configurable": {"thread_id": "gate-S"}})
    assert "__interrupt__" not in out
    assert got["tool"].startswith("Error: blocked by the browser action gate (submit)") and "subagent" in got["tool"]
    assert ["click", "@e42"] not in _verbs(rec)
