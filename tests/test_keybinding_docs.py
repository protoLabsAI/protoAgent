"""The chords the docs advertise must be the chords the console actually registers.

#2949 swapped the two: ⌘K became chat **Clear conversation** and the command palette moved
to ⌘⇧K. The console side of that swap is pinned (`apps/web/src/keybindings/coreDefaults.test.ts`
asserts both `defaultKeys`), but every *prose* claim about the chord — the ADRs, the guides,
the sidebar label, the README feature row — was pinned by nothing at all. So the docs kept
saying ⌘K, and ADR 0063's defaults table did worse than go stale: it listed the two chords
the wrong way round, teaching a reader that ⌘⇧K wipes their conversation (#3281).

This is the `test_plugin_view_bridge_docs.py` treatment for a second hand-written page set —
a test that reddens when the code moves and the prose doesn't. It reads the chord out of
`coreKeybindings.ts` and checks the places where docs bind a glyph to a name:

    "Command palette (⌘⇧K)"   ·   "⌘⇧K palette"   ·   "⌘K clear"

Deliberately narrow. It only judges a glyph written *adjacent to* the thing it names, because
that is the construction that carries a claim; a glyph in running prose ("⌘K at the time")
is left alone rather than met with a pile of exceptions. It is also one-directional: it never
demands that a page mention a chord, only that the ones it does mention are right.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE_KEYBINDINGS = REPO / "apps" / "web" / "src" / "keybindings" / "coreKeybindings.ts"
PALETTE_GUIDE = REPO / "docs" / "guides" / "command-palette.md"

# The pages that state chords. `.vitepress/dist` + `cache` are build output — gitignored, but
# present in any tree where `npm run docs:build` has run, and they carry stale copies of every
# page below.
_SKIP_DIRS = {"dist", "cache"}

_BINDING_RE = re.compile(r'id:\s*"([\w.]+)".*?defaultKeys:\s*"([^"]+)"', re.S)
# ⌘⇧K, ⌘K, ⌃Tab … — modifiers then one key token.
_GLYPH = r"[⌘⌃⌥⇧]+[A-Za-z0-9]+"
_NAMES_THE_PALETTE = re.compile(rf"({_GLYPH})\s+(?:command\s+)?palette")
_PALETTE_NAMES_A_CHORD = re.compile(rf"palette\s*\(({_GLYPH})\)")
_NAMES_THE_CLEAR = re.compile(rf"({_GLYPH})\s+clears?\b", re.I)  # "⌘K clear" and "⌘K clears the chat"

# ⌘ before the secondary modifiers — the order every chord in the docs is written in.
_MODIFIERS = [("mod", "⌘"), ("cmd", "⌘"), ("ctrl", "⌃"), ("alt", "⌥"), ("shift", "⇧")]


def _glyph(combo: str) -> str:
    """`mod+shift+k` → `⌘⇧K`, the form the docs render chords in."""
    parts = combo.lower().split("+")
    out = "".join(g for token, g in _MODIFIERS if token in parts)
    key = parts[-1]
    return out + (key.upper() if len(key) == 1 else key.capitalize())


def _core_defaults() -> dict[str, str]:
    assert CORE_KEYBINDINGS.exists(), (
        f"keybinding defaults moved: {CORE_KEYBINDINGS.relative_to(REPO)} — update this test"
    )
    src = CORE_KEYBINDINGS.read_text(encoding="utf-8")
    return {bid: keys for bid, keys in _BINDING_RE.findall(src)}


def _doc_pages() -> list[Path]:
    pages = [p for p in (REPO / "docs").rglob("*.md") if _SKIP_DIRS.isdisjoint(p.parts)]
    pages += [REPO / "README.md", REPO / "docs" / ".vitepress" / "config.mts"]
    return pages


def _claims(page: Path) -> list[tuple[int, re.Pattern[str], str]]:
    """Every (line, pattern, glyph) where the page writes a chord next to what it opens."""
    found = []
    for n, raw in enumerate(page.read_text(encoding="utf-8").split("\n"), start=1):
        # Emphasis is decoration: `⌘K`, **⌘K** and ⌘K all make the same claim.
        line = raw.replace("`", "").replace("**", "").replace("*", "").replace("_", " ")
        for pattern in (_NAMES_THE_PALETTE, _PALETTE_NAMES_A_CHORD, _NAMES_THE_CLEAR):
            for m in pattern.finditer(line):
                found.append((n, pattern, m.group(1)))
    return found


def test_the_two_swapped_bindings_are_still_parseable() -> None:
    """Guard the guard: if the scan breaks, every assertion below passes vacuously."""
    defaults = _core_defaults()
    assert defaults.get("palette.toggle"), "palette.toggle not found in coreKeybindings.ts"
    assert defaults.get("chat.clear"), "chat.clear not found in coreKeybindings.ts"
    assert _glyph(defaults["palette.toggle"]) != _glyph(defaults["chat.clear"]), (
        "the palette and clear now share a chord — the docs cannot distinguish them either"
    )
    assert any(_claims(p) for p in _doc_pages()), "no chord claims matched — the scan broke"


def test_no_page_names_the_wrong_chord() -> None:
    """The #3281 failure, in the form that lets it recur: prose that binds a glyph to a name."""
    defaults = _core_defaults()
    expected = {
        _NAMES_THE_PALETTE: _glyph(defaults["palette.toggle"]),
        _PALETTE_NAMES_A_CHORD: _glyph(defaults["palette.toggle"]),
        _NAMES_THE_CLEAR: _glyph(defaults["chat.clear"]),
    }
    wrong = [
        f"{page.relative_to(REPO)}:{line} says {glyph}, shipped is {expected[pattern]}"
        for page in _doc_pages()
        for line, pattern, glyph in _claims(page)
        if glyph != expected[pattern]
    ]
    assert not wrong, (
        "Docs advertise a chord the console doesn't register:\n  " + "\n  ".join(wrong) + "\n"
        "Either the docs are stale, or a rebind landed without them — see "
        "apps/web/src/keybindings/coreKeybindings.ts. A *historical* mention is fine, but "
        "write it so the glyph doesn't sit next to the name it no longer opens."
    )


def test_the_palette_guide_states_the_chord_both_ways() -> None:
    """The one page a user opens to learn the chord — it must carry the mac AND the
    Windows/Linux rendering, and the scan above only sees the mac glyph."""
    combo = _core_defaults()["palette.toggle"]
    pc = "-".join(p.capitalize() if p != "mod" else "Ctrl" for p in combo.split("+"))
    guide = PALETTE_GUIDE.read_text(encoding="utf-8")
    for form in (_glyph(combo), pc):
        assert form in guide, (
            f"{PALETTE_GUIDE.relative_to(REPO)} never states {form} — the palette is bound to "
            f"{combo}, so the guide is telling users the wrong keys."
        )
