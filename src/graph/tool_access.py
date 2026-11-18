"""Default-deny classification for subagent inheritance and private segments.

This module selects candidates only; dispatch applies each subagent's policy.
"""

from tools.lg_tools import SUBAGENT_DENIED_TOOLS


CORE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_projects",
        "list_dir",
        "read_file",
        "view_image",
        "find_files",
        "search_files",
    }
)

NEVER_INHERITED: frozenset[str] = SUBAGENT_DENIED_TOOLS | frozenset(
    {
        "task",
        "task_batch",
        "stop_task",
        "execute_code",
        "run_command",
        "delegate_to",
    }
)


def is_read_tool(tool) -> bool:
    """Allow only known core reads or tools explicitly declaring read access."""
    return tool.name in CORE_READ_TOOLS or (getattr(tool, "metadata", None) or {}).get("access") == "read"


def inherited_read_tools(lead_tools, config) -> list:
    """Select unique reads in input order, excluding control and held tools.

    ``config`` is the lead's graph config. ADR 0117 held tools must reach a
    subagent through its explicit allowlist, never through inheritance.
    """
    blocked = NEVER_INHERITED | set(getattr(config, "tools_subagent_only", None) or [])
    seen = set()
    inherited = []
    for tool in lead_tools:
        if tool.name not in blocked and tool.name not in seen and is_read_tool(tool):
            inherited.append(tool)
            seen.add(tool.name)
    return inherited


def private_segment_fence(tools) -> list[str]:
    """Local reads only; MCP names use the loader's ``server__tool`` form."""
    from graph.middleware.subagent_fence import FENCE_DENY_ALL

    allowed = [tool.name for tool in tools
               if getattr(tool, "name", None) and is_read_tool(tool) and tool.name not in {"fetch_url", "web_search"}
               and "__" not in tool.name]
    return list(dict.fromkeys(allowed)) or [FENCE_DENY_ALL]
