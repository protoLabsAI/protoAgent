"""Untrusted-content boundaries around page-derived tool output.

The CLI's ``--content-boundaries`` (agent-browser 0.27.1, ``cli/src/output.rs``) wraps the
output of ``snapshot`` / ``get text`` / ``get html`` / ``eval`` in::

    --- AGENT_BROWSER_PAGE_CONTENT nonce=<32 hex, CSPRNG, per CLI process> origin=<page URL> ---
    …page output…
    --- END_AGENT_BROWSER_PAGE_CONTENT nonce=<same> ---

The flag is per INVOCATION (client-side formatting, not a daemon launch option), so the tools
pass it only on the calls whose output goes to the model. It is never set globally (the
``AGENT_BROWSER_CONTENT_BOUNDARIES`` env / config): the form tools parse ``eval`` output as
JSON, and a wrapped eval made every one of them fail as "unreadable data" (verified live).

``browser_form_read`` renders its output in Python from an internal eval, so it runs that eval
with the flag, :func:`unwrap` s the JSON for its parser, and :func:`rewrap` s the rendered
result in the CLI's own markers — same nonce, same origin, one format for the model.

Dependency-free so other plugins can strip the markers from a result they parse
(:func:`strip`).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

FLAG = "--content-boundaries"

_OPEN = re.compile(r"^--- AGENT_BROWSER_PAGE_CONTENT nonce=([0-9A-Za-z]+) origin=(.*) ---$")
_CLOSE = "--- END_AGENT_BROWSER_PAGE_CONTENT nonce={nonce} ---"


@dataclass(frozen=True)
class Marker:
    nonce: str
    origin: str


def unwrap(text: str) -> tuple[str, Marker | None]:
    """``(inner, marker)`` when ``text`` is exactly one wrapped block (the first line opens it,
    the last line closes it with the SAME nonce), else ``(text, None)``. Only the outermost
    lines are trusted: page content can't forge the closing line without the nonce."""
    lines = (text or "").strip("\n").split("\n")
    if len(lines) < 2:
        return text, None
    m = _OPEN.match(lines[0].strip())
    if not m or lines[-1].strip() != _CLOSE.format(nonce=m.group(1)):
        return text, None
    return "\n".join(lines[1:-1]), Marker(m.group(1), m.group(2))


def wrap(inner: str, marker: Marker | None = None, origin: str = "unknown") -> str:
    """Wrap ``inner`` in the markers — the given ``marker``'s nonce/origin, or a fresh nonce."""
    marker = marker or Marker(secrets.token_hex(16), origin or "unknown")
    return (f"--- AGENT_BROWSER_PAGE_CONTENT nonce={marker.nonce} origin={marker.origin} ---\n"
            f"{inner}\n{_CLOSE.format(nonce=marker.nonce)}")


def rewrap(inner: str, marker: Marker | None) -> str:
    """Wrap ``inner`` with the marker an :func:`unwrap` returned. An ``Error:`` result is the
    tool's own message, not page data, so it stays bare (the failure contract)."""
    if inner.startswith("Error:"):
        return inner
    return wrap(inner, marker)


def close_cut(text: str) -> str:
    """Close a wrapped block whose end was cut off (an output past the byte cap keeps its first
    line, the opening marker, but loses the closing one): append the closing line for the SAME
    nonce, so whatever the tool adds after it (the truncation note) sits outside the page data.
    Text that isn't an open, unclosed block comes back unchanged."""
    lines = (text or "").strip("\n").split("\n")
    m = _OPEN.match(lines[0].strip())
    if not m:
        return text
    close = _CLOSE.format(nonce=m.group(1))
    if len(lines) > 1 and lines[-1].strip() == close:
        return text
    return f"{text.rstrip(chr(10))}\n{close}"


def strip(text: str) -> str:
    """The page data inside the markers, or ``text`` unchanged when it isn't wrapped."""
    return unwrap(text)[0]
