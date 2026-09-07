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
different peer must not send that peer a context id the old one minted.
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


def snapshot() -> dict[tuple[str, str, str], str]:
    """Everything remembered (copy) — for tests and debugging, never the wire."""
    return dict(_CONTEXTS)


def reset() -> None:
    """Forget everything (tests)."""
    _CONTEXTS.clear()
