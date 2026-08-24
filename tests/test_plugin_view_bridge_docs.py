"""Every message type the console's plugin-view bridge speaks must be documented.

`docs/reference/plugin-view-bridge.md` is hand-written — its subject is TypeScript, and the
useful part is the *contract* (direction, ordering, trust rules), not signatures a generator
could extract. That makes it exactly the kind of page that rots: someone adds a
`protoagent:something` message to the console and the reference silently stops describing
the protocol, with nothing failing.

So the page gets the same treatment as the generated ones — a test that reddens when the
code grows a message the docs don't mention. It is deliberately one-directional: it does not
demand that every documented message still exist, because the docs legitimately describe
messages the console only *receives* from plugin pages.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "reference" / "plugin-view-bridge.md"
BRIDGE_SOURCES = [
    REPO / "apps" / "web" / "src" / "app" / "PluginView.tsx",
    REPO / "apps" / "web" / "src" / "lib" / "pluginEventRelay.ts",
    REPO / "apps" / "web" / "src" / "lib" / "pluginKeybindings.ts",
    REPO / "apps" / "web" / "src" / "lib" / "pluginContextMenu.ts",
]

_MESSAGE_RE = re.compile(r'"(protoagent:[a-z:]+)"')


def _message_types() -> set[str]:
    found: set[str] = set()
    for src in BRIDGE_SOURCES:
        assert src.exists(), f"bridge source moved: {src.relative_to(REPO)} — update this test"
        found |= set(_MESSAGE_RE.findall(src.read_text(encoding="utf-8")))
    # `protoagent:theme` is also a plain window event name inside the console; it is a real
    # bridge message too, so no exclusion is needed — but keep this list honest if one appears.
    return found


def test_bridge_sources_still_speak_a_protocol() -> None:
    """Guard the guard: if the regex stops matching, every assertion below passes vacuously."""
    assert len(_message_types()) >= 10, "found almost no protoagent:* messages — the scan broke"


def test_every_bridge_message_is_documented() -> None:
    doc = DOC.read_text(encoding="utf-8")
    missing = sorted(m for m in _message_types() if m not in doc)
    assert not missing, (
        f"Undocumented plugin-view bridge messages: {missing}. "
        f"Add them to {DOC.relative_to(REPO)} — a view author has no other way to learn them."
    )


def test_messages_are_in_the_summary_table() -> None:
    """Each message needs a row in the at-a-glance table, not just a passing mention in prose —
    the table is what someone scans when they're looking for the one they need."""
    doc = DOC.read_text(encoding="utf-8")
    table = doc[doc.index("## Messages at a glance") : doc.index("## The handshake")]
    missing = sorted(m for m in _message_types() if f"`{m}`" not in table)
    assert not missing, f"Bridge messages missing from the summary table: {missing}"
