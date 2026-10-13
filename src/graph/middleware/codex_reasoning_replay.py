"""Recover a thread whose replayed encrypted reasoning was rejected (ADR 0097).

``graph/providers/codex_client.py`` stops protoAgent from *minting* an
un-verifiable reasoning item. This is the other half: the blob can be rejected for
reasons the sender cannot see ahead of time —

- **cross-issuer replay.** ``encrypted_content`` is sealed to the endpoint that
  minted it. protoAgent lets every slot name its own connection
  (``gateway:…`` / ``anthropic-oauth:…`` / ``openai-codex:…``), lets each chat tab
  override the model per turn, and retries a failed turn against the fallback
  chain — all of which replay ONE thread's history to an endpoint that did not
  mint it.
- **a rotated credential**, a relay that only pretends to persist reasoning state,
  or a blob written by an older/newer client.

The provider answers all of them the same way::

    400 invalid_encrypted_content — The encrypted content for item rs_… could not
    be verified. Reason: Encrypted content could not be decrypted or parsed.

Left alone that is terminal, not transient: the offending item is checkpointed,
so every later turn in the thread re-sends it and fails identically. So this
middleware makes the agent self-heal, the same way ``tool_call_repair`` does for a
dangling ``tool_call``:

1. **One-shot retry.** The first matching failure strips the replay state from the
   request's messages and re-runs the call. The rejection is a request-validation
   400 — it lands before any token streams — so the retry cannot double-emit.
2. **Then the session stops replaying.** The session is flagged, and
   ``before_model`` rewrites the offending assistant messages in place (same ids,
   so the ``add_messages`` reducer REPLACES them), which takes the bad item out of
   the checkpoint instead of merely dodging it for one call.

Guarded so it can only ever touch the thread it is meant to fix: an unrelated
error propagates unchanged, a matching error on a history that carries no replay
state propagates unchanged (the provider is objecting to something else), and a
second failure after the strip propagates too. It is a no-op on every healthy turn
— which is why it is registered unconditionally rather than per-provider: a
gateway relaying the Responses API can raise this just as easily.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

log = logging.getLogger(__name__)

# The key langchain-openai stashes a Responses reasoning item under in v0 mode.
_REASONING_KEY = "reasoning"

# Bounded so a long-lived server can't accumulate session ids forever; a flagged
# session only needs to stay flagged for as long as it is being served.
_MAX_TRACKED_SESSIONS = 512
_replay_disabled: dict[str, None] = {}


def is_invalid_encrypted_content(exc: BaseException) -> bool:
    """Does ``exc`` say a replayed reasoning blob failed verification?

    Matches the error code when the client exposes one and falls back to the
    message text, which is what survives once a gateway or SDK re-wraps the 400.
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
    if isinstance(code, str) and code.strip().lower() == "invalid_encrypted_content":
        return True

    text = str(exc).lower()
    return (
        "invalid_encrypted_content" in text
        or ("encrypted content for item" in text and "could not be verified" in text)
        or "could not decrypt the provided encrypted_content" in text
    )


def _without_reasoning(message: Any) -> Any | None:
    """A copy of ``message`` with its reasoning replay state removed, or ``None``
    when it carried none (so callers can tell "changed" from "already clean")."""
    if not isinstance(message, AIMessage):
        return None
    extra = getattr(message, "additional_kwargs", None) or {}
    if _REASONING_KEY not in extra:
        return None
    return message.model_copy(update={"additional_kwargs": {k: v for k, v in extra.items() if k != _REASONING_KEY}})


def carries_replay_state(messages) -> bool:
    """Is there anything in ``messages`` this middleware could strip?"""
    return any(_without_reasoning(m) is not None for m in messages or [])


def strip_replay_state(messages) -> list:
    """The full message list, cleaned — what the retry sends."""
    return [(_without_reasoning(m) or m) for m in messages or []]


def replay_repairs(messages) -> list:
    """Only the messages that changed — replacements by id, for ``before_model``."""
    return [c for c in (_without_reasoning(m) for m in messages or []) if c is not None]


def _session_of(state) -> str:
    return str((state or {}).get("session_id") or "")


def _disable(session_id: str) -> None:
    if len(_replay_disabled) >= _MAX_TRACKED_SESSIONS:
        _replay_disabled.pop(next(iter(_replay_disabled)), None)
    _replay_disabled[session_id] = None


def is_replay_disabled(session_id: str) -> bool:
    return session_id in _replay_disabled


class CodexReasoningReplayRecoveryMiddleware(AgentMiddleware):
    """Self-heal a thread whose replayed encrypted reasoning is being rejected."""

    def _repair(self, state):
        if not is_replay_disabled(_session_of(state)):
            return None
        repairs = replay_repairs((state or {}).get("messages") or [])
        if not repairs:
            return None
        try:
            from observability.trajectory import log_surface_op

            log_surface_op(
                _session_of(state),
                "repair",
                cause="rejected encrypted reasoning replay",
                rewritten_ids=[getattr(m, "id", None) for m in repairs],
            )
        except Exception:  # noqa: BLE001 — trajectory is best-effort
            pass
        return {"messages": repairs}

    def before_model(self, state, runtime):  # type: ignore[override]
        return self._repair(state)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        return self._repair(state)

    def _recoverable(self, request, exc: BaseException) -> bool:
        """Only a matching error, on a session still replaying, whose history
        actually carries replay state. Anything else is not ours to retry."""
        if not is_invalid_encrypted_content(exc):
            return False
        session_id = _session_of(getattr(request, "state", None))
        if is_replay_disabled(session_id):
            return False  # already stripped once — the provider means something else
        return carries_replay_state(getattr(request, "messages", None))

    def _recover(self, request):
        session_id = _session_of(getattr(request, "state", None))
        _disable(session_id)
        log.warning(
            "[codex] the provider rejected replayed encrypted reasoning "
            "(invalid_encrypted_content); stripped the replay state from this thread "
            "and retrying once. Reasoning replay is off for the rest of this session."
        )
        return request.override(messages=strip_replay_state(request.messages))

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except Exception as exc:
            if not self._recoverable(request, exc):
                raise
            return handler(self._recover(request))

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except Exception as exc:
            if not self._recoverable(request, exc):
                raise
            return await handler(self._recover(request))
