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
    # The ACP-org adapter, NOT `@zed-industries/codex-acp`. The zed package is a sealed
    # ~188 MB Rust bundle with a codex core compiled IN — it never invokes the `codex` on
    # your PATH — and its last publish was 2026-06-08. So it pinned every protoAgent user
    # to a June-era codex, and a newer model answered `400 … requires a newer version of
    # Codex` with no way to act on it: upgrading the codex CLI does nothing, and `npx -y`
    # already had the newest adapter. The ACP-org package is a thin TS wrapper that depends
    # on `@openai/codex` as an ordinary npm dep, so the codex it drives tracks the real
    # package instead of being frozen into a binary.
    {"id": "codex", "label": "Codex", "command": "npx", "args": ["-y", "@agentclientprotocol/codex-acp"]},
    {"id": "claude", "label": "Claude Code", "command": "npx", "args": ["-y", "@agentclientprotocol/claude-agent-acp"]},
    {"id": "gemini", "label": "Gemini CLI", "command": "gemini", "args": ["--experimental-acp"]},
    {"id": "opencode", "label": "OpenCode", "command": "opencode", "args": ["acp"]},
    {"id": "copilot", "label": "Copilot CLI", "command": "copilot", "args": ["--acp"]},
]

# Deprecated agents: still LAUNCHABLE, no longer OFFERED (#2633).
#
# The catalog above was doing two jobs — the list we offer (Settings picker, setup wizard,
# `runtime list`, /api/acp-agents) and the registry `acp_runtime` resolves a launch command
# from. Deleting an entry outright therefore does more than hide it: `_launch_spec` raises
# "no ACP adapter for 'hermes'", so an existing install with `agent_runtime: acp:hermes` in
# its config stops booting. Retiring an option must never brick the people already using it.
#
# So the two roles are split. `hermes` lives here: absent from everything that OFFERS a
# runtime, present in what LAUNCHES one. ACP delegates superseded the pattern (an external
# agent as a worker the native brain dispatches to, rather than as the brain — which keeps
# goal continuations, telemetry and the whole plugin surface).
DEPRECATED_ACP_AGENTS: list[dict] = [
    {"id": "hermes", "label": "Hermes Agent (deprecated)", "command": "hermes-acp", "args": []},
]


def _merge_custom_agents(extra_agents: dict | None) -> list[dict]:
    """The built-in catalog (fresh dicts) with user-registered ``acp.agents.<id>`` entries
    merged in (ADR 0033). ``extra_agents`` is a ``config.acp_agents`` mapping
    (``{id: {command, args, label}}``). A KNOWN id gets its ``command``/``args``/``label``
    overridden; a WHOLLY-NEW id is appended only if it carries a launch ``command`` (without
    one it isn't launchable, so it stays out of the pickers). Order: built-ins first, then
    custom ids in declaration order. Pure data — no imports (this stays an import-cycle-free
    leaf), so the config dict is passed in rather than read here."""
    out = [{**a, "args": list(a["args"])} for a in ACP_AGENT_CATALOG]
    if not extra_agents:
        return out
    by_id = {a["id"]: a for a in out}
    for raw_id, spec in extra_agents.items():
        aid = raw_id.strip() if isinstance(raw_id, str) else ""
        if not aid or not isinstance(spec, dict):
            continue
        command = str(spec.get("command") or "").strip()
        entry = by_id.get(aid)
        if entry is None:
            if not command:  # a new agent must be launchable to be worth offering
                continue
            entry = {"id": aid, "label": aid, "command": "", "args": []}
            out.append(entry)
            by_id[aid] = entry
        if spec.get("label"):
            entry["label"] = str(spec["label"])
        if command:
            entry["command"] = command
        if isinstance(spec.get("args"), list):
            entry["args"] = list(spec["args"])
    return out


def acp_agent_catalog(extra_agents: dict | None = None) -> list[dict]:
    """The catalog as fresh dicts (args copied) so callers can't mutate the source.

    Pass a ``config.acp_agents`` dict to include user-registered custom agents + launch-spec
    overrides, so a user's own coding agent surfaces everywhere the built-ins do (the
    ``agent_runtime`` select, the aux-model dropdowns, ``/api/acp-agents``, ``runtime list``)."""
    return _merge_custom_agents(extra_agents)


def acp_runtime_options(extra_agents: dict | None = None) -> list[str]:
    """The ``acp:<id>`` strings — for the agent_runtime select + the aux-model overrides.
    Includes user-registered agents when a ``config.acp_agents`` dict is passed."""
    return [f"acp:{a['id']}" for a in acp_agent_catalog(extra_agents)]
