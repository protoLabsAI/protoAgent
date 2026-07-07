"""Canonical ACP coding-agent catalog — the SINGLE source of truth for which CLI coding
agents protoAgent can drive over the Agent Client Protocol, and how to launch each.

A pure-data leaf (no imports) so every consumer can derive from it without import cycles:
  * ``runtime.acp_runtime`` — ``_ACP_ADAPTERS`` (launch specs for the ACP runtime + aux models)
  * ``graph.settings_schema`` — the ``agent_runtime`` choices + the ``acp:<agent>`` model overrides
  * ``GET /api/acp-agents`` — serves the catalog to the web Delegates picker + the setup wizard

Add/adjust an agent HERE and it propagates everywhere. Each entry's ``command``/``args`` is
the launch incantation; ``id`` is the short key (``agent_runtime: acp:<id>``); ``label`` is
the human name shown in pickers.
"""

from __future__ import annotations

ACP_AGENT_CATALOG: list[dict] = [
    {"id": "proto", "label": "proto (protoCLI)", "command": "proto", "args": ["--acp"]},
    {"id": "codex", "label": "Codex", "command": "npx", "args": ["-y", "@zed-industries/codex-acp"]},
    {"id": "claude", "label": "Claude Code", "command": "npx", "args": ["-y", "@agentclientprotocol/claude-agent-acp"]},
    {"id": "gemini", "label": "Gemini CLI", "command": "gemini", "args": ["--experimental-acp"]},
    {"id": "opencode", "label": "OpenCode", "command": "opencode", "args": ["acp"]},
    {"id": "copilot", "label": "Copilot CLI", "command": "copilot", "args": ["--acp"]},
    # Not a coding agent: NousResearch's personal agent, whose in-tree ACP adapter makes it
    # a full protoAgent brain. Install/preset: `protoagent hermes` (runtime/cli.py).
    {"id": "hermes", "label": "Hermes Agent", "command": "hermes-acp", "args": []},
]


def acp_agent_catalog() -> list[dict]:
    """The catalog as fresh dicts (args copied) so callers can't mutate the source."""
    return [{**a, "args": list(a["args"])} for a in ACP_AGENT_CATALOG]


def acp_runtime_options() -> list[str]:
    """The ``acp:<id>`` strings — for the agent_runtime select + the aux-model overrides."""
    return [f"acp:{a['id']}" for a in ACP_AGENT_CATALOG]
