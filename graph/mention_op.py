"""`@<name>` direct address — one named delegate answers *into* the chat thread (#3042).

The operator types ``@proto fix the flaky test``. The lead agent's routing judgment is
short-circuited: no graph turn runs, ``proto`` is dispatched directly, and its reply
lands in the transcript authored by ``proto`` rather than paraphrased by the lead.

**The checkpointer thread IS the room transcript.** Both halves of the exchange are
written back onto the thread (``aupdate_state``) before this returns, which is the whole
reason a room needs no second store: the next ordinary turn's lead agent reads the same
history and knows what was said and by whom. Skipping that write is what would make
``@proto`` a side channel the lead is blind to — and the operator's *next* bare message
goes to the lead, so it would be blind at exactly the wrong moment.

**Catch-up, not the whole room.** An addressed delegate receives the room messages that
landed since it last spoke, attributed by author, capped by ``_CATCHUP_MAX_MESSAGES`` /
``_CATCHUP_MAX_CHARS``. That bound is what keeps the cost of a room proportional to the
conversation rather than to its length — and it's also the only continuity some delegate
types get: ``conversation_key`` is ACP-only (``DelegateRegistry.dispatch`` refuses it for
every other type), so an ``a2a`` fleet member or a model endpoint remembers nothing
between calls and the catch-up is its entire picture of the room.

Host-free-ish: takes the graph + a registry-shaped object like ``aside_op`` /
``rewind_op``, so both the transcript write and the catch-up window are unit-testable
against fakes with no server.
"""

from __future__ import annotations

import logging
import re

from langgraph.constants import START

log = logging.getLogger(__name__)

# The catch-up window handed to an addressed delegate. Whichever bound trips first wins,
# and the window is taken from the END (the newest messages are the ones being replied
# to). A delegate that has been silent for 300 messages gets the recent room and a note
# saying so — not a context-window-sized bill for its own silence.
_CATCHUP_MAX_MESSAGES = 40
_CATCHUP_MAX_CHARS = 8000

# Marks a message this module wrote onto the thread. `lc_source` mirrors the compaction
# convention; `room` carries authorship STRUCTURALLY so later readers (catch-up windowing
# here, the console, chat_bundle, export) recover who spoke without parsing the envelope
# text back out.
_SOURCE = "room"

# The inverse of `_envelope` — see `_text_of`. Anchored and exact, so it can only ever
# match a carrier this module wrote, never prose that happens to mention the tag.
_ENVELOPE_RE = re.compile(r"^<room-message\b[^>]*>\n(.*)\n</room-message>$", re.DOTALL)


def _room_meta(message) -> dict:
    """The ``{"from", "to"}`` authorship stamp on a room message, or ``{}``."""
    kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(kwargs, dict):
        return {}
    meta = kwargs.get("room")
    return meta if isinstance(meta, dict) else {}


def _text_of(message) -> str:
    """A message's text, flattening the multi-part content shape and unwrapping the room
    envelope.

    The unwrap is what keeps a long room from compounding: a room message read back for
    someone else's catch-up would otherwise arrive wrapped in the tags it was stored
    with, re-wrapped by the next catch-up, and again by the one after that.
    """
    content = getattr(message, "content", "")
    if isinstance(content, list):  # multi-part → join the text blocks
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = str(content or "").strip()
    unwrapped = _ENVELOPE_RE.match(text)
    return unwrapped.group(1).strip() if unwrapped else text


def _author_of(message, *, lead_name: str) -> str | None:
    """Who said this, for the catch-up transcript. ``None`` = not room conversation.

    Tool traffic is deliberately excluded: the lead agent's tool calls and their results
    are how it did its work, not something anyone said in the room, and replaying them to
    every addressed delegate would be both confusing and expensive.
    """
    meta = _room_meta(message)
    if meta.get("from"):
        return str(meta["from"])
    kind = message.__class__.__name__
    if kind == "ToolMessage":
        return None
    if kind == "AIMessage":
        # An assistant message that only carries tool_calls has no words in it.
        return lead_name if _text_of(message) else None
    if kind == "HumanMessage":
        return "operator"
    return None


def catchup_window(messages: list, target: str, *, lead_name: str = "assistant") -> tuple[list[tuple[str, str]], bool]:
    """The room since ``target`` last spoke, as ``[(author, text), …]`` + a truncated flag.

    Everything after the target's own most recent message; the full room when it has
    never spoken. Trimmed from the front to the message/char caps, because the newest
    messages are the ones the target is being asked about.
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if _room_meta(messages[i]).get("from") == target:
            start = i + 1
            break
    window: list[tuple[str, str]] = []
    for message in messages[start:]:
        author = _author_of(message, lead_name=lead_name)
        text = _text_of(message)
        if author and text:
            window.append((author, text))

    truncated = False
    if len(window) > _CATCHUP_MAX_MESSAGES:
        window = window[-_CATCHUP_MAX_MESSAGES:]
        truncated = True
    total = sum(len(a) + len(t) for a, t in window)
    while window and total > _CATCHUP_MAX_CHARS:
        author, text = window.pop(0)
        total -= len(author) + len(text)
        truncated = True
    return window, truncated


def _prompt(window: list[tuple[str, str]], truncated: bool, target: str, message: str) -> str:
    """What the addressed delegate actually receives.

    Self-contained by construction — same contract as ``delegate_to``'s ``query``: the
    delegate is not in our conversation, so the room it needs is spelled out rather than
    assumed.
    """
    if not window:
        return message
    lines = "\n".join(f"[{author}] {text}" for author, text in window)
    preface = (
        f"You are taking part in a group chat. Here is what has been said since you last spoke"
        f"{' (earlier messages omitted)' if truncated else ''}:\n\n"
        f"{lines}\n\n"
        f"You have been addressed directly as @{target}. Reply to this message:\n\n"
    )
    return preface + message


def _attr(value: str) -> str:
    """An XML attribute value, escaped — a participant name is not trusted markup.

    Today both names come from the configured delegate roster, but ``run_mention`` is a
    public entry point and #3050 adds agent-originated addressing. A name carrying `"`
    or `>` would otherwise break the envelope so `_ENVELOPE_RE` stops round-tripping it,
    and — worse — let a forged ``from=`` attribution reach the lead agent.
    """
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _envelope(author: str, text: str, *, to: str = "") -> str:
    """The model-facing carrier for a room message on the thread.

    An XML-tagged ``HumanMessage``, matching how a delegated result already enters a
    thread (``_drain_background_messages``'s ``<task-notification>``). Deliberately NOT
    an ``AIMessage(name=…)``: the `name` field's handling varies by gateway, and the
    lead agent must never read another participant's words as its own prior output.
    """
    attrs = f'from="{_attr(author)}"' + (f' to="{_attr(to)}"' if to else "")
    return f"<room-message {attrs}>\n{text}\n</room-message>"


async def run_mention(
    graph,
    registry,
    thread_id: str,
    target: str,
    message: str,
    *,
    session_id: str = "",
    lead_name: str = "assistant",
    permissions: str | None = None,
) -> dict:
    """Address ``target`` directly and record the exchange on ``thread_id``.

    Returns ``{ok, author, reply, error, catchup, truncated}``. ``permissions`` is the
    per-call ceiling — left unset for an operator-typed mention (the operator is the
    authority) and set to ``"readonly"`` by any agent-originated path.

    The thread write is best-effort and happens even on a dispatch error, so a failed
    address is visible to the lead agent as something that happened in the room rather
    than vanishing.

    **Callers must hold the per-thread lock** for ``thread_id``. This writes the shared
    checkpointer thread, and every other writer (a turn, a goal continuation, compact,
    rewind) serializes on that lock — an unlocked write here lost-updates the
    transcript. Kept as a caller contract rather than taken here so the module stays
    host-free, the way ``_hold_if_hitl_pending`` documents the same requirement.
    """
    # A missing graph is NOT a refusal. The operator asked a delegate a question; the
    # thread record is bookkeeping on top of that, and losing the bookkeeping must never
    # cost them the answer. Without a graph there is simply no room to read or write.
    if registry is None:
        return {"ok": False, "author": target, "reply": "", "error": "no_registry", "catchup": 0, "truncated": False}
    if not (message or "").strip():
        return {"ok": False, "author": target, "reply": "", "error": "empty_message", "catchup": 0, "truncated": False}

    from langchain_core.messages import HumanMessage

    lg_config = {"configurable": {"thread_id": thread_id}}

    # 1. READ the room.
    try:
        snapshot = await graph.aget_state(lg_config) if graph is not None else None
        history = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
    except Exception:  # noqa: BLE001 — an unreadable thread means no catch-up, not a failed turn
        log.exception("[room] reading thread %s failed", thread_id)
        history = []

    window, truncated = catchup_window(history, target, lead_name=lead_name)

    # 2. DISPATCH. `conversation_key` is ACP-only — dispatch() raises for every other
    #    type, so it rides only where it is accepted. Everyone else gets the catch-up.
    delegate = registry.get(target)
    if delegate is None:
        return {
            "ok": False,
            "author": target,
            "reply": "",
            "error": f"unknown delegate {target!r}",
            "catchup": 0,
            "truncated": False,
        }
    conversation_key = thread_id if getattr(delegate, "type", "") == "acp" else None

    ok, reply, error = True, "", ""
    try:
        reply = str(
            await registry.dispatch(
                target,
                _prompt(window, truncated, target, message),
                conversation_key=conversation_key,
                permissions=permissions,
            )
            or ""
        ).strip()
    except Exception as exc:  # noqa: BLE001 — surfaced in the room, not raised at the operator
        # Bare `str(exc)` — this string is read by an operator, and the exception TYPE
        # is noise to them. The type goes to the log, which is who wants it.
        ok, error = False, str(exc) or type(exc).__name__
        log.warning("[room] dispatch to %r failed: %s: %s", target, type(exc).__name__, error)

    # 3. WRITE both halves back onto the thread, so the lead agent's next turn reads the
    #    same room. Ordered operator-then-reply, as they happened.
    written = [
        HumanMessage(
            content=_envelope("operator", message, to=target),
            additional_kwargs={"lc_source": _SOURCE, "room": {"from": "operator", "to": target}},
        )
    ]
    if ok and reply:
        written.append(
            HumanMessage(
                content=_envelope(target, reply),
                additional_kwargs={"lc_source": _SOURCE, "room": {"from": target}},
            )
        )
    elif not ok:
        written.append(
            HumanMessage(
                content=_envelope(target, f"(could not be reached: {error})"),
                additional_kwargs={"lc_source": _SOURCE, "room": {"from": target, "failed": True}},
            )
        )
    if graph is None:
        return {
            "ok": ok,
            "author": target,
            "reply": reply,
            "error": error,
            "catchup": len(window),
            "truncated": truncated,
        }
    try:
        # `as_node` is REQUIRED, not optional garnish: on a thread that already has
        # history the compiled graph has several nodes that could have produced this
        # update and LangGraph refuses it as ambiguous. Since the write below is
        # best-effort, that refusal was silent — the room record simply never landed for
        # any chat past its first turn, which is every real chat.
        #
        # `__start__` is the honest node: a room message ARRIVES on the thread, it is not
        # some node's output. It's also the only stable choice — the rest of the node list
        # is middleware-dependent and changes as middleware is added or removed.
        await graph.aupdate_state(lg_config, {"messages": written}, as_node=START)
    except Exception:  # noqa: BLE001 — the operator already has the reply; the room record is best-effort
        log.exception("[room] recording the exchange on thread %s failed", thread_id)

    return {
        "ok": ok,
        "author": target,
        "reply": reply,
        "error": error,
        "catchup": len(window),
        "truncated": truncated,
    }
