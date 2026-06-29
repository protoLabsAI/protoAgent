# Model output & the leaked-reasoning guard

The model **answers naturally**. Its reasoning streams on the gateway's native
`reasoning_content` channel (lifted by `graph.llm._ReasoningChatOpenAI`, surfaced as
`reasoning` frames by `server.chat`), and its answer is plain text/markdown. There is
**no `<scratch_pad>`/`<output>` text protocol** — the model is never told to wrap its
response in tags.

Server-side, `graph/output_format.py::extract_output` is now just a thin pass-through
that strips any *leaked* reasoning before the answer reaches consumers (A2A artifacts,
the OpenAI-compatible `/v1` endpoint, the console, subagent return values).

## What survives: the leaked-reasoning guard

`strip_reasoning` (and the `extract_output` that wraps it) removes balanced or
truncated `<think>` / `<scratch_pad>` blocks from the answer **content** channel. Two
reasons it still earns its keep:

- **Provider leaks.** Some gateway/model combos leak raw `<think>…</think>` into the
  content channel instead of `reasoning_content` — notably MiniMax via
  [LiteLLM #22392](https://github.com/BerriAI/litellm/issues/22392). Model selection is
  gateway config, not code, so the runtime can't guarantee a clean channel; a cheap,
  idempotent strip at the boundary is the insurance.
- **Storage guardrail (ADR 0021).** Leaked reasoning must never persist to the
  knowledge base or session memory. `strip_reasoning` is the guard the memory/knowledge
  write paths call before storing assistant text.

The strip is **backtick-guarded**: an answer that *mentions* a tag in inline code
(`` `<think>` ``) is left intact — only a genuine leak (an unbacktick-ed tag) is removed.

## History: the retired `<scratch_pad>`/`<output>` protocol

Earlier versions instructed the model to wrap deliberation in `<scratch_pad>` and the
final answer in `<output>`, and `extract_output` parsed the `<output>` block out. That
made sense before models exposed a native reasoning channel — it let strong models
"think out loud" without the reasoning reaching the user.

It was retired once native reasoning landed:

- **#1328** dropped the protocol for the **lead agent** (native `reasoning_content`).
- A follow-up de-protocoled the **subagents** (they now answer naturally too).
- The parser retirement then deleted the `<output>`-extraction, the dropped-turn
  "kicker" retry, the streaming `<output>` views, and the `<confidence>` self-report —
  none of which had a live producer anymore — leaving only the leaked-reasoning guard
  above.

Native reasoning is strictly better here: the channel separation is structural (no tag
spanning chunk boundaries, no unclosed `<output>`, no stray `</output>` in scratch), and
the console renders the model's real thinking above the answer with zero parsing.

## Related

- [Architecture](/explanation/architecture) — where in the runtime this lives
- [`graph/output_format.py`](https://github.com/protoLabsAI/protoAgent/blob/main/graph/output_format.py) — the implementation (now ~65 lines)
