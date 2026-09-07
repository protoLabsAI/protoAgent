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
import itertools
import json as _json
import time as _time

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


@pytest.fixture
def fast_clock(monkeypatch):
    """A monotonic clock that jumps 1000s per read, so the adapter's poll deadline is
    already blown on the first check — the "still running after Ns" terminus without a
    real wait."""
    ticks = itertools.count(0.0, 1000.0)
    monkeypatch.setattr(_time, "monotonic", lambda: next(ticks))


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
    """A second chat thread is a second conversation on OUR side of the map: it must not
    inherit the first's context, or we would splice two unrelated rooms together.

    That is all this can prove, and all the map can promise. The id is the PEER's, so a
    peer that answers with one constant ``contextId`` (its authenticated session, say)
    hands the same id to both threads; both learn it, both send it, and the peer merges
    the two rooms on its own side. Nothing on the wire lets a client detect or prevent
    that — see the guide's "a best effort the peer owns"."""
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
    """No key ⇒ no continuity, in both directions: nothing sent, and nothing remembered
    that a later keyed address could pick up.

    This is the registry seam, not ``delegate_to``. The foreground ``delegate_to`` tool
    goes through the room helper and DOES get this thread's key — see
    ``tests/test_delegate_to_room.py``; the key-less callers are
    ``delegate_to(background=True)``, a parked-task resume, a managed-git ``item_id``
    claim, and a plugin calling ``host.invoke_delegate`` directly."""
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


# ── continuity must not outlive the EXCHANGE either ───────────────────────────
#
# A remembered context has to name a peer-side conversation that is idle and whose last
# exchange is on this thread. Every terminus that breaks that invariant drops the pointer,
# which is the pre-#3360 wire: the next address opens a fresh conversation and just runs.


def _parks(*, context_id="ctx-room", task_id="parked-9"):
    """A peer that parks on an input interrupt (its ``ask_human`` / a tool approval)."""
    return _always(context_id=context_id, task_id=task_id, state="TASK_STATE_INPUT_REQUIRED", text="which branch?")


async def test_a_park_drops_the_rooms_continuity_instead_of_re_parking_forever(wire):
    """THE livelock this closes. A park leaves the peer's thread holding a pending
    interrupt, and a room address is not a resume — a protoAgent peer queues a fresh
    message on such a thread as steering and re-yields the SAME interrupt (origin ``a2a``
    is deliberately not autonomous, ``server.chat._hold_if_hitl_pending``). So re-sending
    the room's context would hand the room the identical question back on every later
    address, parking another task each time, with no escape but a rewind or a restart:
    only ``delegate_to(..., resume_task_id=…)`` can answer a park, and that bypasses the
    room. Drop the pointer and the operator's next ``@`` is answered normally."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")
    await reg.dispatch("peer", "and now?", conversation_key="thread-1")
    assert _sends(bodies)[-1]["contextId"] == "ctx-room"

    wire(_parks(context_id="ctx-room"))
    reply = await reg.dispatch("peer", "fix the build", conversation_key="thread-1")
    assert "needs input" in reply and "parked-9" in reply
    assert conversations.remembered("thread-1", "peer", PEER_URL) == ""

    wire(_always(context_id="ctx-after"))
    assert await reg.dispatch("peer", "yes, the main branch", conversation_key="thread-1") == "ok"
    assert "contextId" not in _sends(bodies)[-1]


async def test_a_park_on_a_first_address_is_never_remembered(wire):
    """Nothing to drop, and nothing to learn either — the parked context must not become
    the room's, or the very next address walks into the hold."""
    bodies = wire(_parks(context_id="ctx-parked"))
    reg = _registry()

    assert "needs input" in await reg.dispatch("peer", "deploy X", conversation_key="thread-1")
    assert conversations.snapshot() == {}

    wire(_always(context_id="ctx-after"))
    await reg.dispatch("peer", "never mind, just the version", conversation_key="thread-1")
    assert all("contextId" not in m for m in _sends(bodies))


async def test_a_peer_still_working_past_the_deadline_loses_the_pointer(wire, fast_clock):
    """The room records this address as FAILED and drops the member for the rest of the
    turn, so whatever the peer eventually writes into that context is history this side
    never sees. Keeping the pointer would give the next address that invisible history —
    and queue it behind the very turn the room gave up on, since a protoAgent peer
    serializes turns per thread."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")

    working = {"jsonrpc": "2.0", "result": {"task": {"id": "t9", "contextId": "ctx-room", "status": {"state": "TASK_STATE_WORKING"}}}}
    wire(lambda _u, _b: _Resp(working))
    with pytest.raises(DelegateError, match="still running"):
        await reg.dispatch("peer", "run the migration", conversation_key="thread-1")
    assert conversations.remembered("thread-1", "peer", PEER_URL) == ""

    wire(_always(context_id="ctx-after"))
    await reg.dispatch("peer", "how did it go?", conversation_key="thread-1")
    assert "contextId" not in _sends(bodies)[-1]


async def test_a_read_timeout_loses_the_pointer_but_unreachable_keeps_it(wire):
    """The two transport failures are not the same event. A READ timeout means the peer
    TOOK the message and is still working on it — the protoAgent case, since our own
    server answers SendMessage inline, so the read budget blows before any poll loop. An
    unreachable peer never got the message at all, so its conversation is exactly as the
    map describes it and the next address belongs in it."""
    wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")
    await reg.dispatch("peer", "hi again", conversation_key="thread-2")

    def _read_timeout(_url, _body):
        raise httpx.ReadTimeout("peer still thinking")

    wire(_read_timeout)
    with pytest.raises(DelegateError, match="timed out"):
        await reg.dispatch("peer", "slow one", conversation_key="thread-1")
    assert conversations.remembered("thread-1", "peer", PEER_URL) == ""

    def _unreachable(_url, _body):
        raise httpx.ConnectError("no route")

    wire(_unreachable)
    with pytest.raises(DelegateError, match="unreachable"):
        await reg.dispatch("peer", "anyone home?", conversation_key="thread-2")
    assert conversations.remembered("thread-2", "peer", PEER_URL) == "ctx-room"


async def test_a_terminal_task_with_no_readable_answer_loses_the_pointer(wire):
    """The peer has an exchange the thread does not. Same divergence, same fallback."""
    wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")

    silent = {"jsonrpc": "2.0", "result": {"task": {"id": "t9", "contextId": "ctx-room", "status": {"state": "TASK_STATE_COMPLETED"}}}}
    wire(lambda _u, _b: _Resp(silent))
    with pytest.raises(DelegateError, match="returned no text"):
        await reg.dispatch("peer", "?", conversation_key="thread-1")
    assert conversations.remembered("thread-1", "peer", PEER_URL) == ""


async def test_a_resume_never_drops_the_rooms_pointer(wire):
    """A resume is the LEAD answering one parked task; it says nothing about the context
    the room continues in, so neither its learn nor its drop is the room's to make."""
    bodies = wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hi", conversation_key="thread-1")

    wire(_park_and_resume_handler(parked_context="ctx-parked"))
    await reg.dispatch("peer", "the main one", conversation_key="thread-1", resume_task_id="parked-1")

    wire(_always(context_id="ctx-room"))
    await reg.dispatch("peer", "and now?", conversation_key="thread-1")
    assert _sends(bodies)[-1]["contextId"] == "ctx-room"


def test_forget_one_drops_a_single_participant():
    """The per-participant half of ``forget()``: the adapter's own cleanup must not
    evict the rest of the cast, or a park by one member would reset everyone."""
    conversations.remember("thread-1", "alpha", PEER_URL, "a1")
    conversations.remember("thread-1", "beta", OTHER_URL, "b1")

    assert conversations.forget_one("thread-1", "alpha", PEER_URL) is True
    assert conversations.forget_one("thread-1", "alpha", PEER_URL) is False  # idempotent
    assert conversations.remembered("thread-1", "alpha", PEER_URL) == ""
    assert conversations.remembered("thread-1", "beta", OTHER_URL) == "b1"
    assert conversations.forget_one("", "alpha", PEER_URL) is False


# ── the credential is part of the key ─────────────────────────────────────────


def _authed(token: str, *, name="peer", url=PEER_URL) -> DelegateRegistry:
    return DelegateRegistry([{"name": name, "type": "a2a", "url": url, "auth": {"scheme": "bearer", "token": token}}])


async def test_rotating_a_delegates_credential_starts_a_fresh_conversation(wire):
    """The map's docstring justifies keying on the delegate NAME because two rows can
    carry two different credentials and merging them would cross that boundary. Editing
    ONE row's token in place (same name, same url) crosses exactly that boundary unless
    the credential is in the key too."""
    bodies = wire(_always(context_id="ctx-room"))

    await _authed("old-token").dispatch("peer", "hi", conversation_key="thread-1")
    await _authed("new-token").dispatch("peer", "hi", conversation_key="thread-1")
    # And the ORIGINAL credential still continues its own conversation.
    await _authed("old-token").dispatch("peer", "still me", conversation_key="thread-1")

    assert [m.get("contextId") for m in _sends(bodies)] == [None, None, "ctx-room"]


def test_a_credential_is_never_stored_in_the_key():
    """It rides as a one-way digest — the map is a debugging surface (``snapshot()``)."""
    conversations.remember("thread-1", "peer", PEER_URL, "ctx-room", "bearer:hunter2")
    assert not any("hunter2" in part for key in conversations.snapshot() for part in key)
    assert conversations.remembered("thread-1", "peer", PEER_URL, "bearer:hunter2") == "ctx-room"
    assert conversations.remembered("thread-1", "peer", PEER_URL, "bearer:other") == ""


def test_a_non_string_context_id_is_read_as_absent():
    """The id is ECHOED onto the next request, and an a2a-sdk peer's ParseDict rejects a
    non-string ``contextId`` — so coercing one out-of-spec reply would turn every later
    address in that conversation into a JSON-RPC error with no fallback. Reading nothing
    degrades to the pre-#3360 wire; echoing junk breaks the conversation outright."""
    from tools.a2a_parse import _extract_context_id

    assert _extract_context_id({"task": {"contextId": 12345}}) == ""
    assert _extract_context_id({"task": {"contextId": {"id": "c"}}}) == ""
    assert _extract_context_id({"task": {"contextId": None}}) == ""
    # A well-formed sibling envelope still wins over an out-of-spec task-level one.
    assert _extract_context_id({"task": {"contextId": 1}, "message": {"contextId": "c"}}) == "c"


async def test_a_pool_timeout_keeps_the_conversation_a_read_timeout_drops_it(wire):
    """``PoolTimeout`` is OUR connection pool stalling, not the peer timing out.

    Nothing was sent, so the peer's conversation is exactly where we left it and the
    remembered context is still good — dropping it would throw a live room away over a
    local resource stall. A READ timeout is the opposite: bytes went out, the peer may be
    working, and its answer lands in a context this side will never see, so that one must
    drop. ``httpx.TimeoutException`` covers both, which is why they are caught separately.
    """
    from plugins.delegates.adapters import KIND_TIMEOUT, KIND_UNREACHABLE

    # Establish a remembered context the timeouts can threaten.
    wire(_always(context_id="ctx-room"))
    reg = _registry()
    await reg.dispatch("peer", "hello", conversation_key="thread-1")
    await reg.dispatch("peer", "again", conversation_key="thread-1")
    assert conversations.remembered("thread-1", "peer", PEER_URL) == "ctx-room"

    def _pool_timeout(*_a, **_kw):
        raise httpx.PoolTimeout("no connection available")

    wire(_pool_timeout)
    with pytest.raises(Exception) as caught:
        await reg.dispatch("peer", "…", conversation_key="thread-1")
    assert getattr(caught.value, "kind", "") == KIND_UNREACHABLE
    assert conversations.remembered("thread-1", "peer", PEER_URL) == "ctx-room", (
        "a local pool stall must not cost the room its conversation"
    )

    def _read_timeout(*_a, **_kw):
        raise httpx.ReadTimeout("peer is still working")

    wire(_read_timeout)
    with pytest.raises(Exception) as caught:
        await reg.dispatch("peer", "…", conversation_key="thread-1")
    assert getattr(caught.value, "kind", "") == KIND_TIMEOUT
    assert conversations.remembered("thread-1", "peer", PEER_URL) == "", (
        "the peer may have the request; its answer lands where we cannot see it"
    )


# ── the originating session, kept apart from the resolved key (#3362) ─────────
#
# A resolved conversation key cannot be reversed into the chat session it came from — a
# custom thread-id resolver (ADR 0029 §D4 / #571) mints keys off request metadata that a
# delete route never carries. So each entry records its origin session too, and an
# origin-scoped forget drops by that recorded value EXACTLY — never by the shape of a key.


def test_a_remembered_context_records_its_origin_session_apart_from_its_key():
    """The resolved thread key and the chat session it came from are two separate
    identities on the entry: the key can't be reversed into the session, so the origin is
    kept explicitly when the caller knows it. The lookup contract is unchanged — still
    keyed on the resolved conversation key, still returns the bare contextId."""
    conversations.remember("resolved-thread-key", "peer", PEER_URL, "ctx-1", session_id="s1")

    (entry,) = conversations.snapshot().values()
    assert entry.context_id == "ctx-1"
    assert entry.session_id == "s1"
    assert conversations.remembered("resolved-thread-key", "peer", PEER_URL) == "ctx-1"


def test_an_origin_is_optional_and_defaults_to_none_recorded():
    """A caller that does not know the origin (or a delegates plugin from before this seam)
    records none — the entry is stored and reachable by KEY exactly as before, it just
    carries no session for the origin-scoped forget to match."""
    conversations.remember("thread-1", "peer", PEER_URL, "ctx-1")  # no session_id

    (entry,) = conversations.snapshot().values()
    assert entry.context_id == "ctx-1"
    assert entry.session_id == ""
    assert conversations.remembered("thread-1", "peer", PEER_URL) == "ctx-1"


def test_forget_by_session_drops_every_entry_with_that_origin_whatever_the_key():
    """r2: a custom resolver can key a session's thread to ANY value — a UUID, a
    tenant-scoped handle — so a delete cannot find the entries by the shape of the key.
    Recording the originating session lets it drop exactly them, across delegates and urls,
    and leave a different session's rows alone."""
    conversations.remember("weird-key-1", "alpha", PEER_URL, "a1", session_id="s1")
    conversations.remember("weird-key-2", "beta", OTHER_URL, "b1", session_id="s1")
    conversations.remember("weird-key-3", "alpha", PEER_URL, "c1", session_id="s2")

    assert conversations.forget_by_session("s1") == 2
    assert conversations.remembered("weird-key-1", "alpha", PEER_URL) == ""
    assert conversations.remembered("weird-key-2", "beta", OTHER_URL) == ""
    assert conversations.remembered("weird-key-3", "alpha", PEER_URL) == "c1"  # other session untouched


def test_forget_by_session_is_exact_never_a_prefix_or_substring_match():
    """r3: the origin is matched WHOLE. A session whose id is a prefix (or substring) of
    another's — or of an entry's resolved KEY — must not drag the other down with it, the
    trap a ``key.startswith(...)`` / ``sid in key`` heuristic would fall into."""
    conversations.remember("a2a:s1", "peer", PEER_URL, "c1", session_id="s1")
    conversations.remember("a2a:s1-child", "peer", PEER_URL, "c2", session_id="s1-child")

    # 's1' is a prefix of BOTH keys and of the second session id — none of that matters.
    assert conversations.forget_by_session("s1") == 1
    assert conversations.remembered("a2a:s1", "peer", PEER_URL) == ""
    assert conversations.remembered("a2a:s1-child", "peer", PEER_URL) == "c2"


def test_forget_by_session_ignores_a_blank_and_never_sweeps_originless_entries():
    """A blank origin match is a no-op, NOT a sweep of every entry that recorded no origin
    — otherwise a stray ``forget_by_session('')`` would erase the pre-#3362 entries the
    key-scoped forget is still responsible for."""
    conversations.remember("thread-1", "peer", PEER_URL, "ctx-1")  # no origin recorded
    conversations.remember("thread-2", "peer", PEER_URL, "ctx-2", session_id="s1")

    assert conversations.forget_by_session("") == 0
    assert conversations.forget_by_session("s-not-present") == 0
    assert conversations.remembered("thread-1", "peer", PEER_URL) == "ctx-1"
    assert conversations.remembered("thread-2", "peer", PEER_URL) == "ctx-2"


def test_key_scoped_and_session_scoped_forget_are_independent():
    """The two forgets answer different events and reach different rows. ``forget`` still
    drops by resolved key alone — origin or not — and ``forget_by_session`` drops by origin
    alone; neither is a substring of the other's behaviour."""
    conversations.remember("k1", "peer", PEER_URL, "c1", session_id="s1")
    conversations.remember("k2", "peer", PEER_URL, "c2", session_id="s1")

    # Key-scoped drops only the matching key, though both share an origin.
    assert conversations.forget("k1") == 1
    assert conversations.remembered("k2", "peer", PEER_URL) == "c2"
    # Session-scoped then mops up the rest by origin.
    assert conversations.forget_by_session("s1") == 1
    assert conversations.remembered("k2", "peer", PEER_URL) == ""


def test_the_registry_exposes_session_scoped_forget():
    """The registry publishes the origin-scoped forget the same way it does the key-scoped
    one, so core can reach it duck-typed through ``STATE.delegate_registry``."""
    reg = _registry()
    conversations.remember("resolver-minted-key", "peer", PEER_URL, "ctx-room", session_id="s1")
    conversations.remember("another-key", "peer", PEER_URL, "ctx-other", session_id="s2")

    assert reg.forget_conversations_for_session("s1") == 1
    assert conversations.remembered("resolver-minted-key", "peer", PEER_URL) == ""
    assert conversations.remembered("another-key", "peer", PEER_URL) == "ctx-other"


def test_the_session_scoped_registry_seam_never_raises(monkeypatch):
    """r5: same best-effort contract as ``forget_conversation`` — a cleanup path must not
    be able to fail the gesture it is cleaning up after."""
    reg = _registry()

    def _boom(_sid):
        raise RuntimeError("store on fire")

    monkeypatch.setattr(conversations, "forget_by_session", _boom)
    assert reg.forget_conversations_for_session("s1") == 0


def test_the_session_scoped_core_seam_is_duck_typed_and_swallowing(monkeypatch):
    """The origin-scoped server seam degrades exactly like its key-scoped sibling — a
    missing roster, a plugin from before it existed, or a raising one all mean 'dropped
    nothing'. No route calls it yet; this is the plumbing the DELETE slice will use."""
    from runtime.state import STATE
    from server.chat import forget_delegate_conversations_for_session

    class _Older:
        """A fork pinned to a delegates plugin from before this seam existed."""

    class _Broken:
        def forget_conversations_for_session(self, _sid):
            raise RuntimeError("nope")

    for roster in (None, _Older(), _Broken()):
        monkeypatch.setattr(STATE, "delegate_registry", roster, raising=False)
        assert forget_delegate_conversations_for_session("s1") == 0

    monkeypatch.setattr(STATE, "delegate_registry", _registry(), raising=False)
    conversations.remember("resolver-minted", "peer", PEER_URL, "ctx-room", session_id="s1")
    assert forget_delegate_conversations_for_session("", "s1") == 1  # blanks skipped, not counted


def test_the_delete_route_still_uses_key_scoped_forget_unchanged():
    """This preparatory slice adds the origin-scoped plumbing but does NOT rewire DELETE —
    the route still reaches ``forget_delegate_conversations`` (key-scoped), so its behaviour
    is byte-for-byte what it was. The origin-scoped seam ships alongside, wired by a
    following slice."""
    import inspect

    from operator_api import chat_routes

    src = inspect.getsource(chat_routes)
    assert "forget_delegate_conversations(" in src
    assert "forget_delegate_conversations_for_session(" not in src
