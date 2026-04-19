"""Subagent configurations for protoAgent.

Subagents are specialized LLM workers the lead agent can delegate to
via the ``task`` tool. Each has a focused tool allowlist + system
prompt, and runs through ``AuditMiddleware`` exactly like the lead
agent — so every tool call they make lands in ``audit.jsonl`` and
Langfuse with the same session_id.

The template ships with a single ``worker`` subagent to show the
pattern. Extend, rename, or delete to match your agent's actual
delegation surface. Quinn's reference layout had three (``auditor``
for scans, ``verifier`` for validation, ``reporter`` for publishing);
keep whatever shape fits your work.

Rules:
- ``tools`` — allowlist of tool names from ``tools/lg_tools.py``. If
  empty, the subagent gets no tools and can only reply with text.
- ``disallowed_tools`` — explicitly blocked names. Always includes
  ``task`` so subagents can't spawn further subagents (recursion
  guard).
- ``max_turns`` — hard cap on tool-call iterations. Keep tight; a
  subagent that can't finish in ~20 turns probably needs a better
  prompt or more tools, not more turns.
"""

from dataclasses import dataclass, field


@dataclass
class SubagentConfig:
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=lambda: ["task"])
    max_turns: int = 30
    # When False, skill-v1 artifact emission is suppressed even if the caller
    # passes emit_skill=True to task(). Set to False for subagents whose
    # workflows should not be captured as reusable skills (e.g. agents that
    # handle sensitive data or that produce non-deterministic outputs).
    allow_skill_emission: bool = True


WORKER_CONFIG = SubagentConfig(
    name="worker",
    description=(
        "Example subagent — handles a scoped piece of work and reports "
        "back to the lead. Replace with your agent's actual specialist "
        "roles."
    ),
    system_prompt="""You are a worker subagent for protoAgent.

Your job: execute the delegated task using the tools available to you
and return a concise result.

Rules:
- Keep responses focused — the lead agent is waiting on your return
  value, not a conversation.
- If a tool fails, surface the error in plain text; the lead decides
  whether to retry or route differently.
- Use the same <scratch_pad> / <output> format as the lead agent:
  put deliberation in scratch_pad, the final result in output.

Replace this prompt with domain-specific guidance once your agent has
real specialized roles.""",
    tools=["echo", "current_time", "calculator", "web_search", "fetch_url"],
    max_turns=20,
)


SUBAGENT_REGISTRY: dict[str, SubagentConfig] = {
    "worker": WORKER_CONFIG,
}
