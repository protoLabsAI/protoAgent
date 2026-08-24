"""RoomCastMiddleware — tell the lead agent who is in this chat (#3049, the deliberate cut).

A participant is someone with **conversational state in this thread**: they have spoken
here (a ``room``-stamped message from #3042's `@` addressing / delegation record), so the
room will catch them up on everything since, the next time anyone addresses them. That is
what "in the chat" truthfully means in this system — presence-as-context. Nothing listens
passively; an agent acts only when addressed or delegated to.

The injected line is **awareness, never permission**: it names who is already caught up,
so the lead prefers them for follow-ups for the same reason an operator would — they know
the thread — while every delegate remains reachable. A membership list that *gated*
``delegate_to`` was considered and rejected as confusion by construction: either it
fences tools the roster says exist, or it doesn't and the list is a control without
teeth.

Mechanics: the cast is derived from the thread's own ``room`` stamps at each model call
and delivered via ``wrap_model_call`` as a system-prompt suffix — ephemeral, recomputed,
never checkpointed, so it cannot drift from the transcript (the failure mode that sank
the keystroke-tracked participant list). Appended as the LAST system block so the stable
prompt prefix stays cache-friendly; the cast changes only when someone new speaks.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

# Tagged so the block is recognizable in prompt snapshots (and greppable in captures).
_CAST_MARK = "[room]"


def participants(messages) -> list[str]:
    """Who has SPOKEN in this thread, in first-spoken order.

    Read from the structural ``room`` stamps, never parsed out of envelope text. The
    operator is excluded (always present); a failed address is excluded — the delegate
    never received anything, so it holds no context and listing it would claim presence
    it doesn't have.
    """
    seen: list[str] = []
    for m in messages or []:
        kwargs = getattr(m, "additional_kwargs", None) or {}
        room = kwargs.get("room")
        if not isinstance(room, dict) or room.get("failed"):
            continue
        speaker = str(room.get("from") or "").strip()
        if speaker and speaker not in ("operator", "assistant") and speaker not in seen:
            seen.append(speaker)
    return seen


def cast_line(names: list[str], *, lead_name: str = "") -> str:
    """The awareness note. Short — it rides every model call — but the identity fence
    is load-bearing: delegate names are often SIMILAR to the lead's own name (a
    delegate `proto` on an agent named `protoagent` made the lead answer a status
    question AS the delegate, in first person). The line therefore says outright who
    the reader is, that the cast are other agents regardless of name similarity, and
    what to do when asked ABOUT one — report or ask them, never impersonate."""
    listed = ", ".join(names)
    you = f"You are {lead_name}, " if lead_name else "You are "
    return (
        f"{_CAST_MARK} {you}the lead agent of this chat. Also in this chat, having "
        f"spoken here: {listed}. They are OTHER agents — distinct from you and from the "
        "operator, even where names look similar; `<room-message from=\"X\">` marks what "
        "X said. They are already caught up on this conversation, so prefer them for "
        "follow-ups on it — awareness, not permission; delegate to whoever fits. When "
        "the operator asks ABOUT one of them, answer about them or ask them via "
        "delegate_to — never answer AS them."
    )


class RoomCastMiddleware(AgentMiddleware):
    """Append the cast line as the final system block when the thread has a cast."""

    def __init__(self, *, lead_name: str = ""):
        super().__init__()
        self._lead_name = str(lead_name or "").strip()

    def _transform(self, request):
        names = participants(getattr(request, "messages", None))
        if not names:
            return request
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            return request  # create_agent always supplies one; nothing safe to attach to
        block = {"type": "text", "text": cast_line(names, lead_name=self._lead_name)}
        content = getattr(sysmsg, "content", None)
        if isinstance(content, str):
            blocks = ([{"type": "text", "text": content}] if content else []) + [block]
        elif isinstance(content, list):
            # Idempotence: replace a previous cast block rather than stacking them.
            blocks = [b for b in content if not (isinstance(b, dict) and str(b.get("text", "")).startswith(_CAST_MARK))]
            blocks = blocks + [block]
        else:
            return request
        return request.override(system_message=sysmsg.model_copy(update={"content": blocks}))

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
