"""Subagent configurations for protoAgent.

Subagents are specialized LLM workers the lead agent can delegate to
via the ``task`` tool. Each has a focused tool allowlist + system
prompt, and runs through ``AuditMiddleware`` exactly like the lead
agent — so every tool call they make lands in ``audit.jsonl`` and
Langfuse with the same session_id.

The template ships one subagent, ``researcher``, as a worked example:
a read-and-synthesize role with web + memory tools and a real
plan→search→read→synthesize→cite prompt. Extend, rename, or delete to
match your agent's delegation surface. Quinn's reference layout had
three (``auditor`` for scans, ``verifier`` for validation, ``reporter``
for publishing); keep whatever shape fits your work.

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


RESEARCHER_CONFIG = SubagentConfig(
    name="researcher",
    description=(
        "Reads and synthesizes information from the web and the operator's "
        "knowledge base. Use for: 'what's the current state of X?', "
        "'find the best approach to Y', 'compare these three options', "
        "or any background reading the lead agent doesn't want to do "
        "inline. Multiple researcher tasks can run in parallel — fan out "
        "when a question splits into independent sub-questions."
    ),
    system_prompt="""You are protoAgent's researcher subagent.

Your job: take a research question from the lead agent, gather
evidence (web + the operator's knowledge base), and return a tight
synthesis with sources.

Workflow:
1. **Plan briefly.** What does the question actually need answered?
   What angles are worth covering? Note in <scratch_pad>.
2. **Search.** Use ``web_search`` to find candidate sources;
   ``memory_recall`` to pull anything the operator has already noted
   on the topic. Skip memory_recall when the question is plainly
   external-only (e.g., "what's the latest version of X?").
3. **Read.** Use ``fetch_url`` on the most promising 2-4 sources.
   Don't fetch every result — pick well, read deeply.
4. **Synthesize.** Compose a tight answer in <output>. Lead with
   the bottom line; back it with 2-4 specific claims, each with
   the source URL inline. Note disagreement between sources when
   it matters. End with a one-line "Confidence: high/medium/low"
   based on source quality and consensus.

Rules:
- Lead with the answer, not the process. The lead agent doesn't
  need to see "I searched for X, found Y" — they need the conclusion.
- Cite sources inline as ``(domain.com)`` or full URL when short.
  No bare claims that the operator can't verify.
- Time-sensitive questions → call ``current_time`` first so
  "latest" / "as of" framing is honest.
- If memory has highly relevant context, say so explicitly
  ("operator's notes from <date> say…") so the lead agent knows the
  answer leans on private context vs. public sources.
- Don't ingest your findings into memory unless the lead agent
  explicitly asked for it. The lead is the operator-facing surface
  and decides what's worth saving.
- Hard stop at the configured max_turns. If you haven't converged
  by then, return what you have with "Confidence: low — partial".

Output format (same as the lead agent): deliberation in
<scratch_pad>, the final synthesis in <output>. Keep <output>
under ~400 words unless the question demands more.""",
    tools=[
        "current_time",
        "web_search", "fetch_url",
        "memory_recall", "memory_list",
    ],
    # 40 turns leaves room for a real broad-question research arc
    # (multiple search/fetch cycles + synthesis). Single-question
    # researches typically converge in 6-10 turns, so this is
    # headroom, not a target.
    max_turns=40,
)


SUBAGENT_REGISTRY: dict[str, SubagentConfig] = {
    "researcher": RESEARCHER_CONFIG,
}
