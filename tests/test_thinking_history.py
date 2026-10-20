"""Dropping completed prior turns' thinking blocks at the request boundary (#4415).

Claude Opus/Sonnet 5.5 replay — and bill — every earlier turn's thinking as input
(a brandLaunch thread: ~492 blocks, ~40% of a 478k prompt). The API's preserved-thinking
check accepts removing a LEADING run of thinking blocks and rejects removing one from
the middle, so the strip must only ever take blocks before the last genuine user turn.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.config import LangGraphConfig
from graph.thinking_history import strip_prior_turn_thinking


def _think(tag: str) -> dict:
    return {"type": "thinking", "thinking": "", "signature": f"sig-{tag}"}


def _turn(n: int, steps: int, *, done: bool = True) -> list:
    """One user turn: the question, ``steps`` tool round-trips (each AI step carries a
    thinking block), then — when done — a final answer with its own thinking."""
    msgs: list = [HumanMessage(f"job {n}")]
    for s in range(steps):
        tid = f"t{n}-{s}"
        msgs.append(
            AIMessage(
                content=[_think(f"{n}-{s}"), {"type": "tool_use", "id": tid, "name": "record", "input": {}}],
                tool_calls=[{"id": tid, "name": "record", "args": {}}],
            )
        )
        msgs.append(ToolMessage(tool_call_id=tid, content=f"recorded {n}-{s}"))
    if done:
        msgs.append(AIMessage(content=[_think(f"{n}-final"), {"type": "text", "text": f"job {n} done"}]))
    return msgs


def _sigs(payload: dict) -> list[str]:
    return [
        b["signature"]
        for m in payload["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "thinking"
    ]


@pytest.fixture
def oauth_llm(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-WIRE")
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic not installed")
    ao._reset_token_cache()
    from graph.llm import create_llm

    def build(strip: bool):
        llm = create_llm(
            LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-5-5", strip_prior_turn_thinking=strip)
        )
        llm.max_retries = 0
        return llm

    yield build
    ao._reset_token_cache()


def test_only_completed_prior_turns_lose_their_thinking(oauth_llm):
    history = [*_turn(1, 2), *_turn(2, 2), *_turn(3, 2, done=False)]
    payload = oauth_llm(True)._get_request_payload(history)
    # the in-flight turn (job 3, mid tool loop) keeps every block; jobs 1-2 lose theirs
    assert _sigs(payload) == ["sig-3-0", "sig-3-1"]
    # text and tool_use of the earlier turns survive, pairing intact
    wire = json.dumps(payload["messages"])
    assert "job 1 done" in wire and "job 2 done" in wire and '"t1-0"' in wire


def test_stripped_blocks_are_a_leading_run(oauth_llm):
    """The API accepts removing thinking blocks oldest-first only: every removed block
    must precede every kept one."""
    history = [*_turn(1, 3), *_turn(2, 1), *_turn(3, 3, done=False)]
    full = _sigs(oauth_llm(False)._get_request_payload(history))
    kept = _sigs(oauth_llm(True)._get_request_payload(history))
    assert full[len(full) - len(kept) :] == kept  # kept == a suffix of the original order


def test_a_tool_result_turn_is_not_a_boundary(oauth_llm):
    """Inside one turn's tool loop every user message carries tool_results: nothing of
    the in-flight turn may be stripped."""
    history = _turn(1, 4, done=False)
    payload = oauth_llm(True)._get_request_payload(history)
    assert _sigs(payload) == [f"sig-1-{s}" for s in range(4)]


def test_prefix_is_stable_within_a_turn_and_moves_only_at_a_boundary(oauth_llm):
    llm = oauth_llm(True)
    history = [*_turn(1, 2), *_turn(2, 1, done=False)]
    a = llm._get_request_payload(history)
    history2 = [*history, *_turn(2, 2, done=False)[3:]]  # one more tool step in job 2
    b = llm._get_request_payload(history2)
    cut = len(a["messages"]) - 1
    assert json.dumps(a["messages"][:cut]) == json.dumps(b["messages"][:cut])
    # a new user turn rewrites only job 2 (job 1 was already stripped)
    c = llm._get_request_payload([*_turn(1, 2), *_turn(2, 1), *_turn(3, 0, done=False)])
    job1 = len(_turn(1, 2))
    assert json.dumps(b["messages"][:job1]) == json.dumps(c["messages"][:job1])


def test_off_by_default(oauth_llm):
    assert LangGraphConfig().strip_prior_turn_thinking is False
    history = [*_turn(1, 1), *_turn(2, 0, done=False)]
    assert _sigs(oauth_llm(False)._get_request_payload(history)) == ["sig-1-0", "sig-1-final"]


def test_payload_helper_edge_cases():
    nothing = {"messages": [{"role": "user", "content": "hi"}]}
    assert strip_prior_turn_thinking(nothing) is nothing
    only_thinking = {
        "messages": [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": [_think("x")]},
            {"role": "user", "content": [{"type": "text", "text": "b"}]},
        ]
    }
    out = strip_prior_turn_thinking(only_thinking)
    assert [m["role"] for m in out["messages"]] == ["user", "user"]  # emptied message dropped
    assert only_thinking["messages"][1]["content"] == [_think("x")]  # input untouched
