"""Single source of truth for ``@<name>`` mention resolution (roster + parsing).

The twin of ``graph.slash_commands``, for the other addressing sigil. ``/`` picks a
*command*; ``@`` picks a *participant* — one named delegate the operator is addressing
directly, short-circuiting the lead agent's routing judgment (#3042).

The dispatcher (``server.chat``) and the console composer both need to agree on what an
``@<token>`` reaches. ``slash_commands`` exists because encoding that twice is how a
shipped command became silently unreachable; the same reasoning applies here, and more
sharply — a composer that autocompletes a name the dispatcher won't route sends the
operator's message to the wrong participant.

It lives in ``graph/`` for the same reason its twin does: ``operator_api`` must not
import ``server`` (import-linter contract), so shared logic can't live in ``server.chat``.
Both layers may import ``graph``. Depends only on ``runtime.state``.

**One roster, not a second namespace.** ``@`` resolves against the delegate registry —
the same roster ``delegate_to`` dispatches into, which already carries fleet-shared
entries (#2987). A fleet member reachable as a delegate is addressable; one that isn't is
not, and that is the same answer the agent gets from ``delegate_to``.
"""

from __future__ import annotations

import logging
import re

from runtime.state import STATE

log = logging.getLogger("protoagent.server")

# A leading ``@`` token: the sigil, then a run of non-whitespace, then optionally a
# message. Syntactic only — an unknown ``@name`` is still recognized as an ATTEMPTED
# mention so the dispatcher can answer with the roster rather than run the raw text as a
# prompt (#3043).
_AT_TOKEN_RE = re.compile(r"@(\S+)(?:\s+(.*))?\Z", re.DOTALL)


def parse_mention(text: str) -> tuple[str, str] | None:
    """``(token, rest)`` when ``text``'s first non-whitespace run is an ``@``-prefixed
    token, else ``None``. ``rest`` is stripped; a bare ``@name`` yields ``("name", "")``.

    Only a LEADING mention addresses anyone: ``ask @proto about @claude-code's patch`` is
    one message to nobody in particular, not a fan-out. Callers rely on this for the
    composer popover too — offering a completion on a non-leading ``@`` would suggest a
    target the message never reaches.
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("@"):
        return None
    m = _AT_TOKEN_RE.match(stripped)
    if m is None:
        return None
    return (m.group(1), (m.group(2) or "").strip())


def _registry():
    """The live delegate registry, or ``None`` when nothing is addressable."""
    return getattr(STATE, "delegate_registry", None)


def mention_target(token: str) -> str | None:
    """The delegate an ``@<token>`` addresses — its REGISTERED name — or ``None``.

    Case-insensitive, because ``dispatch``/``get`` are case-sensitive and ``@Proto``
    plainly means ``proto``. An *ambiguous* fold (two delegates differing only in case)
    resolves to ``None`` rather than picking one: silently addressing the wrong agent is
    worse than telling the operator the name didn't resolve.
    """
    reg = _registry()
    if reg is None or not token:
        return None
    try:
        names = list(reg.names())
    except Exception:  # noqa: BLE001 — never break a turn over roster introspection
        log.exception("[mentions] reading the delegate roster failed")
        return None
    if token in names:
        return token
    folded = [n for n in names if n.casefold() == token.casefold()]
    return folded[0] if len(folded) == 1 else None


def resolve_mentions() -> list[dict]:
    """The addressable-participant inventory for the console composer — one entry per
    delegate, resolution applied via ``mention_target`` (so the composer can't offer a
    token the dispatcher won't route). Empty when nothing is addressable.
    """
    reg = _registry()
    if reg is None:
        return []
    try:
        roster = reg.roster()
    except Exception:  # noqa: BLE001 — an unavailable roster is an empty one, not a 500
        log.exception("[mentions] building the mention roster failed")
        return []
    out: list[dict] = []
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        # Apply the resolution the docstring promises rather than assuming it: an entry
        # whose name does not round-trip through `mention_target` is one the dispatcher
        # would refuse (an ambiguous case-fold, or a roster/name mismatch), and offering
        # it in the composer is how the operator's message reaches the wrong participant.
        if not name or mention_target(name) != name:
            continue
        out.append(
            {
                "name": name,
                "kind": str(entry.get("type") or "delegate"),
                "description": str(entry.get("description") or ""),
                "usage": f"@{name} …",
            }
        )
    return out
