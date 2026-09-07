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

The delegate's **url** is part of the key on purpose: re-pointing a delegate at a
different peer must not send that peer a context id the old one minted. Its **name** is
too, which is the conservative half of the same rule: one fleet member configured under
two delegate names keeps two peer-side conversations rather than one, because two rows can
carry two different credentials and merging them would cross that boundary on the strength
of a matching url.

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
"""

from __future__ import annotations

from collections import OrderedDict

# One entry is three short strings; the cap exists so a long-lived instance that has
# addressed many threads can't accumulate them forever. Evicted least-recently-used,
# which for a room means the conversations nobody is having any more.
_MAX_ENTRIES = 512

# (conversation_key, delegate name, delegate url) -> contextId
_CONTEXTS: OrderedDict[tuple[str, str, str], str] = OrderedDict()


def _key(conversation_key: str, delegate: str, url: str) -> tuple[str, str, str]:
    return (str(conversation_key or ""), str(delegate or ""), str(url or ""))


def remembered(conversation_key: str, delegate: str, url: str) -> str:
    """The ``contextId`` this peer assigned this conversation, or ``""``.

    ``""`` for anything unknown — no conversation key, a peer we have never heard a
    context from, a delegate that has since been re-pointed at another url. Every one of
    those is "send no contextId", i.e. the pre-#3360 wire.
    """
    if not conversation_key:
        return ""
    key = _key(conversation_key, delegate, url)
    context_id = _CONTEXTS.get(key, "")
    if context_id:
        _CONTEXTS.move_to_end(key)
    return context_id


def remember(conversation_key: str, delegate: str, url: str, context_id: str) -> None:
    """Record the ``contextId`` a peer just used for this conversation.

    A no-op without both a conversation key and a context id: a peer that answers
    without one has told us nothing to remember, and storing an empty string would make
    the next lookup look like a hit.
    """
    if not (conversation_key and context_id):
        return
    key = _key(conversation_key, delegate, url)
    _CONTEXTS[key] = str(context_id)
    _CONTEXTS.move_to_end(key)
    while len(_CONTEXTS) > _MAX_ENTRIES:
        _CONTEXTS.popitem(last=False)


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
    gone = [k for k in _CONTEXTS if k[0] == key]
    for k in gone:
        _CONTEXTS.pop(k, None)
    return len(gone)


def snapshot() -> dict[tuple[str, str, str], str]:
    """Everything remembered (copy) — for tests and debugging, never the wire."""
    return dict(_CONTEXTS)


def reset() -> None:
    """Forget everything (tests)."""
    _CONTEXTS.clear()
