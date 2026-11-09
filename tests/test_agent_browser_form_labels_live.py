"""Live, real-browser tests for question labels, choice groups, autocomplete and option reading
(#4954 — the Career Coach auto-apply eval on 13 real postings).

The eval found the form reader lost the QUESTION on every Lever and Workable custom question
(fields came out as ``cards[<uuid>][field0]`` / ``QA_12487827``), glued junk onto labels
("SVGs not supported by this browser.", "No location found…Loading"), counted Palantir's 31
language checkboxes as 31 required fields, and that browser_select could not pick a Lever /
Places location suggestion. These tests drive the REAL tools against modeled, self-contained
fixtures (``tests/fixtures/ats/{lever,workable,ashby_questions,places_autocomplete}.html``).

Skips unless the agent-browser CLI and Chrome are usable, like test_agent_browser_forms_live.py.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
from pathlib import Path

import pytest

from graph.plugins.testkit import load_plugin

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "plugins" / "agent_browser"
FIXTURES = REPO / "tests" / "fixtures" / "ats"
LEVER = (FIXTURES / "lever.html").as_uri()
WORKABLE = (FIXTURES / "workable.html").as_uri()
ASHBY_Q = (FIXTURES / "ashby_questions.html").as_uri()
PLACES = (FIXTURES / "places_autocomplete.html").as_uri()
GREENHOUSE = (FIXTURES / "greenhouse.html").as_uri()

_PKG = load_plugin(ROOT, "agent_browser")


def _mod(name: str):
    return importlib.import_module(f"{_PKG.__name__}.{name}")


preflight = _mod("preflight")
tools = _mod("tools")
boundaries = _mod("boundaries")

_PROBE = preflight.probe({"binary": "agent-browser"})
_UNAVAILABLE = (not _PROBE.cli_ok) or (_PROBE.chrome == "missing")
_REASON = "agent-browser CLI is not usable here" if not _PROBE.cli_ok else "no Chrome for agent-browser to drive"
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.skipif(_UNAVAILABLE, reason=_REASON)]


@pytest.fixture(scope="module")
def browser():
    """One ISOLATED session for the module — never a live agent's `default` session."""
    prev = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = f"protoagent-formlabels-{os.getpid()}"
    toolset = {t.name: t for t in tools.get_browser_tools(
        {"binary": "agent-browser", "timeout_s": 120, "main_idle_timeout_min": 3})}
    try:
        yield toolset
    finally:
        try:
            asyncio.run(toolset["browser_close"].ainvoke({}))
        except Exception:  # noqa: BLE001 — a failed close must not fail the suite
            pass
        if prev is None:
            os.environ.pop("AGENT_BROWSER_SESSION", None)
        else:
            os.environ["AGENT_BROWSER_SESSION"] = prev


async def _open(toolset, uri):
    out = await toolset["browser_open"].ainvoke({"url": uri})
    if out.startswith("Error:"):
        pytest.skip(f"no usable browser on this host: {out[:160]}")
    return out


async def _rows(toolset) -> list:
    raw = await toolset["browser_form_read"].ainvoke({"scope": ""})
    assert not raw.startswith("Error:"), raw
    return json.loads(boundaries.strip(raw))


def _by_label(rows: list) -> dict:
    return {r["label"]: r for r in rows}


_JUNK = re.compile(r"svgs not supported|no location found|loading|analyzing|start typing|"
                   r"type your response|cards\[|qa_\d|^[0-9a-f]{8}-", re.I)


# ── Lever ────────────────────────────────────────────────────────────────────────


async def test_lever_custom_questions_read_by_their_question_text(browser):
    await _open(browser, LEVER)
    rows = await _rows(browser)
    labels = [r["label"] for r in rows]
    assert not [x for x in labels if _JUNK.search(x)], labels
    by = _by_label(rows)
    # wrapping labels: the question only, `✱` stripped, the widget's status text dropped
    assert {"Resume/CV", "Full name", "Email", "Current location"} <= set(by), labels
    assert by["Current location"]["required"] is True
    # custom "cards" questions: labelled by their .application-label text
    assert by["Preferred Name | What would you like us to call you?"]["kind"] == "text"
    assert by["Why do you want to work here?"]["kind"] == "textarea"
    sel = by["Please tell us how you heard about this opportunity."]
    assert sel["kind"] == "native-select" and "LinkedIn" in sel["options"]
    auth = by["Are you legally authorized to work in the country for which you are applying?"]
    assert auth["kind"] == "radio-group" and auth["options"] == ["Yes", "No"] and auth["required"] is True


async def test_lever_checkbox_group_is_one_field(browser):
    await _open(browser, LEVER)
    rows = await _rows(browser)
    langs = [r for r in rows if r["label"] == "Language Skill(s) (Check all that apply)"]
    assert len(langs) == 1, [r["label"] for r in rows]
    g = langs[0]
    assert g["kind"] == "checkbox-group" and g["required"] is True and g["value"] == []
    assert g["options"] == ["English (ENG)", "Spanish (SPA)", "French (FRA)", "German (DEU)",
                            "Choose not to disclose"]
    assert not [r for r in rows if r["kind"] == "checkbox"]  # no stray per-box rows


async def test_lever_select_ticks_checkbox_group_options_and_never_unticks(browser):
    await _open(browser, LEVER)
    q = "Language Skill(s)"
    out = await browser["browser_select"].ainvoke({"field": q, "option_text": "English"})
    assert out.startswith('Selected "English (ENG)" in Language Skill(s)'), out
    out = await browser["browser_select"].ainvoke({"field": q, "option_text": "French (FRA)"})
    assert out.startswith('Selected "French (FRA)"'), out
    again = await browser["browser_select"].ainvoke({"field": q, "option_text": "English"})
    assert "already selected" in again, again
    g = _by_label(await _rows(browser))["Language Skill(s) (Check all that apply)"]
    assert g["value"] == ["English (ENG)", "French (FRA)"], g


async def test_lever_select_picks_a_radio_by_its_question(browser):
    await _open(browser, LEVER)
    q = "Will you now or in the future require sponsorship?"
    out = await browser["browser_select"].ainvoke({"field": q, "option_text": "No"})
    assert out.startswith('Selected "No" in Will you now'), out
    by = _by_label(await _rows(browser))
    assert by[q]["value"] == "No"
    assert by["Are you legally authorized to work in the country for which you are applying?"]["value"] == ""


async def test_lever_group_question_on_click_is_a_pointer_to_select(browser):
    await _open(browser, LEVER)
    out = await browser["browser_click"].ainvoke({"selector": "Language Skill(s)"})
    assert out.startswith("Error:") and "checkbox group" in out and "browser_select" in out, out


async def test_lever_location_autocomplete_picks_the_suggestion(browser):
    await _open(browser, LEVER)
    out = await browser["browser_select"].ainvoke({"field": "Current location", "option_text": "Portland, OR"})
    assert out.startswith('Selected "Portland, OR, USA" in Current location (autocomplete'), out
    hidden = await browser["browser_eval"].ainvoke(
        {"expression": "document.getElementById('selected-location').value"})
    assert "Portland, OR, USA" in hidden, hidden


async def test_lever_location_ambiguous_lists_suggestions_and_restores(browser):
    await _open(browser, LEVER)
    await browser["browser_fill"].ainvoke({"selector": "Current location", "text": "Somewhere"})
    out = await browser["browser_select"].ainvoke({"field": "Current location", "option_text": "Portland"})
    assert out.startswith("Error:") and "more than one suggestion" in out, out
    assert "'Portland, OR, USA'" in out and "'Portland, ME, USA'" in out and "restored" in out
    val = await browser["browser_get_value"].ainvoke({"selector": "#location-input"})
    assert val.strip() == "Somewhere", val


async def test_lever_location_alternatives_take_the_first_that_matches(browser):
    await _open(browser, LEVER)
    out = await browser["browser_select"].ainvoke(
        {"field": "Current location", "option_text": "Atlantis|London"})
    assert out.startswith('Selected "London, United Kingdom"'), out


async def test_native_select_alternatives_pick_the_decline_wording(browser):
    await _open(browser, LEVER)
    out = await browser["browser_select"].ainvoke({
        "field": "Veteran status",
        "option_text": "Decline to self-identify|I don't wish to answer|Prefer not to say"})
    assert out == 'Selected "I don\'t wish to answer" in Veteran status', out


async def test_css_selector_with_brackets_in_quotes_reaches_the_cli(browser):
    await _open(browser, LEVER)
    sel = 'input[name="cards[1c719ca9][field0]"][value="Yes"]'
    out = await browser["browser_click"].ainvoke({"selector": sel})
    assert not out.startswith("Error:"), out
    by = _by_label(await _rows(browser))
    assert by["Are you legally authorized to work in the country for which you are applying?"]["value"] == "Yes"


async def test_fill_reads_the_value_back(browser):
    await _open(browser, LEVER)
    ok = await browser["browser_fill"].ainvoke({"selector": "Full name", "text": "Testy McTestface"})
    assert not ok.startswith("Error:"), ok
    # a page that wipes the field on input: the read-back catches it
    await browser["browser_eval"].ainvoke({"expression": (
        "document.querySelector('input[name=email]').addEventListener('input', function(e){"
        " e.target.value = ''; }); 'ok'")})
    bad = await browser["browser_fill"].ainvoke({"selector": "Email", "text": "testy@example.com"})
    assert bad.startswith("Error:") and "reads ''" in bad, bad


# ── Workable ─────────────────────────────────────────────────────────────────────


async def test_workable_labels_drop_svg_text_and_name_the_radiogroup(browser):
    await _open(browser, WORKABLE)
    rows = await _rows(browser)
    labels = [r["label"] for r in rows]
    assert not [x for x in labels if _JUNK.search(x)], labels
    by = _by_label(rows)
    assert "First name" in by and "Address (Optional)" in by
    assert by["Resume"]["kind"] == "file"
    q = by["Are you eligible to work in the country you are applying?"]
    assert q["kind"] == "radio-group" and q["options"] == ["YES", "NO"] and q["required"] is True
    assert by["Notice period / availability"]["kind"] == "combobox"
    assert by["I have read the Privacy Notice and consent to the processing of my data."]["kind"] == "checkbox"
    assert "city" not in by  # the address picker's hidden helper is not a field


async def test_workable_form_read_lists_a_select_like_combobox_options_without_committing(browser):
    await _open(browser, WORKABLE)
    combo = _by_label(await _rows(browser))["Notice period / availability"]
    assert combo["options"] == ["Immediately", "2 weeks", "1 month", "More than a month"], combo
    assert combo["value"] == ""
    out = await browser["browser_select"].ainvoke({"field": "Notice period", "option_text": "2 weeks"})
    assert out.startswith('Selected "2 weeks"'), out
    assert _by_label(await _rows(browser))["Notice period / availability"]["value"] == "2 weeks"


async def test_workable_yes_no_by_question(browser):
    await _open(browser, WORKABLE)
    out = await browser["browser_select"].ainvoke(
        {"field": "Are you eligible to work", "option_text": "yes"})
    assert out.startswith('Selected "YES"'), out


# ── Ashby ────────────────────────────────────────────────────────────────────────


async def test_ashby_checkbox_group_and_question_labels(browser):
    await _open(browser, ASHBY_Q)
    rows = await _rows(browser)
    labels = [r["label"] for r in rows]
    assert not [x for x in labels if _JUNK.search(x)], labels
    by = _by_label(rows)
    g = by["How did you hear about this opportunity? (select all that apply)"]
    assert g["kind"] == "checkbox-group"
    assert g["options"] == ["LinkedIn", "Glassdoor", "Company Blog", "Conference or Meetup"]
    assert by["Gender"]["kind"] == "radio-group"          # the description is not glued on
    assert by["Location"]["kind"] == "combobox"
    assert by["Can you work from our office three days a week?"]["kind"] == "checkbox"


async def test_ashby_check_linkedin_in_how_did_you_hear(browser):
    await _open(browser, ASHBY_Q)
    out = await browser["browser_select"].ainvoke(
        {"field": "How did you hear about this opportunity?", "option_text": "LinkedIn"})
    assert out.startswith('Selected "LinkedIn"'), out
    g = _by_label(await _rows(browser))["How did you hear about this opportunity? (select all that apply)"]
    assert g["value"] == ["LinkedIn"]


async def test_ashby_async_location_combobox(browser):
    await _open(browser, ASHBY_Q)
    out = await browser["browser_select"].ainvoke({"field": "Location", "option_text": "Portland, Oregon"})
    assert out.startswith('Selected "Portland, Oregon, United States"'), out


# ── Places-style widget ──────────────────────────────────────────────────────────


async def test_places_autocomplete_picks_by_visible_text(browser):
    await _open(browser, PLACES)
    out = await browser["browser_select"].ainvoke({"field": "Home address", "option_text": "Portland, OR"})
    assert out.startswith('Selected "PortlandOR, USA" in Home address (autocomplete; the field reads '
                          '"Portland, OR, USA")'), out


async def test_plain_text_field_select_restores_and_points_at_fill(browser):
    await _open(browser, PLACES)
    out = await browser["browser_select"].ainvoke({"field": "Note", "option_text": "hello"})
    assert out.startswith("Error: no suggestions appeared in Note") and "browser_fill" in out, out
    val = await browser["browser_get_value"].ainvoke({"selector": "#note"})
    assert val.strip() in ("", "(ok)"), val   # "(ok)" is the tools' stand-in for empty output


# ── react-select options in the read; alternatives on a combobox ──────────────


async def test_greenhouse_form_read_lists_react_select_options(browser):
    await _open(browser, GREENHOUSE)
    by = _by_label(await _rows(browser))
    assert by["Are you authorized to work?"]["options"] == ["Yes, Ireland Highly Skilled Worker Visa", "Yes", "No"]
    assert by["Are you authorized to work?"]["value"] == ""   # reading never commits
    assert "options" not in by["Primary skill"]                # type-to-search: nothing to read


async def test_combobox_alternatives(browser):
    await _open(browser, GREENHOUSE)
    out = await browser["browser_select"].ainvoke(
        {"field": "How did you hear about us?", "option_text": "Facebook|Company website"})
    assert out == 'Selected "Company website" in How did you hear about us?', out


async def test_autocomplete_retries_with_the_city_when_the_full_text_suggests_nothing(browser):
    """Career Coach contract: type option_text; if NO suggestion appears, retry once with the
    first comma-separated part and commit the unique match by clicking it."""
    await _open(browser, PLACES)
    out = await browser["browser_select"].ainvoke(
        {"field": "Home address", "option_text": "Portsmouth, New Hampshire"})
    assert out.startswith('Selected "PortsmouthNH, USA" in Home address (autocomplete; the field reads '
                          '"Portsmouth, NH, USA")'), out
