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
landed since it last spoke, attributed by author, capped by ``max_messages`` /
``max_chars`` (configurable — ``room.catchup_max_messages`` / ``room.catchup_max_chars``;
the module constants below are the defaults, so the pure functions stay callable with no
config at all). That bound is what keeps the cost of a room proportional to the
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

from graph.room_rounds import is_silence

log = logging.getLogger(__name__)

# DEFAULTS for the catch-up window handed to an addressed delegate — the caps themselves
# are per-call arguments (and `room.catchup_max_messages` / `room.catchup_max_chars` in
# config). Whichever bound trips first wins, and the window is taken from the END (the
# newest messages are the ones being replied to). A delegate that has been silent for 300
# messages gets the recent room and a note saying so — not a context-window-sized bill for
# its own silence. These stay module-level so `catchup_window` is a pure function that
# needs no config to call, which is what keeps it unit-testable.
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


def _positive(value, fallback: int) -> int:
    """``value`` as a positive int, or ``fallback`` for anything else (0, negative, junk).

    A non-positive bound means "the operator zeroed the knob", which is a request not to
    bound the window — never a request to send an EMPTY one. An empty catch-up silently
    strips a delegate's entire picture of the room, which for an ``a2a`` or model
    delegate is the only picture it has.
    """
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def catchup_caps(config=None) -> dict:
    """The configured catch-up caps as ``{"max_messages", "max_chars"}`` kwargs.

    One place for the three hosts that thread config into the room (the streaming and
    non-streaming chat drivers, and ``delegate_to``) to spell ``room.catchup_max_*``, and
    ``getattr``-based so a host that predates the fields — or has no config object at
    all — silently gets the module defaults instead of an AttributeError mid-dispatch.
    """
    return {
        "max_messages": _positive(getattr(config, "room_catchup_max_messages", None), _CATCHUP_MAX_MESSAGES),
        "max_chars": _positive(getattr(config, "room_catchup_max_chars", None), _CATCHUP_MAX_CHARS),
    }


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


def catchup_window(
    messages: list,
    target: str,
    *,
    lead_name: str = "assistant",
    max_messages: int = _CATCHUP_MAX_MESSAGES,
    max_chars: int = _CATCHUP_MAX_CHARS,
) -> tuple[list[tuple[str, str]], bool]:
    """The room since ``target`` last spoke, as ``[(author, text), …]`` + a truncated flag.

    Everything after the target's own most recent message; the full room when it has
    never spoken. Trimmed from the front to ``max_messages`` / ``max_chars``, because the
    newest messages are the ones the target is being asked about.

    Both caps are ARGUMENTS with the module defaults, not reads of a config: the host
    threads the operator's configured values in (``room.catchup_max_*``) while the
    function itself stays pure and callable from a test with nothing wired.
    """
    max_messages = _positive(max_messages, _CATCHUP_MAX_MESSAGES)
    max_chars = _positive(max_chars, _CATCHUP_MAX_CHARS)
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
    if len(window) > max_messages:
        window = window[-max_messages:]
        truncated = True
    total = sum(len(a) + len(t) for a, t in window)
    while window and total > max_chars:
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
    speaker: str = "operator",
    max_messages: int = _CATCHUP_MAX_MESSAGES,
    max_chars: int = _CATCHUP_MAX_CHARS,
    record_address: bool = True,
    drop_silence: bool = False,
) -> dict:
    """Address ``target`` directly and record the exchange on ``thread_id``.

    Returns ``{ok, author, reply, error, catchup, truncated, silent}``. ``permissions``
    is the per-call ceiling — left unset for an operator-typed mention (the operator is
    the authority) and set to ``"readonly"`` by any agent-originated path.

    ``max_messages`` / ``max_chars`` bound the catch-up window (``room.catchup_max_*``).
    ``record_address`` and ``drop_silence`` are the multi-round driver's two levers —
    both default to today's single-round behavior; see ``dispatch_into_room``.

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
        return {"ok": False, "author": target, "reply": "", "error": "no_registry", "catchup": 0, "truncated": False, "silent": False}
    if not (message or "").strip():
        return {"ok": False, "author": target, "reply": "", "error": "empty_message", "catchup": 0, "truncated": False, "silent": False}

    lg_config = {"configurable": {"thread_id": thread_id}}

    # 1. READ the room.
    try:
        snapshot = await graph.aget_state(lg_config) if graph is not None else None
        history = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
    except Exception:  # noqa: BLE001 — an unreadable thread means no catch-up, not a failed turn
        log.exception("[room] reading thread %s failed", thread_id)
        history = []

    outcome = await dispatch_into_room(
        registry,
        target,
        message,
        history,
        thread_id=thread_id,
        lead_name=lead_name,
        permissions=permissions,
        speaker=speaker,
        max_messages=max_messages,
        max_chars=max_chars,
        record_address=record_address,
        drop_silence=drop_silence,
    )
    written = outcome.pop("messages")
    if graph is None or not written:
        return outcome
    try:
        # `as_node` is REQUIRED, not optional garnish: on a thread that already has
        # history the compiled graph has several nodes that could have produced this
        # update and LangGraph refuses it as ambiguous.
        await graph.aupdate_state(lg_config, {"messages": written}, as_node=START)
    except Exception:  # noqa: BLE001 — the operator already has the reply; the room record is best-effort
        log.exception("[room] recording the exchange on thread %s failed", thread_id)
    return outcome


async def dispatch_into_room(
    registry,
    target: str,
    message: str,
    history: list,
    *,
    thread_id: str,
    lead_name: str = "assistant",
    permissions: str | None = None,
    speaker: str = "operator",
    timeout: float | None = None,
    max_messages: int = _CATCHUP_MAX_MESSAGES,
    max_chars: int = _CATCHUP_MAX_CHARS,
    record_address: bool = True,
    drop_silence: bool = False,
) -> dict:
    """Dispatch an address and return its room envelopes without writing state.

    ``run_mention`` writes these after an out-of-turn operator ``@``. A foreground
    ``delegate_to`` instead returns them in a ``Command`` so they are reduced into the
    active turn rather than lost to that turn's next checkpoint.

    ``max_messages`` / ``max_chars`` bound the catch-up window (see ``catchup_window``).

    The last two exist for the bounded multi-round driver (``graph/room_rounds.py``) and
    both default to the single-round behavior every existing caller already has:

    * ``record_address`` — write the operator's own message onto the thread as the
      ``from=<speaker> to=<target>`` half of the exchange. False for rounds 2..N of one
      address: the room already carries that message, and re-writing it per round would
      read, in everyone's catch-up, as the operator repeating themselves.
    * ``drop_silence`` — treat an empty reply or a bare ``pass`` token as SILENCE:
      flagged ``silent`` in the outcome and omitted from the thread rather than written
      as a message. That is what lets a participant with nothing to add decline without
      polluting the transcript. Off by default, so a single-round `@` still records a
      literal "pass" reply exactly as it always has — and ``silent`` stays False, which
      is what every consumer keys off.
    """
    if registry is None:
        return {
            "ok": False, "author": target, "reply": "", "error": "no_registry",
            "catchup": 0, "truncated": False, "silent": False, "messages": [],
        }
    if not (message or "").strip():
        return {
            "ok": False, "author": target, "reply": "", "error": "empty_message",
            "catchup": 0, "truncated": False, "silent": False, "messages": [],
        }

    from langchain_core.messages import HumanMessage

    window, truncated = catchup_window(
        history, target, lead_name=lead_name, max_messages=max_messages, max_chars=max_chars
    )

    # `conversation_key` is ACP-only — dispatch() raises for every other type, so it
    # rides only where it is accepted. Everyone else gets the attributed catch-up.
    delegate = registry.get(target)
    if delegate is None:
        return {
            "ok": False,
            "author": target,
            "reply": "",
            "error": f"unknown delegate {target!r}",
            "catchup": 0,
            "truncated": False,
            "silent": False,
            "messages": [],
        }
    conversation_key = thread_id if getattr(delegate, "type", "") == "acp" else None

    ok, reply, error, error_kind = True, "", "", ""
    try:
        dispatch_kwargs = {
            "conversation_key": conversation_key,
            "permissions": permissions,
        }
        if timeout is not None:
            dispatch_kwargs["timeout"] = timeout
        reply = str(
            await registry.dispatch(
                target,
                _prompt(window, truncated, target, message),
                **dispatch_kwargs,
            )
            or ""
        ).strip()
    except Exception as exc:  # noqa: BLE001 — surfaced in the room, not raised at the operator
        # Bare `str(exc)` — this string is read by an operator, and the exception TYPE
        # is noise to them. The type goes to the log, which is who wants it.
        ok, error = False, str(exc) or type(exc).__name__
        # Preserve the adapter's machine-readable failure class for callers that can
        # recover from one narrow case. The operator-facing error remains the plain
        # string above; this field is routing metadata, not new UI copy.
        error_kind = str(getattr(exc, "kind", "") or "")
        log.warning("[room] dispatch to %r failed: %s: %s", target, type(exc).__name__, error)

    # `silent` means "this reply WAS treated as silence" — never merely "looks like a
    # pass". Gating it on drop_silence is what keeps a single-round address byte-identical
    # to before: there, a delegate that literally replies "pass" is quoted like any other
    # answer, on the thread and to the operator.
    silent = bool(drop_silence and ok and is_silence(reply))

    # Both halves are ordered as they happened. The caller decides whether they join
    # the current turn via Command or are written after an operator-only address.
    written = []
    if record_address:
        written.append(
            HumanMessage(
                content=_envelope(speaker, message, to=target),
                additional_kwargs={"lc_source": _SOURCE, "room": {"from": speaker, "to": target}},
            )
        )
    if ok and reply and not silent:
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
    return {
        "ok": ok,
        "author": target,
        "reply": reply,
        "error": error,
        "error_kind": error_kind,
        "catchup": len(window),
        "truncated": truncated,
        "silent": silent,
        "messages": written,
    }
