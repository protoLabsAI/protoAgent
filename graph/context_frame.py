"""The injected-context frame — per-turn dynamic context in the message stream.

ADR 0101 D2 (#2776): the volatile context layer (recalled memory, skills index,
working state, one-shot toolset notices) is composed ONCE per turn and delivered
as a message in the turn's input frame — not as a second system block. Anthropic's
prompt cache is prefix-based, so a per-call system block sitting between the
cached stable prefix and the history invalidated any history caching every call;
an appended message is part of the log and caches like everything else.

Deliberately tiny and dependency-free: both writers (KnowledgeMiddleware and
ToolDeltaMiddleware — the latter must not depend on the switchable knowledge
middleware) and both readers (export/bundle rendering) import from here.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

# Marker on ``additional_kwargs``: this message is an injected frame, not
# something the operator typed. Same tagging lane as ``protoagent_turn_failed``.
CONTEXT_FRAME_KWARG = "protoagent_injected_context"


def context_frame_message(text: str) -> HumanMessage:
    """Wrap composed context as a tagged frame message.

    The ``<injected_context>`` envelope makes the role unmistakable to the model
    even without the kwarg (which providers never see); the parts inside carry
    their own framing (``<injected_memory>`` untrusted-reference header, the
    skills index, ``<working_state>``) exactly as they did in the system block —
    position changed, contract didn't (ADR 0069).
    """
    return HumanMessage(
        content=f"<injected_context>\n{text}\n</injected_context>",
        additional_kwargs={CONTEXT_FRAME_KWARG: True},
    )


def is_context_frame(message) -> bool:
    """Whether ``message`` is an injected context frame (never operator speech)."""
    return bool(getattr(message, "additional_kwargs", None) or {}) and bool(
        message.additional_kwargs.get(CONTEXT_FRAME_KWARG)
    )
