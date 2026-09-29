"""Session operations — the out-of-turn gestures on a chat thread (#3810, epic #3804).

Extracted from ``server/chat.py``. Everything here acts on a session's checkpointed
thread *between* turns rather than driving one: ``/compact``, export, publish
(preview / publish / revoke), the ``/btw`` aside, rewind, fork, and the delegate
transport-continuity cleanup those destructive gestures share.

**The turn driver's collaborators stay in ``server.chat``.** The per-thread lock
(``_thread_lock`` — its ``_THREAD_LOCKS`` registry is module-level state with exactly
one home) and the thread-id resolver (``_resolve_thread_id``) are reached through
:func:`_chat` at CALL time, never bound at import. Two reasons: a ``server.chat``
attribute patched by a test is what these gestures actually call, and there is no
import-time edge back into ``server.chat``, so ``import server.chat_session_ops``
works standalone and ``server.chat`` can re-export every name here at its tail.

``_force_compact_for_overflow`` is deliberately NOT here: it is the streaming /
non-streaming turn drivers' overflow recovery (its only caller is
``server.chat._overflow_compacted``), not the operator's ``/compact`` gesture.

``server.chat`` re-exports every public name (and the private helpers tests import)
so ``from server.chat import rewind_session`` and ``operator_api.chat_routes``' imports
keep resolving — that import edge is the one lint-imports already sanctions.
Patch these names HERE, not on ``server.chat``: a re-export is a copy of the binding,
so patching it on ``server.chat`` intercepts nothing
(``tests/test_chat_session_ops_seam.py`` enforces that).
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType

from runtime.state import STATE

# Same logger as server.chat, so the moved warnings keep their channel.
log = logging.getLogger("protoagent.server")

__all__ = [
    "aside_session",
    "compact_session",
    "export_session",
    "forget_delegate_conversations",
    "forget_delegate_conversations_for_session",
    "fork_session",
    "publish_preview",
    "publish_session",
    "revoke_published_link",
    "rewind_session",
]


def _chat() -> ModuleType:
    """``server.chat``, resolved at call time — see the module docstring.

    ``importlib`` rather than ``from server import chat``: the ``server`` package
    re-exports the ``chat`` FUNCTION under that name, shadowing the submodule.
    """
    return importlib.import_module("server.chat")


def _compaction_message(result: dict) -> str:
    """Human-readable status line for a compaction result — surfaced as the
    system-note in the chat thread (and returned to non-UI callers)."""
    reason = result.get("reason") or ""
    if reason == "too_short":
        return f"Nothing to compact — this conversation is already short ({result.get('kept', 0)} messages)."
    if reason == "no_store":
        return (
            "Compaction skipped — no searchable knowledge store is configured, so the raw history "
            "couldn't be archived. Nothing was changed (your full context is intact)."
        )
    if reason == "incognito":
        return (
            "Compaction skipped — this chat is incognito, so its history is never archived to memory, "
            "and /compact doesn't remove history it hasn't archived. Nothing was changed."
        )
    if reason in ("empty", "empty_archive", "archive_error"):
        return "Compaction skipped — the conversation couldn't be archived, so nothing was changed."
    if reason in ("no_summary", "summary_error"):
        return (
            f"Archived {result.get('archived_chunks', 0)} chunk(s) to searchable memory, but the summary "
            "couldn't be generated — kept your full context rather than compacting it."
        )
    if reason == "no_checkpointer":
        return "Compaction unavailable — no conversation checkpoint to compact."
    removed, kept = result.get("removed", 0), result.get("kept", 0)
    return (
        f"Compacted this conversation — archived {removed} older message(s) to searchable memory and kept the "
        f"last {kept}. The agent now carries a summary of the earlier messages plus the recent ones, at a "
        f"fraction of the token cost; the full raw history stays searchable via memory recall."
    )


async def compact_session(session_id: str, *, request_metadata: dict | None = None) -> dict:
    """Compact a chat session's live context (the ``/compact`` gesture, #1527).

    Resolves the session's checkpointer ``thread_id`` (the A2A ``a2a:<session_id>``
    thread — the one the live streaming turns write to) and runs
    ``compact_thread`` under the per-thread lock, so a compaction can never race a
    live streaming turn on the same thread (mirrors the turn driver). Returns the
    ``compact_thread`` result dict plus a human-readable ``message``.
    """
    base = {"summary": "", "archived_chunks": 0, "kept": 0, "removed": 0, "archived": False, "refused": True}
    if STATE.graph is None:
        return {**base, "reason": "setup", "message": "Setup required — finish the setup wizard first."}

    from graph.compaction_op import compact_thread

    tid = _chat()._resolve_thread_id(request_metadata, session_id)
    async with _chat()._thread_lock(tid):
        result = await compact_thread(
            STATE.graph,
            STATE.checkpointer,
            STATE.knowledge_store,
            STATE.graph_config,
            tid,
            session_id,
        )
    return {**result, "message": _compaction_message(result)}


def _export_message(result: dict) -> str:
    """Human-readable status line for an export result (surfaced to non-UI callers /
    logs). Names the redactions when there were any — the operator is meant to review
    before sharing, so a silent scrub would be the wrong default."""
    reason = result.get("reason") or ""
    if reason == "no_checkpointer":
        return "Export unavailable — no conversation checkpoint to export."
    if reason == "empty_thread":
        return "Nothing to export — this conversation has no messages yet."
    redactions = result.get("redactions") or []
    note = (
        f" Redacted before export: {', '.join(redactions)} — read it through before sharing."
        if redactions
        else ""
    )
    return f"Exported {result.get('message_count', 0)} message(s) as Markdown.{note}"


async def export_session(
    session_id: str,
    *,
    title: str | None = None,
    request_metadata: dict | None = None,
) -> dict:
    """Export a chat session's conversation as Markdown (the "share this thread"
    gesture, #2158 P1).

    Resolves the session's checkpointer ``thread_id`` exactly as ``compact_session`` /
    ``rewind_session`` do, then runs ``export_thread``. The per-thread lock is held even
    though this is a **pure read**: it guarantees a consistent snapshot, so an export can
    never capture a half-written turn (an ``AIMessage`` whose answering ``ToolMessage``\\s
    haven't landed yet). Returns the ``export_thread`` result plus a human-readable
    ``message``.
    """
    if STATE.graph is None:
        return {
            "found": False,
            "markdown": "",
            "message_count": 0,
            "redactions": [],
            "reason": "setup",
            "message": "Setup required — finish the setup wizard first.",
        }

    from graph.export_op import export_thread

    tid = _chat()._resolve_thread_id(request_metadata, session_id)
    async with _chat()._thread_lock(tid):
        result = await export_thread(STATE.graph, STATE.checkpointer, tid, title=title)
    return {**result, "message": _export_message(result)}


def _artifact_resolver():
    """``plugins.artifact.resolve_for_bundle``, imported defensively — the artifact
    plugin is in-tree and on by default but still a plugin an operator can disable.
    ``None`` degrades ``chat_bundle.build_bundle`` to unavailable artifact parts rather
    than an ``ImportError`` (ADR 0099 D3)."""
    try:
        from plugins.artifact import resolve_for_bundle

        return resolve_for_bundle
    except ImportError:
        return None


async def _build_bundle(session_id: str, *, title: str | None, request_metadata: dict | None):
    """Shared by ``publish_preview`` and ``publish_session`` — the exact same bundle a
    preview shows is what gets published; there is no second build path."""
    tid = _chat()._resolve_thread_id(request_metadata, session_id)
    async with _chat()._thread_lock(tid):
        from graph.chat_bundle import export_bundle

        return await export_bundle(
            STATE.graph, STATE.checkpointer, tid, title=title, artifact_resolver=_artifact_resolver()
        )


def _publish_preview_message(result: dict) -> str:
    reason = result.get("reason") or ""
    if reason == "no_checkpointer":
        return "Nothing to preview — no conversation checkpoint yet."
    if reason == "empty_thread":
        return "Nothing to publish — this conversation has no messages yet."
    redactions = result.get("redactions") or []
    note = f" {len(redactions)} secret pattern(s) would be redacted." if redactions else ""
    return f"{result.get('message_count', 0)} message(s) ready to review.{note}"


async def publish_preview(
    session_id: str,
    *,
    title: str | None = None,
    request_metadata: dict | None = None,
) -> dict:
    """Build the structured chat-bundle for the pre-publish review (#2682) — **read-only,
    never sends anything anywhere**. The operator reviews this before deciding to publish;
    ``publish_session`` rebuilds fresh from the live thread rather than trusting this
    snapshot, so a stale preview can never diverge from what actually gets published.

    Returns ``{found, manifest, message_count, redactions, reason, message}``.
    """
    if STATE.graph is None:
        return {
            "found": False,
            "manifest": None,
            "message_count": 0,
            "redactions": [],
            "reason": "setup",
            "message": "Setup required — finish the setup wizard first.",
        }
    result = await _build_bundle(session_id, title=title, request_metadata=request_metadata)
    return {**result, "message": _publish_preview_message(result)}


def _publish_message(outcome: dict) -> str:
    if outcome.get("published"):
        return f"Published — {outcome.get('public_url')}"
    reason = outcome.get("reason") or "internal"
    if reason == "not_configured":
        return "Hosted publishing isn't configured on this instance yet."
    if reason in ("no_checkpointer", "empty_thread"):
        return "Nothing to publish — this conversation has no messages yet."
    return f"Publish failed ({reason}) — {outcome.get('error') or 'see server logs'}."


async def publish_session(
    session_id: str,
    *,
    title: str | None = None,
    request_metadata: dict | None = None,
) -> dict:
    """Publish a chat thread to the hosted viewer (#2179 P2, #2683).

    Builds the bundle **server-side, fresh** — never accepts a client-supplied bundle,
    the same trust boundary ``export_session`` already draws, now with a public network
    hop behind it. Returns
    ``{published, public_url, revoke_token, expires_at, redactions, artifact_notes,
    reason, message}``; ``published`` is ``False`` with a ``reason`` (``not_configured``
    when ``publish.endpoint_url`` is unset — the honest default until #2685's hosted
    service exists — or an ``infra.publish.PublishErrorKind`` value) rather than raising.
    """
    if STATE.graph is None:
        outcome = {"published": False, "reason": "setup"}
        return {**outcome, "message": "Setup required — finish the setup wizard first."}

    result = await _build_bundle(session_id, title=title, request_metadata=request_metadata)
    if not result["found"]:
        outcome = {"published": False, "reason": result["reason"]}
        return {**outcome, "message": _publish_message(outcome)}

    from graph.chat_bundle import build_bundle_zip
    from infra.publish import publish_bundle

    bundle = build_bundle_zip(result["manifest"], result["redactions"])
    cfg = STATE.graph_config
    publish_result = publish_bundle(
        bundle.data,
        endpoint_url=getattr(cfg, "publish_endpoint_url", "") or "",
        timeout_seconds=getattr(cfg, "publish_timeout_seconds", 15.0) or 15.0,
    )
    if not publish_result.ok:
        outcome = {
            "published": False,
            "reason": publish_result.error_kind.value if publish_result.error_kind else "internal",
            "error": publish_result.error,
        }
        return {**outcome, "message": _publish_message(outcome)}

    # Record it LOCALLY so it can be listed/revoked later (#2684) — best-effort: the
    # bundle is already live on the hosted service at this point, so a local disk hiccup
    # must not make a successful publish read back as a failure. It just means this
    # instance loses its own memory of the link (still revocable by hand, if the operator
    # kept the URL/token some other way).
    link_id = None
    from infra.publish import record_publish

    try:
        link_id = record_publish(
            thread_id=result["manifest"]["thread_id"],
            title=result["manifest"]["title"],
            public_url=publish_result.public_url,
            revoke_token=publish_result.revoke_token or "",
            expires_at=publish_result.expires_at,
        ).id
    except OSError:
        log.warning("[publish] could not record published link locally", exc_info=True)

    outcome = {
        "published": True,
        "link_id": link_id,
        "public_url": publish_result.public_url,
        "revoke_token": publish_result.revoke_token,
        "expires_at": publish_result.expires_at,
        "redactions": result["redactions"],
        "artifact_notes": bundle.artifact_notes,
    }
    return {**outcome, "message": _publish_message(outcome)}


async def revoke_published_link(link_id: str) -> dict:
    """Un-share a previously published thread (#2684).

    Looks up the link's stored revoke_token and presents it to the hosted service —
    marks it revoked LOCALLY only once that call confirms, never before (a local-only
    revoke would tell the operator a link is dead while it's still live). Returns
    ``{ok, error?, reason?}``.
    """
    from infra.publish import get_link, mark_revoked, revoke_bundle

    link = get_link(link_id)
    if link is None:
        return {"ok": False, "reason": "not_found", "error": "unknown published link"}
    if link.revoked_at is not None:
        return {"ok": True}  # idempotent — already revoked, nothing to do

    cfg = STATE.graph_config
    result = revoke_bundle(
        link.revoke_token,
        endpoint_url=getattr(cfg, "publish_revoke_endpoint_url", "") or "",
        timeout_seconds=getattr(cfg, "publish_timeout_seconds", 15.0) or 15.0,
    )
    if not result.ok:
        return {
            "ok": False,
            "reason": result.error_kind.value if result.error_kind else "internal",
            "error": result.error,
        }
    mark_revoked(link_id)
    return {"ok": True}


async def aside_session(
    session_id: str,
    question: str,
    *,
    request_metadata: dict | None = None,
) -> dict:
    """`/btw` — answer a side question about the session's context WITHOUT changing it
    (the incognito side turn, #2180).

    Resolves the session's checkpointer ``thread_id`` and runs ``run_aside``, which reads
    that thread's messages and runs an incognito turn on a fresh EPHEMERAL thread — so the
    main thread's checkpoint is never written. Returns ``{found, answer, reason, message}``.

    Deliberately does NOT hold the per-thread lock across the turn: the aside never writes
    the main thread (nothing to guard), and a side chat is meant to run *alongside* the main
    conversation — locking would block the very thread it's supposed to sit beside."""
    if STATE.graph is None:
        return {"found": False, "answer": "", "reason": "setup", "message": "Setup required — finish the setup wizard first."}

    from graph.aside_op import run_aside

    tid = _chat()._resolve_thread_id(request_metadata, session_id)
    result = await run_aside(
        STATE.graph,
        STATE.checkpointer,
        tid,
        question,
        session_id=session_id,
        db_path=getattr(STATE, "checkpoint_path", None),
    )
    reason = result.get("reason")
    msg = {
        "no_checkpointer": "No conversation yet — start chatting, then ask a side question.",
        "empty_question": "Ask a question after /btw, e.g. `/btw what did we decide about the schema?`",
    }.get(reason or "", "")
    return {**result, "message": msg}


def forget_delegate_conversations(*thread_ids: str) -> int:
    """Drop the transport continuity delegates hold for these checkpointer threads (#3360).

    A room hands an ``a2a`` participant this thread id as its ``conversation_key``, and the
    delegates plugin remembers the A2A ``contextId`` the peer assigned it — so the peer
    keeps a conversation of its own, keyed to this thread. That pointer has to die with the
    thread's history, or a gesture whose whole point is that something is GONE leaves the
    peer still holding it and answering from it:

    * **rewind** — destructive by design; before continuity existed it was total, because
      the participant remembered nothing. Keep it total.
    * **delete** — the same promise the attachment / prompt-snapshot / session-summary
      purges beside it already make ("its history will be removed").
    * **fork** (destination) — a new thread id that need not be an unused one.

    Compaction is deliberately NOT here: it summarizes to save this side's window and
    claims nothing was unsaid, and the peer manages its own context.

    Reached through ``STATE.delegate_registry`` — the roster the plugin publishes on
    runtime state and the ``@``-dispatch above already reads — so core keeps its
    duck-typed distance from ``plugins/``. ``hasattr``-guarded for a fork pinned to an
    older delegates plugin, and swallowing, because a cleanup must never fail the gesture.
    Returns how many contexts were dropped (0 when nothing is wired).
    """
    reg = getattr(STATE, "delegate_registry", None)
    if reg is None or not hasattr(reg, "forget_conversation"):
        return 0
    dropped = 0
    # De-duplicated: a caller passes every id that could name this conversation (both
    # retired prefixes, plus whatever a custom resolver answers) and they routinely
    # coincide — dropping the same one twice would double-count and re-log.
    for tid in dict.fromkeys(t for t in thread_ids if t):
        try:
            dropped += int(reg.forget_conversation(tid) or 0)
        except Exception as exc:  # noqa: BLE001 — best-effort, see docstring
            log.warning("[chat] delegate-continuity cleanup failed for %s: %s", tid, exc)
    return dropped


def forget_delegate_conversations_for_session(*session_ids: str) -> int:
    """Drop delegate transport continuity recorded as ORIGINATING from these chat sessions
    (#3362).

    The origin-scoped companion to ``forget_delegate_conversations``: that seam drops by
    the resolved thread KEY a room dispatched under (the id rewind/fork already hold, because
    they resolve it); this one drops by the chat SESSION a context was recorded against. A
    custom thread-id resolver (ADR 0029 §D4 / #571) can map a session to any key and the map
    is one-way, so a caller that knows only the session id — a DELETE route carries no request
    metadata to re-resolve — can still reach every context that session minted, without a
    prefix/substring guess at which keys belong to it.

    Reached through ``STATE.delegate_registry`` and ``hasattr``-guarded, exactly like its
    sibling, so a fork pinned to an older delegates plugin degrades to 'dropped nothing'; it
    swallows because a cleanup must never fail the gesture it cleans up after. No route calls
    it yet — this is the plumbing a following slice wires into the delete path.
    """
    reg = getattr(STATE, "delegate_registry", None)
    if reg is None or not hasattr(reg, "forget_conversations_for_session"):
        return 0
    dropped = 0
    for sid in dict.fromkeys(s for s in session_ids if s):
        try:
            dropped += int(reg.forget_conversations_for_session(sid) or 0)
        except Exception as exc:  # noqa: BLE001 — best-effort, see docstring
            log.warning("[chat] delegate-continuity session cleanup failed for %s: %s", sid, exc)
    return dropped


def _rewind_message(result: dict) -> str:
    """Human-readable status line for a rewind result (surfaced to non-UI callers /
    logs; the console just truncates its own thread on success)."""
    reason = result.get("reason") or ""
    if reason == "not_found":
        return "Couldn't rewind — that message is no longer in the agent's live context."
    if reason == "no_checkpointer":
        return "Rewind unavailable — no conversation checkpoint to rewind."
    if reason == "noop":
        return "Nothing to rewind — that's already the last message."
    return f"Rewound the conversation — discarded {result.get('removed', 0)} later message(s)."


async def rewind_session(
    session_id: str,
    *,
    message_id: str | None = None,
    index: int | None = None,
    content: str | None = None,
    occurrence: int | None = None,
    before: bool = False,
    request_metadata: dict | None = None,
) -> dict:
    """Rewind a chat session's live context to a target message (the "Rewind to
    here" gesture, #1535): discard everything after it and rewrite the LangGraph
    checkpoint in place.

    Resolves the session's checkpointer ``thread_id`` (the A2A ``a2a:<session_id>``
    thread the live streaming turns write to) and runs ``rewind_thread`` under the
    per-thread lock, so a rewind can never race a live streaming turn on the same
    thread (mirrors ``compact_session``). The checkpoint is the agent's REAL
    context, so a client-only truncate would leave it intact — the rewrite here is
    what actually rolls the agent's memory back. Returns the ``rewind_thread``
    result dict plus a human-readable ``message``.
    """
    base = {"found": False, "kept": 0, "removed": 0}
    if STATE.graph is None:
        return {**base, "reason": "setup", "message": "Setup required — finish the setup wizard first."}

    from graph.rewind_op import rewind_thread

    tid = _chat()._resolve_thread_id(request_metadata, session_id)
    async with _chat()._thread_lock(tid):
        result = await rewind_thread(
            STATE.graph,
            STATE.checkpointer,
            tid,
            target_index=index,
            target_id=message_id,
            target_content=content,
            occurrence=occurrence,
            before=before,
        )
        if result.get("found") and result.get("removed"):
            # Only when messages were actually discarded: a "that's already the last
            # message" rewind erased nothing, so throwing away a participant's continuity
            # would be a pure loss with no leak to close.
            #
            # INSIDE the lock, with the rewrite: outside it, a concurrent `@` dispatch can
            # take the thread between the two and either re-learn a context for the thread
            # this forget is about to clear, or learn one just after it — which is the leak
            # this call exists to close, reopened by a race.
            forget_delegate_conversations(tid)
    return {**result, "message": _rewind_message(result)}


async def fork_session(
    session_id: str,
    new_session_id: str,
    *,
    message_id: str | None = None,
    index: int | None = None,
    content: str | None = None,
    occurrence: int | None = None,
    request_metadata: dict | None = None,
) -> dict:
    """Fork a chat session at a target message (#2803): copy the checkpoint
    prefix through the target onto ``new_session_id``'s thread, leaving the
    source untouched — so the forked tab's agent actually REMEMBERS the branch
    point instead of starting amnesiac behind a seeded-looking transcript.

    Both thread locks are taken in sorted order (never a lock cycle), so the
    fork can't race a live turn on either thread.
    """
    base = {"found": False, "kept": 0, "discarded": 0}
    if STATE.graph is None:
        return {**base, "reason": "setup", "message": "Setup required — finish the setup wizard first."}
    if not (new_session_id or "").strip():
        return {**base, "reason": "no_target", "message": "Fork needs the new session's id."}

    from graph.rewind_op import fork_thread

    src_tid = _chat()._resolve_thread_id(request_metadata, session_id)
    dst_tid = _chat()._resolve_thread_id(None, new_session_id)
    if src_tid == dst_tid:
        return {**base, "reason": "same_thread", "message": "A fork must target a different session."}
    first, second = sorted((src_tid, dst_tid))
    async with _chat()._thread_lock(first):
        async with _chat()._thread_lock(second):
            result = await fork_thread(
                STATE.graph,
                STATE.checkpointer,
                src_tid,
                dst_tid,
                target_index=index,
                target_id=message_id,
                target_content=content,
                occurrence=occurrence,
            )
            if result.get("found"):
                # The destination thread's history is now the source's prefix, so any peer
                # continuity a PREVIOUS occupant of this id left behind points at a
                # conversation that has nothing to do with it. Both retired prefixes, so a
                # destination that previously served non-streaming turns (`chat:`) does not
                # keep the pointer the delete route would have dropped. The fork does not
                # inherit the source's context either — that falls out of keying on the
                # thread id, and it must not change: two threads writing into one peer
                # conversation would splice two divergent rooms together on the peer's side.
                # Inside the destination's lock, for the same reason the rewind is.
                forget_delegate_conversations(dst_tid, f"chat:{new_session_id}")
    return {**result, "message": _fork_message(result)}


def _fork_message(result: dict) -> str:
    """Human-readable status line for a fork result — honest about the failure
    modes, because the console falls back to a display-only seed and must SAY so."""
    if result.get("found"):
        return f"Forked with {result.get('kept', 0)} message(s) of real context."
    reason = result.get("reason") or ""
    if reason in ("no_checkpointer", "empty_thread"):
        return (
            "No server history to fork — this branch starts fresh (the transcript "
            "below is a display copy the agent can't see)."
        )
    if reason == "target_exists":
        return "That session already has history — fork into a fresh tab instead."
    if reason == "empty_prefix":
        return "Nothing forkable before that point — the branch starts fresh."
    if reason == "not_found":
        return (
            "Couldn't locate that message in the server history — the branch starts "
            "fresh (display copy only)."
        )
    return "Fork unavailable — the branch starts fresh (display copy only)."
