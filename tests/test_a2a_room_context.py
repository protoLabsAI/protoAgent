"""A2A room continuity — an addressed peer keeps ONE conversation across addresses (#3360).

Before this, ``dispatch_into_room`` handed a ``conversation_key`` only to ``acp``
delegates and ``DelegateRegistry.dispatch`` refused it for every other type, so an
``a2a`` participant — a fleet member, a peer protoAgent — started a brand-new
conversation on every single address and the bounded catch-up window was its entire
picture of the room. Multi-round rooms (#3359) re-shipped that window up to
``room.max_rounds`` times per address for exactly the delegates that couldn't resume.

The fix is the protocol's own answer: A2A's ``contextId`` groups messages into one
conversation, and protoAgent's own server already treats an inbound ``context_id`` as the
session the turn runs in. We **echo what the peer assigned** rather than deriving one —
see ``plugins/delegates/conversations`` for why — so a peer that assigns none, or ignores
ours, sees exactly the wire it saw before.
"""

from __future__ import annotations

import asyncio
import json as _json

import httpx
import pytest

from plugins.delegates import conversations
from plugins.delegates.adapters import DelegateError
from plugins.delegates.registry import DelegateRegistry

PEER_URL = "https://peer.example/a2a"
OTHER_URL = "https://other.example/a2a"


# ── harness ───────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    """Fake ``httpx.AsyncClient`` — records every posted body, answers via ``handler``.

    ``handler(url, body) -> _Resp``. No ``get``, so the adapter's pre-flight agent-card
    probe fails inside its own try/except and dispatch proceeds (it only fails fast on a
    card that CLEARLY advertises an incompatible protocol version).
    """

    def __init__(self, handler, bodies, **_client_kw):
        self._handler = handler
        self._bodies = bodies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        self._bodies.append({"url": url, **(json or {})})
        return self._handler(url, json or {})


def _task(*, context_id="ctx-1", text="ok", task_id="t1", state="TASK_STATE_COMPLETED") -> dict:
    task = {"id": task_id, "status": {"state": state}, "artifacts": [{"parts": [{"text": text}]}]}
    if context_id:
        task["contextId"] = context_id
    return task


def _result(**kw) -> _Resp:
    return _Resp({"jsonrpc": "2.0", "result": {"task": _task(**kw)}})


def _always(**kw):
    """A handler that answers every method with the same completed task."""
    return lambda _url, _body: _result(**kw)


@pytest.fixture
def wire(monkeypatch):
    """Install the fake transport; the returned callable takes a handler and hands back
    the (shared) list of posted bodies, so a test can swap the peer's behavior mid-run."""
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    async def _noop(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    bodies: list[dict] = []

    def _install(handler):
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(handler, bodies, **kw))
        return bodies

    return _install


@pytest.fixture(autouse=True)
def _forget_contexts():
    conversations.reset()
    yield
    conversations.reset()


def _registry(*, url=PEER_URL, name="peer") -> DelegateRegistry:
    return DelegateRegistry([{"name": name, "type": "a2a", "url": url}])


def _sends(bodies) -> list[dict]:
    return [b["params"]["message"] for b in bodies if b.get("method") == "SendMessage"]


# ── the room's repeated addresses land in one peer-side conversation ───────────


async def test_repeated_addresses_in_one_thread_reuse_the_peer_assigned_context(wire):
    """The whole point: address the same member twice from one thread and the second
    SendMessage carries the ``contextId`` the peer assigned the first."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()

    assert await reg.dispatch("peer", "what broke?", conversation_key="thread-1") == "ok"
    assert await reg.dispatch("peer", "and now?", conversation_key="thread-1") == "ok"

    first, second = _sends(bodies)
    # Nothing had been assigned yet on the first address — the peer owns context identity,
    # so we send none and let it mint one.
    assert "contextId" not in first
    assert second["contextId"] == "ctx-room"


async def test_a_third_address_still_carries_the_same_context(wire):
    """Continuity is not a one-shot: every later address rides the same conversation."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()

    for _ in range(3):
        await reg.dispatch("peer", "…", conversation_key="thread-1")

    assert [m.get("contextId") for m in _sends(bodies)] == [None, "ctx-room", "ctx-room"]


async def test_different_threads_do_not_collide(wire):
    """A second chat thread is a second conversation — it must not inherit the first's
    context, or two unrelated rooms would be spliced together on the peer's side."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()

    await reg.dispatch("peer", "hi", conversation_key="thread-1")
    await reg.dispatch("peer", "hi", conversation_key="thread-2")
    await reg.dispatch("peer", "again", conversation_key="thread-1")

    assert [m.get("contextId") for m in _sends(bodies)] == [None, None, "ctx-room"]


async def test_different_delegates_do_not_collide(wire):
    """Two peers addressed from one thread each keep their OWN context."""
    reg = DelegateRegistry(
        [
            {"name": "alpha", "type": "a2a", "url": PEER_URL},
            {"name": "beta", "type": "a2a", "url": OTHER_URL},
        ]
    )
    bodies = wire(lambda url, _b: _result(context_id="ctx-alpha" if url == PEER_URL else "ctx-beta"))

    await reg.dispatch("alpha", "hi", conversation_key="thread-1")
    await reg.dispatch("beta", "hi", conversation_key="thread-1")
    await reg.dispatch("alpha", "again", conversation_key="thread-1")
    await reg.dispatch("beta", "again", conversation_key="thread-1")

    assert [m.get("contextId") for m in _sends(bodies)] == [None, None, "ctx-alpha", "ctx-beta"]


async def test_re_pointing_a_delegate_forgets_the_old_peers_context(wire):
    """The url is part of the key: a context id is only meaningful to the peer that
    minted it, so an operator re-pointing the delegate must not leak it to another host."""
    bodies = wire(_always(context_id="ctx-room"))

    await _registry(url=PEER_URL).dispatch("peer", "hi", conversation_key="thread-1")
    await _registry(url=OTHER_URL).dispatch("peer", "hi", conversation_key="thread-1")

    assert [m.get("contextId") for m in _sends(bodies)] == [None, None]


# ── degradation: a peer that assigns nothing gets exactly today's wire ─────────


async def test_a_peer_that_returns_no_context_id_degrades_to_todays_behavior(wire):
    """No contextId on the way back ⇒ none on the way out, ever. Not an error, not a
    retry, not an invented id — the pre-#3360 request, byte for byte."""
    bodies = wire(_always(context_id=""))
    reg = _registry()

    assert await reg.dispatch("peer", "hi", conversation_key="thread-1") == "ok"
    assert await reg.dispatch("peer", "again", conversation_key="thread-1") == "ok"

    assert all("contextId" not in m for m in _sends(bodies))
    assert conversations.snapshot() == {}


async def test_a_dispatch_without_a_conversation_key_never_sends_a_context(wire):
    """``delegate_to`` passes no conversation key; that path is untouched — no contextId
    sent, and nothing remembered that a later room address could pick up."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()

    await reg.dispatch("peer", "one-off")
    await reg.dispatch("peer", "another one-off")

    assert all("contextId" not in m for m in _sends(bodies))
    assert conversations.snapshot() == {}


# ── the parked-task resume path still wins ────────────────────────────────────


def _park_and_resume_handler(*, parked_context: str):
    """GetTask answers a task parked on input; SendMessage completes it."""

    def _handler(_url, body):
        if body.get("method") == "GetTask":
            return _Resp(
                {
                    "jsonrpc": "2.0",
                    "result": {
                        "task": _task(
                            context_id=parked_context,
                            task_id="parked-1",
                            state="TASK_STATE_INPUT_REQUIRED",
                            text="which branch?",
                        )
                    },
                }
            )
        return _result(context_id=parked_context, text="done")

    return _handler


async def test_a_parked_tasks_context_beats_the_remembered_room_context(wire):
    """Both exist ⇒ the parked task's own context wins. A resume answers ONE task, and
    the peer resumes it only under the context it parked in; sending the room's context
    would open a second task and leave the park waiting forever."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")  # learns ctx-room
    assert _sends(bodies)[0].get("contextId") is None

    wire(_park_and_resume_handler(parked_context="ctx-parked"))
    reply = await reg.dispatch("peer", "the main one", conversation_key="thread-1", resume_task_id="parked-1")
    assert reply == "done"

    resume = _sends(bodies)[-1]
    assert resume["taskId"] == "parked-1"
    assert resume["contextId"] == "ctx-parked"


async def test_a_resume_does_not_overwrite_the_rooms_remembered_context(wire):
    """A resume says nothing about which context the ROOM continues in — the next
    ordinary address must still land in the conversation the room was having."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")

    wire(_park_and_resume_handler(parked_context="ctx-parked"))
    await reg.dispatch("peer", "answer", conversation_key="thread-1", resume_task_id="parked-1")

    wire(_always(context_id="ctx-room"))
    await reg.dispatch("peer", "and now?", conversation_key="thread-1")

    assert _sends(bodies)[-1]["contextId"] == "ctx-room"


async def test_resume_without_a_conversation_key_is_unchanged(wire):
    """The HITL delegation chain as ``delegate_to`` drives it: no conversation key at
    all, and the parked task's context still rides the resume."""
    bodies = wire(_park_and_resume_handler(parked_context="ctx-parked"))
    reg = _registry()

    assert await reg.dispatch("peer", "the main one", resume_task_id="parked-1") == "done"

    resume = _sends(bodies)[-1]
    assert resume["taskId"] == "parked-1" and resume["contextId"] == "ctx-parked"


# ── the refusals that remain ──────────────────────────────────────────────────


async def test_openai_compat_still_refuses_a_conversation_key_and_says_why():
    """A stateless chat endpoint has no server-side conversation to key — the refusal
    has to say that, rather than the (now false) "acp only"."""
    reg = DelegateRegistry([{"name": "model", "type": "openai", "url": "https://g/v1", "model": "m"}])
    with pytest.raises(DelegateError) as ei:
        await reg.dispatch("model", "hi", conversation_key="thread-1")
    message = str(ei.value)
    assert "stateless" in message
    assert "openai" in message
    assert "only applies to acp" not in message


async def test_a2a_still_refuses_a_permissions_ceiling():
    """Continuity is not permission: only ``acp`` can enforce a readonly ceiling, and
    widening ``conversation_key`` must not have widened that too."""
    reg = _registry()
    with pytest.raises(DelegateError, match="cannot enforce a permissions ceiling"):
        await reg.dispatch("peer", "hi", conversation_key="thread-1", permissions="readonly")


def test_the_rooms_conversational_types_mirror_the_registrys():
    """``graph/mention_op`` keeps the list as a literal (it must stay host-free, and
    ``graph/`` never imports ``plugins/``), so pin the two together — a room that
    withholds the key from a type the registry accepts silently loses continuity."""
    from graph.mention_op import _CONVERSATIONAL_TYPES as room_types
    from plugins.delegates.registry import _CONVERSATIONAL_TYPES as registry_types

    assert room_types == registry_types


# ── the store itself ──────────────────────────────────────────────────────────


def test_the_context_map_is_bounded():
    """One entry per (thread, delegate, url) with an LRU ceiling, so a long-lived
    instance that has addressed many threads can't grow it without limit."""
    for i in range(conversations._MAX_ENTRIES + 25):
        conversations.remember(f"thread-{i}", "peer", PEER_URL, f"ctx-{i}")

    assert len(conversations.snapshot()) == conversations._MAX_ENTRIES
    # The oldest went first; the newest is still there.
    assert conversations.remembered("thread-0", "peer", PEER_URL) == ""
    last = conversations._MAX_ENTRIES + 24
    assert conversations.remembered(f"thread-{last}", "peer", PEER_URL) == f"ctx-{last}"


def test_an_empty_context_id_is_never_remembered():
    conversations.remember("thread-1", "peer", PEER_URL, "")
    assert conversations.remembered("thread-1", "peer", PEER_URL) == ""
    assert conversations.snapshot() == {}


# ── reading the id off whatever envelope the peer used ────────────────────────


def test_extract_context_id_reads_every_envelope_the_adapter_can_see():
    """``_extract_context_id`` claims the same envelope tolerance as ``_extract_text``;
    pin it, because the only shape a protoAgent peer produces is the first one and the
    rest would rot unnoticed behind it."""
    from tools.a2a_parse import _extract_context_id

    assert _extract_context_id({"task": {"contextId": "c"}}) == "c"  # SendMessage
    assert _extract_context_id({"id": "t1", "contextId": "c"}) == "c"  # bare GetTask task
    assert _extract_context_id({"message": {"contextId": "c"}}) == "c"  # bare Message reply
    assert _extract_context_id({"task": {"status": {"message": {"contextId": "c"}}}}) == "c"
    # Nothing to learn ⇒ "", which is "send no contextId", never an invented one.
    assert _extract_context_id({"task": {"id": "t1"}}) == ""
    assert _extract_context_id({}) == ""
    assert _extract_context_id(None) == ""
    assert _extract_context_id("not a dict") == ""


async def test_the_context_can_arrive_on_the_poll_rather_than_the_ack(wire):
    """An ASYNC-style peer acknowledges SendMessage with a bare accepted task and fills
    the envelope out as it works, so the contextId shows up on ``GetTask``. protoAgent
    peers answer inline and never reach the poll loop — which is exactly why learning
    only off the ack would have been an invisible gap for everyone else."""
    acked = {"jsonrpc": "2.0", "result": {"task": {"id": "t1", "status": {"state": "TASK_STATE_WORKING"}}}}

    def _handler(_url, body):
        if body.get("method") == "GetTask":
            return _result(context_id="ctx-late")
        return _Resp(acked)

    bodies = wire(_handler)
    reg = _registry()

    assert await reg.dispatch("peer", "slow one", conversation_key="thread-1") == "ok"
    assert await reg.dispatch("peer", "and now?", conversation_key="thread-1") == "ok"

    assert [m.get("contextId") for m in _sends(bodies)] == [None, "ctx-late"]


async def test_a_failed_address_neither_learns_nor_forgets(wire):
    """A transport failure never reaches the learn step, so it can't pin the conversation
    to a context we were never answered in — and it must not drop the one we had either:
    the room writes the failure onto the thread and carries on, and the next address
    belongs in the same conversation as the last successful one."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")

    def _refuse(_url, _body):
        raise httpx.ConnectError("no route")

    wire(_refuse)
    with pytest.raises(DelegateError):
        await reg.dispatch("peer", "boom", conversation_key="thread-1")

    wire(_always(context_id="ctx-room"))
    await reg.dispatch("peer", "still here?", conversation_key="thread-1")
    assert _sends(bodies)[-1]["contextId"] == "ctx-room"


async def test_one_member_under_two_delegate_names_keeps_two_conversations(wire):
    """The conservative half of the key. Two roster rows can carry two different
    credentials, so a matching url is not licence to merge their conversations — the
    member holds two half-rooms instead, which the rooms guide says out loud."""
    reg = DelegateRegistry(
        [
            {"name": "alpha", "type": "a2a", "url": PEER_URL},
            {"name": "beta", "type": "a2a", "url": PEER_URL},
        ]
    )
    bodies = wire(_always(context_id="ctx-room"))

    for name in ("alpha", "beta", "alpha", "beta"):
        await reg.dispatch(name, "hi", conversation_key="thread-1")

    assert [m.get("contextId") for m in _sends(bodies)] == [None, None, "ctx-room", "ctx-room"]
    assert len(conversations.snapshot()) == 2


def test_a_second_context_for_one_conversation_replaces_the_first():
    """Addresses racing on one (thread, delegate) both find nothing remembered, so the
    peer mints two contexts and both are learned. The map holds exactly ONE — the last —
    so the cost is a lost conversation, never a mixed or spliced id. (The room dispatches
    sequentially and never does this; ``host.invoke_delegate`` is a public seam and
    nothing stops a plugin from it.)"""
    conversations.remember("thread-1", "peer", PEER_URL, "ctx-a")
    conversations.remember("thread-1", "peer", PEER_URL, "ctx-b")

    assert conversations.remembered("thread-1", "peer", PEER_URL) == "ctx-b"
    assert len(conversations.snapshot()) == 1


# ── continuity must not outlive the history it points at ──────────────────────


def test_forget_drops_one_conversation_whole_and_nothing_else():
    """A rewind is about the CONVERSATION, not about whichever participant happened to
    be addressed last — every member of the cast loses the erased exchange, and the
    room next door loses nothing."""
    conversations.remember("thread-1", "alpha", PEER_URL, "a1")
    conversations.remember("thread-1", "beta", OTHER_URL, "b1")
    conversations.remember("thread-2", "alpha", PEER_URL, "a2")

    assert conversations.forget("thread-1") == 2
    assert conversations.remembered("thread-1", "alpha", PEER_URL) == ""
    assert conversations.remembered("thread-1", "beta", OTHER_URL) == ""
    assert conversations.remembered("thread-2", "alpha", PEER_URL) == "a2"


def test_forgetting_something_we_never_held_is_a_no_op():
    """Called from best-effort cleanup paths, so it has to be safe on every input."""
    assert conversations.forget("never-seen") == 0
    assert conversations.forget("") == 0


async def test_a_forgotten_conversation_opens_a_fresh_context(wire):
    """The whole point of forgetting: the next address is the pre-#3360 wire again, so
    the peer starts a new conversation instead of resuming the one that was erased."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")
    await reg.dispatch("peer", "again", conversation_key="thread-1")
    assert _sends(bodies)[-1]["contextId"] == "ctx-room"

    assert reg.forget_conversation("thread-1") == 1
    await reg.dispatch("peer", "after the rewind", conversation_key="thread-1")
    assert "contextId" not in _sends(bodies)[-1]


def test_the_registry_seam_never_raises(monkeypatch):
    """It is called from cleanup paths that must not be able to fail the gesture they
    are cleaning up after."""
    reg = _registry()

    def _boom(_key):
        raise RuntimeError("store on fire")

    monkeypatch.setattr(conversations, "forget", _boom)
    assert reg.forget_conversation("thread-1") == 0


# ── the core-side wiring: rewind / fork / delete ──────────────────────────────


def test_the_core_seam_is_duck_typed_and_swallowing(monkeypatch):
    """``server.chat`` reaches the plugin through ``STATE.delegate_registry`` — the same
    roster the ``@`` dispatch already reads — so core keeps its distance from
    ``plugins/``. Every way that can be absent or broken degrades to 'dropped nothing'."""
    from runtime.state import STATE
    from server.chat import forget_delegate_conversations

    class _Older:
        """A fork pinned to a delegates plugin from before this seam existed."""

    class _Broken:
        def forget_conversation(self, _key):
            raise RuntimeError("nope")

    for roster in (None, _Older(), _Broken()):
        monkeypatch.setattr(STATE, "delegate_registry", roster, raising=False)
        assert forget_delegate_conversations("a2a:s1") == 0

    monkeypatch.setattr(STATE, "delegate_registry", _registry(), raising=False)
    conversations.remember("a2a:s1", "peer", PEER_URL, "ctx-room")
    assert forget_delegate_conversations("", "a2a:s1") == 1  # blanks skipped, not counted


def _core_state(monkeypatch):
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "checkpointer", None, raising=False)
    monkeypatch.setattr(STATE, "thread_id_resolver", None, raising=False)
    monkeypatch.setattr(STATE, "delegate_registry", _registry(), raising=False)


async def test_rewinding_a_room_forgets_its_peer_contexts(monkeypatch):
    """Rewind is destructive by design, and before continuity existed it was TOTAL —
    the participant remembered nothing. Keep it total: leave the pointer alive and the
    next address rejoins the peer's copy, so the discarded exchange comes back in the
    participant's voice."""
    import graph.rewind_op as rop
    from server.chat import rewind_session

    _core_state(monkeypatch)

    async def _rewound(_graph, _cp, _tid, **_kw):
        return {"found": True, "kept": 2, "removed": 3, "reason": ""}

    monkeypatch.setattr(rop, "rewind_thread", _rewound)
    conversations.remember("a2a:s1", "peer", PEER_URL, "ctx-room")

    await rewind_session("s1")
    assert conversations.snapshot() == {}


async def test_a_rewind_that_discarded_nothing_keeps_continuity(monkeypatch):
    """"That's already the last message" erased nothing, so dropping the participant's
    continuity would be a pure loss with no leak to close."""
    import graph.rewind_op as rop
    from server.chat import rewind_session

    _core_state(monkeypatch)

    async def _noop(_graph, _cp, _tid, **_kw):
        return {"found": True, "kept": 5, "removed": 0, "reason": "noop"}

    monkeypatch.setattr(rop, "rewind_thread", _noop)
    conversations.remember("a2a:s1", "peer", PEER_URL, "ctx-room")

    await rewind_session("s1")
    assert conversations.remembered("a2a:s1", "peer", PEER_URL) == "ctx-room"


async def test_a_fork_clears_the_destination_and_never_inherits_the_source(monkeypatch):
    """Two threads writing into one peer conversation would splice two divergent rooms
    together on the peer's side. The destination gets a clean slate — including from
    whatever previously occupied that session id."""
    import graph.rewind_op as rop
    from server.chat import fork_session

    _core_state(monkeypatch)

    async def _forked(_graph, _cp, _src, _dst, **_kw):
        return {"found": True, "kept": 4, "discarded": 0, "reason": ""}

    monkeypatch.setattr(rop, "fork_thread", _forked)
    conversations.remember("a2a:src", "peer", PEER_URL, "ctx-source")
    conversations.remember("a2a:dst", "peer", PEER_URL, "ctx-stale")

    await fork_session("src", "dst")
    assert conversations.remembered("a2a:dst", "peer", PEER_URL) == ""  # cleared
    assert conversations.remembered("a2a:src", "peer", PEER_URL) == "ctx-source"  # untouched
