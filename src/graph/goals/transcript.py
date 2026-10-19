"""The tool-call digest the goal verifier reads (``VerifyContext.tool_summary``).

The ``llm`` verifier judges a goal on "concrete evidence" — but it was only ever handed
the turn's final TEXT: nothing built ``tool_summary``, so every judgment saw
``Recent tool calls: (none)`` and a goal whose evidence lives in tool results
(``write_note`` → ``read_note`` showing three lines) could never pass. It looped to its
iteration budget and was marked unachievable.

:func:`summarize_tool_calls` renders the CURRENT turn's tool calls — each call's name and
arguments paired with its result — from the thread's real LangChain messages. "Current
turn" is everything after the last operator/kickoff/continuation ``HumanMessage``; the
mid-turn human messages middleware injects (guard notes, steering interjections) don't
end it. Bounded: each argument/result is clipped, and only the most recent calls are kept.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

# Per-call clip lengths and the overall budget (most recent calls win).
_ARGS_CAP = 300
_RESULT_CAP = 400
_TOTAL_CAP = 6000
_MAX_CALLS = 40

_GUARD_NOTE_KWARG = "protoagent_guard_note"  # graph.middleware.guard_notes.GUARD_NOTE_KWARG
_STEERING_PREFIX = "[User message received while you were working"  # graph.middleware.steering


def _clip(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def message_text(content: Any) -> str:
    """A message content (str, or a list of content blocks) as plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _is_turn_boundary(msg: BaseMessage) -> bool:
    """A human message that starts a turn (not one middleware injected mid-turn)."""
    if not isinstance(msg, HumanMessage):
        return False
    if (msg.additional_kwargs or {}).get(_GUARD_NOTE_KWARG):
        return False
    return not message_text(msg.content).startswith(_STEERING_PREFIX)


def _calls_of(msg: AIMessage) -> list[tuple[str, str, Any]]:
    """``(id, name, args)`` for each tool call on an AI message — ``tool_calls`` first,
    else provider content blocks (``tool_use`` / ``function_call``) a provider left there."""
    calls = [(str(c.get("id") or ""), str(c.get("name") or "?"), c.get("args")) for c in (msg.tool_calls or [])]
    if calls:
        return calls
    if isinstance(msg.content, list):
        for block in msg.content:
            if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_call", "function_call"):
                args = block.get("input", block.get("args", block.get("arguments")))
                calls.append((str(block.get("id") or block.get("call_id") or ""), str(block.get("name") or "?"), args))
    return calls


def _render_args(args: Any) -> str:
    if args in (None, "", {}):
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def summarize_tool_calls(messages: list | None) -> str:
    """The current turn's tool calls and their results, one line each; ``""`` for none."""
    msgs = list(messages or [])
    start = 0
    for i in range(len(msgs) - 1, -1, -1):
        if _is_turn_boundary(msgs[i]):
            start = i + 1
            break
    turn = msgs[start:]
    results: dict[str, ToolMessage] = {
        str(m.tool_call_id): m for m in turn if isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None)
    }
    lines: list[str] = []
    for msg in turn:
        if not isinstance(msg, AIMessage):
            continue
        for call_id, name, args in _calls_of(msg):
            line = f"- {name}({_clip(_render_args(args), _ARGS_CAP)})"
            res = results.get(call_id)
            if res is None:
                line += " → (no result)"
            else:
                status = " [error]" if getattr(res, "status", "") == "error" else ""
                line += f" →{status} {_clip(message_text(res.content), _RESULT_CAP) or '(empty)'}"
            lines.append(line)
    dropped = max(0, len(lines) - _MAX_CALLS)
    lines = lines[dropped:]
    while lines and sum(len(x) + 1 for x in lines) > _TOTAL_CAP:
        lines.pop(0)
        dropped += 1
    head = [f"(… {dropped} earlier call(s) omitted)"] if dropped else []
    return "\n".join(head + lines)
