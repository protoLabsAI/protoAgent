"""Tests for the rendering-artifacts skill's presentation ladder (ADR 0118 S9).

The skill (``plugins/artifact/skills/rendering-artifacts/SKILL.md``) teaches the
four-tier ladder, the html authoring order and the interactive-answer quality bar
(ADR 0118 D7). These guard that:

- the file still LOADS — ``skill_md_problems`` is the loader's own contract, so an
  empty list is the same green the always-on skill index requires;
- the ladder, the authoring order and every quality-bar item are present, and the
  inline placement + ``send`` bridge are named, so the guidance can't silently rot;
- ``evals.md`` carries ~15 labelled prompts, each with one of the four tiers.
"""

from __future__ import annotations

import re
from pathlib import Path

from graph.skills.loader import parse_skill_md, skill_md_problems

SKILL_DIR = Path(__file__).resolve().parent.parent / "plugins" / "artifact" / "skills" / "rendering-artifacts"
SKILL_MD = SKILL_DIR / "SKILL.md"
EVALS_MD = SKILL_DIR / "evals.md"

TIERS = {"text", "component", "inline", "panel"}


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _squished(text: str) -> str:
    """Lowercased with runs of whitespace collapsed, so a phrase that the source
    wraps across a line still matches."""
    return re.sub(r"\s+", " ", text.lower())


def test_skill_md_has_no_loader_problems():
    """r1: the updated SKILL.md loads cleanly (same contract the loader enforces)."""
    text = _skill_text()
    assert skill_md_problems(text) == []
    # And it actually parses into a skill (problems == [] iff parse succeeds).
    assert parse_skill_md(SKILL_MD) is not None


def test_skill_md_mentions_inline_placement_and_send():
    """r2: the two new mechanisms this slice introduces are named verbatim."""
    text = _skill_text()
    assert 'placement="inline"' in text
    assert "protoArtifact.send" in text


def test_skill_md_teaches_the_degrade_paths():
    """r2: naming the mechanisms isn't enough — the skill must teach them SAFELY.

    Inline placement (ADR 0118 S4) and the ``send`` bridge (S10) land in sibling
    slices, so a console at this point in the epic may not offer either yet. The
    skill must therefore teach the panel as inline's universal fallback and
    require ``send`` to be feature-detected, so an answer written to the guidance
    renders (and never throws ``send is not a function``) regardless of which
    slices have merged. This asserts the guidance, not just the keyword."""
    squished = _squished(_skill_text())
    # Inline degrades to the panel — the answer is never lost.
    assert "universal fallback" in squished
    assert "the answer is never lost" in squished
    # `send` must be feature-detected before it's called.
    assert "feature-detect" in squished
    assert 'typeof window.protoartifact?.send === "function"' in squished


def test_skill_md_describes_the_four_tier_ladder():
    """r2: the ladder is present with all four tiers and the two rules of thumb."""
    text = _skill_text()
    squished = _squished(text)
    assert "presentation ladder" in squished
    assert "prefer the lowest tier that answers" in squished
    assert "inline for an answer" in squished and "panel for a work product" in squished
    # The four mechanisms, each tier's distinguishing phrase.
    assert "show_component" in text
    assert "component-v1" in text
    assert 'placement="panel"' in text  # the explicit default for the panel tier


def test_skill_md_teaches_the_html_authoring_order():
    """r2: <style> first, readable markup, scripts last."""
    squished = _squished(_skill_text())
    assert "authoring order" in squished
    style_pos = squished.find("`<style>` first")
    markup_pos = squished.find("readable markup")
    scripts_pos = squished.find("scripts last")
    assert -1 not in (style_pos, markup_pos, scripts_pos)
    # The order is taught in order.
    assert style_pos < markup_pos < scripts_pos


def test_skill_md_covers_every_quality_bar_item():
    """r2: each OIU quality-bar item is present (fail names the missing one)."""
    lower = _squished(_skill_text())
    required = {
        "quality bar section": "quality bar",
        "every control does real work": "every enabled control does real work",
        "connected label": "<label>",
        "keyboard operation": "keyboard",
        "visible focus": "focus",
        "valueAsNumber": "valueasnumber",
        "Number.isFinite": "number.isfinite",
        "domain bounds": "domain bounds",
        "no zero divisors": "zero divisors",
        "no NaN": "nan",
        "no Infinity": "infinity",
        "no stale result": "stale",
        "units shown": "unit",
        "assumptions shown": "assumption",
        "reduced motion": "prefers-reduced-motion",
        "pause control": "pause",
        "reset control": "reset",
        "provenance: sample": "sample",
        "provenance: user-provided": "user-provided",
        "provenance: retrieved": "retrieved",
        "provenance: calculated": "calculated",
        "send from a labelled button": "labelled button",
    }
    missing = [name for name, needle in required.items() if needle not in lower]
    assert not missing, f"quality-bar items missing from SKILL.md: {missing}"


def _eval_rows() -> list[tuple[str, str]]:
    """Return (prompt, tier) for each numbered row of the evals.md table."""
    rows: list[tuple[str, str]] = []
    for line in EVALS_MD.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # columns: # | Prompt | Expected tier | Why
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        rows.append((cells[1], cells[2].lower()))
    return rows


def test_evals_md_lists_about_fifteen_labelled_prompts():
    """r3: ~15 prompts, each with a valid tier label; both anchor examples present."""
    rows = _eval_rows()
    assert 12 <= len(rows) <= 20, f"expected ~15 eval prompts, found {len(rows)}"
    for prompt, tier in rows:
        assert prompt, "an eval row has an empty prompt"
        assert tier in TIERS, f"row {prompt!r} has unknown tier {tier!r}"
    # The two anchor examples the card calls out explicitly.
    joined = " ".join(f"{p}|{t}" for p, t in rows).lower()
    assert re.search(r"capital of peru.*text", joined), "missing the 'capital of Peru' → text anchor"
    assert re.search(r"\$180 bill.*inline", joined), "missing the '$180 bill' → inline anchor"
    # All four tiers are exercised at least once.
    assert {t for _, t in rows} == TIERS
