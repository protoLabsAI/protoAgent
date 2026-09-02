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
_DEFAULT_PAGE_LIMIT = 100
_MAX_PAGE_LIMIT = 500
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
                out[k] = {
                    ik: (_REDACTED if iv not in (None, "") else iv) for ik, iv in v.items()
                }
            else:
                out[k] = redact_for_agent(v, key=str(k))
        return out
    if isinstance(value, list):
        return [redact_for_agent(v, key=key) for v in value]
    return value


def _dump(doc: Any) -> str:
    text = json.dumps(doc, indent=2, default=str, sort_keys=True)
    if len(text) > _MAX_CHARS:
        return (
            text[:_MAX_CHARS]
            + f"\n… (truncated at {_MAX_CHARS} chars — ask for one section instead)"
        )
    return text


def _selected_dump(doc: Any) -> str | None:
    text = json.dumps(doc, indent=2, default=str, sort_keys=True)
    if len(text) <= _MAX_CHARS:
        return text
    return None


def _escape_segment(key: Any) -> str:
    """Encode a single literal key so it survives dotted-path splitting. A dot in the key
    becomes ``\\.`` and a backslash ``\\\\``; every other key is unchanged. This is what
    lets a drill-in pointer address a child whose key itself contains a dot (or is empty)
    without the deeper path re-splitting through the middle of that key."""
    return str(key).replace("\\", "\\\\").replace(".", "\\.")


def _split_path(path: str) -> list[str]:
    """Split a dotted selector into literal key segments, honouring the escapes that
    ``_escape_segment`` writes. Only an UNescaped dot separates segments, so ``a\\.b``
    is the single key ``a.b``; an empty segment is a real (empty-string) key, not an
    error, so an empty-keyed child stays addressable. The inverse of joining escaped
    segments with dots — a pointer built that way always resolves back to its child."""
    segments: list[str] = []
    current: list[str] = []
    i, n = 0, len(path)
    while i < n:
        ch = path[i]
        if ch == "\\" and i + 1 < n:
            current.append(path[i + 1])
            i += 2
            continue
        if ch == ".":
            segments.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


def _join_path(segments: list[str]) -> str:
    """Render a resolved segment list back into an escaped dotted selector that
    ``_split_path``/``_resolve_path`` accept verbatim — the canonical form for a
    ``read_with`` pointer and the envelope's ``section`` field."""
    return ".".join(_escape_segment(s) for s in segments)


def _path_error(path: str, part: str, seen: list[str], current: Any) -> str:
    at = ".".join(seen) or "<root>"
    if isinstance(current, dict):
        available = ", ".join(sorted(str(k) for k in current))
        suffix = (
            f" Available keys at {at}: {available}"
            if available
            else f" {at} is an empty object."
        )
    elif isinstance(current, list):
        suffix = f" {at} is a list with {len(current)} item(s); use a numeric path segment."
    else:
        suffix = f" {at} is {type(current).__name__}, so it has no child keys."
    return f"Error: no config path {path!r}; missing {part!r} at {at}.{suffix}"


def _resolve_path(doc: dict, path: str) -> tuple[bool, Any, str, list[str]]:
    """Walk a dotted (escape-aware) selector. Returns the literal segments actually
    traversed alongside the value, so a drill-in pointer can be rebuilt by escaping and
    re-joining them — which is the only way a child whose key holds a dot (or is empty)
    stays addressable."""
    current: Any = doc
    seen: list[str] = []
    for part in _split_path(path):
        if isinstance(current, dict):
            if part not in current:
                return False, None, _path_error(path, part, seen, current), seen
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return False, None, _path_error(path, part, seen, current), seen
            if idx < 0 or idx >= len(current):
                return (
                    False,
                    None,
                    f"Error: no config path {path!r}; list index {idx} is out of range "
                    f"at {'.'.join(seen)}.",
                    seen,
                )
            current = current[idx]
        else:
            return False, None, _path_error(path, part, seen, current), seen
        seen.append(part)
    return True, current, "", seen


def _page_envelope(
    section: str,
    *,
    offset: int,
    limit: int,
    returned: int,
    total: int,
    page: Any,
    note: str = "",
) -> dict:
    """The one shape every paged response takes: the selected ``section``, an explicit
    ``pagination`` cursor (so the boundary/continuation is stated, never implied), and
    the ``value`` slice. ``next_offset`` is the resume cursor, or ``None`` at the end."""
    next_offset = offset + returned
    has_more = next_offset < total
    env: dict = {
        "section": section,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": returned,
            "total": total,
            "next_offset": next_offset if has_more else None,
            "has_more": has_more,
        },
        "value": page,
    }
    if note:
        env["note"] = note
    return env


def _oversized_pointer(section_segments: list[str], child: Any, value: Any) -> dict:
    """A stand-in for a single child too large to embed in one page: it names how to
    read the child with a deeper dotted selector, so a page advances by one instead of
    dead-ending on one big value. The selector is built by ESCAPING and re-joining the
    resolved segments plus the child key, so a child whose key contains a dot (or is
    empty) still resolves back to itself instead of re-splitting into the wrong path.
    Carries only shape metadata — never the value — so it cannot leak a credential the
    value might hold."""
    if isinstance(value, dict):
        shape = {"type": "object", "keys": len(value)}
    elif isinstance(value, list):
        shape = {"type": "array", "items": len(value)}
    elif isinstance(value, str):
        shape = {"type": "string", "chars": len(value)}
    else:
        shape = {"type": type(value).__name__}
    return {
        "__truncated__": True,
        "reason": f"value exceeds the {_MAX_CHARS}-char per-response cap",
        "read_with": _join_path([*section_segments, str(child)]),
        **shape,
    }


def _page_value(value: Any, *, section_segments: list[str], offset: int, limit: int) -> str:
    section = _join_path(section_segments)
    if offset < 0:
        return "Error: offset must be >= 0."
    if limit < 0:
        return "Error: limit must be >= 0."
    page_limit = min(limit or _DEFAULT_PAGE_LIMIT, _MAX_PAGE_LIMIT)
    if not isinstance(value, (dict, list)):
        text = _selected_dump({section: value})
        if text is not None and (
            not isinstance(value, str) or (not offset and not limit)
        ):
            return text
        if not isinstance(value, str):
            return (
                f"Error: selected scalar {section!r} exceeds {_MAX_CHARS} chars and "
                "cannot be paged safely unless it is a string."
            )
        page = value[offset : offset + page_limit]
        envelope = _page_envelope(
            section,
            offset=offset,
            limit=page_limit,
            returned=len(page),
            total=len(value),
            page=page,
        )
        text = _selected_dump(envelope)
        if text is not None:
            return text
        return (
            f"Error: scalar page for {section!r} exceeds {_MAX_CHARS} chars. "
            "Reduce limit."
        )

    keys = sorted(value, key=str) if isinstance(value, dict) else list(range(len(value)))
    total = len(value)
    pl = page_limit
    while True:
        window = keys[offset : offset + pl]
        page = (
            {k: value[k] for k in window}
            if isinstance(value, dict)
            else [value[k] for k in window]
        )
        envelope = _page_envelope(
            section, offset=offset, limit=pl, returned=len(window), total=total, page=page
        )
        text = _dump(envelope)
        if len(text) <= _MAX_CHARS:
            return text
        if len(window) <= 1:
            break
        pl //= 2

    # One child at `offset` is bigger than a whole page holds. Don't dead-end: return it
    # as a drill-in pointer so the cursor still advances by one and nothing is silently
    # dropped — the full value stays reachable via the deeper dotted path it names.
    if not window:  # unreachable (an empty page always fits) — an explicit floor, not a cut
        return f"Error: could not render {section!r} within {_MAX_CHARS} chars."
    child = window[0]
    pointer = _oversized_pointer(section_segments, child, value[child])
    page = {str(child): pointer} if isinstance(value, dict) else [pointer]
    envelope = _page_envelope(
        section,
        offset=offset,
        limit=pl,
        returned=1,
        total=total,
        page=page,
        note=f"one item was too large to embed; read it with section={pointer['read_with']!r}",
    )
    return _dump(envelope)


def build_config_tools(config) -> list:
    """Bind ``show_config`` against the LIVE config object.

    Takes the config rather than reading global state so the tool reports the same
    object the rest of the graph was built from — the whole point is that it can be
    trusted to answer "how am I actually wired right now"."""

    @tool
    def show_config(section: str = "", offset: int = 0, limit: int = 0) -> str:
        """Read your own effective configuration — the merged, live settings this agent
        is actually running with, including plugin sections.

        Use this when behaviour doesn't match what you expect and you suspect the
        WIRING rather than the code: which repo a board is bound to, which model or
        gateway is in use, which plugins are enabled, which folders your filesystem
        tools can reach. Reading a plugin's defaults from its source tells you what it
        would do unconfigured — this tells you what YOUR instance actually set.

        - ``section``: a dotted key path (e.g. "project_board",
          "project_board.projects", "filesystem", "mcp", "model"). Omit it to see
          the whole config, or to list the sections when it's too large to show at once.
        - ``offset`` / ``limit``: page a selected dict, list, or oversized string.
          Dict keys are sorted deterministically. Large selected values return an
          explicit page envelope instead of being silently cut off; ``next_offset``
          tells you how to continue. A single child too big to embed comes back as a
          ``__truncated__`` pointer naming the deeper ``read_with`` path — the page
          never dead-ends, so nothing is silently dropped.

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
            if wanted in doc:
                # An exact top-level key wins before dotted parsing, so a section whose
                # own name contains a dot stays addressable. Its literal name is the one
                # traversed segment — escaping it keeps drill-in pointers resolvable.
                ok, selected, error, segments = True, doc[wanted], "", [wanted]
            else:
                ok, selected, error, segments = _resolve_path(doc, wanted)
            if not ok and "." not in wanted:
                near = sorted(k for k in doc if wanted.lower() in k.lower())
                suggestion = f" Did you mean: {', '.join(near)}?" if near else ""
                return (
                    f"Error: no config section {wanted!r}.{suggestion}\n"
                    f"Sections: {', '.join(sorted(doc))}"
                )
            if not ok:
                return error
            if offset or limit:
                return _page_value(
                    selected, section_segments=segments, offset=offset, limit=limit
                )
            text = _selected_dump({wanted: selected})
            if text is None:
                return _page_value(
                    selected, section_segments=segments, offset=0, limit=_DEFAULT_PAGE_LIMIT
                )
            return text

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
