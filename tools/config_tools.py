"""Read-only self-config introspection for the agent (#2540).

An agent could not read its own effective configuration. Its filesystem tools reach
only managed projects, and its own ``config/langgraph-config.yaml`` lives outside all
of them — so the one file that says how the agent is wired was the one file it could
not open.

The cost is not hypothetical. protoEngineer's board was bound to the wrong repo
(``project_board.repo`` left pointing elsewhere after a temporary repoint) and every
mark-ready failed a path-existence gate. The agent spent two sessions on it: verified
the paths on disk and on GitHub, read the *default* plugin config from source, bisected
its glob tools, and concluded — wrongly — that the board pointed at a near-empty
worktree. Its operator found the answer in one grep of a file the agent couldn't see.

``GET /api/config`` already serves this merged view; the seam existed, and this exposes
it at the tool layer.

REDACTION IS NOT INHERITED. ``config_to_dict`` blanks secret-typed schema fields and
plugin-declared secrets, which is the right bar for the token-gated operator API. It is
NOT the right bar here, because the destination is different: this output lands in a
model's context and in the chat transcript, and the model can be steered by content it
reads. Two config areas carry credentials that ``config_to_dict`` emits verbatim —
``mcp.servers[].env`` and ``mcp.servers[].headers`` are free-form string maps that
routinely hold API keys and bearer tokens. So this module applies its OWN pass on top,
by shape (whole env/header maps) and by key name, and fails closed on anything new.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import tool

log = logging.getLogger("protoagent.tools.config")

_MAX_CHARS = 12_000
_REDACTED = "«redacted»"

# Free-form string maps whose VALUES are credentials often enough that the whole map is
# masked — an MCP server's env is where a token lives when it isn't a schema field.
_OPAQUE_MAPS = ("env", "headers")

# Substrings in a key name that mark its value as sensitive regardless of where it sits.
# Matched on the leaf key, case-insensitively.
_SENSITIVE_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "_key",
    "credential",
    "authorization",
    "auth_header",
    "cookie",
    "dsn",
    "private",
    "signature",
)


def _sensitive(key: str) -> bool:
    k = str(key).strip().lower()
    return k == "key" or any(h in k for h in _SENSITIVE_HINTS)


def redact_for_agent(value: Any, *, key: str = "") -> Any:
    """Mask credentials anywhere in a config document, for a MODEL-facing destination.

    Masks rather than drops: an agent diagnosing its own wiring needs to know that a
    header is set — just not what it says. Recurses through every dict and list, so a
    plugin section added later is covered by shape without anyone remembering to come
    back here."""
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _sensitive(k) and not isinstance(v, (dict, list)):
                out[k] = _REDACTED if v not in (None, "", [], {}) else v
            elif str(k).strip().lower() in _OPAQUE_MAPS and isinstance(v, dict):
                # Every value, not just the sensibly-named ones: these maps are
                # free-form, and "FOO_BAR" is as likely to be a token as "API_KEY".
                out[k] = {ik: (_REDACTED if iv not in (None, "") else iv) for ik, iv in v.items()}
            else:
                out[k] = redact_for_agent(v, key=str(k))
        return out
    if isinstance(value, list):
        return [redact_for_agent(v, key=key) for v in value]
    return value


def _dump(doc: Any) -> str:
    text = json.dumps(doc, indent=2, default=str, sort_keys=True)
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + f"\n… (truncated at {_MAX_CHARS} chars — ask for one section instead)"
    return text


def build_config_tools(config) -> list:
    """Bind ``show_config`` against the LIVE config object.

    Takes the config rather than reading global state so the tool reports the same
    object the rest of the graph was built from — the whole point is that it can be
    trusted to answer "how am I actually wired right now"."""

    @tool
    def show_config(section: str = "") -> str:
        """Read your own effective configuration — the merged, live settings this agent
        is actually running with, including plugin sections.

        Use this when behaviour doesn't match what you expect and you suspect the
        WIRING rather than the code: which repo a board is bound to, which model or
        gateway is in use, which plugins are enabled, which folders your filesystem
        tools can reach. Reading a plugin's defaults from its source tells you what it
        would do unconfigured — this tells you what YOUR instance actually set.

        - ``section``: a top-level key (e.g. "project_board", "filesystem", "mcp",
          "model"). Omit it to see the whole config, or to list the sections when it's
          too large to show at once.

        Read-only — this never changes anything. Secrets are masked as «redacted»;
        seeing that marker means the value IS set, just not readable here."""
        from graph.config_io import config_to_dict

        try:
            doc = redact_for_agent(config_to_dict(config))
        except Exception as exc:  # noqa: BLE001 — a tool must not raise into the turn
            log.exception("[config] show_config failed to serialize the live config")
            return f"Error: could not read the effective config: {exc}"

        wanted = (section or "").strip()
        if wanted:
            if wanted not in doc:
                near = sorted(k for k in doc if wanted.lower() in k.lower())
                suggestion = f" Did you mean: {', '.join(near)}?" if near else ""
                return f"Error: no config section {wanted!r}.{suggestion}\nSections: {', '.join(sorted(doc))}"
            return _dump({wanted: doc[wanted]})

        text = _dump(doc)
        if not text.endswith("chars — ask for one section instead)"):
            return text
        # Too big to be useful as a wall of JSON: an index is the more actionable answer.
        scalars = {k: v for k, v in sorted(doc.items()) if not isinstance(v, (dict, list))}
        nested = sorted(k for k, v in doc.items() if isinstance(v, (dict, list)))
        return (
            "The full config is too large to show at once. Top-level values:\n"
            f"{json.dumps(scalars, indent=2, default=str, sort_keys=True)}\n\n"
            f'Sections — call show_config("<name>") for one:\n  {", ".join(nested)}'
        )

    return [show_config]
