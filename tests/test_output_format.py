"""Tests for graph.output_format — <scratch_pad>/<output> protocol.

Covers the three shapes of traffic we see live:

1. Well-behaved model — emits both tags in the documented order.
2. Mixed — emits `<scratch_pad>` but forgets `<output>` wrapper.
3. Native thinking — provider (MiniMax, DeepSeek, Qwen3) leaks
   `<think>...</think>` regions that the filter must also strip.

The one-shot terminal path runs the complete text through ``extract_output``.
The incremental path (``stream_visible_output``) streams the user-facing
``<output>`` region token-by-token without leaking ``<scratch_pad>``; the
terminal ``extract_output`` reconciles any held-back tail. Both are covered.
"""

from __future__ import annotations

import random

import pytest

from graph.output_format import (
    OUTPUT_FORMAT_INSTRUCTIONS,
    StreamingOutputView,
    StreamingReasoningView,
    _strip_reasoning,
    extract_output,
    stream_visible_output,
    stream_visible_reasoning,
)


def test_extract_output_happy_path():
    text = "<scratch_pad>reasoning here</scratch_pad>\n<output>the answer</output>"
    assert extract_output(text) == "the answer"


def test_extract_output_strips_scratch_when_output_missing():
    text = "<scratch_pad>reasoning</scratch_pad>\nthe answer without output tag"
    assert extract_output(text) == "the answer without output tag"


def test_extract_output_strips_orphan_scratch_open():
    """MiniMax M2.x sometimes leaves scratch_pad unclosed — treat it as
    'everything from the orphan to EOT is reasoning' and strip it."""
    text = "real prose here <scratch_pad>unfinished reasoning never closed"
    assert extract_output(text) == "real prose here"


def test_extract_output_passthrough_no_tags():
    text = "just a plain response with no tags"
    assert extract_output(text) == "just a plain response with no tags"


def test_extract_output_takes_first_output_block():
    text = "<output>first</output> junk <output>second</output>"
    assert extract_output(text) == "first"


def test_extract_output_is_case_insensitive():
    assert extract_output("<OUTPUT>x</OUTPUT>") == "x"


def test_extract_output_strips_think_inside_output():
    """LiteLLM #22392: MiniMax leaks `<think>...</think>` blocks inside
    `<output>`. _strip_reasoning runs over the output region too."""
    text = "<output>head <think>inner reasoning</think> tail</output>"
    assert extract_output(text) == "head  tail"


def test_extract_output_strips_orphan_think():
    """Orphaned `<think>` opening with no close — drop to EOT."""
    text = "<output>visible <think>unfinished reasoning"
    # Output is unclosed, falls to passthrough branch which strips orphan think
    result = extract_output(text)
    assert "<think>" not in result
    assert "unfinished" not in result
    assert "visible" in result


def test_extract_output_strips_orphan_think_close():
    """Orphaned `</think>` (opener was somewhere upstream already)."""
    text = "<output>real answer</think></output>"
    assert extract_output(text) == "real answer"


def test_strip_reasoning_idempotent():
    """Real content never contains literal tag markers, so applying
    _strip_reasoning twice is safe and produces the same result."""
    text = "<think>THINK_BODY</think>real<scratch_pad>SCRATCH_BODY</scratch_pad>content"
    once = _strip_reasoning(text)
    twice = _strip_reasoning(once)
    assert once == twice
    assert "THINK_BODY" not in once
    assert "SCRATCH_BODY" not in once
    assert once == "realcontent"


def test_instructions_mention_both_tags():
    """Sanity check — the prompt fragment must teach both tags."""
    assert "<scratch_pad>" in OUTPUT_FORMAT_INSTRUCTIONS
    assert "<output>" in OUTPUT_FORMAT_INSTRUCTIONS


def test_extract_output_recovers_truncated_orphan_output():
    """max_tokens hit mid-<output>: no closing tag. Tier 2 recovers the
    partial answer from the opener to EOT, scratch stripped."""
    text = "<scratch_pad>planning the reply</scratch_pad><output>The partial answer that got cut o"
    assert extract_output(text) == "The partial answer that got cut o"


def test_extract_output_empty_when_scratch_only(caplog):
    """Scratch-only with no output → empty (never leak reasoning), and a
    WARNING diagnostic so the operator can see the turn went silent."""
    import logging

    with caplog.at_level(logging.WARNING, logger="protoagent.output_format"):
        assert extract_output("<scratch_pad>only reasoning, never committed</scratch_pad>") == ""
    assert any("empty after stripping" in r.message for r in caplog.records)


def test_extract_output_empty_input_returns_empty():
    assert extract_output("") == ""
    assert extract_output("   \n  ") == ""


# ── dropped-turn detection ────────────────────────────────────────────────────

from graph.output_format import DROPPED_SCRATCH_KICKER, is_dropped_scratch_turn


def test_dropped_scratch_only_is_detected():
    assert is_dropped_scratch_turn("<scratch_pad>thinking, never committed</scratch_pad>") is True


def test_dropped_think_only_is_detected():
    """Qwen-style <think> with no output is also a drop."""
    assert is_dropped_scratch_turn("<think>reasoning</think>") is True


def test_not_dropped_when_output_present():
    assert is_dropped_scratch_turn("<scratch_pad>x</scratch_pad><output>answer</output>") is False


def test_not_dropped_when_no_reasoning_markers():
    assert is_dropped_scratch_turn("plain text answer") is False
    assert is_dropped_scratch_turn("") is False


def test_kicker_is_actionable():
    k = DROPPED_SCRATCH_KICKER.lower()
    assert "<output>" in k and ("tool" in k)


# ── self-referential answers (literal tag mentions) ──────────────────────────


def test_extract_output_keeps_literal_scratch_pad_mention():
    """Regression: when the answer *describes the protocol* (e.g. "what can you
    do?"), it names the tags in prose. A closed <output> must NOT treat a
    literal `<scratch_pad>` mention as leaked reasoning and truncate the reply
    at it — that silently cut answers off mid-sentence."""
    raw = (
        "<output>Here's what I can do: search and calculate.\n\n"
        "My Approach\nI think in `<scratch_pad>` tags, then write the answer "
        "in `<output>`.</output>"
    )
    out = extract_output(raw)
    assert out.endswith("write the answer in `<output>`.")
    assert "<scratch_pad>" in out  # the literal mention survives


def test_extract_output_keeps_backticked_tag_mention_without_output_wrapper():
    """Regression (Batch-4): the same literal-protocol-mention answer but with NO
    <output> wrapper falls to the orphan-strip fallback tiers. The eat-to-EOT orphan
    openers are backtick-guarded too, so a backticked `<scratch_pad>` / `<think>`
    mention no longer truncates the stored/A2A/Discord copy at that token — while a
    genuine (un-backticked) orphan is still recovered (eat-to-EOT)."""
    text = (
        "Here's how I work: I reason in `<scratch_pad>` and `<think>` blocks, then "
        "write the final answer. No protocol wrapper on this reply."
    )
    assert extract_output(text) == text  # whole answer survives the fallback tier
    assert extract_output("real answer <scratch_pad>leaked unfinished") == "real answer"


def test_extract_output_ignores_backticked_open_tag_in_scratch():
    """Regression: the model commonly explains the protocol *in its scratch_pad*
    ("answer in `<output>` format"). The matcher must open on the real
    <output>, not the backticked mention — otherwise scratch reasoning leaks
    into the user-facing answer."""
    raw = (
        "<scratch_pad>\nI reason in `<scratch_pad>`, answer in `<output>` format "
        "with optional confidence.\nThis is a template, so I'll give an overview.\n"
        "</scratch_pad>\n\n<output>\n# What I Can Do\nSearch, calculate, remember.\n</output>"
    )
    assert extract_output(raw) == "# What I Can Do\nSearch, calculate, remember."


def test_stream_visible_ignores_backticked_open_in_scratch():
    """The same guard for streaming: while still in scratch_pad (which names the
    tag in backticks), nothing user-facing is streamed."""
    mid = "<scratch_pad>plan: answer in `<output>` format, then finish"
    assert stream_visible_output(mid) == ""


def test_extract_output_keeps_literal_close_tag_mention():
    """Regression: a backtick-wrapped `</output>` mention in the answer must not
    close the block early. The real closer ends prose, not a backtick."""
    raw = (
        "<output>How I work:\n1. Reason in `<scratch_pad>`\n"
        "2. Answer in `<output>`\n3. Confidence goes after `</output>`.\n\n"
        "That is the whole protocol.</output>"
    )
    out = extract_output(raw)
    assert out.endswith("That is the whole protocol.")
    assert "`</output>`" in out


def test_extract_output_still_takes_first_of_two_real_blocks():
    """The backtick guard must not change multi-block behavior: two real
    (non-backticked) <output> blocks still resolve to the first."""
    assert extract_output("<output>first</output> junk <output>second</output>") == "first"


def test_extract_output_still_strips_balanced_reasoning_inside_output():
    """The fix is scoped: balanced <think>/<scratch_pad> blocks inside a closed
    <output> are still stripped (real provider leakage)."""
    raw = "<output>before <scratch_pad>leaked plan</scratch_pad> after</output>"
    assert extract_output(raw) == "before  after"


def test_extract_output_orphan_tier_still_strips_truncated_scratch():
    """An *orphan-open* <output> (max_tokens truncation) still uses the
    eat-to-end stripper — that case really is truncated mid-reasoning."""
    raw = "<output>partial answer <scratch_pad>then it got cut off mid-think"
    assert extract_output(raw) == "partial answer"


# ── stream_visible_output (incremental token streaming) ──────────────────────


def test_stream_visible_empty_while_in_scratch():
    """Before <output> opens, nothing user-facing is streamed — the scratch_pad
    must never leak token-by-token."""
    assert stream_visible_output("<scratch_pad>still thinking about") == ""
    assert stream_visible_output("") == ""
    assert stream_visible_output("no tags yet") == ""


def test_stream_visible_orphan_open_streams_content():
    """An open <output> with no close yet (mid-stream) surfaces its content."""
    assert stream_visible_output("<scratch_pad>x</scratch_pad><output>Hello there") == "Hello there"


def test_stream_visible_closed_output():
    assert stream_visible_output("<output>the answer</output>") == "the answer"


# ── stream_visible_reasoning (live "thinking" channel) ───────────────────────


def test_stream_reasoning_empty_before_any_tag():
    assert stream_visible_reasoning("") == ""
    assert stream_visible_reasoning("no tags yet") == ""


def test_stream_reasoning_open_block_streams_content():
    assert stream_visible_reasoning("<scratch_pad>deciding which tool") == "deciding which tool"


def test_stream_reasoning_holds_back_partial_close_tag():
    # A half-written </scratch_pad> must not flash into the reasoning view.
    assert stream_visible_reasoning("<scratch_pad>thinking</scratc") == "thinking"


def test_stream_reasoning_is_monotonic_prefix():
    # As raw grows, the result only ever extends — required for delta streaming.
    steps = [
        "<scratch_pad>step one",
        "<scratch_pad>step one and two</scratch_pad><output>ans",
    ]
    a = stream_visible_reasoning(steps[0])
    b = stream_visible_reasoning(steps[1])
    assert b.startswith(a)
    assert "step one and two" in b


def test_stream_reasoning_concatenates_multiple_blocks():
    raw = "<scratch_pad>plan A</scratch_pad><output>x</output><scratch_pad>plan B</scratch_pad>"
    out = stream_visible_reasoning(raw)
    assert "plan A" in out and "plan B" in out


def test_stream_reasoning_includes_provider_think():
    assert stream_visible_reasoning("<think>provider chain of thought</think>") == "provider chain of thought"


def test_stream_visible_holds_back_partial_closing_tag():
    """A half-written </output> must not flash on screen."""
    assert stream_visible_output("<output>done </out") == "done "
    assert stream_visible_output("<output>done<") == "done"


def test_stream_visible_holds_back_partial_confidence_tag():
    assert stream_visible_output("<output>answer</output><conf") == "answer"


def test_stream_visible_strips_think_regions():
    assert stream_visible_output("<output>A<think>noise</think>B</output>") == "AB"
    assert stream_visible_output("<output>A<think>partial reasoning") == "A"


def test_stream_visible_keeps_legit_angle_brackets():
    """A '<' followed later by '>' is real content, not a partial tag."""
    assert stream_visible_output("<output>a < b > c") == "a < b > c"


def test_stream_visible_is_monotonic_prefix():
    """As raw grows, the visible result only extends — so a caller can safely
    emit result[already_emitted:] each step."""
    chunks = ["<output>", "Hel", "lo wor", "ld</output><confidence>0.9</confidence>"]
    raw = ""
    seen = ""
    for c in chunks:
        raw += c
        vis = stream_visible_output(raw)
        assert vis.startswith(seen) or seen.startswith(vis)  # never diverges
        seen = vis
    assert seen == "Hello world"
    # The terminal extractor agrees with the final streamed text.
    assert extract_output(raw) == "Hello world"


# ── incremental streaming views == the pure functions (oracle equivalence, #1310) ──
# StreamingOutputView / StreamingReasoningView only scan the new tail per chunk, so they
# must return EXACTLY what the (O(N²)) pure functions return at every growing prefix.
# These pin the fast incremental paths to the proven pure implementations.

_STREAM_CASES = [
    "",
    "no tags at all, just a bare answer",
    "<scratch_pad>thinking</scratch_pad><output>Hello world</output>",
    "<scratch_pad>plan the work</scratch_pad><output>answer with a < b and c > d comparison</output>",
    "<output>partial answer with no closing tag yet",  # orphan open (max_tokens truncation)
    "<scratch_pad>step 1</scratch_pad><scratch_pad>step 2</scratch_pad><output>done</output>",  # multi scratch
    "<output>before<think>leaked provider reasoning</think>after</output>",  # think inside output
    "<output>x</output><confidence>0.9</confidence>",
    "<scratch_pad>reasoning that mentions `<output>` in backticks</scratch_pad><output>the real answer</output>",
    "<think>provider think block</think><output>ok</output>",
    "<output>```python\nxs: list[int] = []\nif a < b and c > d:\n    pass\n```\ndone</output>",  # code: many < >
    "<output>trailing partial close </outp",  # partial </output> at the very end
    "<output>answer</output>\n<confidence>0.8</confidence>\n<confidence_explanation>why it scored</confidence_explanation>",
    "<scratch_pad>only reasoning, model never opened output and stopped</scratch_pad>",
    "<scratch_pad>a</scratch_pad><output>one</output>more raw<output>two</output>",  # second (ignored) output
]


@pytest.mark.parametrize("text", _STREAM_CASES)
@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13, 50])
def test_streaming_output_view_matches_pure(text, chunk):
    view = StreamingOutputView()
    for i in range(chunk, len(text) + chunk, chunk):
        prefix = text[:i]
        assert view.update(prefix) == stream_visible_output(prefix), f"mismatch at prefix {prefix!r}"


@pytest.mark.parametrize("text", _STREAM_CASES)
@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 13, 50])
def test_streaming_reasoning_view_matches_pure(text, chunk):
    view = StreamingReasoningView()
    for i in range(chunk, len(text) + chunk, chunk):
        prefix = text[:i]
        assert view.update(prefix) == stream_visible_reasoning(prefix), f"mismatch at prefix {prefix!r}"


def test_streaming_views_match_pure_under_random_fuzz():
    """Random documents from a tag-heavy alphabet, chunked at random offsets — the
    incremental views must equal the pure functions at every step (catches boundary /
    holdback / whitespace edges the curated cases miss)."""
    rng = random.Random(1310)
    alphabet = ["<output>", "</output>", "<scratch_pad>", "</scratch_pad>", "<think>", "</think>",
                "<confidence>", "</confidence>", "`", "<", ">", " ", "\n", "a", "b", "x", "word"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        out_view, reason_view = StreamingOutputView(), StreamingReasoningView()
        pos = 0
        while pos < len(text):
            pos = min(len(text), pos + rng.randint(1, 6))
            prefix = text[:pos]
            assert out_view.update(prefix) == stream_visible_output(prefix), f"output mismatch: {prefix!r}"
            assert reason_view.update(prefix) == stream_visible_reasoning(prefix), f"reasoning mismatch: {prefix!r}"
