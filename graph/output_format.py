"""Structured output protocol for protoAgent — `<scratch_pad>` / `<output>` tags.

The model is instructed to wrap internal deliberation in ``<scratch_pad>``
and the user-facing answer in ``<output>``. Server-side, we parse those
tags and forward only the ``<output>`` content to consumers (A2A
artifacts, the console + OpenAI-compat chat, subagent return values).

We deliberately do NOT parse the protocol mid-stream — chunk-boundary
tag splitting turned that into a state-machine rabbit hole and the
per-token text rendering consumers were doing didn't add real value.
Instead, ``_chat_langgraph_stream`` accumulates the model's tokens
silently while still emitting tool-start / tool-end status events, then
passes the complete text through ``extract_output`` once on the
terminal ``done`` frame. The consumer sees tool progress during the run
and the clean final artifact at completion.

``_strip_reasoning`` also removes provider-emitted ``<think>...</think>``
regions (LiteLLM bug #22392 leaks these as raw tags from MiniMax) and
any orphaned scratch_pad / think openings.

The prompt fragment that teaches the protocol to the model lives in
``OUTPUT_FORMAT_INSTRUCTIONS`` below; ``graph.prompts`` appends it to
both the lead agent and subagent system prompts.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("protoagent.output_format")

OUTPUT_FORMAT_INSTRUCTIONS = """
# Response format

Structure every response as:

    <scratch_pad>
    Internal reasoning — which tools to call, what you're learning from
    each result, how you'll assemble the final answer. This is not shown
    to the user; use it freely to think.
    </scratch_pad>
    <output>
    The user-facing answer. This is what lands in the A2A artifact /
    Discord / the console chat. Be clean, scannable, markdown-formatted.
    </output>

Rules:
- Always emit both tags, in that order, exactly once.
- Never include literal `<scratch_pad>` or `<output>` markers inside the
  user-facing content.
- Keep tool-calling deliberation in `<scratch_pad>`. Keep only the
  finished, customer-ready answer in `<output>`.
- If you must defer or ask for clarification, put the question inside
  `<output>` too — the user never sees `<scratch_pad>`.

## When the task needs tools: act first, then answer

`<output>` is your FINAL result, written AFTER the work is done — never a
"working on it" placeholder. While you still have tools to call you have not
reached `<output>` yet: keep the running narration ("checking the board",
"auto-mode is off, starting it") in `<scratch_pad>`. Emit `<output>` once, on
your last turn, holding the complete result. A turn that calls a tool *and*
writes a progress line into `<output>` burns your one `<output>` on "doing it
now" — the user then sees only that and never the actual result. So: narrate in
scratch_pad across every intermediate step; produce `<output>` only when you
have the answer.

### Example — "sweep the board and get it moving"

    <scratch_pad>
    Plan: check auto-mode, list features, decide, then summarize.
    [get_auto_mode_status → OFF] [list_features → 6 ready, 0 running]
    Ready work + no agent → start it. [start_auto_mode → ok, 1 agent now running]
    Now I have the result; write the roll-up.
    </scratch_pad>
    <output>
    **protocli — ✓ now flowing.** Auto-mode was OFF with 6 ready features and no
    agent; I started it. 1 agent now running on the streaming-timeout fix.
    </output>

The narration ("check auto-mode", "start it") stayed in scratch_pad; `<output>`
is the single finished summary — not "Sweeping the board now…".

Optionally, after `</output>`, you may self-report confidence:

    <confidence>0.85</confidence>
    <confidence_explanation>one short sentence on what drove the score</confidence_explanation>

- `<confidence>` is a number in [0, 1] — your honest self-assessment of
  whether the answer is correct/complete. Omit it if you'd only be guessing.
- `<confidence_explanation>` is optional. Neither tag is shown to the user;
  they ride a confidence-v1 DataPart on the A2A artifact.
""".strip()


# Neither the opening nor closing tag may be preceded by a backtick. A reply
# (or its scratch_pad reasoning) often names the protocol in inline code —
# ``answer in `<output>` format``, ``confidence after `</output>` ``. Without
# the guards the matcher would open on a backticked `<output>` mention inside
# the scratch_pad (leaking reasoning) or close on a backticked `</output>`
# (truncating the answer). The real tags are never backtick-wrapped.
_OUTPUT_RE = re.compile(r"(?<!`)<output>([\s\S]*?)(?<!`)</output>", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"<scratch_pad>[\s\S]*?</scratch_pad>", re.IGNORECASE)
# Orphan eat-to-end (truncation recovery). Backtick-guarded like _OUTPUT_RE: a
# plain answer that *mentions* the protocol in inline code (e.g. ``I reason in
# `<scratch_pad>` then write `<output>` ``) must not be eaten to end-of-text on
# the no-<output>-wrapper fallback tiers. A real (un-backticked) orphan tag — a
# genuinely truncated/leaked reasoning block — still gets stripped.
_ORPHAN_SCRATCH_OPEN_RE = re.compile(r"(?<!`)<scratch_pad>[\s\S]*$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_ORPHAN_THINK_OPEN_RE = re.compile(r"(?<!`)<think>[\s\S]*$", re.IGNORECASE)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>\s*", re.IGNORECASE)
_CONFIDENCE_BLOCK_RE = re.compile(r"<confidence>[\s\S]*?</confidence>", re.IGNORECASE)
_CONFIDENCE_EXPL_BLOCK_RE = re.compile(
    r"<confidence_explanation>[\s\S]*?</confidence_explanation>",
    re.IGNORECASE,
)
_CONFIDENCE_RE = re.compile(r"<confidence>\s*(-?[\d.]+)\s*</confidence>", re.IGNORECASE)
_CONFIDENCE_EXPLANATION_RE = re.compile(
    r"<confidence_explanation>([\s\S]*?)</confidence_explanation>",
    re.IGNORECASE,
)


def _strip_reasoning(text: str) -> str:
    """Remove all reasoning markers (``<think>``, ``<scratch_pad>``, and
    orphaned variants) from a complete response.

    Idempotent — real user content should never contain literal tag
    markers, so applying this twice is safe.
    """
    text = _THINK_RE.sub("", text)
    text = _ORPHAN_THINK_OPEN_RE.sub("", text)
    text = _ORPHAN_THINK_CLOSE_RE.sub("", text)
    text = _SCRATCH_RE.sub("", text)
    text = _ORPHAN_SCRATCH_OPEN_RE.sub("", text)
    # Confidence tags ride a DataPart, never the user-facing text. Strip them
    # in case the model emits them inside (or right after) <output>.
    text = _CONFIDENCE_EXPL_BLOCK_RE.sub("", text)
    text = _CONFIDENCE_BLOCK_RE.sub("", text)
    return text


def strip_reasoning(text: str) -> str:
    """Public reasoning-stripper for *storage* guardrails (ADR 0021).

    Removes leaked ``<scratch_pad>`` / ``<think>`` / confidence markers but
    keeps everything else intact — unlike ``extract_output``, it does NOT pull
    out only the ``<output>`` block, so a stored note that legitimately mentions
    the protocol isn't reshaped. The contract: the model's internal reasoning
    must never be persisted to the knowledge base. Idempotent.
    """
    return _strip_reasoning(text or "")


def _strip_reasoning_balanced(text: str) -> str:
    """Strip only *balanced* reasoning blocks — no orphan eat-to-end variants.

    For content inside a properly-closed ``<output>...</output>`` block, where
    the text is the finished answer. The orphan strippers
    (``<scratch_pad>[\\s\\S]*$``) would treat a literal tag *mention* in the
    answer — e.g. the agent describing its own protocol ("I think in
    ``<scratch_pad>`` then write ``<output>``") — as real leaked reasoning and
    delete everything from that point to the end, silently truncating the
    reply. A closed ``<output>`` can't have been truncated mid-reasoning, so
    only balanced blocks are stripped here; the orphan eaters stay reserved for
    the truncation-recovery tiers below.
    """
    text = _THINK_RE.sub("", text)
    text = _ORPHAN_THINK_CLOSE_RE.sub("", text)
    text = _SCRATCH_RE.sub("", text)
    text = _CONFIDENCE_EXPL_BLOCK_RE.sub("", text)
    text = _CONFIDENCE_BLOCK_RE.sub("", text)
    return text


_ORPHAN_OUTPUT_OPEN_RE = re.compile(r"(?<!`)<output>([\s\S]*)$", re.IGNORECASE)


def stream_visible_output(raw: str) -> str:
    """The portion of the user-facing ``<output>`` that's safe to show mid-stream.

    Given a *partial* (still-streaming) raw response, returns only the text
    inside the first (possibly still-open) ``<output>`` block, with reasoning
    stripped and any partial trailing tag held back — so a half-written
    ``</output>`` or ``<confidence>`` never flashes on screen. Returns ``""``
    while the model is still in ``<scratch_pad>`` (before ``<output>`` opens),
    so internal reasoning is never streamed.

    This is the incremental counterpart to ``extract_output``: callers stream
    the growing prefix of this, and the terminal artifact (full
    ``extract_output``) reconciles any held-back tail at the end. Monotonic —
    as ``raw`` grows, the result only ever extends (until ``</output>`` closes
    it), so a caller can emit ``result[already_emitted:]`` each step.
    """
    # Open on the first real <output> — skip backticked mentions in the
    # scratch_pad (e.g. ``answer in `<output>` format``) so reasoning that
    # names the tag never starts the stream early.
    open_m = re.search(r"(?<!`)<output>", raw, re.IGNORECASE)
    if open_m is None:
        return ""  # still in scratch_pad — nothing user-facing yet
    after = raw[open_m.end() :]
    # Close on the first real </output> — skip backtick-wrapped literal mentions
    # (`` `</output>` ``) so a self-describing answer isn't cut short.
    m = re.search(r"(?<!`)</output>", after, re.IGNORECASE)
    if m:
        after = after[: m.start()]
    # Strip provider reasoning that can appear inside the output region.
    after = _THINK_RE.sub("", after)
    after = _ORPHAN_THINK_OPEN_RE.sub("", after)
    # Hold back a partial trailing tag ("</outp", "<conf", a lone "<") so it
    # never flashes; the terminal replace delivers the full, clean text.
    lt = after.rfind("<")
    if lt != -1 and ">" not in after[lt:]:
        after = after[:lt]
    return after


# Reasoning regions for live "thinking" display — the protocol scratch_pad and any
# provider <think> block. Match open→content (with no nested reasoning tag) → close.
_REASONING_BLOCK_RE = re.compile(r"(?<!`)<(scratch_pad|think)>([\s\S]*?)</\1>", re.IGNORECASE)
# A still-open trailing reasoning block (content runs to the end, no close yet).
_REASONING_OPEN_TAIL_RE = re.compile(
    r"(?<!`)<(scratch_pad|think)>((?:(?!</?(?:scratch_pad|think)>)[\s\S])*)$", re.IGNORECASE
)


def stream_visible_reasoning(raw: str) -> str:
    """The model's reasoning so far — for a live, collapsible "thinking" view.

    The counterpart to :func:`stream_visible_output`, but for the *hidden* side:
    concatenates every ``<scratch_pad>`` / ``<think>`` block seen so far (so
    multi-step turns show all their deliberation), plus any still-open trailing
    block with a partial tag held back. Monotonic as ``raw`` grows, so a caller
    can stream ``result[already_emitted:]`` each step. Empty until the first
    reasoning tag opens.
    """
    chunks: list[str] = []
    for m in _REASONING_BLOCK_RE.finditer(raw):
        body = m.group(2).strip()
        if body:
            chunks.append(body)
    tail = _REASONING_OPEN_TAIL_RE.search(raw)
    if tail:
        body = tail.group(2)
        # Hold back a partial trailing tag ("</scr", a lone "<") so it never flashes.
        lt = body.rfind("<")
        if lt != -1 and ">" not in body[lt:]:
            body = body[:lt]
        body = body.strip()
        if body:
            chunks.append(body)
    return "\n\n".join(chunks)


# Compiled openers for the incremental streaming views below. The lookbehind skips a
# backticked mention (matching the pure functions); `Pattern.search(raw, pos)` keeps the
# FULL string visible to the lookbehind, so backing the search start up by the tag length
# catches a tag that straddles a chunk boundary without a slice false-positive.
_OUTPUT_OPEN_RE = re.compile(r"(?<!`)<output>", re.IGNORECASE)
_REASONING_OPEN_RE = re.compile(r"(?<!`)<(?:scratch_pad|think)>", re.IGNORECASE)


class StreamingOutputView:
    """Incremental, amortized-O(total) equivalent of :func:`stream_visible_output`.

    ``stream_visible_output`` re-scans the ENTIRE accumulated text every chunk, so a turn
    streaming N chars over ~N chunks costs O(N²) (#1310). This keeps the same contract —
    feed it the monotonically growing accumulated raw, get the full visible-so-far back —
    but only looks at the newly-appended tail in the common cases:

      * before ``<output>`` opens, it scans only the tail for the opener (the
        ``<scratch_pad>`` is never re-scanned), returning ``""`` meanwhile;
      * once ``<output>`` is open with no ``<`` in the output region (the steady
        answer-body stream), it just appends the delta.

    Anything ambiguous — a ``<`` that may begin ``</output>`` / ``<think>`` /
    ``<confidence>`` / a partial tag, or the closed state — falls back to the
    authoritative ``stream_visible_output``, the oracle the equivalence test pins this
    against. Construct one per turn; call :meth:`update` per chunk.
    """

    __slots__ = ("_opened", "_fast", "_prev_len", "_visible")

    def __init__(self) -> None:
        self._opened = False
        self._fast = False
        self._prev_len = 0
        self._visible = ""

    def update(self, raw: str) -> str:
        delta_start = self._prev_len
        self._prev_len = len(raw)
        # Steady answer-body stream: open, unclosed, no `<` in the region — the visible
        # simply grows by a delta that adds no tag-significant character.
        if self._fast and "<" not in raw[delta_start:]:
            self._visible += raw[delta_start:]
            return self._visible
        # Pre-output: scan only the tail for the opener (never re-scan the scratch_pad).
        if not self._opened:
            start = max(0, delta_start - 8)  # catch a "<output>" split across the boundary
            if _OUTPUT_OPEN_RE.search(raw, start) is None:
                return ""  # still in scratch_pad — nothing user-facing yet
            self._opened = True
        # Authoritative recompute (close / <think> / partial-tag handling), then decide
        # whether the next chunks can take the fast path.
        self._visible = stream_visible_output(raw)
        m = _OUTPUT_OPEN_RE.search(raw)
        self._fast = m is not None and "<" not in raw[m.end() :]
        return self._visible


class StreamingReasoningView:
    """Incremental, amortized-O(total) equivalent of :func:`stream_visible_reasoning`.

    Same idea as :class:`StreamingOutputView` for the hidden "thinking" stream: only the
    open trailing reasoning block grows on a plain delta (closed blocks are committed and
    not re-joined; ``str.strip`` scans only end-whitespace, not the whole body). A ``<``
    (a block open/close, a nested or partial tag) drops to the authoritative
    ``stream_visible_reasoning`` — the oracle the equivalence test pins this against.
    """

    __slots__ = ("_opened", "_fast", "_prev_len", "_committed", "_open_raw", "_visible")

    def __init__(self) -> None:
        self._opened = False
        self._fast = False
        self._prev_len = 0
        self._committed = ""  # joined, stripped bodies of CLOSED reasoning blocks
        self._open_raw = ""  # raw body of the open trailing block (fast mode only)
        self._visible = ""

    def _join(self, body: str) -> str:
        if not body:
            return self._committed
        if not self._committed:
            return body
        return self._committed + "\n\n" + body

    def update(self, raw: str) -> str:
        delta_start = self._prev_len
        self._prev_len = len(raw)
        # Steady deliberation: inside one open trailing block, delta adds no tag char —
        # extend that block's body and re-join.
        if self._fast and "<" not in raw[delta_start:]:
            self._open_raw += raw[delta_start:]
            self._visible = self._join(self._open_raw.strip())
            return self._visible
        # Pre-reasoning: cheap tail-scan for the first scratch_pad/think opener.
        if not self._opened:
            start = max(0, delta_start - 13)  # catch a "<scratch_pad>" split across the boundary
            if _REASONING_OPEN_RE.search(raw, start) is None:
                return ""
            self._opened = True
        # Authoritative recompute, then refresh fast-mode state.
        self._visible = stream_visible_reasoning(raw)
        self._committed = "\n\n".join(b for m in _REASONING_BLOCK_RE.finditer(raw) if (b := m.group(2).strip()))
        tail = _REASONING_OPEN_TAIL_RE.search(raw)
        if tail is not None and "<" not in tail.group(2):
            self._open_raw = tail.group(2)
            self._fast = True
        else:
            self._open_raw = ""
            self._fast = False
        return self._visible


def extract_confidence(text: str) -> tuple[float | None, str | None]:
    """Parse an optional self-reported ``<confidence>`` (and explanation).

    Returns ``(confidence, explanation)`` where confidence is a float or
    None (malformed/absent) and explanation is a stripped string or None.
    The A2A handler clamps confidence to [0, 1] on write.
    """
    confidence: float | None = None
    m = _CONFIDENCE_RE.search(text)
    if m:
        try:
            confidence = float(m.group(1))
        except ValueError:
            confidence = None
    explanation: str | None = None
    me = _CONFIDENCE_EXPLANATION_RE.search(text)
    if me:
        explanation = me.group(1).strip() or None
    return confidence, explanation


def extract_output(text: str) -> str:
    """Return the user-facing content from a complete model response.

    Order of preference:
    1. Content inside the first ``<output>...</output>`` pair (with any
       nested reasoning markers stripped).
    2. Orphan-open ``<output>`` with no closing tag — recovers responses
       truncated mid-output when ``max_tokens`` is hit. Everything from the
       opener to end of text, scratch stripped.
    3. Full text with all reasoning markers stripped — covers the case
       where the model skipped ``<output>`` but still wrapped scratch.

    Returns "" when every strategy yields empty, logging a WARNING with a
    sanitized preview so operators can tell *why* a turn went silent
    (truncated mid-scratch vs. truly empty vs. odd shape). ``scratch_pad`` is
    never surfaced — leaking internal reasoning breaks the protocol contract.
    """
    if not text or not text.strip():
        return ""

    # 1. Closed <output>...</output> — balanced-only stripping so a literal
    #    tag mention in the answer (self-describing replies) isn't treated as
    #    leaked reasoning and truncated.
    m = _OUTPUT_RE.search(text)
    if m:
        cleaned = _strip_reasoning_balanced(m.group(1)).strip()
        if cleaned:
            return cleaned

    # 2. Orphan <output> opener (max_tokens truncation mid-output).
    orphan = _ORPHAN_OUTPUT_OPEN_RE.search(text)
    if orphan:
        cleaned = _strip_reasoning(orphan.group(1)).strip()
        if cleaned:
            return cleaned

    # 3. Last resort — strip reasoning, return what's left.
    fallback = _strip_reasoning(text).strip()
    if fallback:
        return fallback

    preview = text[:400].replace("\n", "\\n")
    log.warning(
        "[extract_output] empty after stripping — len=%d scratch=%s output=%s preview=%r",
        len(text),
        "<scratch_pad>" in text.lower(),
        "<output>" in text.lower(),
        preview,
    )
    return ""


def is_dropped_scratch_turn(text: str) -> bool:
    """Detect the 'scratch-only, never committed' dropped-turn pattern.

    Failure mode: the model writes reasoning (``<scratch_pad>...`` or
    ``<think>...``) and then stops without emitting a tool call or an
    ``<output>`` block. ``extract_output`` strips the reasoning, returns
    empty, and the turn silently drops. Detecting it lets the server issue a
    kicker and retry once. Callers should also confirm no tool call fired this
    turn (the LangChain tool channel is separate from text content) — an empty
    extract_output with a tool call is a normal mid-loop step, not a drop.

    True when the text has ``<scratch_pad>`` or ``<think>`` content and no
    ``<output>`` tag.
    """
    if not text:
        return False
    lower = text.lower()
    if "<scratch_pad>" not in lower and "<think>" not in lower:
        return False
    return "<output>" not in lower


# Follow-up user message sent on the same thread when is_dropped_scratch_turn
# fires — the dropped turn is still in the checkpointer history, so the model
# has full context to pick up where it left off.
DROPPED_SCRATCH_KICKER = (
    "Your previous turn emitted only reasoning (`<scratch_pad>`/`<think>`) — "
    "no tool call and no `<output>` block, so it was dropped. Pick up where "
    "you left off: if you were about to call a tool, call it now; if you have "
    "enough to answer, write the answer in `<output>` directly. Do not emit "
    "another bare reasoning block without committing to one of those paths."
)
