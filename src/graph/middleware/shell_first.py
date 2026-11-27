"""ShellFirstMiddleware — the lead browses files with the shell (``filesystem.shell_first``).

When ``run_command`` runs without an approval pause, ``list_dir`` / ``find_files`` /
``search_files`` duplicate what the model already does fluently with ``ls`` / ``find``
/ ``rg``, and their schemas cost prompt on every call. This hides the three from the
LEAD's model view only:

* They stay in the graph's ToolNode and in the lead's published toolset, so subagents
  keep inheriting them (a subagent never gets ``run_command``, so these are its only
  way to browse) and a fenced background subagent pass still sees them.
* A call the lead makes anyway (a replayed or remembered name) still runs: a fenced,
  read-only call is harmless, and refusing it would only cost a turn.
* ``read_file`` / ``edit_file`` / ``write_file`` are untouched — the precise edit path,
  its per-file lock and the code pane's change announcements depend on them.
"""

from __future__ import annotations

import sys

from langchain.agents.middleware import AgentMiddleware

from graph.middleware.subagent_fence import _bound_tool_name, _state_fence

SHELL_COVERED_TOOLS: frozenset[str] = frozenset({"list_dir", "find_files", "search_files"})


def shell_first_hidden(config, lead_tool_names) -> frozenset[str]:
    """The tool names to hide from the lead, or an empty set when shell-first is inert.

    Inert unless ``filesystem.shell_first`` is on, ``run_command`` is actually bound, its
    approval gate is off (otherwise every read would pause for the operator) and the host
    isn't Windows (``cmd.exe`` has no ``ls`` / ``find`` / ``rg``)."""
    names = frozenset(lead_tool_names)
    if not getattr(config, "filesystem_shell_first", False):
        return frozenset()
    if "run_command" not in names or getattr(config, "filesystem_run_requires_approval", True):
        return frozenset()
    if sys.platform == "win32":
        return frozenset()
    return SHELL_COVERED_TOOLS & names


class ShellFirstMiddleware(AgentMiddleware):
    """Drop the shell-covered read schemas from every unfenced (lead) model call."""

    def __init__(self, hidden: frozenset[str] | set[str]):
        super().__init__()
        self.hidden = frozenset(str(n) for n in hidden)

    def _hide(self, request):
        if _state_fence(request):
            return request
        tools = getattr(request, "tools", None) or []
        kept = [t for t in tools if _bound_tool_name(t) not in self.hidden]
        return request if len(kept) == len(tools) else request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        return handler(self._hide(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._hide(request))
