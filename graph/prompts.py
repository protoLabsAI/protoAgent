"""System prompt composer for protoAgent.

Composes the system prompt from, in order:

1. Agent identity (``SOUL.md`` in the workspace, falls back to a
   template-generic placeholder).
2. Skill methodology (``skills/<slug>/SKILL.md`` — loaded per skill
   if the consumer passes a ``skill`` hint; the template ships no
   skill docs by default).
3. Subagent delegation rules (built from ``SUBAGENT_REGISTRY``).
4. Dynamic context injected by ``KnowledgeMiddleware`` when the agent
   ships a knowledge store.
5. Operator guidelines (the template ships neutral defaults — override
   in your fork to encode domain behavior like "verify, don't trust"
   or "always end with a PASS/WARN/FAIL verdict").
6. Response format (``<scratch_pad>`` / ``<output>`` protocol, parsed
   by ``graph/output_format.py`` and routed server-side so scratch
   content never reaches consumers).

When forking, the main thing to edit is the operator guidelines block
— that's where you encode how the agent behaves in its specific
domain.
"""

from pathlib import Path

from graph.output_format import OUTPUT_FORMAT_INSTRUCTIONS
from graph.subagents.config import SUBAGENT_REGISTRY


def _read_file(path: str | Path) -> str:
    """Read a file if it exists, return empty string otherwise."""
    p = Path(path)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def build_system_prompt(
    workspace: str = "/sandbox",
    include_subagents: bool = True,
    context: str = "",
) -> str:
    """Build the complete system prompt for the lead agent.

    ``context`` is injected verbatim at the end of the prompt (before
    the response-format block) — ``KnowledgeMiddleware`` is the typical
    caller, passing in retrieved knowledge-store hits.
    """
    parts = []

    # 1. Identity — prefer the runtime workspace (entrypoint.sh copies
    # config/SOUL.md to /sandbox/SOUL.md at container start). Fall back
    # to the repo source so local `python server.py` runs without a
    # /sandbox mount still pick up persona edits made via the drawer.
    soul = _read_file(f"{workspace}/SOUL.md")
    if not soul:
        soul = _read_file(Path(__file__).parent.parent / "config" / "SOUL.md")
    if soul:
        parts.append(soul)
    else:
        parts.append(
            "# Agent\n\n"
            "You are a protoAgent — an A2A-compliant LangGraph agent. "
            "Replace this placeholder by writing an SOUL.md in the workspace "
            "with your agent's identity, role, and personality."
        )

    # 2. Subagent instructions
    if include_subagents:
        parts.append(_build_subagent_section())

    # 3. Dynamic context (typically from KnowledgeMiddleware)
    if context:
        parts.append(f"\n# Context\n\n{context}")

    # 4. Operator guidelines — OVERRIDE THIS in your fork
    parts.append("""
# Guidelines

- Prefer direct answers for simple requests; use tools when they add
  information the user asked for.
- Delegate to subagents via the `task` tool only for genuinely parallel
  or specialized work.
- If a tool fails, read the error, try once with corrected inputs, then
  surface the failure to the user with the concrete error string.
- Keep internal deliberation in `<scratch_pad>`; put only the
  user-facing answer in `<output>` — the handler parses these tags and
  never forwards scratch content to consumers.
""")

    # 5. Response format (scratch_pad + output tags — parsed server-side)
    parts.append(OUTPUT_FORMAT_INSTRUCTIONS)

    return "\n\n".join(parts)


def _build_subagent_section() -> str:
    """Build the subagent delegation instructions."""
    lines = [
        "# Subagent Delegation",
        "",
        "You can delegate tasks to specialized subagents using the `task` tool.",
        "Each subagent has focused tools and a domain-specific prompt:",
        "",
    ]

    for name, config in SUBAGENT_REGISTRY.items():
        lines.append(f"- **{name}**: {config.description}")
        if config.tools:
            lines.append(f"  Tools: {', '.join(config.tools)}")
        lines.append("")

    lines.extend([
        "**Rules:**",
        "- Delegate only when parallel work or specialized context helps",
        "- For simple requests, answer directly without delegation",
        "- Max 3 concurrent subagent tasks",
        "- Subagents cannot spawn further subagents",
    ])

    return "\n".join(lines)


def build_subagent_prompt(agent_name: str, workspace: str = "/sandbox") -> str:
    """Build system prompt for a specific subagent.

    Subagents' return values are parsed by the lead agent, so they use
    the same ``<scratch_pad>`` / ``<output>`` protocol — keeps the
    pipeline uniform and lets ``OutputFilter`` handle subagent output
    identically to top-level streaming.
    """
    config = SUBAGENT_REGISTRY.get(agent_name)
    base = (
        config.system_prompt
        if config
        else "You are a subagent. Complete the delegated task efficiently."
    )
    return f"{base}\n\n{OUTPUT_FORMAT_INSTRUCTIONS}"
