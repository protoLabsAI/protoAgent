"""protoAgent tool calls → ACP ``tool_call`` presentation: kind, title, locations.

The A2A tool-call-v1 frame carries the tool ``name`` and its ``args`` — but ``args`` is a
*preview*: the server JSON-encodes the call's arguments and caps the string at 800 chars
(``server/chat.py::_coerce_tool_value``), so a ``write_file`` with a large ``content`` arrives
as truncated, unparseable JSON. :func:`parse_args` therefore falls back to pulling the short
scalar fields (``project``, ``path``, ``offset``, …) out of the prefix with a regex — they
precede the bulky ones in every fs tool's signature, so they survive the cap.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .roots import RootMap

# protoAgent core tool names (tools/fs_tools.py, tools/lg_tools.py, …) → ACP ToolKind.
_KINDS: dict[str, str] = {
    "read_file": "read",
    "list_dir": "read",
    "list_projects": "read",
    # ADR 0112 / the editor hand-off: the agent POINTING at code (console code pane, the
    # operator's editor). No content to show, but a precise place to follow.
    "show_code": "read",
    "open_in_editor": "read",
    "find_files": "search",
    "search_files": "search",
    "session_search": "search",
    "memory_recall": "search",
    "knowledge_search": "search",
    "web_search": "search",
    "write_file": "edit",
    "edit_file": "edit",
    "delete_file": "delete",
    "run_command": "execute",
    "execute_code": "execute",
    "fetch_url": "fetch",
    "web_fetch": "fetch",
    "task": "think",
    "task_batch": "think",
}

_SCALAR = re.compile(r'"(?P<k>project|path|pattern|query|command|offset|limit|line|end_line|subagent_type|description|url)"\s*:\s*'
                     r'(?P<v>"(?:[^"\\]|\\.)*"|-?\d+)')

# search_files hit lines: "rel/path.py:342: text" (or "rel/path.py-341- ctx" with context).
_HIT = re.compile(r"^(?P<path>[^\s:][^:]*?):(?P<line>\d+):")


def tool_kind(name: str) -> str:
    return _KINDS.get(name, "other")


def parse_args(raw: Any) -> dict[str, Any]:
    """The tool's arguments as a dict, from a dict, a JSON string, or a truncated JSON
    string (best-effort scalar extraction)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except ValueError:
        pass
    out: dict[str, Any] = {}
    for m in _SCALAR.finditer(raw):
        val = m.group("v")
        try:
            out.setdefault(m.group("k"), json.loads(val))
        except ValueError:
            continue
    return out


def _int(v: Any) -> int | None:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i >= 1 else None


def describe(name: str, args: dict[str, Any], roots: RootMap) -> tuple[str, list[dict[str, Any]]]:
    """``(title, locations)`` for a tool start. Locations are ACP ``{path, line?}`` with
    absolute paths and 1-based lines — only when the project root is known."""
    if not args:
        # The early announce (streamed tool name, no args yet): a neutral label that the
        # second announce replaces — never "Search for ''".
        return (name or "tool").replace("_", " ").capitalize(), []
    project = args.get("project")
    path = args.get("path")
    locations: list[dict[str, Any]] = []
    shown = f"{project}/{path}" if project and path else (path or project or "")

    if name in ("read_file", "write_file", "edit_file", "delete_file", "list_dir"):
        abs_path = roots.resolve(project, path or ".")
        if abs_path:
            loc: dict[str, Any] = {"path": abs_path}
            line = _int(args.get("offset")) if name == "read_file" else None
            if line:
                loc["line"] = line
            locations.append(loc)
        verb = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "delete_file": "Delete", "list_dir": "List"}[name]
        suffix = ""
        if name == "read_file" and (_int(args.get("offset")) or _int(args.get("limit"))):
            start = _int(args.get("offset")) or 1
            lim = _int(args.get("limit"))
            suffix = f" (lines {start}–{start + lim - 1})" if lim else f" (from line {start})"
        return f"{verb} {shown or 'file'}{suffix}", locations
    if name in ("show_code", "open_in_editor"):
        abs_path = roots.resolve(project, path)
        line = _int(args.get("line"))
        if abs_path:
            locations.append({"path": abs_path, **({"line": line} if line else {})})
        end = _int(args.get("end_line"))
        span = f":{line}" + (f"–{end}" if end and line and end > line else "") if line else ""
        verb = "Show" if name == "show_code" else "Open in editor"
        return f"{verb} {shown or 'file'}{span}", locations
    if name == "search_files":
        q = args.get("query") or ""
        return f"Search {project or ''} for {q!r}".replace("  ", " "), locations
    if name == "find_files":
        return f"Find {args.get('pattern') or '**/*'} in {project or 'project'}", locations
    if name == "run_command":
        return f"Run `{args.get('command') or '…'}`" + (f" in {project}" if project else ""), locations
    if name in ("task", "task_batch"):
        who = args.get("subagent_type") or "subagent"
        what = args.get("description") or ""
        return f"Delegate to {who}" + (f": {what}" if what else ""), locations
    if name == "list_projects":
        return "List projects", locations
    return name or "tool", locations


def result_locations(name: str, args: dict[str, Any], result: Any, roots: RootMap, *, limit: int = 20) -> list[dict[str, Any]]:
    """Extra locations learned from a finished call — ``search_files`` hits are
    ``rel/path:line:`` (relative to the PROJECT root, not the search base)."""
    if name != "search_files" or not isinstance(result, str):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line in result.splitlines():
        m = _HIT.match(line)
        if not m:
            continue
        abs_path = roots.resolve(args.get("project"), m.group("path"))
        if not abs_path:
            continue
        key = (abs_path, int(m.group("line")))
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": abs_path, "line": key[1]})
        if len(out) >= limit:
            break
    return out


def result_text(result: Any, *, cap: int = 4000) -> str:
    if result is None:
        return ""
    if not isinstance(result, str):
        try:
            result = json.dumps(result, ensure_ascii=False, indent=1)
        except (TypeError, ValueError):
            result = str(result)
    return result if len(result) <= cap else result[:cap] + "\n…"
