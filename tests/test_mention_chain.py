"""Agent-to-agent `@` addressing and the bounds that make it safe (#3050).

This is the one path in the room that can spend money with no human in the loop, so the
guards get more tests than the feature. Every one of these is a termination proof: given
adversarial replies, the chain must stop.
"""

from __future__ import annotations

import pytest

import graph.mention_op as mop


class _Graph:
    def __init__(self):
        self.written = []

    async def aget_state(self, config):
        class _S:
            values = {"messages": []}

        return _S()

    async def aupdate_state(self, config, update, *, as_node=None):
        assert as_node is not None
        self.written.append(update)
        return None


class _Delegate:
    type = "acp"


class _Registry:
    """Replies are scripted per delegate name, so a chain can be driven exactly."""

    def __init__(self, replies: dict[str, list[str]] | None = None, default="ok"):
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.default = default
        self.calls: list[dict] = []

    def get(self, name):
        return _Delegate()

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        self.calls.append({"name": name, "query": query, "permissions": permissions})
        queue = self.replies.get(name)
        return queue.pop(0) if queue else self.default


ROSTER = {"proto", "claude-code", "vera"}


def resolve(token):
    return token if token in ROSTER else None


# --- the default is OFF -------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_cannot_address_each_other_by_default():
    """max_hops=0 is the shipped default — a reply full of mentions routes nowhere."""
    reg = _Registry({"proto": ["@claude-code please check line 40"]})
    out = await mop.run_mention_chain(_Graph(), reg, "t", "proto", "look at auth", resolve=resolve)

    assert [c["name"] for c in reg.calls] == ["proto"]
    assert len(out) == 1


@pytest.mark.asyncio
async def test_a_chain_never_starts_without_a_resolver():
    """Fail CLOSED: a caller that forgets `resolve` gets no agent-to-agent addressing."""
    reg = _Registry({"proto": ["@claude-code check it"]})
    out = await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=5)
    assert [c["name"] for c in reg.calls] == ["proto"] and len(out) == 1


# --- one hop, enabled ---------------------------------------------------------


@pytest.mark.asyncio
async def test_one_hop_pulls_the_named_participant_in():
    reg = _Registry({"proto": ["@claude-code please check line 40"], "claude-code": ["patched"]})
    out = await mop.run_mention_chain(_Graph(), reg, "t", "proto", "look at auth", max_hops=1, resolve=resolve)

    assert [c["name"] for c in reg.calls] == ["proto", "claude-code"]
    assert reg.calls[1]["query"] == "please check line 40"
    assert [e["from"] for e in out] == ["operator", "proto"]


@pytest.mark.asyncio
async def test_the_operators_address_is_unceilinged_and_every_hop_is_readonly():
    """A mention the operator typed is the operator speaking. A mention inside an
    agent's reply is not — same syntax, different authority."""
    reg = _Registry({"proto": ["@claude-code check it"], "claude-code": ["@vera verify it"], "vera": ["done"]})
    await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=2, resolve=resolve)

    assert [c["permissions"] for c in reg.calls] == [None, "readonly", "readonly"]


@pytest.mark.asyncio
async def test_hops_are_bounded_even_when_every_reply_mentions_someone():
    reg = _Registry(
        {"proto": ["@claude-code go"], "claude-code": ["@vera go"], "vera": ["@proto go"]},
        default="@proto go",
    )
    await mop.run_mention_chain(_Graph(), reg, "t", "proto", "start", max_hops=2, resolve=resolve)
    assert [c["name"] for c in reg.calls] == ["proto", "claude-code", "vera"]


# --- termination proofs -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_participant_cannot_address_itself():
    """The degenerate one-agent loop, and the cheapest to fall into."""
    reg = _Registry({"proto": ["@proto keep going"]})
    out = await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=10, resolve=resolve)

    assert [c["name"] for c in reg.calls] == ["proto"]
    assert out[-1]["chain_stopped"] == "self_mention"


@pytest.mark.asyncio
async def test_ping_pong_is_stopped_by_the_per_target_bound_not_by_luck():
    """A→B→A→B with generous hops: the per-target bound is what terminates it."""
    reg = _Registry(default="@proto your turn")
    reg.replies = {
        "proto": ["@claude-code your turn", "@claude-code your turn", "@claude-code your turn"],
        "claude-code": ["@proto your turn", "@proto your turn", "@proto your turn"],
    }
    out = await mop.run_mention_chain(
        _Graph(), reg, "t", "proto", "start", max_hops=50, max_per_target=2, resolve=resolve
    )

    names = [c["name"] for c in reg.calls]
    assert names.count("proto") <= 2 and names.count("claude-code") <= 2
    assert out[-1]["chain_stopped"] == "per_target_limit"


@pytest.mark.asyncio
async def test_an_unknown_mention_in_a_reply_ends_the_chain_quietly():
    reg = _Registry({"proto": ["@nobody can you look"]})
    out = await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=5, resolve=resolve)
    assert [c["name"] for c in reg.calls] == ["proto"] and "chain_stopped" not in out[-1]


@pytest.mark.asyncio
async def test_a_mention_that_is_not_leading_does_not_route():
    """Same rule as the operator's input: only a LEADING mention addresses anyone."""
    reg = _Registry({"proto": ["I think @claude-code should look at this"]})
    await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=5, resolve=resolve)
    assert [c["name"] for c in reg.calls] == ["proto"]


@pytest.mark.asyncio
async def test_a_bare_mention_with_no_message_does_not_route():
    reg = _Registry({"proto": ["@claude-code"]})
    await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=5, resolve=resolve)
    assert [c["name"] for c in reg.calls] == ["proto"]


@pytest.mark.asyncio
async def test_a_failed_hop_ends_the_chain():
    class _Flaky(_Registry):
        async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
            self.calls.append({"name": name, "query": query, "permissions": permissions})
            if name == "claude-code":
                raise RuntimeError("delegate is down")
            return "@claude-code check it"

    reg = _Flaky()
    out = await mop.run_mention_chain(_Graph(), reg, "t", "proto", "go", max_hops=5, resolve=resolve)
    assert [c["name"] for c in reg.calls] == ["proto", "claude-code"]
    assert out[-1]["ok"] is False


@pytest.mark.asyncio
async def test_every_exchange_is_recorded_in_the_room():
    """A hop the operator didn't type still happened — the thread must show it."""
    graph = _Graph()
    reg = _Registry({"proto": ["@claude-code check it"], "claude-code": ["patched"]})
    await mop.run_mention_chain(graph, reg, "t", "proto", "go", max_hops=1, resolve=resolve)

    stamps = [m.additional_kwargs["room"] for update in graph.written for m in update["messages"]]
    assert stamps == [
        {"from": "operator", "to": "proto"},
        {"from": "proto"},
        {"from": "proto", "to": "claude-code"},  # proto asked, not the operator
        {"from": "claude-code"},
    ]


@pytest.mark.asyncio
async def test_a_hop_is_attributed_to_the_agent_that_made_it_not_the_operator():
    """The lie that would propagate: `run_mention` hardcoding "operator" as the author
    of every addressing message would put proto's words in the operator's mouth, and
    every later participant's catch-up would repeat it."""
    graph = _Graph()
    reg = _Registry({"proto": ["@claude-code check line 40"], "claude-code": ["patched"]})
    await mop.run_mention_chain(graph, reg, "t", "proto", "go", max_hops=1, resolve=resolve)

    written = [m for update in graph.written for m in update["messages"]]
    hop = written[2]  # the addressing message of the second exchange
    assert hop.additional_kwargs["room"] == {"from": "proto", "to": "claude-code"}
    assert 'from="proto"' in hop.content


@pytest.mark.asyncio
async def test_a_later_participant_sees_the_hop_attributed_correctly():
    """End to end: what the room recorded is what the next catch-up reads back."""
    graph = _Graph()
    reg = _Registry({"proto": ["@claude-code check line 40"], "claude-code": ["patched"]})
    await mop.run_mention_chain(graph, reg, "t", "proto", "go", max_hops=1, resolve=resolve)

    room = [m for update in graph.written for m in update["messages"]]
    window, _ = mop.catchup_window(room, "vera")
    assert ("proto", "check line 40") in window
    assert not any(author == "operator" and "check line 40" in text for author, text in window)
