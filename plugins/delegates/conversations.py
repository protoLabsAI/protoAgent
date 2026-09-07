"""Per-conversation A2A continuity — the ``contextId`` a peer assigned us (#3360).

A2A's ``contextId`` is the protocol's own "these messages are one conversation"
grouping key, and protoAgent's own server already treats it as first-class: an inbound
``context_id`` IS the session the turn runs in (``a2a_impl/executor`` hands it to
``_chat_langgraph_stream`` as ``session_id``, which resolves the thread). So sending the
same one back on the next address is what turns a room's repeated dispatches to an
``a2a`` member from N unrelated conversations into one — instead of the bounded catch-up
window being the participant's entire picture of the room, re-shipped per address and,
since multi-round rooms, per round.

**We echo what the peer assigned; we never invent one.** A2A makes the SERVER the owner
of context identity, and a value the peer minted is one it cannot reject. Deriving a
contextId from our own ``(thread_id, delegate)`` would be store-free, but it hands a
remote peer a value derived from our session id and assumes every peer accepts a
client-supplied context — a peer that validates the id fails the dispatch outright, which
is a BREAK, not the graceful degradation this seam has to promise. Echoing degrades
correctly in every case: a peer that returns no ``contextId`` teaches us nothing, so we
send nothing, so it sees exactly the traffic it saw before this existed.

**Process-local and in-memory, like ``status.py``.** This is transport bookkeeping, not
operator-authored config, so ``store.py`` — the ``delegates:`` list plus ``secrets.yaml``
routing — is the wrong shelf for it, and the room's "the thread IS the transcript" rule is
about the transcript, not about a wire handle. Losing the map on restart costs one
conversation's continuity and is invisible: the next address opens a fresh context, which
is what every address did before this existed. Bounded LRU, so a long-lived instance with
many threads cannot grow it without limit.

**The origin session rides beside the resolved key (#3362).** The key is the *resolved*
conversation/thread id a room dispatched under; a custom thread-id resolver (ADR 0029 §D4 /
#571) may mint that from request metadata to anything, and the map is one-way — a resolved
key cannot be reversed into the chat session it came from. So each entry ALSO records its
originating session id explicitly, when the caller knew it, and ``forget_by_session`` drops
by that recorded origin — never by the *shape* of a key (no prefix/substring guess). That
is what lets a delete, which knows only the session id, reach every context that session
minted even under a resolver's arbitrary keys, without replaying request metadata it has
none of. The session is a property of the entry, not part of the key: keying still isolates
delegate / url / credential exactly as before.

The delegate's **url** is part of the key on purpose: re-pointing a delegate at a
different peer must not send that peer a context id the old one minted. Its **name** is
too, which is the conservative half of the same rule: one fleet member configured under
two delegate names keeps two peer-side conversations rather than one. And so is a digest
of its **credential**, which is what actually makes that second sentence true: the reason
two names must not merge is that two rows can carry two different credentials — so editing
one row's token IN PLACE (same name, same url) crosses exactly the boundary the name was
there to protect. A rotated or re-pointed credential starts a fresh conversation; the
digest is one-way and never leaves this process.

**What continuity must not outlive.** Everything here is a pointer at a conversation the
PEER holds, so anything that rewrites this side's history has to drop the pointer or the
peer keeps answering from what the operator just erased:

* ``forget()`` on a **rewind** — rewind is destructive by design ("discard everything
  after this"), and before #3360 it was total, because the peer remembered nothing. It has
  to stay total: the next address opens a fresh context and the peer starts from the
  (rewound) catch-up window.
* ``forget()`` on a **delete** — the delete dialog promises the history is removed; a live
  pointer into the peer's copy of it is the same leak the session-summary purge closed.
* ``forget()`` on the destination of a **fork**, whose thread id is new but need not be
  unused. (A fork must never *inherit* the source's context either — two threads writing
  into one peer conversation would splice two divergent rooms together. That falls out of
  keying on the thread id, and ``tests/test_a2a_room_context.py`` pins it.)

**Compaction deliberately keeps its context.** ``/compact`` summarizes to save OUR window;
it is not a claim that anything was unsaid, and the peer manages its own context. Dropping
continuity there would throw away exactly the thing that makes a long room affordable.

**A pointer is only as good as the exchange that minted it.** ``forget_one()`` is the
per-participant half of the same rule, called by the A2A adapter when an exchange ends
leaving the peer holding something this side does NOT have. The invariant the two halves
buy: *a remembered context names a peer-side conversation that is idle, and whose last
exchange is on this thread.* Without it, a peer that parked on a HITL interrupt swallows
every later address into the same hold — the room passes no resume handle, so the peer
re-parks with the same question and the room livelocks — and a peer still working past the
poll deadline makes the next address queue behind the very turn the room gave up on.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import NamedTuple


class _Entry(NamedTuple):
    """One remembered context: the ``contextId`` the peer assigned, plus the chat session
    this side resolved the key FROM (``""`` when the caller didn't know it — a resolved key
    can't be reversed into its session, so an entry with no recorded origin stays unreachable
    to ``forget_by_session`` rather than being swept by a blank match)."""

    context_id: str
    session_id: str


# One entry is four short key strings and a two-field value; the cap exists so a long-lived
# instance that has addressed many threads can't accumulate them forever. Evicted
# least-recently-used, which for a room means the conversations nobody is having any more.
_MAX_ENTRIES = 512

# (conversation_key, delegate name, delegate url, credential digest) -> _Entry(contextId, session)
_CONTEXTS: OrderedDict[tuple[str, str, str, str], _Entry] = OrderedDict()

# Guards every mutation of _CONTEXTS as a GROUP: `__setitem__` + `move_to_end` + the LRU
# eviction loop are three statements, and `forget()` deletes while it walks the mapping.
# One event loop makes that safe today, but background delegations and the scheduler
# dispatch from elsewhere, and the failure mode of getting it wrong is a `RuntimeError:
# dictionary changed size during iteration` raised inside a cleanup path that swallows
# exceptions — i.e. a forget that silently does nothing.
_LOCK = threading.Lock()


def _digest(credential: str) -> str:
    """A short one-way digest of a delegate row's auth material (never the material itself)."""
    credential = str(credential or "")
    return hashlib.sha256(credential.encode("utf-8", "replace")).hexdigest()[:16] if credential else ""


def _key(conversation_key: str, delegate: str, url: str, credential: str = "") -> tuple[str, str, str, str]:
    return (str(conversation_key or ""), str(delegate or ""), str(url or ""), _digest(credential))


def remembered(conversation_key: str, delegate: str, url: str, credential: str = "") -> str:
    """The ``contextId`` this peer assigned this conversation, or ``""``.

    ``""`` for anything unknown — no conversation key, a peer we have never heard a
    context from, a delegate that has since been re-pointed at another url or had its
    credential rotated. Every one of those is "send no contextId", i.e. the pre-#3360 wire.
    """
    if not conversation_key:
        return ""
    key = _key(conversation_key, delegate, url, credential)
    with _LOCK:
        entry = _CONTEXTS.get(key)
        if entry is not None:
            _CONTEXTS.move_to_end(key)
    return entry.context_id if entry is not None else ""


def remember(
    conversation_key: str,
    delegate: str,
    url: str,
    context_id: str,
    credential: str = "",
    session_id: str = "",
) -> None:
    """Record the ``contextId`` a peer just used for this conversation.

    A no-op without both a conversation key and a context id: a peer that answers
    without one has told us nothing to remember, and storing an empty string would make
    the next lookup look like a hit.

    ``session_id`` is the chat session this conversation originated from, kept beside the
    resolved key so a delete that knows only the session can find the entry later (#3362).
    It is optional and defaults to ``""``: a caller that does not know the origin (or a
    plugin from before this seam) records none, and such an entry is simply not reachable
    by ``forget_by_session`` — the key-scoped ``forget``/``forget_one`` still find it.

    Called only for an exchange that ANSWERED — see the module docstring's invariant and
    ``forget_one`` for the other half.
    """
    if not (conversation_key and context_id):
        return
    key = _key(conversation_key, delegate, url, credential)
    with _LOCK:
        _CONTEXTS[key] = _Entry(str(context_id), str(session_id or ""))
        _CONTEXTS.move_to_end(key)
        while len(_CONTEXTS) > _MAX_ENTRIES:
            _CONTEXTS.popitem(last=False)


def forget_one(conversation_key: str, delegate: str, url: str, credential: str = "") -> bool:
    """Drop ONE participant's pointer in one conversation; returns whether we held one.

    The per-participant counterpart to ``forget()``, and the one the A2A adapter itself
    calls: ``forget()`` answers a thread-lifecycle event about the whole conversation,
    this answers "that exchange left this peer somewhere the next address must not go".

    Two of those, both of which the room has no way to recover from on its own:

    * **The peer PARKED** on a HITL interrupt (``input_required``). Its thread now holds a
      pending interrupt, and a protoAgent peer holds a *fresh* message on such a thread in
      its steering queue and re-yields the same interrupt (``server.chat._hold_if_hitl_pending``
      — origin ``a2a`` is deliberately not autonomous). So a later ``@member`` sent into
      that context is never answered: it parks a second task with the identical question,
      and the round after that a third. Only the lead's ``delegate_to(..., resume_task_id=…)``
      can answer a park, and that path bypasses the room. Dropping the pointer restores
      exactly the pre-#3360 outcome — the next address opens a clean context and just runs.
    * **The peer is still WORKING** past the poll deadline (or its inline answer outran our
      read budget). The dispatch raises, the room records the address as failed and drops
      the member for the rest of the turn — so whatever the peer eventually writes into that
      context is in a conversation this side has no record of, and the next address would
      both inherit that invisible history and queue behind the still-running turn (a
      protoAgent peer serializes turns per thread) for the whole read budget.

    A transport failure is NOT one of these: the peer never accepted the message, so its
    conversation is unchanged and the pointer still describes it correctly.
    """
    if not conversation_key:
        return False
    with _LOCK:
        return _CONTEXTS.pop(_key(conversation_key, delegate, url, credential), None) is not None


def forget(conversation_key: str) -> int:
    """Forget every peer context remembered for one conversation; returns how many.

    Every delegate at once, because the caller is a thread-lifecycle event (a rewind, a
    delete, a fork onto this id) and it is talking about the CONVERSATION, not about one
    participant in it — a rewind that dropped only the member who happened to be addressed
    last would leave the rest of the cast still holding the erased exchange.

    Idempotent and total-loss-tolerant: forgetting a key we never held is a no-op, and a
    key we did hold degrades to exactly the pre-#3360 wire (no contextId sent, the peer
    mints a fresh conversation on the next address). That is why this is safe to call
    best-effort from a cleanup path that must not fail the operation it is cleaning up
    after.
    """
    if not conversation_key:
        return 0
    key = str(conversation_key)
    with _LOCK:
        gone = [k for k in _CONTEXTS if k[0] == key]
        for k in gone:
            _CONTEXTS.pop(k, None)
    return len(gone)


def forget_by_session(session_id: str) -> int:
    """Forget every context whose recorded ORIGIN session equals ``session_id``; returns
    how many.

    The origin-scoped counterpart to ``forget()``: that one takes the resolved conversation
    KEY a room dispatched under, this one takes the chat SESSION id a delete knows. The two
    exist because a custom thread-id resolver (ADR 0029 §D4 / #571) can map a session to any
    key and the map is one-way — a delete that only holds the session id cannot recover the
    keys to hand ``forget()``. So it matches the recorded origin EXACTLY: no ``startswith``,
    no ``in``, nothing that would let one session's id drag down another whose key or id it
    happens to be a prefix of. An entry that recorded no origin (``session_id == ""``) is
    never matched, which is why a blank argument is a no-op rather than a sweep of them all.

    Idempotent and total-loss-tolerant like ``forget()``, so it is safe to call best-effort
    from a cleanup path that must not fail the operation it is cleaning up after.
    """
    if not session_id:
        return 0
    sid = str(session_id)
    with _LOCK:
        gone = [k for k, entry in _CONTEXTS.items() if entry.session_id == sid]
        for k in gone:
            _CONTEXTS.pop(k, None)
    return len(gone)


def snapshot() -> dict[tuple[str, str, str, str], _Entry]:
    """Everything remembered (copy) — for tests and debugging, never the wire."""
    with _LOCK:
        return dict(_CONTEXTS)


def reset() -> None:
    """Forget everything (tests)."""
    with _LOCK:
        _CONTEXTS.clear()
