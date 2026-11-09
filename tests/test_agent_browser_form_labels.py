"""Host-free tests for the #4954-eval form fixes in plugins/agent_browser (forms.py / tools.py):
quote-aware CSS classification, the `✱` required marker, checkbox-group folding, group-aware
label matching, `|` alternatives, the browser_fill read-back and the browser_select autocomplete
flow. The in-page JavaScript itself runs in tests/test_agent_browser_form_labels_live.py.
"""

from __future__ import annotations

import importlib
import io
import json

import pytest

from graph.plugins.testkit import load_plugin
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "plugins" / "agent_browser"
_PKG = load_plugin(ROOT, "agent_browser")
forms = importlib.import_module(f"{_PKG.__name__}.forms")
tools = importlib.import_module(f"{_PKG.__name__}.tools")
boundaries = importlib.import_module(f"{_PKG.__name__}.boundaries")


# ── item 4: a CSS selector with brackets inside a quoted value is CSS ───────────


@pytest.mark.parametrize("sel", [
    'input[name="cards[a69a985a][field0]"][value="Yes"]',
    "input[name='cards[x][field1]']",
    'textarea[name="cards[1dd0c1f9][field0]"]',
    'form input[name="a [b] > c"]',
    'input[value="a ] b"]',
    'li > input[name="q[1]"]',
])
def test_bracketed_quoted_attribute_selectors_are_css(sel):
    assert forms.classify(sel) == "css"


@pytest.mark.parametrize("label", ["First name", "C++ experience", "Address", "Phone [mobile]",
                                   "Why us? > tell us"])
def test_labels_stay_labels(label):
    assert forms.classify(label) == "label"


def test_required_marker_lookalikes_are_stripped():
    assert forms.normalize("Full name✱") == "full name"
    assert forms.normalize("Email ＊") == "email"
    assert forms.normalize("Phone *") == "phone"


# ── item 1: checkbox groups fold into one field ──────────────────────────────────


def _member(i, option, checked=False, required=False, kind="checkbox-group", group="checkbox:n:langs"):
    return {"label": option, "labels": [option, "Languages"], "kind": kind, "name": "cards[x][field0]",
            "id": "", "required": required, "selector": f'[data-ab-field="{i}"]', "group": group,
            "groupLabel": "Languages", "optionLabel": option, "checked": checked, "value": option}


def test_checkbox_group_folds_to_one_row_with_a_list_value():
    fields = [
        {"label": "Name", "labels": ["Name"], "kind": "text", "name": "name", "id": "", "required": True,
         "value": "", "selector": "[a]"},
        _member(1, "English", checked=True, required=True),
        _member(2, "Spanish"),
        _member(3, "French", checked=True),
    ]
    rows = json.loads(forms.render_form_read(json.dumps({"ok": True, "fields": fields})))
    assert [r["label"] for r in rows] == ["Name", "Languages"]
    g = rows[1]
    assert g == {"label": "Languages", "kind": "checkbox-group", "name": "cards[x][field0]", "id": "",
                 "required": True, "value": ["English", "French"], "options": ["English", "Spanish", "French"]}


def test_an_unticked_checkbox_group_reads_an_empty_list():
    rows = forms.collapse_radio_groups([_member(0, "A"), _member(1, "B")])
    assert rows[0]["value"] == [] and rows[0]["kind"] == "checkbox-group"


def test_radio_and_checkbox_groups_sharing_a_key_stay_apart():
    fields = [_member(0, "Yes", kind="radio-group", group="g"), _member(1, "Yes", group="g")]
    rows = forms.collapse_radio_groups(fields)
    assert [r["kind"] for r in rows] == ["radio-group", "checkbox-group"]


def test_long_option_lists_are_capped_with_a_count():
    opts = [f"University {i}" for i in range(3302)]
    rows = json.loads(forms.render_form_read(json.dumps({"ok": True, "fields": [
        {"label": "School", "labels": ["School"], "kind": "native-select", "name": "s", "id": "",
         "required": True, "value": "", "options": opts, "selector": "[s]"}]})))
    assert len(rows[0]["options"]) == forms.OPTIONS_SHOWN and rows[0]["options_more"] == 3302 - forms.OPTIONS_SHOWN
    big = [_member(i, f"Lang {i}") for i in range(70)]
    g = forms.collapse_radio_groups(big)[0]
    assert len(g["options"]) == forms.OPTIONS_SHOWN and g["options_more"] == 10


def test_group_question_resolves_for_select_and_points_click_at_select():
    fields = [_member(0, "English"), _member(1, "Spanish")]
    m = forms.match_fields("Languages", fields)
    assert not m.ok and "checkbox group" in m.error and "'English'" in m.error
    assert "browser_select(field='Languages'" in m.error
    g = forms.match_fields("Languages", fields, group_ok=True)
    assert g.ok and g.selector == '[data-ab-field="0"]' and g.kind == "checkbox-group" and g.label == "Languages"
    # an option's own label still addresses that one box
    one = forms.match_fields("Spanish", fields)
    assert one.ok and one.selector == '[data-ab-field="1"]'


def test_two_groups_sharing_a_label_stay_ambiguous_even_for_select():
    fields = [_member(0, "Yes", group="a"), _member(1, "Yes", group="b")]
    m = forms.match_fields("Languages", fields, group_ok=True)
    assert not m.ok and "matches 2 fields" in m.error


def test_the_in_page_lib_strips_svg_fallbacks_and_knows_question_containers():
    lib = forms._JS_LIB
    assert "svg" in lib.split("var AB_NONTEXT")[1].split(";")[0]
    for fn in ("function abQuestionText", "function abLabelLead", "function abGroupInfo",
               "function abIsJunk", "function abIsHiddenHelper"):
        assert fn in lib, fn
    assert "async function abChoice" in forms._SELECT_LIB
    assert "abReadComboOptions" in forms.read_form_js("")


# ── item 3: alternatives ──────────────────────────────────────────────────────────


def test_alternatives_split_on_pipes():
    assert forms.alternatives("Decline to self-identify|I don't wish to answer| Prefer not to say ") == [
        "Decline to self-identify", "I don't wish to answer", "Prefer not to say"]
    assert forms.alternatives("Yes") == ["Yes"]
    assert forms.alternatives("A||B|") == ["A", "B"]


def test_select_js_carries_a_list_only_for_alternatives():
    assert 'var WANT = ["A", "B"];' in forms.select_js("#x", "A|B")
    assert 'var WANT = "Yes";' in forms.select_js("#x", "Yes")


# ── item 5: browser_fill read-back ───────────────────────────────────────────────


@pytest.mark.parametrize("wanted,actual,ok", [
    ("Testy McTestface", "Testy McTestface", True),
    ("5550100199", "(555) 010-0199", True),
    ("150000", "150,000", True),
    ("a  b", "a b", True),
    ("testy@example.com", "", False),
    ("Testy McTestface", "Testy", False),
    ("Portland", "Portland, OR, USA", False),
    ("abc1", "abc2", False),
])
def test_fill_matches(wanted, actual, ok):
    assert forms.fill_matches(wanted, actual) is ok


class _Proc:
    def __init__(self, out=b"", rc=0):
        self.stdin = _Sink()
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(b"")
        self._rc, self.returncode, self.pid = rc, None, None

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def kill(self):
        self.returncode = -9


class _Sink(io.BytesIO):
    def close(self):
        pass


class _CLI:
    """Scripted CLI: `answer(argv, stdin)` -> stdout text; records argv + stdin."""

    def __init__(self, answer):
        self.calls, self._answer = [], answer

    def __call__(self, argv, **kw):
        p = _Proc()
        self.calls.append((list(argv[1:]), p))
        # stdin is fed after Popen returns; answer lazily from what arrives via a property
        out = self._answer(list(argv[1:]), p)
        p.stdout = io.BytesIO(out.encode() if isinstance(out, str) else out)
        return p

    @property
    def argvs(self):
        return [a for a, _ in self.calls]


def _toolset(monkeypatch, answer):
    cli = _CLI(answer)
    monkeypatch.setattr(tools.subprocess, "Popen", cli)
    return {t.name: t for t in tools.get_browser_tools({"binary": "ab", "confirm_actions": ""})}, cli


async def test_fill_mismatch_is_an_error_with_the_actual_value(monkeypatch):
    t, cli = _toolset(monkeypatch, lambda argv, p: "" if argv[:2] == ["get", "value"] else "✓ Done")
    out = await t["browser_fill"].ainvoke({"selector": "#email", "text": "testy@example.com"})
    assert out.startswith("Error: #email reads '' after filling 'testy@example.com'"), out
    assert cli.argvs == [["fill", "#email", "testy@example.com"], ["get", "value", "#email"]]


async def test_fill_keeps_the_cli_result_when_the_value_reads_back(monkeypatch):
    t, _ = _toolset(monkeypatch, lambda argv, p: "(555) 010-0199" if argv[:2] == ["get", "value"] else "✓ Done")
    assert await t["browser_fill"].ainvoke({"selector": "#phone", "text": "5550100199"}) == "✓ Done"


async def test_fill_with_an_unreadable_value_keeps_the_cli_result(monkeypatch):
    def answer(argv, p):
        if argv[:2] == ["get", "value"]:
            p._rc = 1
            return "✗ not an input"
        return "✓ Done"
    t, _ = _toolset(monkeypatch, answer)
    assert await t["browser_fill"].ainvoke({"selector": "#editor", "text": "hi"}) == "✓ Done"


# ── item 2: browser_select autocomplete flow ─────────────────────────────────────


def test_autocomplete_target_parses_both_handoffs():
    plain = json.dumps({"ok": False, "reason": "autocomplete", "input": '[data-ab-open="c1"]',
                        "value": "old", "label": "Current location"})
    assert forms.autocomplete_target(plain) == {"input": '[data-ab-open="c1"]', "value": "old",
                                                "label": "Current location"}
    empty = json.dumps({"ok": False, "reason": "no-option", "options": [], "input": "[i]", "label": "L"})
    assert forms.autocomplete_target(empty)["input"] == "[i]"
    listed = json.dumps({"ok": False, "reason": "no-option", "options": ["A"], "input": "[i]"})
    assert forms.autocomplete_target(listed) is None       # options shown: the normal no-match error
    assert forms.autocomplete_target("garbage") is None


def test_click_needed_asks_for_a_trusted_click():
    out = json.dumps({"ok": False, "reason": "click-needed", "control": "[data-ab-open=x]", "wanted": "Yes"})
    assert forms.select_reopen_target(out) == "[data-ab-open=x]"
    assert forms.render_select(out, "Q", "Yes").startswith('Error: "Yes" in Q did not tick')
    already = json.dumps({"ok": True, "already": True, "chosen": "Yes", "label": "Q"})
    assert "already selected" in forms.render_select(already, "Q", "Yes")
    assert forms.render_select(already, "Q", "Yes", fresh=True) == 'Selected "Yes" in Q'


def _ac_cli(pick: dict, verify: dict | None = None):
    """A scripted page for browser_select's autocomplete path: the resolve, the select probe
    (a plain text input), then pick/verify evals answered from their script."""
    fields = [{"label": "Current location", "labels": ["Current location"], "kind": "text",
               "selector": '[data-ab-field="0"]'}]

    def answer(argv, p):
        if argv[:2] == ["eval", "--stdin"]:
            # the script is on stdin, written after Popen returns — decide by call order instead
            n = sum(1 for a, _ in cli.calls if a[:1] == ["eval"])
            if n == 1:
                return json.dumps({"mode": "enumerate", "fields": fields})
            if n == 2:
                return json.dumps({"ok": False, "reason": "autocomplete", "input": '[data-ab-open="c1"]',
                                   "value": "Somewhere", "label": "Current location"})
            if n == 3:
                return json.dumps(pick)
            return json.dumps(verify or {"ok": True}) if n == 4 else '"ok"'
        return "✓ Done"
    cli = _CLI(answer)
    return cli


async def test_select_autocomplete_types_presses_end_clicks_and_verifies(monkeypatch):
    cli = _ac_cli({"ok": True, "option": '[data-ab-opt="n"]', "chosen": "Portland, OR, USA", "label": "Current location"},
                  {"ok": True, "value": "Portland, OR, USA"})
    monkeypatch.setattr(tools.subprocess, "Popen", cli)
    t = {x.name: x for x in tools.get_browser_tools({"binary": "ab", "confirm_actions": ""})}
    out = await t["browser_select"].ainvoke({"field": "Current location", "option_text": "Portland, OR"})
    assert out == 'Selected "Portland, OR, USA" in Current location (autocomplete; the field reads "Portland, OR, USA")'
    acts = [a for a in cli.argvs if a[:1] != ["eval"]]
    assert acts == [["fill", '[data-ab-open="c1"]', "Portland, OR"], ["press", "End"], ["click", '[data-ab-opt="n"]']]


async def test_select_autocomplete_miss_restores_the_field(monkeypatch):
    cli = _ac_cli({"ok": False, "reason": "ambiguous", "label": "Current location",
                   "options": ["Portland, OR, USA", "Portland, ME, USA"]})
    monkeypatch.setattr(tools.subprocess, "Popen", cli)
    t = {x.name: x for x in tools.get_browser_tools({"binary": "ab", "confirm_actions": ""})}
    out = await t["browser_select"].ainvoke({"field": "Current location", "option_text": "Portland"})
    assert out.startswith("Error: 'Portland' matches more than one suggestion in Current location"), out
    assert "'Portland, ME, USA'" in out and "restored to 'Somewhere'" in out
    fills = [a for a in cli.argvs if a[:1] == ["fill"]]
    assert fills == [["fill", '[data-ab-open="c1"]', "Portland"], ["fill", '[data-ab-open="c1"]', "Somewhere"]]
    assert not [a for a in cli.argvs if a[:1] == ["click"]]


def test_render_autocomplete_miss_no_suggestions_points_at_fill():
    out = forms.render_autocomplete_miss({"reason": "no-suggestions", "label": "Note"}, "Note", ["hi"], "")
    assert out.startswith("Error: no suggestions appeared in Note after typing 'hi'") and "browser_fill" in out


def test_select_docstring_stays_short_and_names_the_new_modes():
    d = tools.get_browser_tools({"binary": "ab"})
    desc = next(x for x in d if x.name == "browser_select").description
    assert len(desc) < 900
    low = desc.lower()
    assert "group" in low and "autocomplete" in low and "a|b|c" in low
