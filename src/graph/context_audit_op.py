"""Context audit — what a thread's window is actually made of (#2245 family).

``audit_messages`` sizes a checkpoint's message list into the categories an operator
asks about when a thread reads "121k": assistant prose vs tool-call arguments vs tool
results vs injected memory frames — plus per-tool totals and the biggest single blocks.
Built on ``graph.message_blocks`` so tool arguments are counted exactly once (an
``AIMessage``'s ``content`` already contains the ``tool_use`` blocks; summing content
plus ``tool_calls`` double-counts — the bug that motivated this op).

Token figures are the chars//4 estimate — good to ~±10% for ranking, not billing.
The *fixed* per-call overhead (system prompt + SOUL + bound tool schemas + hot memory)
is not in any checkpoint; compute it as telemetry's per-turn ``context_tokens`` minus
this op's ``total_est_tokens`` (the CLI in ``scripts/context_audit.py`` does exactly
that join).
"""

from __future__ import annotations

import json

from graph.message_blocks import role_of, text_of, tool_calls_of

# Marker of a memory-delivery frame (ADR 0069) — an injected HumanMessage, not operator prose.
_INJECTED_MARKER = "<injected_context>"
_PREVIEW_CHARS = 80


def _est_tokens(text: str) -> int:
    return len(text) // 4


def audit_messages(messages: list, *, top_n: int = 15) -> dict:
    """Size ``messages`` into an audit breakdown. Pure — no I/O, no host state.

    Returns ``{total_est_tokens, message_count, categories, tool_call_args,
    tool_results, top_blocks}`` where ``categories`` maps category → est tokens,
    ``tool_call_args`` maps tool name → est tokens (args, counted once),
    ``tool_results`` maps tool name → {est_tokens, calls}, and ``top_blocks`` is the
    ``top_n`` largest single blocks as {est_tokens, kind, preview}.
    """
    categories: dict[str, int] = {}
    call_args: dict[str, int] = {}
    results: dict[str, dict] = {}
    blocks: list[dict] = []

    def _bump(cat: str, n: int) -> None:
        categories[cat] = categories.get(cat, 0) + n

    def _block(n: int, kind: str, sample: str) -> None:
        blocks.append({"est_tokens": n, "kind": kind, "preview": sample[:_PREVIEW_CHARS]})

    for m in messages:
        role = role_of(m)
        text = text_of(m)
        if role in ("human", "user"):
            n = _est_tokens(text)
            if _INJECTED_MARKER in text:
                _bump("injected_context_frames", n)
                _block(n, "injected frame", text)
            else:
                _bump("operator_messages", n)
                _block(n, "operator message", text)
        elif role in ("ai", "assistant"):
            n = _est_tokens(text)
            _bump("assistant_text", n)
            if n:
                _block(n, "assistant text", text)
            for call in tool_calls_of(m):
                name = str(call.get("name") or "?")
                try:
                    args = json.dumps(call.get("args") or {})
                except (TypeError, ValueError):
                    args = str(call.get("args"))
                an = _est_tokens(args)
                call_args[name] = call_args.get(name, 0) + an
                _bump("tool_call_args", an)
                _block(an, f"call {name}", args)
        elif role == "tool":
            name = str(getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else None) or "?")
            n = _est_tokens(text)
            row = results.setdefault(name, {"est_tokens": 0, "calls": 0})
            row["est_tokens"] += n
            row["calls"] += 1
            _bump("tool_results", n)
            _block(n, f"result {name}", text)
        else:
            _bump(role or "other", _est_tokens(text))

    blocks.sort(key=lambda b: -b["est_tokens"])
    return {
        "total_est_tokens": sum(categories.values()),
        "message_count": len(messages),
        "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "tool_call_args": dict(sorted(call_args.items(), key=lambda kv: -kv[1])),
        "tool_results": dict(sorted(results.items(), key=lambda kv: -kv[1]["est_tokens"])),
        "top_blocks": blocks[:top_n],
    }
