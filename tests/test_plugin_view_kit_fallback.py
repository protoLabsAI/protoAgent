"""#2392 — plugin view pages must not substitute a bearer-less fetch for the DS kit.

The token arrives THROUGH the kit's protoagent:init handshake, so when the kit
fails to import there is nothing to attach: a fallback fetch can only 401 gated
data routes — silently (docs rendered it as "no docs") or misleadingly (orgchart
surfaced "HTTP 401" and sent operators auditing tokens). The pages now fail
loudly and name the kit; docs additionally keeps 404→null as a NORMAL answer
(its cross-reference handler probes candidate paths) while any other non-ok
throws into a visible error state.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

VIEW_SOURCES = [
    REPO / "plugins" / "docs" / "__init__.py",
    REPO / "plugins" / "orgchart" / "view.py",
    # Found while fixing the issue: artifact carried the same shim (on by default).
    # The shell moved out of __init__.py in the #2817 decomposition.
    REPO / "plugins" / "artifact" / "_shell.py",
]


def test_no_bearerless_kit_fallback_anywhere():
    for src in VIEW_SOURCES:
        text = src.read_text()
        assert "apiFetch:(p,i)=>fetch" not in text.replace(" ", ""), f"{src}: bearer-less kit fallback"
        assert "plugin kit failed to load" in text.lower(), f"{src}: kit failure must be named"


def test_docs_api_distinguishes_auth_failure_from_absent_doc():
    text = (REPO / "plugins" / "docs" / "__init__.py").read_text()
    assert "r.status===404" in text  # absent doc stays null — the link handler probes paths
    assert "bearer missing or rejected" in text  # a 401 reads as auth, never as "no docs"
