"""A subagent that stops mid-loop is continued, not reported "completed" (#3552).

An agent loop ends on the first model turn with no tool call, and the delegation's
answer is the last AIMessage with content. A model that just STOPS — narrates its next
read and never makes the call, or returns an empty turn so the walk-back lands on an
earlier narration — therefore looked exactly like one that finished. Seen live: 17% of
``review-finder`` steps "completed" on one sentence ("Let me check the other callers
of X:"), no findings array, p50 509s into the loop.

Driven through the REAL runner (real ``create_agent``, the real subagent middleware
stack, a real tool; only the chat model is scripted), like ``test_subagent_turn_budget``.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.middleware.completion_guard import NUDGE_MARK, CompletionGuardMiddleware, wrap_up_at
from graph.middleware.guard_notes import is_guard_note
from graph.review.findings import findings_delivered
from graph.subagents.config import REVIEW_FINDER_CONFIG, REVIEW_SYNTHESIZER_CONFIG, SUBAGENT_REGISTRY, SubagentConfig

PROBE = "completion-probe"
MARKER = "```json"
DELIVERABLE = "No defects.\n\n```json\n[]\n```"
NARRATION = "Let me check the other callers of `resolveCustomTest`:"


def _call(i: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "ping", "args": {}, "id": f"call-{i}"}])


class _ScriptedModel(BaseChatModel):
    """Replays ``script`` one turn per call; records the messages each call was shown."""

    script: list = []
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        turn = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        # A fresh message per call, as a real model returns: the state reducer merges by id.
        msg = AIMessage(content=turn.content, tool_calls=list(turn.tool_calls))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-turns"


@pytest.fixture
def probe(monkeypatch):
    """``arm(script, marker=…, max_turns=…)`` registers the probe subagent and scripts its model."""
    models: list[_ScriptedModel] = []
    state: dict = {}

    @tool
    def ping() -> str:
        """Return pong."""
        return "pong"

    def _create_llm(*_a, **_k):
        m = _ScriptedModel(script=state["script"], seen=[])
        models.append(m)
        return m

    monkeypatch.setattr(agent_mod, "create_llm", _create_llm)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def arm(script, *, marker=MARKER, max_turns=8):
        state["script"] = script
        monkeypatch.setitem(
            SUBAGENT_REGISTRY,
            PROBE,
            SubagentConfig(
                name=PROBE,
                description="d",
                system_prompt="p",
                tools=["ping"],
                max_turns=max_turns,
                completion_marker=marker,
            ),
        )
        return ping, models

    return arm


async def _run(ping, truncate=None) -> str:
    return await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={"ping": ping},
        available_subagents=PROBE,
        description="lane",
        prompt="go",
        subagent_type=PROBE,
        truncate=truncate,
    )


def _nudges(messages) -> int:
    # By the guard tag, the same mechanism as the code under test (#3556) — and the tag
    # must survive to the model call: the prompt-cache middleware re-shapes request content.
    return sum(1 for m in messages if is_guard_note(m, "completion"))


async def test_a_task_prompt_that_starts_with_the_tag_does_not_use_up_a_nudge(probe):
    # #3556: the task prompt is a HumanMessage too. Counted by text, a prompt opening with
    # "[completion-guard]" read as a nudge already sent, leaving the run one nudge instead of two.
    ping, models = probe([AIMessage(content=NARRATION)])  # never recovers: spends every nudge
    out = await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={"ping": ping},
        available_subagents=PROBE,
        description="lane",
        prompt=f"{NUDGE_MARK} is the name of the middleware under review — check it.",
        subagent_type=PROBE,
    )
    assert out.startswith(f"[{PROBE} ended without its deliverable: lane"), out
    assert len(models[-1].seen) == 3  # the original turn + BOTH nudges


async def test_a_narration_that_ends_the_loop_is_sent_back_to_the_model(probe):
    # One tool round, then the live failure shape: text announcing a read, no tool call.
    ping, models = probe([_call(0), AIMessage(content=NARRATION), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out
    assert len(models[-1].seen) == 3
    assert _nudges(models[-1].seen[-1]) == 1  # the recovering call saw exactly one nudge


async def test_an_empty_final_turn_is_sent_back_too(probe):
    # The other shape: the turn after a narrated tool round comes back empty, and the
    # walk-back used to surface the narration as the lane's whole answer.
    narrated_call = AIMessage(content=NARRATION, tool_calls=[{"name": "ping", "args": {}, "id": "c0"}])
    ping, models = probe([narrated_call, AIMessage(content=""), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out


async def test_the_model_may_answer_a_nudge_with_the_tool_call_it_described(probe):
    ping, models = probe([AIMessage(content=NARRATION), _call(0), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out


async def test_nudges_are_bounded_and_the_failure_is_labelled(probe):
    ping, models = probe([AIMessage(content=NARRATION)])  # never recovers
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} ended without its deliverable: lane"), out
    assert "INCOMPLETE" in out and "Gap" in out and NARRATION in out
    assert "completed" not in out.splitlines()[0]
    assert len(models[-1].seen) == 3  # the original turn + two nudged retries, then it ends


async def test_a_delivered_answer_is_never_nudged(probe):
    ping, models = probe([_call(0), AIMessage(content=DELIVERABLE)])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    assert len(models[-1].seen) == 2 and _nudges(models[-1].seen[-1]) == 0


async def test_no_marker_means_no_guard(probe):
    ping, models = probe([AIMessage(content=NARRATION)], marker="")
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    assert len(models[-1].seen) == 1


async def test_the_deliverable_is_judged_before_truncation(probe):
    # A fan-out's `truncate` can cut the fence off a long answer; that is not a dead lane.
    ping, _ = probe([AIMessage(content="x" * 500 + "\n" + DELIVERABLE)])
    out = await _run(ping, truncate=100)
    assert out.startswith(f"[{PROBE} completed: lane]"), out


async def test_nudges_spend_the_turn_budget_and_never_outrun_it(probe):
    # max_turns is still the ceiling: a model that only ever narrates cannot loop on nudges.
    ping, models = probe([AIMessage(content=NARRATION)], max_turns=1)
    out = await _run(ping)
    assert len(models[-1].seen) <= 3
    assert "completed: lane]" not in out.splitlines()[0]


async def test_a_run_with_no_output_at_all_is_not_labelled_completed(probe):
    # Every turn empty: there is no AIMessage with content to walk back to.
    ping, models = probe([AIMessage(content="")])
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} ended without its deliverable: lane]"), out
    assert "no output produced" in out and "Gap" in out
    assert len(models[-1].seen) == 3  # nudged twice before giving up


async def test_a_blank_contract_quotes_the_marker_in_the_nudge(probe):
    ping, models = probe([AIMessage(content=NARRATION), AIMessage(content=DELIVERABLE)])
    await _run(ping)
    nudge = next(m for m in models[-1].seen[-1] if isinstance(m, HumanMessage) and str(m.text).startswith(NUDGE_MARK))
    assert repr(MARKER) in str(nudge.text)
    # An explicit contract wins; a check-only subagent gets the guard's generic phrase.
    cfg = SubagentConfig(
        name="x", description="d", system_prompt="p", completion_marker=MARKER, completion_contract="the array"
    )
    assert cfg.nudge_contract() == "the array"
    assert (
        SubagentConfig(
            name="x", description="d", system_prompt="p", completion_check=findings_delivered
        ).nudge_contract()
        == ""
    )


def test_a_turn_with_tool_calls_is_left_alone():
    guard = CompletionGuardMiddleware(delivered=lambda text: MARKER in text)
    assert guard._intervene({"messages": [HumanMessage(content="go"), _call(0)]}) is None


def test_the_review_lanes_require_a_parseable_findings_array():
    # A substring cannot vouch for this deliverable: a fence can open and never close.
    for cfg in (REVIEW_FINDER_CONFIG, REVIEW_SYNTHESIZER_CONFIG):
        delivered = cfg.delivered()
        assert delivered is findings_delivered
        assert "```json" in cfg.system_prompt  # the contract the prompt states
    assert findings_delivered(DELIVERABLE)
    assert findings_delivered('Two defects.\n\n```json\n[{"file": "a.py", "claim": "x"}]\n```')
    for near_miss in (
        NARRATION,
        "I will now produce a ```json findings array.",  # mentions the fence
        'Found one.\n\n```json\n[{"file": "a.py", "cla',  # cut off mid-array
        "```json\n{}\n```",  # a fenced object is not a findings array
        "The result is [] — nothing to report.",  # bare array in prose
        "",
    ):
        assert not findings_delivered(near_miss), near_miss


async def test_a_reply_that_only_mentions_the_fence_is_sent_back(monkeypatch, probe):
    ping, models = probe(
        [AIMessage(content="I will now produce a ```json findings array."), AIMessage(content=DELIVERABLE)]
    )
    cfg = SUBAGENT_REGISTRY[PROBE]
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(
            name=PROBE,
            description="d",
            system_prompt="p",
            tools=cfg.tools,
            max_turns=cfg.max_turns,
            completion_check=findings_delivered,
        ),
    )
    out = await _run(ping)
    assert out.startswith(f"[{PROBE} completed: lane]") and DELIVERABLE in out, out
    assert len(models[-1].seen) == 2  # the substring marker would have accepted the first turn


# ── a closing line the CALLER requires (pr-reviewer-plugin#145) ────────────────

STATUS = "FINDER_STATUS:"
ASKS = f"Review the diff. End with `{STATUS} reviewed n=<count>`."


def _arm_finder_like(monkeypatch, probe, script, *, max_turns=8):
    ping, models = probe(script, max_turns=max_turns)
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(
            name=PROBE,
            description="d",
            system_prompt="p",
            tools=["ping"],
            max_turns=max_turns,
            completion_check=findings_delivered,
            completion_prompt_markers=(STATUS,),
        ),
    )
    return ping, models


async def _run_with(ping, prompt) -> str:
    return await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={"ping": ping},
        available_subagents=PROBE,
        description="lane",
        prompt=prompt,
        subagent_type=PROBE,
    )


async def test_a_finished_review_missing_the_required_line_is_sent_back_once(monkeypatch, probe):
    # Seen live: a full, correct review ending in a fenced array — and no FINDER_STATUS line.
    # The caller voids that lane. One nudge, and the answer comes back WHOLE with the line.
    whole = f"{DELIVERABLE}\n\n{STATUS} reviewed n=0"
    ping, models = _arm_finder_like(monkeypatch, probe, [AIMessage(content=DELIVERABLE), AIMessage(content=whole)])
    out = await _run_with(ping, ASKS)
    assert out.startswith(f"[{PROBE} completed: lane]") and whole in out, out
    assert len(models[-1].seen) == 2
    note = next(m for m in models[-1].seen[-1] if is_guard_note(m, "completion-line"))
    assert STATUS in str(note.text) and "COMPLETE answer" in str(note.text)  # not "reply with just the line"


async def test_the_closing_line_is_asked_for_once_not_twice(monkeypatch, probe):
    # A finished review that STILL omits the line after being asked is not asked again: the
    # ask is a courtesy on finished work, not a loop. The lane ends, honestly labelled.
    ping, models = _arm_finder_like(monkeypatch, probe, [AIMessage(content=DELIVERABLE)])  # never adds it
    out = await _run_with(ping, ASKS)
    assert len(models[-1].seen) == 2  # the original answer + ONE ask
    assert out.startswith(f"[{PROBE} ended without its deliverable: lane"), out


async def test_mentioning_the_marker_is_not_the_same_as_giving_the_line(monkeypatch, probe):
    # "FINDER_STATUS: is missing from the other lane" names the marker mid-sentence.
    prose = f"I note the other lane omitted its {STATUS} line.\n\n```json\n[]\n```"
    whole = f"{prose}\n\n`{STATUS} reviewed n=0`"  # the model often wraps it in backticks
    ping, models = _arm_finder_like(monkeypatch, probe, [AIMessage(content=prose), AIMessage(content=whole)])
    out = await _run_with(ping, ASKS)
    assert len(models[-1].seen) == 2  # the mention did not pass; it was asked, then delivered
    assert out.startswith(f"[{PROBE} completed: lane]"), out


async def test_a_caller_that_never_asked_for_the_line_is_not_nudged_for_it(monkeypatch, probe):
    # The core `code-review` recipe shares this subagent and asks for no status line.
    ping, models = _arm_finder_like(monkeypatch, probe, [AIMessage(content=DELIVERABLE)])
    out = await _run_with(ping, "Review the diff.")
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    assert len(models[-1].seen) == 1


async def test_a_reply_holding_only_the_missing_line_does_not_pass_for_the_review(monkeypatch, probe):
    # The answer is the LAST message: a bare status line would REPLACE the review. It lacks
    # the array, so it is sent back again; if that fails too the lane is labelled, not passed.
    bare = f"{STATUS} reviewed n=0"
    ping, models = _arm_finder_like(monkeypatch, probe, [AIMessage(content=DELIVERABLE), AIMessage(content=bare)])
    out = await _run_with(ping, ASKS)
    assert out.startswith(f"[{PROBE} ended without its deliverable: lane"), out
    assert len(models[-1].seen) == 3  # asked for the line, then nudged for the lost array


# ── a wrap-up warning before the turn budget is gone (#3559) ───────────────────


def test_the_wrap_up_point_leaves_room_to_write():
    assert {n: wrap_up_at(n) for n in (3, 5, 6, 8, 10, 40, 60)} == {3: 0, 5: 0, 6: 3, 8: 5, 10: 7, 40: 34, 60: 51}


async def test_a_lane_that_keeps_reading_is_told_once_that_its_budget_is_nearly_spent(monkeypatch, probe):
    # Seen live: 60 rounds into a four-file diff, hard-stopped on "Let me find the
    # `SpyDispatcher` definition" — all of it lost. The model cannot see its own counter.
    script = [_call(i) for i in range(5)] + [AIMessage(content=DELIVERABLE)]
    ping, models = _arm_finder_like(monkeypatch, probe, script, max_turns=8)  # warns at round 5
    out = await _run_with(ping, "Review the diff.")
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    seen = models[-1].seen
    warned = [sum(1 for m in call if is_guard_note(m, "turn-budget")) for call in seen]
    assert warned == [0, 0, 0, 0, 0, 1]  # only the call AFTER five tool rounds, and only once
    note = next(m for m in seen[-1] if is_guard_note(m, "turn-budget"))
    assert "5 of your 8 tool rounds" in str(note.text) and "Gap:" in str(note.text)


async def test_the_warning_is_not_a_nudge_and_a_small_budget_gets_none(monkeypatch, probe):
    # The wrap-up note must not use up a completion nudge…
    script = [_call(i) for i in range(5)] + [AIMessage(content=NARRATION)]
    ping, models = _arm_finder_like(monkeypatch, probe, script, max_turns=8)
    await _run_with(ping, "Review the diff.")
    assert sum(1 for m in models[-1].seen[-1] if is_guard_note(m, "completion")) == 2  # both nudges still spent
    # …and a budget too small to have a "nearly spent" is left alone.
    ping, models = _arm_finder_like(monkeypatch, probe, [_call(0), AIMessage(content=DELIVERABLE)], max_turns=4)
    await _run_with(ping, "Review the diff.")
    assert not any(is_guard_note(m, "turn-budget") for call in models[-1].seen for m in call)


def test_every_registered_subagents_completion_contract_is_well_formed():
    # Structural, over the whole registry: these fields steer control flow, and the easy
    # mistakes are silent — `completion_prompt_markers="FINDER_STATUS:"` (a string, not a
    # tuple) reads as the markers "F", "I", "N"…; a non-callable check raises mid-review.
    for name, cfg in SUBAGENT_REGISTRY.items():
        markers = cfg.completion_prompt_markers
        assert isinstance(markers, tuple), f"{name}: completion_prompt_markers must be a tuple, got {type(markers)}"
        assert all(isinstance(m, str) and m.strip() for m in markers), f"{name}: empty/non-string marker in {markers}"
        assert cfg.completion_check is None or callable(cfg.completion_check), name
        assert isinstance(cfg.completion_marker, str) and isinstance(cfg.completion_contract, str), name
        if markers:  # a closing line is only ever owed ON TOP of a deliverable
            assert cfg.delivered() is not None, (
                f"{name}: prompt markers without a completion contract are never checked"
            )
        if cfg.delivered() is not None:
            assert cfg.max_turns > 0, name


def test_a_marker_given_as_a_bare_string_is_one_marker_not_its_characters():
    from graph.middleware.completion_guard import missing_markers

    prompt, answer = f"End with {STATUS} reviewed.", "Finished, no status."
    assert missing_markers(STATUS, prompt, answer) == [STATUS]
    assert missing_markers(STATUS, prompt, f"ok\n{STATUS} reviewed n=0") == []


def test_review_finder_names_the_line_its_callers_may_require():
    assert REVIEW_FINDER_CONFIG.completion_prompt_markers == ("FINDER_STATUS:",)
    assert REVIEW_SYNTHESIZER_CONFIG.completion_prompt_markers == ()


# ── the verifier is asked for its status line, like a finder for its own (#3578) ────────


def _arm_verifier_like(monkeypatch, probe, script):
    from graph.subagents.config import VERIFIER_CONFIG

    ping, models = probe(script, max_turns=8)
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(
            name=PROBE,
            description="d",
            system_prompt="p",
            tools=["ping"],
            max_turns=8,
            completion_check=VERIFIER_CONFIG.completion_check,
            completion_prompt_markers=VERIFIER_CONFIG.completion_prompt_markers,
            completion_contract=VERIFIER_CONFIG.completion_contract,
        ),
    )
    return ping, models


PROSE_CHECK = "**Claim: the lanes are reclassified** — confirmed at dispatch.py:1740.\n\n```json\n[]\n```"


async def test_a_verifier_that_did_its_work_in_prose_is_asked_once_for_its_status_line(monkeypatch, probe):
    # pr-reviewer-plugin#178's review: 77 s of claim-tracing, no `VERIFY_STATUS:` — read as
    # "the verify pass did not run", capping a clean round at WARN.
    whole = f"VERIFY_STATUS: nothing-to-verify\n\n{PROSE_CHECK}"
    ping, models = _arm_verifier_like(monkeypatch, probe, [AIMessage(content=PROSE_CHECK), AIMessage(content=whole)])
    out = await _run_with(
        ping, "Verify these findings. **Begin your reply with exactly one status line** `VERIFY_STATUS: …`."
    )
    assert out.startswith(f"[{PROBE} completed: lane]") and whole in out, out
    assert len(models[-1].seen) == 2
    assert any(is_guard_note(m, "completion-line") for m in models[-1].seen[-1])


async def test_a_verify_prompt_that_names_no_status_line_asks_for_none(monkeypatch, probe):
    # The research workflow's verifier: prose claim checks are the whole deliverable.
    ping, models = _arm_verifier_like(
        monkeypatch, probe, [AIMessage(content="Claim 1: supported. Claim 2: unsupported.")]
    )
    out = await _run_with(ping, "Check these research claims against their sources.")
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    assert len(models[-1].seen) == 1


# ── a wrap-up warning before the CONTEXT is gone, and relief on the way there (#3576) ──


def _read_call(i: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "read", "args": {}, "id": f"read-{i}"}])


def _arm_reader(monkeypatch, probe, script, *, window, config):
    """A finder-like lane whose tool returns 10k chars per call, on a model whose window
    is `window` tokens (chars//4 estimate, so 2.5k tokens per read)."""
    from langchain_core.tools import tool

    @tool
    def read() -> str:
        """Return a big file."""
        return "x" * 10_000

    _, models = _arm_finder_like(monkeypatch, probe, script, max_turns=40)
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        PROBE,
        SubagentConfig(
            name=PROBE,
            description="d",
            system_prompt="p",
            tools=["read"],
            max_turns=40,
            completion_check=findings_delivered,
            completion_prompt_markers=(STATUS,),
        ),
    )
    monkeypatch.setattr(agent_mod, "context_window_for", lambda *_a, **_k: window, raising=False)
    import graph.model_window as mw

    monkeypatch.setattr(mw, "context_window_for", lambda *_a, **_k: window)

    async def run():
        return await agent_mod._run_subagent(
            config=config,
            tool_map={"read": read},
            available_subagents=PROBE,
            description="lane",
            prompt="Review the diff.",
            subagent_type=PROBE,
        )

    return run, models


async def test_a_lane_that_reads_toward_the_context_wall_is_told_once_to_stop(monkeypatch, probe):
    # mythxengine#858: 7 of 8 panels died on ContextWindowExceeded — 229k tokens of the
    # finder's own file reads, prompt ~3k chars. The model cannot see its context size.
    script = [_read_call(i) for i in range(4)] + [AIMessage(content=DELIVERABLE)]
    # 8k-token window → note at 6.4k tokens (25.6k chars): after the 3rd 10k-char read.
    run, models = _arm_reader(monkeypatch, probe, script, window=8_000, config=LangGraphConfig(pruning_enabled=False))
    out = await run()
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    seen = models[-1].seen
    warned = [sum(1 for m in call if is_guard_note(m, "context-budget")) for call in seen]
    assert warned == [0, 0, 0, 1, 1]  # once three reads are in history, and only once
    note = next(m for m in seen[3] if is_guard_note(m, "context-budget"))
    assert "nearly full" in str(note.text) and "Gap:" in str(note.text)


async def test_a_subagent_gets_tool_result_pruning_like_the_lead(monkeypatch, probe):
    # The lead stack has had the pruner since #2782; a delegation had no relief valve.
    script = [_read_call(i) for i in range(4)] + [AIMessage(content=DELIVERABLE)]
    cfg = LangGraphConfig(pruning_keep_messages=2, pruning_min_chars=1_000, pruning_at_fraction=0.5)
    run, models = _arm_reader(monkeypatch, probe, script, window=8_000, config=cfg)  # prunes past 4k tokens
    out = await run()
    assert out.startswith(f"[{PROBE} completed: lane]"), out
    last_call = models[-1].seen[-1]
    stubbed = [m for m in last_call if "chars pruned by protoAgent" in str(getattr(m, "content", ""))]
    assert stubbed, "an older 10k-char read should have been stubbed head+tail"
