"""SteeringMiddleware (spike) — fold queued mid-turn user input into a running turn.

``before_model`` runs before every model call. Here it drains the per-session
steering queue (graph/steering.py) and, if the user injected anything while the
turn was working, appends it as HumanMessage(s) to the thread — so the model sees
the redirection on its very next step. Because it runs before the *next* model
call (i.e. right after a tool result comes back), the new input lands exactly at
the "next tool-call boundary" the user expects, without the stream being stopped.

No-op on a turn with an empty queue, so it never affects a normal turn. The queue
is keyed by the turn's ``session_id`` state channel (state_schema=ProtoAgentState).
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from graph import steering

# Frame the injected text so the model reads it as a mid-task interjection (it may
# redirect, or continue if it doesn't change the task) rather than a fresh request.
# The wording mirrors the convergent comp pattern — Claude Code's h2A queue and
# protoCLI's handleCompletedTools both wrap mid-turn input this way — validated in
# the steering due-diligence.
_INTERJECTION = (
    "[User message received while you were working — address it now if it changes the "
    "task, otherwise acknowledge briefly and keep going with your current work]\n\n"
)


class SteeringMiddleware(AgentMiddleware):
    """Inject queued mid-turn user messages before the next model call."""

    def before_model(self, state, runtime):  # type: ignore[override]
        return self._inject(state)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._inject(state)

    @staticmethod
    def _inject(state) -> dict | None:
        session_id = state.get("session_id") if isinstance(state, dict) else getattr(state, "session_id", None)
        if not session_id:
            return None
        queued = steering.drain(session_id)
        if not queued:
            return None
        # One framed HumanMessage carrying all queued text. The add_messages reducer
        # appends it to the thread before the model node runs, so the very next model
        # call sees it. The console settles the user's ORIGINAL text into the thread
        # (per-id); only the model sees the framed interjection.
        combined = "\n\n".join(item["text"] for item in queued)
        return {"messages": [HumanMessage(content=_INTERJECTION + combined)]}
