"""Round governance — keep a long agentic turn coherent, not just affordable.

ADR 0101 D8 (#2710). The August 2026 audits found the cost of a runaway turn is
``rounds x per-call context floor`` — but the sharper finding was behavioral: 21
rounds into one turn, an agent violated its own persona's "check the board and
open PRs before creating ANYTHING" rule and created a duplicate work item. Round
count is an instruction-adherence lever, not just a cost lever — model attention
to standing instructions decays as its own tool transcript grows. (The cache
work, #2776/#2777, attacks the cost from the other side by making each round's
context nearly free; this middleware keeps the rounds themselves coherent.)

Two thresholds over the model rounds since the last REAL operator input:

- ``nudge_after`` (soft): inject ONE re-grounding note per turn — re-read the
  working state and the original request, re-check before creating anything new,
  prefer finishing over starting. Once per turn: the marker itself is the latch.
- ``hard_cap`` (0 = off): end the turn with an honest hand-back instead of
  running to the recursion limit — same ``jump_to: end`` mechanism as the stall
  guard. Deliberately OFF by default: ``max_iterations`` already bounds the
  loop; the hard cap is for operators who want a tighter, message-level budget.

"Real operator input" excludes machinery: injected context frames (#2776),
stall-guard / round-governor notes, and compaction summaries do not reset the
count — but genuine mid-turn steering does, because the operator just re-grounded
the agent themselves.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

log = logging.getLogger(__name__)

# Leading tag on the injected note — informative to the model, and the once-per-
# turn latch (its presence after the last real human means we already nudged).
NUDGE_MARK = "[round-governor]"

# The stall guard's own marker — its notes are machinery, never a count reset.
_STALL_MARK = "[stall-guard]"


def _is_real_human(m) -> bool:
    """A genuine operator message — the turn boundary the round count keys on."""
    if not isinstance(m, HumanMessage):
        return False
    kwargs = getattr(m, "additional_kwargs", None) or {}
    if kwargs.get("protoagent_injected_context"):  # context frame (#2776)
        return False
    if kwargs.get("lc_source") == "compaction":  # compaction summary
        return False
    content = m.content if isinstance(m.content, str) else str(m.content)
    return not content.startswith((NUDGE_MARK, _STALL_MARK))


def rounds_since_last_input(messages) -> tuple[int, bool]:
    """``(model rounds since the last real operator input, already nudged)``."""
    rounds = 0
    nudged = False
    for m in reversed(messages or []):
        if _is_real_human(m):
            break
        if isinstance(m, AIMessage):
            rounds += 1
        elif isinstance(m, HumanMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            if content.startswith(NUDGE_MARK):
                nudged = True
    return rounds, nudged


class RoundGovernorMiddleware(AgentMiddleware):
    """Soft re-grounding nudge at ``nudge_after`` rounds; optional hard cap."""

    def __init__(self, *, nudge_after: int = 25, hard_cap: int = 0):
        super().__init__()
        self._nudge_after = max(0, int(nudge_after))
        self._hard_cap = max(0, int(hard_cap))

    def _intervene(self, state) -> dict | None:
        if not self._nudge_after and not self._hard_cap:
            return None
        rounds, nudged = rounds_since_last_input(state.get("messages") or [])
        if self._hard_cap and rounds >= self._hard_cap:
            text = (
                f"I'm pausing here: this turn has run {rounds} model rounds, which is the "
                f"configured budget (model.round_hard_cap: {self._hard_cap}). Rather than keep "
                "going unsupervised, here's where things stand — tell me to continue (or "
                "narrow the task) and I'll pick up exactly where I left off."
            )
            log.warning("[round-governor] hard cap: ending the turn at %d rounds", rounds)
            return {"jump_to": "end", "messages": [AIMessage(content=text)]}
        if self._nudge_after and rounds >= self._nudge_after and not nudged:
            note = (
                f"{NUDGE_MARK} You are {rounds} model rounds into this turn. Long runs decay "
                "adherence to standing instructions, so re-ground before continuing: re-read "
                "the original request and your working state/plan; before CREATING anything "
                "new (an issue, a card, a PR), re-check whether it already exists; drop "
                "subgoals that stopped mattering; prefer FINISHING what is in flight over "
                "starting more. Then continue."
            )
            log.info("[round-governor] soft nudge at %d rounds", rounds)
            return {"messages": [HumanMessage(content=note)]}
        return None

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._intervene(state)
