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


# ── resolve "Country" to a picker labelled ONLY by aria-label / listbox (#4032 bug 2b) ──
# The live GitLab phone-country picker has NO <label for>; its only accessible name is
# aria-label="Country" on the select container (and on the listbox it controls). browser_select
# must resolve the LABEL "Country" to it. And on greenhouse.html — where an EXPLICITLY labelled
# react-select "Country *" coexists with an iti whose ul.iti__country-list carries
# aria-label="Country" — the explicit label must still win: the labelled iti keeps "Phone number"
# and never makes "Country" ambiguous.


async def test_phone_country_resolves_by_aria_label_and_commits(browser):
    """r1: the picker has no <label for>, only aria-label="Country" on its container — yet
    addressing it by the LABEL "Country" resolves it and commits United States, with no
    misleading "no options were found"."""
    await _open(browser, PHONE_COUNTRY)
    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    assert out.startswith('Selected "United States'), out
    assert "no options were found" not in out


async def test_phone_country_form_read_row_is_labelled_country(browser):
    """r2: browser_form_read lists the picker's row (id "country") labelled "Country" — the
    accessible name recovered from the container's aria-label, not its lowercase `name` — and
    after the commit its value is the country NAME "United States"."""
    await _open(browser, PHONE_COUNTRY)
    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    assert out.startswith('Selected "United States'), out

    fields = await _form_by_id(browser)
    assert "country" in fields, sorted(fields)
    assert fields["country"]["label"] == "Country"
    assert fields["country"]["value"] == "United States"


async def test_greenhouse_country_label_still_prefers_the_explicit_react_select(browser):
    """r3: on greenhouse.html the explicitly labelled react-select "Country *" coexists with an
    iti whose ul.iti__country-list carries aria-label="Country". The explicit <label for> must
    win — "Country" commits the react-select (never an ambiguous error) — and the iti keeps its
    own label "Phone number" rather than borrowing "Country" off its country-list (which would
    have made the label ambiguous)."""
    await _open(browser, GREENHOUSE)
    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    assert out.startswith('Selected "United States" in'), out

    fields = await _form(browser)
    assert fields["Country"]["kind"] == "combobox"
    assert fields["Country"]["value"] == "United States"
    # the intl-tel-input phone field keeps its own label; the listbox's aria-label="Country" did
    # NOT leak onto it (it has a <label for> of its own), so "Country" stayed unambiguous
    assert "Phone number" in fields and fields["Phone number"]["kind"] == "tel"


async def test_iti_tel_without_its_own_label_does_not_borrow_country(browser):
    """r3 (review): the country-list's aria-label="Country" names the COUNTRY PICKER, not the
    phone-number `<input type=tel>` that merely shares the `.iti` wrapper. A bare tel input (no
    `<label for>` of its own) must NOT borrow "Country" — otherwise browser_form_read reports it
    as "Country" and browser_select(field="Country") goes AMBIGUOUS next to a real "Country"
    select (or resolves to the number field). Inject such a bare iti onto greenhouse.html (whose
    real react-select "Country *" coexists) and confirm the tel row keeps its own placeholder
    label and "Country" still resolves — unambiguously — to the react-select and commits."""
    await _open(browser, GREENHOUSE)
    # A country-list labelled "Country" + a label-less tel input in one `.iti` — the shape the
    # review flagged (the existing fixture's tel input has a <label for>, so it never hit it).
    injected = await browser["browser_eval"].ainvoke({"expression":
        "(function(){var form=document.getElementById('application');"
        "var iti=document.createElement('div');iti.className='iti';"
        "var fc=document.createElement('div');fc.className='iti__flag-container';"
        "var flag=document.createElement('div');flag.className='iti__selected-flag';"
        "flag.setAttribute('role','button');"                       # role=button → not enumerated
        "var ul=document.createElement('ul');ul.className='iti__country-list';"
        "ul.setAttribute('aria-label','Country');"                  # the listbox's accessible name
        "fc.appendChild(flag);fc.appendChild(ul);"
        "var tel=document.createElement('input');tel.type='tel';tel.id='intl_phone';"
        "tel.name='intl_phone';tel.setAttribute('placeholder','Mobile');"   # NO <label for>; own name is the placeholder
        "iti.appendChild(fc);iti.appendChild(tel);form.appendChild(iti);"
        "return !!document.getElementById('intl_phone');})()"})
    assert injected == "true", injected

    by_id = await _form_by_id(browser)
    assert "intl_phone" in by_id, sorted(by_id)
    tel = by_id["intl_phone"]
    assert tel["kind"] == "tel"
    assert tel["label"] != "Country"      # the country-list's name did NOT leak onto the number field
    assert tel["label"] == "Mobile"       # it keeps its own placeholder label

    # "Country" stays unambiguous beside the real react-select: it resolves THERE and commits,
    # never an "matches 2 fields" ambiguity (the old borrow) nor the tel input.
    out = await browser["browser_select"].ainvoke({"field": "Country", "option_text": "United States"})
    assert out.startswith('Selected "United States" in'), out
    assert "matches 2 fields" not in out
    assert (await _form(browser))["Country"]["value"] == "United States"


# ── file input behind an "Attach" button, verified THROUGH a widget re-render (#4032 bug 3) ──
# Greenhouse hides the real input[type=file] behind an "Attach" button and REPLACES it on change
# (a fresh input, same id/name, empty files, plus a filename chip). browser_upload locates #resume
# by id and confirms the attach survived the re-render — via the fresh input's id/name or the
# displayed chip — instead of failing "could not be found to verify". Two fields share the label
# "Attach", so each is addressed by its id (#resume / #cover_letter).


async def test_upload_resume_reads_back_the_filename(browser):
    """r1: addressing by the label still works end to end — the attach is confirmed even though
    the widget re-renders its input, via the displayed filename chip."""
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


async def test_upload_resume_by_id_survives_the_widget_rerender(browser):
    """r1/r2: #resume is an input[type=file] hidden behind an "Attach" button; the widget REPLACES
    it on change. browser_upload resolves the id directly, uploads, and confirms the attach THROUGH
    the re-render — `Uploaded ... to ...`, never "could not be found to verify"."""
    await _open(browser, GREENHOUSE)
    root = storage.capture_root().resolve()
    f = root / "id_resume.pdf"
    f.write_bytes(b"%PDF-1.4 id resume\n%%EOF\n")
    try:
        out = await browser["browser_upload"].ainvoke({"field": "#resume", "file_path": "id_resume.pdf"})
        assert out.startswith("Uploaded id_resume.pdf to"), out
        assert "could not be found to verify" not in out
        # the widget really DID replace its input (a fresh node carries no files) yet the filename
        # stays visible as a chip — the exact shape that stranded the old nonce-only verify
        rerendered = await browser["browser_eval"].ainvoke({"expression":
            "(function(){var i=document.getElementById('resume');"
            "return (i.files.length===0) && !!document.querySelector('#resume_block .file-chip');})()"})
        assert rerendered == "true", rerendered
        assert (await _form_by_id(browser))["resume"]["value"] == "id_resume.pdf"
    finally:
        f.unlink(missing_ok=True)


async def test_upload_cover_letter_and_resume_land_on_their_own_ids(browser):
    """r3: the résumé and cover-letter fields share the label "Attach"; addressing #cover_letter
    attaches ONLY to the cover letter and #resume ONLY to the résumé — never crossed."""
    await _open(browser, GREENHOUSE)
    root = storage.capture_root().resolve()
    cv = root / "the_resume.pdf"; cv.write_bytes(b"%PDF-1.4 r\n%%EOF\n")
    cover = root / "the_cover.pdf"; cover.write_bytes(b"%PDF-1.4 c\n%%EOF\n")
    try:
        up_c = await browser["browser_upload"].ainvoke({"field": "#cover_letter", "file_path": "the_cover.pdf"})
        assert up_c.startswith("Uploaded the_cover.pdf to"), up_c
        up_r = await browser["browser_upload"].ainvoke({"field": "#resume", "file_path": "the_resume.pdf"})
        assert up_r.startswith("Uploaded the_resume.pdf to"), up_r

        by_id = await _form_by_id(browser)   # r3 + r5: each filename reads back on its own id
        assert by_id["resume"]["value"] == "the_resume.pdf"
        assert by_id["cover_letter"]["value"] == "the_cover.pdf"
    finally:
        cv.unlink(missing_ok=True); cover.unlink(missing_ok=True)


async def test_empty_file_fields_do_not_borrow_the_resume_chip(browser):
    """r5 (review): after ONLY #resume is attached, the other, still-empty file fields read back
    '' — an empty input must not cross-read a sibling field's filename chip. The résumé block even
    shares its wrapper with the manual-entry textarea, yet its own chip still reads back."""
    await _open(browser, GREENHOUSE)
    root = storage.capture_root().resolve()
    f = root / "only_resume.pdf"; f.write_bytes(b"%PDF-1.4 only\n%%EOF\n")
    try:
        up = await browser["browser_upload"].ainvoke({"field": "#resume", "file_path": "only_resume.pdf"})
        assert up.startswith("Uploaded only_resume.pdf to"), up

        by_id = await _form_by_id(browser)
        assert by_id["resume"]["value"] == "only_resume.pdf"   # the field's OWN chip reads back
        assert by_id["cover_letter"]["value"] == ""            # empty — NOT "only_resume.pdf"
        assert by_id["transcript"]["value"] == ""              # empty — NOT "only_resume.pdf"
    finally:
        f.unlink(missing_ok=True)


async def test_upload_by_the_shared_attach_label_is_ambiguous(browser):
    """r3: the two file fields share the label "Attach", so addressing by that label is an
    ambiguous error that lists them — which is WHY the id form (#resume / #cover_letter) exists."""
    await _open(browser, GREENHOUSE)
    root = storage.capture_root().resolve()
    f = root / "ambiguous.pdf"; f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    try:
        out = await browser["browser_upload"].ainvoke({"field": "Attach", "file_path": "ambiguous.pdf"})
        assert out.startswith("Error:") and "matches 2 fields" in out, out
    finally:
        f.unlink(missing_ok=True)


async def test_upload_rejecting_widget_is_a_hard_error_with_the_message(browser):
    """r4 (+ bug 3 review, finding 2): a widget that REJECTS the attach (clears the input, shows a
    `role=alert` message that NAMES the file) is a real non-attach. The fixture now WRAPS the
    role=alert in a plain `<div>` (no alert/error class) and ENDS the message with the rejected
    filename ("… could not be attached: rejected.pdf") — the shape the review flagged: an
    ancestor-only exclusion (closest) let that wrapper through as a chip because it shares the
    alert's text. Neither input.files nor a displayed filename CHIP shows the basename — the alert
    text, wrapper and all, does NOT count — so the result is an Error with the message surfaced, and
    NEVER a "verified via the displayed filename" success."""
    await _open(browser, GREENHOUSE)
    root = storage.capture_root().resolve()
    f = root / "rejected.pdf"; f.write_bytes(b"%PDF-1.4 x\n%%EOF\n")
    try:
        out = await browser["browser_upload"].ainvoke({"field": "#transcript", "file_path": "rejected.pdf"})
        assert out.startswith("Error:"), out
        assert not out.startswith("Uploaded")
        assert "verified via the field's displayed filename" not in out   # the alert is NOT a chip
        assert "nothing is attached" in out
        assert "could not be attached" in out               # the field's message is surfaced
        assert "rejected.pdf" in out                         # and it names the rejected file
        # the alert's message ENDS in the filename, yet the plain wrapper around it is not read as a
        # displayed-filename chip (both the alert and anything containing it are excluded)
        rejected = await browser["browser_eval"].ainvoke({"expression":
            "(function(){var b=document.getElementById('transcript_block');"
            "var w=b.querySelector('.transcript-status');var a=b.querySelector('[role=alert]');"
            "return !!(w && a && w.contains(a) && !w.matches('[role=alert],[class*=error]')"
            " && /rejected\\.pdf$/.test(a.textContent.trim()));})()"})
        assert rejected == "true", rejected       # the fixture really built the finding-2 shape
        assert (await _form_by_id(browser))["transcript"]["value"] == ""   # no silent attach
    finally:
        f.unlink(missing_ok=True)


async def test_form_read_ignores_incidental_dotted_text_without_a_detach_control(browser):
    """r5 (bug 3 review, finding 1): an EMPTY file field reads back '' even when its container holds
    incidental text that merely ENDS in a dotted suffix — "jobs@acme.com", "greenhouse.io",
    "Accepted: .pdf" — none of which is an attached-file chip. The extension test ALONE matched such
    text on a single-upload form (the climb never meets a second file input to stop at), so an
    unattached field reported a non-empty value and a verify-fill treated it as attached. Only a chip
    with a DETACH control (× / Remove) counts as an attached filename; plain page text never does."""
    await _open(browser, GREENHOUSE)
    # drop incidental dotted text (an email, a domain, a stray "Accepted: .pdf"), NO detach control,
    # into the still-empty cover-letter block — exactly the live-page noise the review warned about
    injected = await browser["browser_eval"].ainvoke({"expression":
        "(function(){var b=document.getElementById('cover_letter_block');"
        "['Questions? jobs@acme.com','See greenhouse.io','Accepted: .pdf'].forEach(function(s){"
        "var d=document.createElement('div');d.textContent=s;b.appendChild(d);});"
        "return String(b.querySelectorAll('div').length);})()"})
    assert injected and injected != "0", injected

    by_id = await _form_by_id(browser)
    assert by_id["cover_letter"]["value"] == ""   # incidental dotted text is NOT an attached filename
    assert by_id["resume"]["value"] == ""         # and a truly empty résumé stays empty too


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


# ── react-select's hidden required "shadow" input is folded, not emitted (#4032 bug 4) ──
# A required react-select renders a sibling `<input required tabindex="-1" aria-hidden="true">`
# NEXT TO `.select__control` (inside the container, NOT inside the control). abInCombo only skips
# nodes inside the control, so each one used to surface as an unlabelled, id-less, required "text"
# row after every combobox. form_read must fold that `required` into the combobox row and emit no
# such phantom field — while real fields (a visible unlabelled input, the hidden #resume file
# input) are still listed.


async def test_form_read_folds_the_hidden_required_shadow_into_the_combobox(browser):
    """r1/r2/r3: no unlabelled id-less "text" row survives, every combobox row carries the folded
    `required: true`, and the hidden #resume file input is still listed with kind "file"."""
    await _open(browser, GREENHOUSE)
    raw = await browser["browser_form_read"].ainvoke({"scope": ""})
    assert not raw.startswith("Error:"), raw
    rows = json.loads(raw)

    # r1: NO row with an empty label AND empty id AND kind "text" (the shadow input's shape)
    phantom = [f for f in rows if f["kind"] == "text" and not f["label"] and not f["id"]]
    assert phantom == [], phantom

    # r2: every react-select combobox row reports required == True (folded from its shadow input)
    combos = [f for f in rows if f["kind"] == "combobox"]
    assert combos, rows
    assert all(f["required"] is True for f in combos), [(f["label"], f["required"]) for f in combos]

    # r3: the hidden #resume file input is still listed, as kind "file"
    by_id = {f["id"]: f for f in rows if f.get("id")}
    assert "resume" in by_id, sorted(by_id)
    assert by_id["resume"]["kind"] == "file"


async def test_form_read_keeps_a_visible_unlabelled_text_input(browser):
    """r3: the fold must NOT over-filter — a REAL, visible, unlabelled text input (no label,
    aria-label or placeholder, full-size, not aria-hidden) is still emitted as a "text" field,
    even sitting right next to a react-select `.select__control`."""
    await _open(browser, GREENHOUSE)
    # inject a visibly-sized, label-less text input INTO a select container, beside its control —
    # exactly where a shadow input lives, but visible, so it must stay a field
    injected = await browser["browser_eval"].ainvoke({"expression":
        "(function(){var c=document.querySelector('.select__container');"
        "var i=document.createElement('input');i.type='text';i.id='visible_bare';"
        "i.style.width='200px';i.style.height='32px';c.appendChild(i);"
        "return !!document.getElementById('visible_bare');})()"})
    assert injected == "true", injected

    by_id = await _form_by_id(browser)
    assert "visible_bare" in by_id, sorted(by_id)
    assert by_id["visible_bare"]["kind"] == "text"


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
