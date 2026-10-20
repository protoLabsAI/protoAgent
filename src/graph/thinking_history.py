"""Drop thinking blocks from COMPLETED prior turns at the request boundary (#4415).

Why: on Claude Opus 5.5 / Sonnet 5.5 ("preserved thinking") thinking blocks from
earlier turns are replayed and billed as input on every later request — older Claude
models dropped them server-side. A long brandLaunch thread (~9 recording jobs in one
chat) carried ~492 replayed thinking blocks, an estimated ~40% (~200k) of a 478k-token
prompt, re-sent on every tool step.

Why it is safe — and only in this shape. A thinking block's signature binds it to the
prefix that produced it plus the block before it, so the API accepts removing a LEADING
run of thinking blocks, oldest first, and rejects removing one from the middle (every
later block becomes invalid). Every block in an assistant message before the last
genuine user turn is, by construction, a leading run: the in-flight turn's blocks all
come after it and are left untouched, so the current tool loop keeps its reasoning.
A user message that carries ``tool_result`` blocks is part of the in-flight tool loop,
never a boundary — the turn's assistant tool-use messages keep their blocks.

Cache: the boundary only moves when a new user turn starts; the turn that just ended is
the one rewritten (earlier turns were already stripped), so the cached prefix up to it
survives. Between boundaries the prefix is byte-stable.

Trade-off: the model starts each turn without its earlier turns' reasoning (their text
and tool calls stay) — the pre-preserved-thinking behaviour. So it is OPT-IN:
``model.strip_prior_turn_thinking``. Copy-on-write; the checkpointed history is never
mutated.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.thinking_history")

_THINKING = frozenset({"thinking", "redacted_thinking"})


def _is_turn_boundary(message: dict) -> bool:
    """A genuine user turn: a user message with content and no ``tool_result`` block."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list) or not content:
        return False
    return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def strip_prior_turn_thinking(payload: dict) -> dict:
    """``payload`` (an Anthropic Messages body) without the thinking blocks of assistant
    messages that precede the last genuine user turn. Returns the same object when there
    is nothing to strip. An assistant message left empty is dropped (the API rejects
    empty content; adjacent same-role messages are merged by the API)."""
    if not isinstance(payload, dict):
        return payload
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    boundary = next((i for i in range(len(messages) - 1, -1, -1) if _is_turn_boundary(messages[i])), None)
    if not boundary:
        return payload

    out: list = []
    stripped = 0
    for i, msg in enumerate(messages):
        content = msg.get("content") if isinstance(msg, dict) else None
        if i >= boundary or msg.get("role") != "assistant" or not isinstance(content, list):
            out.append(msg)
            continue
        kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in _THINKING)]
        if len(kept) == len(content):
            out.append(msg)
            continue
        stripped += len(content) - len(kept)
        if kept:
            out.append({**msg, "content": kept})
    if not stripped:
        return payload
    log.debug("[thinking] stripped %d thinking block(s) from completed prior turns", stripped)
    return {**payload, "messages": out}
