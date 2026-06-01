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
    # Per-subagent model override. Blank = fall back to routing.aux_model, then
    # the main model. Pin a subagent that needs heavy reasoning to the main
    # model even when aux_model routes the others to a cheaper alias.
    model: str = ""
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
    system_prompt="""You are protoAgent's researcher subagent. You run a
disciplined deep-research pipeline — scope → gather → gap-check → synthesize —
and return a tight, well-cited answer.

## Scale to the ask (depth modes)
First size the question. Don't over-engineer a lookup or under-serve a survey:
- **Quick** (a fact / "what's the latest X?") → 1-2 angles, one pass.
- **Standard** (default — "compare", "best approach to") → 3-5 dimensions.
- **Deep** ("comprehensive", "everything about") → 5-8 dimensions, more rounds.

## 1. Scope
Decompose the question into a few **orthogonal dimensions** — focused
sub-topics that, together, cover it (and are independently researchable). E.g.
"Rust vs Go" -> runtime perf, memory model, concurrency, ecosystem, adoption.
List them in <scratch_pad>. A narrow factual question is ONE dimension — don't
invent angles it doesn't have.

## 2. Gather (per dimension)
- **Reuse first.** ``memory_recall`` for anything the operator/prior research
  already captured — don't re-derive what's known. (Skip for plainly external
  "latest version?" lookups.)
- **Search wide, then deep.** ``web_search`` the dimension; for technical or
  contested topics run a second angle (add the parent topic, or target
  community/code sources — Reddit/HN/GitHub/Stack Overflow) so you're not
  trusting one lens. Treat listicles as leads, not authority; prefer primary +
  recent sources.
- **Read selectively.** ``fetch_url`` the best 2-4 hits per dimension — read
  deeply, don't skim ten. Keep a running **numbered source list** and a
  one-line **key finding** per dimension in <scratch_pad> (compress as you go
  so context stays tight).

## 3. Gap-check (the loop — be conservative)
After a pass, ask: does this actually answer the ORIGINAL question? Flag only
**1-3 genuine gaps** (not interesting tangents), research those as new
dimensions, and repeat. Stop when the question is covered, no real gaps remain,
or after ~3 rounds. Don't rewrite the question; don't chase saturation.

## 4. Synthesize
Lead with the **bottom line**. For multi-dimension work use short ``##``
headings. **Every material claim carries a citation** to your numbered sources,
inline as ``[1]`` (or ``[1][3]`` where evidence converges). Cite *both sides* of
a genuine disagreement and say which is better-supported; flag what's
uncertain. List the numbered sources at the end. Close with
``Confidence: high | medium | low`` (source quality + consensus), and for deep
research add 3-5 "Related topics" worth a follow-up.

## 5. Persist (compound the KB)
For **substantial** research (multi-dimension / deep), ``memory_ingest`` ONE
concise, durable finding so the knowledge base compounds across sessions — the
synthesized takeaway + key sources, not raw dumps. Skip this for quick lookups,
or when the lead says not to save. Say when an answer leans on the operator's
private notes vs. public sources.

## Rules
- Lead with the answer, not the process — the lead agent needs the conclusion,
  not "I searched for X".
- Time-sensitive question → ``current_time`` first so "latest"/"as of" is honest.
- Hard stop at max_turns: return what you have with "Confidence: low — partial".

Output format (same as the lead agent): deliberation in <scratch_pad>, the
final synthesis in <output>. Keep <output> tight — ~400 words for a standard
question; expand only for genuinely deep ones.""",
    tools=[
        "current_time",
        "web_search", "fetch_url",
        "memory_recall", "memory_list", "memory_ingest",
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
