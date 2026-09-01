"""The chords and commands the docs advertise must be the ones the console registers.

#2949 swapped two chords: ⌘K became chat **Clear conversation** and the command palette moved
to ⌘⇧K. The console side of that swap is pinned (`apps/web/src/keybindings/coreDefaults.test.ts`
asserts both `defaultKeys`), but every *prose* claim about the chord — the ADRs, the guides,
the sidebar label, the README feature row — was pinned by nothing at all. So the docs kept
saying ⌘K, and ADR 0063's defaults table did worse than go stale: it listed the two chords
the wrong way round, teaching a reader that ⌘⇧K wipes their conversation (#3281).

This is the `test_plugin_view_bridge_docs.py` treatment for a second hand-written page set —
a test that reddens when the console moves and the prose doesn't. Three checks, because a
docs claim rots in three different ways:

1. **A page names the wrong chord** — every construction that binds a glyph to the thing it
   opens is re-derived from `coreKeybindings.ts`. Not just adjacency ("⌘⇧K palette"): the
   dominant instructional forms put words in between ("**⌘⇧K** / **Ctrl-Shift-K** for the
   command palette", "so ⌘⇧K still opens the palette"), and the first draft of this test
   read none of them — three shipped guides could go stale under a green gate.
2. **A page that teaches the chord stops stating it** — the scan above is one-directional
   (it only judges glyphs a page *does* write), so a rebind that quietly deletes the chord
   from the console guide would pass it. `_MUST_STATE_THE_CHORD` pins those pages positively.
3. **A page tells you to run a command that no longer exists** — "press ⌘⇧K → Toggle Fleet
   Agent" survived in `docs/guides/fleet.md` long after #1769 folded that command into the
   Fleet Room, and a chord-only check can never see it. Command *names* are re-derived from
   the palette adapter (`apps/web/src/app/palette/`) too.

Two deliberate limits, so the gate stays a tripwire and not a pile of exceptions:

* **A glyph only counts as a claim when it is joined to the name** — by adjacency or by a
  short connective ("opens", "for", "→"), inside one sentence. A glyph in running prose
  ("⌘K at the time — it is ⌘⇧K now") is left alone.
* **Only the console's own chords are judged.** The docs legitimately name OS-global chords
  the desktop shell owns — the ⌥Space quick launcher *is* this same palette — so those are
  read out of `apps/desktop/src-tauri/src/lib.rs` and skipped. Without that, CI would tell an
  author to "correct" a true sentence about ⌥Space into the in-app chord.
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE_KEYBINDINGS = REPO / "apps" / "web" / "src" / "keybindings" / "coreKeybindings.ts"
# The adapter that registers the root commands. `app/usePaletteRegistry.ts` is a four-line
# re-export barrel since #3289 split the adapter into `app/palette/*`, so scanning THAT file
# for `label:` yields nothing — which is precisely how a docs gate goes vacuum-green. The
# whole directory is read instead, and `test_the_two_swapped_bindings_are_still_parseable`
# asserts a known label comes back out of it.
PALETTE_ADAPTER = REPO / "apps" / "web" / "src" / "app" / "palette"
DESKTOP_SHELL = REPO / "apps" / "desktop" / "src-tauri" / "src" / "lib.rs"
PALETTE_GUIDE = REPO / "docs" / "guides" / "command-palette.md"

# The pages that state chords. `.vitepress/dist` + `cache` are build output — gitignored, but
# present in any tree where `npm run docs:build` has run, and they carry stale copies of every
# page below.
_SKIP_DIRS = {"dist", "cache"}

# Pages whose *job* includes telling a reader which keys open the palette: the guide itself,
# the console guide a new operator opens first, the feature row, the sidebar/index labels, and
# the two pages that instruct a user or a plugin author to press it. If a page here genuinely
# stops needing to name the chord, drop it from this list in the same commit.
_MUST_STATE_THE_CHORD = (
    "docs/guides/command-palette.md",
    "docs/guides/react-tauri-ui.md",
    "docs/guides/fleet.md",
    "docs/guides/building-react-plugin-views.md",
    "docs/guides/index.md",
    "docs/.vitepress/config.mts",
    "README.md",
)

_BINDING_RE = re.compile(r'id:\s*"([\w.]+)".*?defaultKeys:\s*"([^"]+)"', re.S)
# The desktop shell's OS-global hotkeys: `(HOTKEY_CONSOLE, "super+shift+p".to_string())` and
# the `let launcher = if cfg!(…) { "alt+space" } else { "ctrl+alt+space" }` pair.
_SHELL_CHORD_RE = re.compile(r'"((?:super|shift|alt|ctrl)(?:\+[a-z]+)+)"')

# ⌘⇧K, ⌘K, ⌥Space, ⌃Tab … — modifiers then one key token.
_GLYPH = r"[⌘⌃⌥⇧]+[A-Za-z0-9]+"
# Filler inside ONE sentence: may wrap across a single line break, never across a sentence
# end or a blank line (so a glyph can't reach into the next paragraph for a noun).
_SOFT = r"(?:[^.;\n]|\n(?!\s*\n))"
_GAP = r"(?:[ \t]|\n(?!\s*\n))+"
# The noun. `usePaletteHotkey` / `CommandPalette` are identifiers, not claims (the lookbehind
# drops them); `palette.toggle` is a binding id, not the thing a chord opens.
_PALETTE = r"(?<![A-Za-z])(?:command\s+)?palette\b(?!\.)"
_OPENS = r"(?:\b(?:opens?|toggles?|summons?|is|for|brings\s+up)\b|→|->)"

# Claims that START at a glyph — tested at every glyph in the file, so a line that names two
# chords ("⌘⇧K to clear the conversation, ⌘K for the palette") has BOTH judged.
_FORWARD_CLAIMS = (
    # "⌘⇧K palette" · "a ⌘⇧K command palette"
    ("palette.toggle", re.compile(rf"({_GLYPH}){_GAP}{_PALETTE}", re.I)),
    # "⌘⇧K / Ctrl-Shift-K for the command palette" · "so ⌘⇧K still opens the palette"
    ("palette.toggle", re.compile(rf"({_GLYPH}){_SOFT}{{0,34}}?{_OPENS}{_SOFT}{{0,18}}?{_PALETTE}", re.I)),
    # "⌘T new, ⌘K clear, ⌃Tab prev" — the terse defaults table. Bare "clear" only: "⌘F clears
    # the query box" is a different verb about a different thing.
    ("chat.clear", re.compile(rf"({_GLYPH}){_GAP}clear\b", re.I)),
    # "⌘K clears the conversation" · "⌘⇧K wipes your chat"
    (
        "chat.clear",
        re.compile(
            rf"({_GLYPH}){_SOFT}{{0,32}}?\b(?:clears?|wipes?|resets?)\s+"
            rf"(?:(?:the|your|this|their)\s+)?(?:conversation|chat|thread|transcript)\b",
            re.I,
        ),
    ),
    # "plain ⌘K is Clear conversation" — the binding's own label.
    ("chat.clear", re.compile(rf"({_GLYPH}){_SOFT}{{0,14}}?Clear conversation", re.I)),
)
# Claims that START at the name: "Command palette (⌘⇧K)", "command palette — ⌘⇧K".
_REVERSE_CLAIMS = (
    ("palette.toggle", re.compile(rf"{_PALETTE}[\s(—–:-]{{1,4}}({_GLYPH})", re.I)),
    ("chat.clear", re.compile(rf"Clear conversation[\s(—–:-]{{1,4}}({_GLYPH})", re.I)),
)

# "press ⌘⇧K → Fleet Room" — a chord plus the command it lands on.
_INVOKES = re.compile(rf"{_GLYPH}\s*(?:→|->|▸)\s*([A-Z][\w’'-]*(?:\s+[A-Z][\w’'-]*)*)")
_LABEL_RE = re.compile(r'label:\s*"([^"]+)"')
_LINK_RE = re.compile(r'_link\(\s*"[^"]*",\s*"([^"]+)"')
_TEMPLATE_LABEL_RE = re.compile(r"label:\s*`([^`]*)`")

# ⌘ before the secondary modifiers — the order every chord in the docs is written in.
_MODIFIERS = [("mod", "⌘"), ("cmd", "⌘"), ("super", "⌘"), ("ctrl", "⌃"), ("alt", "⌥"), ("shift", "⇧")]


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


def _shell_chords() -> set[str]:
    """The OS-global chords the desktop shell owns (⌥Space quick launcher, ⌘⇧P console).

    The docs name these next to the word "palette" on purpose — the launcher *is* the palette,
    in its own window — so they must never be judged against the in-app binding."""
    assert DESKTOP_SHELL.exists(), (
        f"desktop shell moved: {DESKTOP_SHELL.relative_to(REPO)} — update this test"
    )
    combos = set(_SHELL_CHORD_RE.findall(DESKTOP_SHELL.read_text(encoding="utf-8")))
    assert len(combos) >= 2, (
        "default_hotkeys() no longer parses — the shell chords would start being judged as "
        "in-app ones, and CI would tell authors to 'fix' correct ⌥Space prose"
    )
    return {_glyph(c) for c in combos}


def _palette_labels() -> tuple[set[str], tuple[str, ...]]:
    """(exact labels, label prefixes) the palette registers as ROOT commands."""
    assert PALETTE_ADAPTER.is_dir(), (
        f"palette adapter moved: {PALETTE_ADAPTER.relative_to(REPO)} — update this test"
    )
    sources = [p for p in sorted(PALETTE_ADAPTER.glob("*.ts*")) if ".test." not in p.name]
    src = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    labels = set(_LABEL_RE.findall(src)) | set(_LINK_RE.findall(src))
    # `label: \`Chat with ${chat.name}\`` — only the literal head is checkable.
    prefixes = tuple(t.split("${")[0].strip() for t in _TEMPLATE_LABEL_RE.findall(src) if "${" in t)
    return labels, prefixes


def _doc_pages() -> list[Path]:
    pages = [p for p in (REPO / "docs").rglob("*.md") if _SKIP_DIRS.isdisjoint(p.parts)]
    pages += [REPO / "README.md", REPO / "PROTO.md", REPO / "docs" / ".vitepress" / "config.mts"]
    return pages


def _normalized(page: Path) -> tuple[str, list[int]]:
    """The page with markdown emphasis stripped, plus each line's start offset.

    Whole-file, not line-by-line: the claim this test exists to catch wraps across a line
    ("Press **⌘⇧K** /\\n**Ctrl-Shift-K** for the [command palette]")."""
    lines = [
        raw.replace("`", "").replace("**", "").replace("*", "").replace("_", " ")
        for raw in page.read_text(encoding="utf-8").split("\n")
    ]
    starts, off = [], 0
    for line in lines:
        starts.append(off)
        off += len(line) + 1
    return "\n".join(lines), starts


def _claims(page: Path) -> list[tuple[int, str, str]]:
    """Every (line, binding id, glyph) where the page joins a chord to what it opens."""
    text, starts = _normalized(page)
    found: set[tuple[int, str, str]] = set()

    def line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos)

    for g in re.finditer(_GLYPH, text):
        for binding, pattern in _FORWARD_CLAIMS:
            m = pattern.match(text, g.start())
            if m:
                found.add((line_of(g.start()), binding, m.group(1)))
    for binding, pattern in _REVERSE_CLAIMS:
        for m in pattern.finditer(text):
            found.add((line_of(m.start(1)), binding, m.group(1)))
    return sorted(found)


def test_the_two_swapped_bindings_are_still_parseable() -> None:
    """Guard the guard: if a scan breaks, every assertion below passes vacuously."""
    defaults = _core_defaults()
    assert defaults.get("palette.toggle"), "palette.toggle not found in coreKeybindings.ts"
    assert defaults.get("chat.clear"), "chat.clear not found in coreKeybindings.ts"
    assert _glyph(defaults["palette.toggle"]) != _glyph(defaults["chat.clear"]), (
        "the palette and clear now share a chord — the docs cannot distinguish them either"
    )
    assert any(_claims(p) for p in _doc_pages()), "no chord claims matched — the scan broke"
    labels, _ = _palette_labels()
    assert "Fleet Room" in labels, (
        "palette command labels no longer parse out of apps/web/src/app/palette/ — "
        "test_no_page_invokes_a_command_the_palette_dropped would pass vacuously"
    )


def test_no_page_names_the_wrong_chord() -> None:
    """The #3281 failure, in the form that lets it recur: prose that binds a glyph to a name."""
    defaults = _core_defaults()
    shell = _shell_chords()
    expected = {
        "palette.toggle": _glyph(defaults["palette.toggle"]),
        "chat.clear": _glyph(defaults["chat.clear"]),
    }
    wrong = [
        f"{page.relative_to(REPO)}:{line} says {glyph}, shipped is {expected[binding]}"
        for page in _doc_pages()
        for line, binding, glyph in _claims(page)
        if glyph not in shell and glyph != expected[binding]
    ]
    assert not wrong, (
        "Docs advertise a chord the console doesn't register:\n  " + "\n  ".join(wrong) + "\n"
        "Either the docs are stale, or a rebind landed without them — see "
        "apps/web/src/keybindings/coreKeybindings.ts. A *historical* mention is fine, but "
        "write it so the glyph doesn't sit next to the name it no longer opens."
    )


def test_every_page_that_teaches_the_palette_states_the_shipped_chord() -> None:
    """The scan above only judges chords a page *writes*, so a rebind that deleted the chord
    from the console guide would sail through it. These pages must carry the shipped one."""
    combo = _core_defaults()["palette.toggle"]
    mac = _glyph(combo)
    silent = [rel for rel in _MUST_STATE_THE_CHORD if mac not in (REPO / rel).read_text(encoding="utf-8")]
    assert not silent, (
        f"the palette is bound to {combo} ({mac}), and these pages no longer say so:\n  "
        + "\n  ".join(silent)
        + "\n(If a page genuinely stopped needing to name the chord, drop it from "
        "_MUST_STATE_THE_CHORD in the same commit.)"
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


def test_no_page_invokes_a_command_the_palette_dropped() -> None:
    """A right chord in front of a dead command reads as freshly verified and isn't.

    #1769 folded **Toggle Fleet Agent** into the Fleet Room; `docs/guides/fleet.md` kept
    telling operators to press the chord and type it, so the instruction was impossible in a
    sentence whose glyph had just been corrected."""
    labels, prefixes = _palette_labels()
    dead = []
    for page in _doc_pages():
        text, starts = _normalized(page)
        for m in _INVOKES.finditer(text):
            name = m.group(1).strip()
            known = (
                name in labels
                or any(label.startswith(name) for label in labels)
                or any(name.startswith(p) for p in prefixes)
            )
            if not known:
                dead.append(f"{page.relative_to(REPO)}:{bisect.bisect_right(starts, m.start())} → {name!r}")
    assert not dead, (
        "Docs tell a user to run a palette command that isn't registered:\n  "
        + "\n  ".join(dead)
        + "\nThe root commands live in apps/web/src/app/palette/ — registered: "
        + ", ".join(sorted(labels))
        + ". (Surfaces are behind `Open…`, so name that, not the surface.)"
    )
