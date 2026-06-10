# Output protocol

The template instructs the model to wrap its response as:

```
<scratch_pad>
Internal reasoning — which tools to call, what you're learning from
each result, how you'll assemble the answer. Not shown to the user.
</scratch_pad>
<output>
The user-facing answer. Clean, scannable, markdown-formatted.
</output>
```

Server-side, `graph/output_format.py::extract_output` parses tags and forwards only the `<output>` content to consumers (A2A artifacts, the OpenAI-compatible `/v1` endpoint and the console's non-streaming fallback, subagent return values).

## Why at all

Claude and other strong models produce much better final answers when they can "think out loud" mid-response. But consumers — especially A2A callers whose artifacts land directly in human UIs — don't want to see reasoning. Showing internal deliberation:

- Doubles payload size
- Leaks guesses, dead ends, tool-call decisions that didn't pan out
- Makes it hard to programmatically consume the "real" answer

Stripping reasoning at the boundary fixes this without sacrificing the quality-from-thinking gain.

## Why tag-based, not structured output

LangChain's structured-output features work, but they constrain the model to a JSON schema. That's right for "extract these fields from this document". It's wrong for "reason about the user's question and emit free-form markdown".

Tags are cheap for the model to produce, robust to streaming (every model can emit raw text reliably), and trivial to strip regex-style server-side.

## Why `<scratch_pad>` and `<output>` specifically

Empirically, named tags outperform unnamed delimiters with strong models. Claude and GPT-4-class models handle `<scratch_pad>` / `<output>` fluently; `<thinking>` / `<answer>` also works but is more likely to leak through literal markers ("Here's my thinking:" in the user-facing text).

`<scratch_pad>` is vague enough that the model freely uses it for tool-call reasoning, intermediate summarization, and planning. `<output>` is clear enough that the model knows it's the final artifact the user will see.

## Why not parse mid-stream

A tempting optimization: parse `<scratch_pad>` / `<output>` as tokens arrive and only stream the `<output>` content to consumers in real time.

This was tried. It became a state-machine rabbit hole:

- Tag markers span chunk boundaries (`<scr` + `atch_pad>`)
- The model occasionally opens `<output>` without closing it, then re-opens it
- Models emit stray `</output>` inside what was supposed to be scratch
- Per-token rendering to consumers turned out to add no real value — the cost-of-waiting for the full response is ~seconds, not minutes

The template's current design is simpler and correct:

1. The A2A handler streams `status-update` frames (tool-start, tool-end) mid-run so consumers see progress.
2. Tokens accumulate silently. No mid-stream parsing.
3. On the terminal `done` frame, `extract_output` runs once on the complete text.
4. The cleaned output lands in the terminal artifact.

Consumers see real-time tool progress and the clean final answer. They don't see token-by-token streaming of the model's markdown, but that's a UI polish trade-off almost no consumer cares about.

## Why `_strip_reasoning` handles multiple tag families

In addition to `<scratch_pad>`, the function strips `<think>` / `</think>` and orphaned opens of both. This is because:

- MiniMax (via LiteLLM) occasionally leaks raw `<think>` tags into the user-visible content (LiteLLM bug #22392).
- Other models (especially reasoning-tuned variants) produce `<think>` natively even when the prompt asks for `<scratch_pad>`.
- A mid-stream crash can leave a scratch_pad opened but never closed — rendering that raw is worse than stripping it.

Being a bit over-eager about stripping is safer than leaving reasoning in the output.

## What happens if the model ignores the protocol

`extract_output`'s fallback path: if `<output>` tags aren't found, return the full text with reasoning markers stripped. If the model didn't use any protocol tags at all, the full text is returned as-is.

The system prompt nudges strong models toward the protocol consistently enough that this fallback fires only when something unusual happens (very short responses, clarification questions, model fallback paths). Not a common case, but having the fallback keeps the agent functional.

## Related

- [Architecture](/explanation/architecture) — where in the runtime this parsing lives
- [`graph/output_format.py`](https://github.com/protoLabsAI/protoAgent/blob/main/graph/output_format.py) — the actual implementation
