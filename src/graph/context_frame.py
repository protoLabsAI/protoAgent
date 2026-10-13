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

import contextvars

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


# ---------------------------------------------------------------------------
# Projected-context stash (ADR 0108 D2, #3191; task-boundary fix #3250)
# ---------------------------------------------------------------------------
# KnowledgeMiddleware (inner) stashes what it projected; PromptCaptureMiddleware
# (outer) pops it after the handler returns.
#
# The two are NOT in the same call stack: the outer awaits a handler that runs the
# inner middleware in a CHILD asyncio task. A ContextVar set() there mutates the
# child's copy of the context and is invisible to the parent that awaited it, so
# the original design recorded nothing in production while passing every same-stack
# unit test (#3250 — 6267 chars stashed, popped as None, on a live turn).
#
# So the parent owns a mutable HOLDER: it opens a frame before calling the handler,
# and the child MUTATES that object rather than rebinding the var. Mutation of a
# shared object crosses the task boundary; rebinding never does. Two concurrent
# turns still can't cross wires — each parent opens its own holder, and each child
# inherits only its own parent's.

_PROJECTED_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "protoagent_projected_context", default=None
)


def open_projection_frame() -> contextvars.Token:
    """Open a capture frame; the caller MUST pass the token back to
    ``pop_projected_context`` so a nested capture can't strand the outer one."""
    return _PROJECTED_CONTEXT.set({"text": "", "sections": None})


def stash_projected_context(text: str, sections: list[dict] | None = None) -> None:
    """Called by KnowledgeMiddleware._project_messages inside wrap_model_call.

    A no-op when nobody opened a frame (capture disabled, or a call outside the
    capture middleware) — better a dropped record than a stash that leaks into
    whichever unrelated call pops next.
    """
    frame = _PROJECTED_CONTEXT.get()
    if frame is None:
        return
    frame["text"] = f"{frame['text']}\n\n{text}" if frame["text"] else text
    if frame["sections"] is None:
        frame["sections"] = sections


def pop_projected_context(token: contextvars.Token | None = None) -> tuple[str | None, list[dict] | None]:
    """Called by PromptCaptureMiddleware._capture after the handler returns.

    ``token`` restores the previous frame (a subagent's capture nests inside its
    parent's); without one the frame is simply cleared.
    """
    frame = _PROJECTED_CONTEXT.get()
    if token is not None:
        _PROJECTED_CONTEXT.reset(token)
    else:
        _PROJECTED_CONTEXT.set(None)
    if not frame or not frame["text"]:
        return None, None
    return frame["text"], frame["sections"]
