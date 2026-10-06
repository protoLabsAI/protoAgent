"""Live, real-browser tests for the agent_browser FORM tools (#4032 A5).

The bd-12mo.1–.4 unit tests mock the agent-browser CLI, so the in-page JavaScript in
``plugins/agent_browser/forms.py`` — the react-select / intl-tel-input / file-input /
js-fallback drivers — never actually RUNS. This module runs it: each test drives the REAL
tools from ``get_browser_tools`` against saved, fully self-contained ATS form fixtures
(``tests/fixtures/ats/*.html``) opened over ``file://`` with no network. A vendor markup
change (or a regression in the in-page JS) therefore fails a test here instead of letting
the agent submit a wrong answer.

The whole module SKIPS unless the agent-browser CLI resolves AND Chrome is available — the
plugin's own ``preflight`` probe decides — so the default gate stays host-free and green.
``chrome == "unknown"`` (doctor couldn't tell) is not a skip; ``_open`` below skips
gracefully if the browser turns out unusable.

Run locally:  ``pytest tests/test_agent_browser_forms_live.py``
Refresh a fixture when a vendor changes markup: see the comment at the top of each HTML.
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
GREENHOUSE = (FIXTURES / "greenhouse.html").as_uri()
ASHBY = (FIXTURES / "ashby.html").as_uri()
PHONE_COUNTRY = (FIXTURES / "greenhouse_phone_country.html").as_uri()

# Load the plugin the way the host does, so its relative imports resolve (same shape as
# tests/test_agent_browser_plugin.py).
_PKG = load_plugin(ROOT, "agent_browser")


def _mod(name: str):
    return importlib.import_module(f"{_PKG.__name__}.{name}")


preflight = _mod("preflight")
tools = _mod("tools")
storage = _mod("storage")

# Probe ONCE, with the plugin's own preflight: skip the module unless the CLI is usable and
# Chrome is present (missing == a real gap; unknown == let the per-fixture open decide).
_PROBE = preflight.probe({"binary": "agent-browser"})
_UNAVAILABLE = (not _PROBE.cli_ok) or (_PROBE.chrome == "missing")
_REASON = (
    "agent-browser CLI is not usable here" if not _PROBE.cli_ok
    else "no Chrome for agent-browser to drive"
)
pytestmark = [pytest.mark.platform_sensitive, pytest.mark.skipif(_UNAVAILABLE, reason=_REASON)]


@pytest.fixture(scope="module")
def browser():
    """One ISOLATED agent-browser session for the module — never the operator's default
    session a live agent may be driving. Each test re-opens its fixture, which reloads the
    page (DOM and focus reset), so tests don't leak state into one another; one Chrome
    launch covers them all."""
    prev = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = f"protoagent-forms-{os.getpid()}"
    toolset = {t.name: t for t in tools.get_browser_tools({"binary": "agent-browser", "timeout_s": 120})}
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
    """Open a fixture; skip (don't fail) if this host has no usable browser after all."""
    out = await toolset["browser_open"].ainvoke({"url": uri})
    if out.startswith("Error:"):
        pytest.skip(f"no usable browser on this host: {out[:160]}")
    return out


async def _form(toolset, scope: str = "") -> dict:
    """``browser_form_read`` → ``{label: field}`` for easy assertions."""
    raw = await toolset["browser_form_read"].ainvoke({"scope": scope})
    assert not raw.startswith("Error:"), raw
    return {f["label"]: f for f in json.loads(raw)}


# ── react-select combobox: commit the EXACT option, never the highlighted Enter-trap ──


async def test_visa_combobox_commits_yes_not_the_ireland_option(browser):
    """#4032's headline bug: type "Yes" + Enter committed the highlighted first option
    ("Yes, Ireland Highly Skilled Worker Visa"). browser_select CLICKS the exact match and
    reads it back, so the committed value is "Yes"."""
    await _open(browser, GREENHOUSE)
    field = "Are you authorized to work?"

    out = await browser["browser_select"].ainvoke({"field": field, "option_text": "Yes"})
    assert out.startswith('Selected "Yes" in'), out
    assert "Ireland" not in out
    assert (await _form(browser))[field]["value"] == "Yes"  # read-back, not the trap option

    # A deliberately wrong option is a hard Error that LISTS the options — and the field
    # keeps its prior value (nothing is committed on a miss).
    bad = await browser["browser_select"].ainvoke({"field": field, "option_text": "Maybe someday"})
    assert bad.startswith("Error:") and "Available options" in bad
    assert "Yes, Ireland Highly Skilled Worker Visa" in bad
    assert (await _form(browser))[field]["value"] == "Yes"


async def test_country_combobox_selects_united_states(browser):
    await _open(browser, GREENHOUSE)
    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    assert out.startswith('Selected "United States" in'), out
    assert (await _form(browser))["Country"]["value"] == "United States"

    # the options list surfaced on a miss begins with Afghanistan — the live ordering
    miss = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "Nowhereland"})
    assert miss.startswith("Error:") and "Afghanistan" in miss
    assert (await _form(browser))["Country"]["value"] == "United States"  # unchanged


async def test_phone_country_then_number_both_read_back(browser):
    """intl-tel-input: set the COUNTRY first (browser_select), then fill the national
    number — the order matters because changing the country later reformats the number. Both
    read back correctly."""
    await _open(browser, GREENHOUSE)
    field = "Phone number"

    country = await browser["browser_select"].ainvoke({"field": field, "option_text": "United States"})
    assert country.startswith('Selected "United States" in'), country  # the country read-back

    filled = await browser["browser_fill"].ainvoke({"selector": field, "text": "2015550123"})
    assert not filled.startswith("Error:"), filled
    assert await browser["browser_get_value"].ainvoke({"selector": field}) == "2015550123"
    assert (await _form(browser))[field]["value"] == "2015550123"


# ── the open escalation: browser_select opens a closed react-select ITSELF (#4032 fix) ──


async def test_select_opens_a_closed_combobox_itself_without_a_prior_click(browser):
    """r1: a CLOSED react-select is opened BY browser_select — no browser_click first — then the
    matched option is clicked and the committed value reads back."""
    await _open(browser, GREENHOUSE)
    # prove it is closed before we touch it (a boolean expression returns an unquoted "true")
    closed = await browser["browser_eval"].ainvoke(
        {"expression": "document.getElementById('country_input').getAttribute('aria-expanded') === 'false'"})
    assert closed == "true", closed

    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "Canada"})
    assert out.startswith('Selected "Canada" in'), out
    assert (await _form(browser))["Country"]["value"] == "Canada"


async def test_select_uses_an_already_open_menu_without_toggling_it_closed(browser):
    """r2: when the menu is ALREADY open, browser_select picks straight from it — it must not
    dispatch a mousedown that would TOGGLE react-select closed (then fail to find options)."""
    await _open(browser, GREENHOUSE)
    # open the Country menu the way a user would (focus + ArrowDown), WITHOUT browser_select
    opened = await browser["browser_eval"].ainvoke({"expression":
        "(function(){var i=document.getElementById('country_input');"
        "i.focus();"
        "i.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));"
        "return i.getAttribute('aria-expanded')==='true';})()"})
    assert opened == "true", opened

    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    assert out.startswith('Selected "United States" in'), out
    assert (await _form(browser))["Country"]["value"] == "United States"


async def test_select_commits_a_control_that_ignores_untrusted_mousedown(browser):
    """r3: the sponsorship control IGNORES a synthetic mousedown (opens only on ArrowDown or a
    trusted click) — the live widget that never opened on a JS mousedown. browser_select still
    opens it via the escalation and commits."""
    await _open(browser, GREENHOUSE)
    field = "Will you now or in the future require immigration sponsorship?"
    out = await browser["browser_select"].ainvoke({"field": field, "option_text": "No"})
    assert out.startswith('Selected "No" in'), out
    assert (await _form(browser))[field]["value"] == "No"


async def test_select_falls_back_to_a_trusted_cli_click_to_open(browser):
    """r3: the security-clearance control opens ONLY on a real (trusted) click — a synthetic
    mousedown and ArrowDown are both ignored. browser_select escalates to the trusted CLI-click
    fallback and commits."""
    await _open(browser, GREENHOUSE)
    field = "Do you require security clearance?"
    out = await browser["browser_select"].ainvoke({"field": field, "option_text": "Yes"})
    assert out.startswith('Selected "Yes" in'), out
    assert (await _form(browser))[field]["value"] == "Yes"


async def test_select_opens_a_type_to_search_combobox_by_typing(browser):
    """A TYPE-TO-SEARCH combobox renders its listbox ONLY after input — a pointer sequence and
    ArrowDown never open it (the live sponsorship-style widgets are not the only shape; this one
    regressed when the open escalation dropped the typed-open path). browser_select types the
    filter to open it, then clicks the match."""
    await _open(browser, GREENHOUSE)
    field = "Primary skill"
    # prove it stays closed on a pointer sequence AND ArrowDown — only typing will open it
    not_opened = await browser["browser_eval"].ainvoke({"expression":
        "(function(){var i=document.getElementById('skills_input');"
        "var c=document.getElementById('skills_control');"
        "c.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true,button:0,buttons:1}));"
        "i.focus();"
        "i.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));"
        "return i.getAttribute('aria-expanded')==='false';})()"})
    assert not_opened == "true", not_opened

    out = await browser["browser_select"].ainvoke({"field": field, "option_text": "Python"})
    assert out.startswith('Selected "Python" in'), out
    assert (await _form(browser))[field]["value"] == "Python"


async def test_select_rescans_unfiltered_when_the_typed_filter_hides_the_option(browser):
    """r6: the referral control filters options by a hidden value, not the visible text, so typing
    the wanted option's text hides EVERY option; the unfiltered rescan still finds and commits it."""
    await _open(browser, GREENHOUSE)
    field = "How did you hear about us?"
    out = await browser["browser_select"].ainvoke({"field": field, "option_text": "Referral"})
    assert out.startswith('Selected "Referral" in'), out
    assert (await _form(browser))[field]["value"] == "Referral"


async def test_select_on_a_control_that_never_opens_is_an_explicit_open_error(browser):
    """r5: a control that never opens — even to a trusted click — returns an explicit 'could not
    open the dropdown' error naming aria-expanded, NOT the misleading 'no options were found'."""
    await _open(browser, GREENHOUSE)
    field = "Preferred office"
    out = await browser["browser_select"].ainvoke({"field": field, "option_text": "Remote"})
    assert out.startswith("Error: could not open the dropdown for"), out
    assert "aria-expanded stayed false" in out
    assert "no options were found" not in out
    assert (await _form(browser))[field]["value"] == ""   # nothing committed


async def test_ten_consecutive_selects_across_comboboxes_all_commit(browser):
    """r4: live, only 4 of 10 react-select questions committed (the single-mousedown open was
    flaky). With the open escalation, 10 consecutive selects across the comboboxes all commit and
    read back — the 4/10 becomes 10/10."""
    await _open(browser, GREENHOUSE)
    visa = "Are you authorized to work?"
    sponsorship = "Will you now or in the future require immigration sponsorship?"
    picks = [
        (visa, "Yes"), ("Country", "United States"), (sponsorship, "No"),
        (visa, "No"), ("Country", "Canada"), (sponsorship, "Yes"),
        (visa, "Yes"), ("Country", "Ireland"), (sponsorship, "Not sure"),
        ("Country", "United Kingdom"),
    ]
    assert len(picks) == 10
    for field, option in picks:
        out = await browser["browser_select"].ainvoke({"field": field, "option_text": option})
        assert out.startswith(f'Selected "{option}" in'), out

    fields = await _form(browser)   # the LAST committed value per control reads back
    assert fields[visa]["value"] == "Yes"
    assert fields["Country"]["value"] == "United Kingdom"
    assert fields[sponsorship]["value"] == "Not sure"


# ── phone-country picker: read back the committed country NAME, not the "+1" dial code ──
# (#4032 bug 2a) The live GitLab phone-country react-select commits the country but renders
# only a flag + "+1" as visible text, stashing the NAME in a title on a child span. The
# read-back must read that NAME; a bare "+1" (shared by US and Canada) never counts as a match.


async def _form_by_id(toolset) -> dict:
    """``browser_form_read`` → ``{id: field}`` (the phone picker has no usable label yet)."""
    raw = await toolset["browser_form_read"].ainvoke({"scope": ""})
    assert not raw.startswith("Error:"), raw
    return {f["id"]: f for f in json.loads(raw) if f.get("id")}


async def test_phone_country_reads_back_the_name_not_the_dial_code(browser):
    """r1: `#country` commits United States, whose single-value visible text is only "+1" with
    the country name in a title on a CHILD span. browser_select reads the NAME back — so the
    result is `Selected "United States…` and NOT an `Error: … reads "+1"` mismatch."""
    await _open(browser, PHONE_COUNTRY)
    out = await browser["browser_select"].ainvoke({"field": "#country", "option_text": "United States"})
    assert out.startswith('Selected "United States'), out
    assert 'reads "+1"' not in out


async def test_phone_country_accepts_the_dial_code_suffixed_option_text(browser):
    """r1: addressing the same option by its full visible text ("United States +1") also commits
    and reads back as United States (not "+1")."""
    await _open(browser, PHONE_COUNTRY)
    out = await browser["browser_select"].ainvoke({"field": "#country", "option_text": "United States +1"})
    assert out.startswith('Selected "United States'), out
    assert 'reads "+1"' not in out


async def test_phone_country_shared_dial_code_is_a_hard_mismatch(browser):
    """r2: a shared "+1" never counts as a match. Commit Canada, then force the NEXT click to
    keep committing Canada while we choose United States — the read-back is a hard mismatch
    (both are "+1"), reporting the committed country NAME, not the dial code."""
    await _open(browser, PHONE_COUNTRY)
    first = await browser["browser_select"].ainvoke({"field": "#country", "option_text": "Canada"})
    assert first.startswith('Selected "Canada'), first

    # the fixture hook: the next commit stays Canada regardless of which option is clicked
    forced = await browser["browser_eval"].ainvoke({"expression":
        "(function(){document.querySelector('.select__container')"
        ".setAttribute('data-force-commit','Canada +1');return true;})()"})
    assert forced == "true", forced

    out = await browser["browser_select"].ainvoke({"field": "#country", "option_text": "United States"})
    assert out.startswith("Error:"), out
    assert "does not match" in out
    assert '"Canada"' in out              # the mismatch names the committed country, not "+1"


async def test_phone_country_shared_prefix_options_are_not_conflated(browser):
    """r2 regression: two options that share a prefix and differ only by a parenthetical — both
    under +1 — must still be told apart. Commit "Virgin Islands (British)", force the NEXT click
    to keep committing British, then choose "Virgin Islands (U.S.)". The read-back is a hard
    mismatch: stripping the dial code must NOT also split on "(" (that collapsed both names to
    "Virgin Islands" and wrongly reported ok). The error names the committed country, parenthetical
    and all, not the shared "+1" nor a bare "Virgin Islands"."""
    await _open(browser, PHONE_COUNTRY)
    first = await browser["browser_select"].ainvoke(
        {"field": "#country", "option_text": "Virgin Islands (British)"})
    assert first.startswith('Selected "Virgin Islands (British)'), first

    forced = await browser["browser_eval"].ainvoke({"expression":
        "(function(){document.querySelector('.select__container')"
        ".setAttribute('data-force-commit','Virgin Islands (British) +1284');return true;})()"})
    assert forced == "true", forced

    out = await browser["browser_select"].ainvoke(
        {"field": "#country", "option_text": "Virgin Islands (U.S.)"})
    assert out.startswith("Error:"), out
    assert "does not match" in out
    assert '"Virgin Islands (British)"' in out     # the full name, not "+1" and not "Virgin Islands"


async def test_phone_country_form_read_reports_the_name(browser):
    """r3: after committing United States, browser_form_read's row for id "country" has value
    "United States" — the country name recovered from the child title, not the visible "+1"."""
    await _open(browser, PHONE_COUNTRY)
    out = await browser["browser_select"].ainvoke({"field": "#country", "option_text": "United States"})
    assert out.startswith('Selected "United States'), out

    fields = await _form_by_id(browser)
    assert "country" in fields, sorted(fields)
    assert fields["country"]["value"] == "United States"


# ── file input behind an "Attach" button, verified by read-back ──────────────────


async def test_upload_resume_reads_back_the_filename(browser):
    await _open(browser, GREENHOUSE)
    root = storage.capture_root().resolve()
    resume = root / "live_resume.pdf"
    resume.write_bytes(b"%PDF-1.4 live resume\n%%EOF\n")  # a file inside the upload fence
    try:
        out = await browser["browser_upload"].ainvoke({"field": "Resume/CV", "file_path": "live_resume.pdf"})
        assert out.startswith("Uploaded live_resume.pdf to Resume/CV"), out
        assert (await _form(browser))["Resume/CV"]["value"] == "live_resume.pdf"
    finally:
        resume.unlink(missing_ok=True)


# ── js-fallback click: a native click reports success but does nothing ───────────


async def test_click_js_fallback_reveals_the_enter_manually_textarea(browser):
    """The résumé "Enter manually" trigger reveals its textarea only on a JS-dispatched
    click; a native CLI click exits 0 and does nothing. Run on a FRESH page so nothing is
    focused — the CLI click then moves nothing, the js-fallback fingerprint confirms it, and
    the in-page dispatch fires."""
    await _open(browser, GREENHOUSE)
    before = await browser["browser_eval"].ainvoke(
        {"expression": "!!document.querySelector('#manual_resume').offsetParent"})
    assert before == "false"

    out = await browser["browser_click"].ainvoke({"selector": "#enter_manually", "js_fallback": True})
    assert out == "Clicked #enter_manually (JS fallback)", out

    after = await browser["browser_eval"].ainvoke(
        {"expression": "!!document.querySelector('#manual_resume').offsetParent"})
    assert after == "true"


# ── whole-form read: every field, with its committed value ───────────────────────


async def test_form_read_reports_every_field_with_its_value(browser):
    await _open(browser, GREENHOUSE)
    await browser["browser_fill"].ainvoke({"selector": "First Name", "text": "Ada"})
    await browser["browser_fill"].ainvoke({"selector": "Last Name", "text": "Lovelace"})
    await browser["browser_fill"].ainvoke({"selector": "Email", "text": "ada@example.com"})
    await browser["browser_select"].ainvoke({"field": "Are you authorized to work?", "option_text": "Yes"})
    await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    await browser["browser_select"].ainvoke({"field": "Gender", "option_text": "Female"})

    fields = await _form(browser)
    assert fields["First Name"]["value"] == "Ada"
    assert fields["Last Name"]["value"] == "Lovelace"
    assert fields["Email"]["value"] == "ada@example.com"
    assert fields["Are you authorized to work?"]["value"] == "Yes"
    assert fields["Country"]["value"] == "United States"
    gender = fields["Gender"]
    assert gender["kind"] == "native-select" and gender["value"] == "Female"
    assert "Female" in gender["options"]
    # every field on the form is reported, in one call
    assert {
        "First Name", "Last Name", "Email", "Phone number",
        "Are you authorized to work?", "Country", "Resume/CV", "Gender",
    } <= set(fields)


# ── label addressing survives a re-render (the whole point of #4032) ─────────────


async def test_label_addressing_survives_a_reload(browser):
    await _open(browser, GREENHOUSE)
    await browser["browser_fill"].ainvoke({"selector": "First Name", "text": "Ada"})
    assert await browser["browser_get_value"].ainvoke({"selector": "First Name"}) == "Ada"

    reloaded = await browser["browser_reload"].ainvoke({})
    assert not reloaded.startswith("Error:"), reloaded
    # a snapshot @ref would be stale now; the LABEL re-resolves in the fresh DOM
    refill = await browser["browser_fill"].ainvoke({"selector": "First Name", "text": "Grace"})
    assert not refill.startswith("Error:"), refill
    assert await browser["browser_get_value"].ainvoke({"selector": "First Name"}) == "Grace"


# ── Ashby: a required résumé file + a radio group ────────────────────────────────


async def test_ashby_required_resume_upload_and_radio_group(browser):
    await _open(browser, ASHBY)
    await browser["browser_fill"].ainvoke({"selector": "Full name", "text": "Ada Lovelace"})
    await browser["browser_fill"].ainvoke({"selector": "Email", "text": "ada@example.com"})

    root = storage.capture_root().resolve()
    cv = root / "ashby_cv.pdf"
    cv.write_bytes(b"%PDF-1.4 cv\n%%EOF\n")
    try:
        up = await browser["browser_upload"].ainvoke({"field": "Resume", "file_path": "ashby_cv.pdf"})
        assert up.startswith("Uploaded ashby_cv.pdf to Resume"), up
    finally:
        cv.unlink(missing_ok=True)

    clicked = await browser["browser_click"].ainvoke({"selector": "Yes"})
    assert not clicked.startswith("Error:"), clicked
    # addressing the group by its legend hits BOTH radios — an ambiguous error, never a guess
    legend = "Are you legally authorized to work in the United States?"
    amb = await browser["browser_click"].ainvoke({"selector": legend})
    assert amb.startswith("Error:") and "matches 2 fields" in amb

    fields = await _form(browser)
    assert fields["Full name"]["value"] == "Ada Lovelace"
    assert fields["Email"]["value"] == "ada@example.com"
    resume = fields["Resume"]
    assert resume["kind"] == "file" and resume["required"] and resume["value"] == "ashby_cv.pdf"
    group = fields[legend]
    assert group["kind"] == "radio-group" and group["value"] == "Yes"
    assert group["options"] == ["Yes", "No"]


# ── the fixtures stay OFFLINE (so a live run truly needs no network) ─────────────


def test_fixtures_are_self_contained_with_no_network_resources():
    comment = re.compile(r"<!--.*?-->", re.S)
    for name in ("greenhouse.html", "ashby.html", "greenhouse_phone_country.html"):
        body = comment.sub("", (FIXTURES / name).read_text(encoding="utf-8")).lower()
        for marker in ("http://", "https://", "<script src", "<link ", "<img ", "url("):
            assert marker not in body, f"{name} references an external/network resource: {marker!r}"
