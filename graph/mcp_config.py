"""MCP server-entry normalization + bundle-seed template resolution.

Shared by the operator MCP routes (console add/import — ``operator_api/mcp_routes.py``)
and the bundle/archetype seed path (``graph/workspaces/manager.py``). It lives in
``graph/`` so both the operator API layer and the workspace seeder can reuse one
validator *without* ``graph/`` importing ``operator_api/`` (the import-layering fence,
enforced by ``lint-imports``).

``clean_mcp_entry`` / ``normalize_named`` / ``entries_from_blob`` raise ``ValueError`` on
bad input; the HTTP layer translates that into a 400. ``resolve_bundle_mcp_item`` fills a
bundle's ``${input}`` placeholders at seed time (ADR 0083 D5, #2011).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

TRANSPORTS = {"stdio", "http", "streamable_http", "sse"}

# Map the various "transport"/"type" spellings found in shared MCP JSON to ours.
_TRANSPORT_ALIASES = {
    "streamable-http": "streamable_http",
    "streamablehttp": "streamable_http",
    "http-stream": "streamable_http",
}

# ``${key}`` placeholder, e.g. ``"Bearer ${token}"`` — same syntax the MCP catalog and
# the console's fillTemplate use, so a bundle template reads identically to a catalog row.
_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


def clean_mcp_entry(body: dict) -> dict:
    """Validate + normalize an ``mcp.servers`` entry (from a form or a bundle template).

    Raises ``ValueError`` on bad input.
    """
    name = str(body.get("name", "")).strip()
    transport = str(body.get("transport", "stdio")).strip() or "stdio"
    if not name:
        raise ValueError("name is required")
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {sorted(TRANSPORTS)}")

    entry: dict = {"name": name, "transport": transport}
    if transport == "stdio":
        command = str(body.get("command", "")).strip()
        if not command:
            raise ValueError("stdio transport needs a command")
        entry["command"] = command
        args = body.get("args")
        if isinstance(args, list):
            args = [str(a).strip() for a in args if str(a).strip()]
        elif isinstance(args, str):
            args = args.split()
        else:
            args = []
        if args:
            entry["args"] = args
    else:  # http / streamable_http / sse
        url = str(body.get("url", "")).strip()
        if not url:
            raise ValueError(f"{transport} transport needs a url")
        entry["url"] = url

    env = body.get("env")
    if isinstance(env, dict):
        env = {str(k).strip(): str(v) for k, v in env.items() if str(k).strip()}
        if env:
            entry["env"] = env
    headers = body.get("headers")
    if transport != "stdio" and isinstance(headers, dict):
        headers = {str(k).strip(): str(v) for k, v in headers.items() if str(k).strip()}
        if headers:
            entry["headers"] = headers
    return entry


def normalize_named(name: str, spec: dict) -> dict:
    """A ``{name: {...}}`` entry (Claude-Desktop / mcp.json style) → a clean entry.

    Infers transport when absent: an explicit ``transport``/``type``, else ``stdio`` if
    there's a ``command``, else ``http`` if there's a ``url``. Raises ``ValueError``.
    """
    s = dict(spec or {})
    s["name"] = s.get("name") or name
    if "transport" not in s:
        t = str(s.get("type", "")).strip().lower()
        if t:
            s["transport"] = _TRANSPORT_ALIASES.get(t, t)
        elif s.get("command"):
            s["transport"] = "stdio"
        elif s.get("url"):
            s["transport"] = "http"
    return clean_mcp_entry(s)


def entries_from_blob(data: object) -> list[dict]:
    """Normalize a pasted MCP JSON blob into clean ``mcp.servers`` entries.

    Accepts the common shapes: the standard ``{"mcpServers": {name: spec}}`` wrapper, our
    own ``{"servers": [ {name, ...} ]}`` export, a single server object, or a bare
    ``{name: spec}`` map. Raises ``ValueError`` when none is found.
    """
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    if isinstance(data.get("mcpServers"), dict):
        return [normalize_named(n, s) for n, s in data["mcpServers"].items()]
    if isinstance(data.get("servers"), list):
        return [clean_mcp_entry(s) for s in data["servers"]]
    if data.get("command") or data.get("url"):
        return [clean_mcp_entry(data)]  # a single server object (must carry a name)
    if data and all(isinstance(v, dict) for v in data.values()):
        return [normalize_named(n, s) for n, s in data.items()]
    raise ValueError("no MCP server found in the JSON")


def _sub_placeholders(obj: object, values: Mapping[str, str]) -> object:
    """Recursively replace ``${key}`` in every string of ``obj`` from ``values`` (a
    missing key resolves to ``""`` — matching the console's fillTemplate)."""
    if isinstance(obj, str):
        return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), "")), obj)
    if isinstance(obj, list):
        return [_sub_placeholders(v, values) for v in obj]
    if isinstance(obj, dict):
        return {k: _sub_placeholders(v, values) for k, v in obj.items()}
    return obj


def resolve_bundle_mcp_item(item: dict, env: Mapping[str, str]) -> tuple[dict, list[str]]:
    """Resolve one bundle ``mcp:`` item into a clean ``mcp.servers`` entry (ADR 0083 D5).

    An item mirrors an MCP-catalog row: ``{template: {...}, inputs: [{key, env?, default?,
    required?}]}`` — a bare entry with no ``template`` key is treated as a template with no
    inputs. Each input's ``${key}`` placeholder in the template is filled, at seed time,
    from its ``env`` variable (read out of ``env``) or its ``default``; an unfilled
    placeholder becomes ``""``.

    Returns ``(entry, unresolved_required)`` — the list of *required* input keys that had no
    seed-time value. The caller seeds an entry with any unresolved-required inputs as
    ``enabled: false`` (visible in the console MCP panel but inert) so the operator finishes
    it there, rather than booting a half-templated server. Raises ``ValueError`` if the
    resolved template isn't a valid entry.
    """
    if not isinstance(item, dict):
        raise ValueError("mcp bundle item must be a mapping")
    template = item.get("template") if isinstance(item.get("template"), dict) else item
    inputs = item.get("inputs") if isinstance(item.get("inputs"), list) else []
    values: dict[str, str] = {}
    unresolved: list[str] = []
    for inp in inputs:
        if not isinstance(inp, dict):
            continue
        key = str(inp.get("key", "")).strip()
        if not key:
            continue
        envvar = str(inp.get("env", "")).strip()
        val = env.get(envvar) if envvar else None
        if not val and "default" in inp:
            val = str(inp["default"])
        if not val:
            if inp.get("required"):
                unresolved.append(key)
            continue
        values[key] = val
    entry = clean_mcp_entry(_sub_placeholders(template, values))
    return entry, unresolved
