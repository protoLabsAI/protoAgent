"""Inbound Discord gateway listener — the native half of the Discord surface (ADR 0015).

Self-contained: raw ``httpx`` + ``websockets`` over Discord Gateway/REST **v10**,
no ``discord.py``. Listens for DMs and channel @-mentions and forwards each
user's conversation to the agent, posting the reply back. **Off unless
``DISCORD_BOT_TOKEN`` is set.**

Unlike a one-shot inbox stimulus (ADR 0003), a Discord DM is *conversational*, so
the gateway invokes the agent as a **chat surface**: it calls an injected
``invoke(prompt, session_id)`` with a per-conversation ``session_id`` so the
LangGraph thread stays keyed across turns (server wires this to ``chat()``). It
also publishes a best-effort ``discord.message`` bus event so the console can
surface Discord activity. (Routing the conversation itself through the single
``system:activity`` inbox thread was rejected — it would collapse every Discord
conversation into one thread and lose per-DM continuity.)

Ported UX (from ``-deprecated-gina``): **burst debounce**, **conversation
continuity**, **slow-response reactions** (👀→✅ only when slow), **auto-threading**,
and an **admin allowlist**. Long-window context warming and return-address
delivery are follow-up slices (#489).

Tunables (env): ``DISCORD_ADMIN_IDS`` (CSV; unset ⇒ anyone),
``DISCORD_CHANNEL_CONVERSATION_TIMEOUT_S`` (300), ``DISCORD_DM_CONVERSATION_TIMEOUT_S``
(900), ``DISCORD_BURST_DEBOUNCE_S`` (3), ``DISCORD_SLOW_REACTION_S`` (4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from surfaces.discord import return_address
from surfaces.discord.conversation import ConversationManager

log = logging.getLogger("protoagent.discord")

_DISCORD_API = "https://discord.com/api/v10"
# GUILDS | GUILD_MESSAGES | GUILD_MESSAGE_REACTIONS | DIRECT_MESSAGES | MESSAGE_CONTENT
_GATEWAY_INTENTS = (1 << 0) | (1 << 9) | (1 << 10) | (1 << 12) | (1 << 15)

_MAX_LEN = 1900  # Discord's 2000 cap with headroom
_REACTION_THINKING = "👀"
_REACTION_DONE = "✅"


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return default


def _token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN")


def _admin_ids() -> set[str]:
    raw = os.environ.get("DISCORD_ADMIN_IDS", "").strip()
    return {s.strip() for s in raw.split(",") if s.strip()} if raw else set()


# Injected by start_in_background — the agent invocation + (optional) bus publish.
InvokeFn = Callable[[str, str], Awaitable[str]]
_invoke: InvokeFn | None = None
_publish: Callable[[str, dict], None] | None = None
_delivery_task: "asyncio.Task | None" = None
# Long-window turn log: None=uninitialized, False=disabled (init failed), else a
# TurnLog instance. Lazy so an import/DB failure can't break the gateway.
_turn_log: Any = None


def _get_turn_log():
    global _turn_log
    if _turn_log is None:
        try:
            from surfaces.discord.turn_log import TurnLog
            _turn_log = TurnLog()
        except Exception:  # noqa: BLE001
            log.exception("[discord] turn-log init failed — long-window context disabled")
            _turn_log = False
    return _turn_log if _turn_log is not False else None

# Module-level conversation + burst state, started from _run_gateway's loop.
_conversations = ConversationManager()
# Per-(channel_id, user_id) burst buffer; each entry combines a rapid run of
# messages into one invocation after DISCORD_BURST_DEBOUNCE_S of silence.
_message_buffers: dict[str, dict] = {}


# ── REST helpers ──────────────────────────────────────────────────────────────


async def _api(method: str, path: str, body: dict | None = None) -> dict | None:
    token = _token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(
                method,
                f"{_DISCORD_API}{path}",
                headers={"Authorization": f"Bot {token}"},
                json=body,
            )
    except httpx.HTTPError as e:
        log.warning("Discord %s %s -> %s", method, path, e)
        return None
    if resp.status_code in (200, 201, 204):
        return resp.json() if resp.content else None
    log.warning("Discord %s %s -> %d %s", method, path, resp.status_code, resp.text[:200])
    return None


async def _react(channel_id: str, message_id: str, emoji: str) -> None:
    await _api("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me")


async def _unreact(channel_id: str, message_id: str, emoji: str) -> None:
    await _api("DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me")


async def _start_thread(channel_id: str, message_id: str, name: str) -> dict | None:
    return await _api(
        "POST",
        f"/channels/{channel_id}/messages/{message_id}/threads",
        body={"name": name[:100], "auto_archive_duration": 1440},  # 24h
    )


async def _reply(channel_id: str, message_id: str, content: str, *, is_dm: bool = False) -> str | None:
    """Send the response, splitting long content at line boundaries. Returns the
    first chunk's message ID (used to start a thread on the first guild reply).
    Guild replies use ``message_reference`` so the answer threads under the
    user's message; DMs send plain messages (a reply-quote in 1:1 reads awkward)."""
    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= _MAX_LEN:
            chunks.append(remaining)
            break
        split_at = remaining[:_MAX_LEN].rfind("\n")
        if split_at < 100:
            split_at = _MAX_LEN
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()

    first_id: str | None = None
    for i, chunk in enumerate(chunks):
        body: dict = {"content": chunk}
        if i == 0 and not is_dm:
            body["message_reference"] = {"message_id": message_id}
        result = await _api("POST", f"/channels/{channel_id}/messages", body=body)
        if i == 0 and isinstance(result, dict):
            first_id = result.get("id")
    return first_id


async def _keep_typing(channel_id: str) -> None:
    """Send the typing indicator every 8s until cancelled."""
    try:
        while True:
            await _api("POST", f"/channels/{channel_id}/typing")
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        pass


# ── invocation ────────────────────────────────────────────────────────────────


async def _ask_agent(content: str, session_id: str) -> str:
    """Invoke the agent via the injected callable with a per-conversation
    ``session_id`` (the LangGraph thread key). Degrades to a readable error."""
    if _invoke is None:
        return "(internal error: Discord gateway has no agent invoker)"
    try:
        return await _invoke(content, session_id)
    except Exception as e:  # noqa: BLE001
        log.error("[discord] agent invocation failed: %s", e)
        return f"(internal error: {e})"


def _strip_mentions(content: str, bot_id: str) -> str:
    out = content
    for tag in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        out = out.replace(tag, "")
    return out.strip()


def _emit(event: str, data: dict) -> None:
    """Best-effort bus publish for console visibility (ADR 0003). Never raises."""
    if _publish is None:
        return
    try:
        _publish(event, data)
    except Exception:  # noqa: BLE001
        log.debug("[discord] bus publish failed (non-fatal)", exc_info=True)


# ── return-address delivery (reactive output → operator's DM) ─────────────────


async def _deliver_event(evt: dict) -> bool:
    """Forward a reactive Activity-thread message to the operator's Discord DM.

    Returns True if delivered. Live Discord replies use per-conversation contexts
    (not ``system:activity``), so only scheduler/inbox/proactive output publishes
    ``activity.message`` — there's no double-post of the gateway's own replies."""
    if evt.get("event") != "activity.message":
        return False
    text = ((evt.get("data") or {}).get("text") or "").strip()
    if not text:
        return False
    channel_id = return_address.get()
    if not channel_id:
        return False  # no DM captured yet — nothing to deliver to
    await _reply(channel_id, "", text, is_dm=True)
    return True


async def _delivery_loop(subscribe: Callable[[], Any]) -> None:
    """Subscribe to the event bus and deliver reactive output to the return
    address. Runs for the process lifetime; cancelled on shutdown."""
    try:
        async for evt in subscribe():
            try:
                await _deliver_event(evt)
            except Exception:
                log.exception("[discord] activity delivery failed")
    except asyncio.CancelledError:
        return


# ── message handling ──────────────────────────────────────────────────────────


async def _handle_message(d: dict, bot_id: str) -> None:
    """MESSAGE_CREATE handler. Validates + buffers the message; the actual
    invocation happens after the burst-debounce window in ``_flush_burst``."""
    author = d.get("author", {})
    if author.get("bot") or author.get("id") == bot_id:
        return

    channel_id = d.get("channel_id", "")
    message_id = d.get("id", "")
    user_id = author.get("id", "")
    raw_content = d.get("content") or ""
    if not channel_id or not message_id or not user_id:
        return

    is_dm = d.get("guild_id") is None
    is_mentioned = any(m.get("id") == bot_id for m in d.get("mentions", []))
    buffer_key = f"{channel_id}:{user_id}"
    has_active_buffer = buffer_key in _message_buffers

    # Guild messages need a mention, an active conversation, or an in-progress
    # burst; DMs always continue.
    if not is_dm and not (is_mentioned or _conversations.has(channel_id, user_id) or has_active_buffer):
        return

    admins = _admin_ids()
    if admins and user_id not in admins:
        log.info("[discord] ignored message from %s (not in DISCORD_ADMIN_IDS)", user_id)
        return

    content = _strip_mentions(raw_content, bot_id)
    if not content:
        return

    log.info("[discord] msg from %s (%s) in %s: %s",
             author.get("username"), user_id, channel_id, content[:80])
    _emit("discord.message", {"channel_id": channel_id, "user_id": user_id,
                              "username": author.get("username"), "is_dm": is_dm})

    # Capture the DM channel as the operator's return address so scheduler-fired /
    # proactive turns have somewhere to deliver. Only DM channels — a guild
    # channel is not a private inbox. Idempotent + best-effort.
    if is_dm:
        return_address.record(channel_id)

    # Buffer + (re)arm the debounce timer. No immediate reaction — fast replies
    # leave the channel clean; the slow 👀 is armed in _flush_burst.
    entry = _message_buffers.get(buffer_key)
    if entry is None:
        timeout_s = _f("DISCORD_DM_CONVERSATION_TIMEOUT_S", 900) if is_dm \
            else _f("DISCORD_CHANNEL_CONVERSATION_TIMEOUT_S", 300)
        conversation_id, is_new, _turn = _conversations.get_or_create(
            channel_id, user_id, timeout_s=timeout_s)
        entry = {
            "messages": [], "channel_id": channel_id, "user_id": user_id,
            "is_dm": is_dm, "conversation_id": conversation_id,
            "is_new_conversation": is_new, "timer": None,
        }
        _message_buffers[buffer_key] = entry

    entry["messages"].append({"id": message_id, "content": content})
    if entry.get("timer") is not None:
        entry["timer"].cancel()
    entry["timer"] = asyncio.create_task(_burst_timer(buffer_key))


async def _slow_reaction_arm(channel_id: str, msgs: list[dict], is_dm: bool, state: dict) -> None:
    """Sleep the slow-response window, then 👀 every message in the burst.
    Cancellation means the reply came back fast — no reaction needed. DMs skip
    this (the typing indicator is signal enough)."""
    if is_dm:
        return
    try:
        await asyncio.sleep(_f("DISCORD_SLOW_REACTION_S", 4))
    except asyncio.CancelledError:
        return
    state["placed"] = True
    await asyncio.gather(*[_react(channel_id, m["id"], _REACTION_THINKING) for m in msgs],
                         return_exceptions=True)


async def _burst_timer(buffer_key: str) -> None:
    try:
        await asyncio.sleep(_f("DISCORD_BURST_DEBOUNCE_S", 3))
    except asyncio.CancelledError:
        return
    try:
        await _flush_burst(buffer_key)
    except Exception:
        log.exception("[discord] burst flush failed")


async def _flush_burst(buffer_key: str) -> None:
    """Pop the buffer, combine its messages into one prompt, invoke the agent,
    and reply on the last message — with typing, slow-reaction, and auto-thread."""
    entry = _message_buffers.pop(buffer_key, None)
    if entry is None or not entry["messages"]:
        return

    msgs: list[dict] = entry["messages"]
    channel_id: str = entry["channel_id"]
    is_dm: bool = entry["is_dm"]
    conversation_id: str = entry["conversation_id"]
    is_new: bool = entry["is_new_conversation"]
    last_message_id: str = msgs[-1]["id"]

    combined = "\n\n".join(m["content"] for m in msgs).strip()
    if not combined:
        return

    # Long-window context warming: prepend the last N turns for this
    # (channel, user) so a fresh conversation (after a timeout or restart) still
    # sees prior exchanges. Record the user turns AFTER reading recent ones so
    # this burst doesn't appear in its own context block.
    user_id: str = entry["user_id"]
    forward_content = combined
    tlog = _get_turn_log()
    if tlog is not None:
        try:
            recent = tlog.get_recent_turns(channel_id, user_id, limit=8, max_age_hours=24)
            if recent:
                from surfaces.discord.context import assemble_discord_context
                forward_content = assemble_discord_context(recent, combined)
        except Exception:
            log.exception("[discord] context assembly failed — proceeding without history")
        for m in msgs:
            try:
                tlog.record_user_turn(channel_id, user_id, m["content"], conversation_id=conversation_id)
            except Exception:
                log.exception("[discord] record_user_turn failed (non-fatal)")

    typing_task = asyncio.create_task(_keep_typing(channel_id))
    slow_state = {"placed": False}
    slow_task = asyncio.create_task(_slow_reaction_arm(channel_id, msgs, is_dm, slow_state))

    # Surface-tagged session_id: keeps the LangGraph thread keyed per conversation
    # while showing "discord" provenance in audit/traces instead of a bare UUID.
    surface_tag = "discord-dm" if is_dm else f"discord-channel-{channel_id}"
    session_id = f"{surface_tag}:{conversation_id}"
    try:
        reply_text = await _ask_agent(forward_content, session_id)
    finally:
        slow_task.cancel()
        typing_task.cancel()

    if not reply_text.strip():
        log.warning("[discord] empty reply for conversation %s — graceful fallback", conversation_id)
        reply_text = ("Sorry — I lost the thread on that one. Could you say it again, "
                      "maybe with a little more detail?")

    reply_message_id = await _reply(channel_id, last_message_id, reply_text, is_dm=is_dm)

    # Record the reply so the next turn's warming includes it.
    if tlog is not None and reply_text.strip():
        try:
            tlog.record_assistant_turn(channel_id, user_id, reply_text, conversation_id=conversation_id)
        except Exception:
            log.exception("[discord] record_assistant_turn failed (non-fatal)")

    # Swap a placed 👀 for ✅; if it never fired (fast reply), leave it clean.
    if not is_dm and slow_state["placed"]:
        ops: list = []
        for m in msgs:
            ops.append(_unreact(channel_id, m["id"], _REACTION_THINKING))
            ops.append(_react(channel_id, m["id"], _REACTION_DONE))
        await asyncio.gather(*ops, return_exceptions=True)

    # Auto-thread the first turn of a guild conversation to keep the channel tidy.
    if not is_dm and is_new and reply_message_id:
        thread_name = msgs[-1]["content"].split("\n", 1)[0][:80] or "thread"
        await _start_thread(channel_id, reply_message_id, thread_name)


# ── gateway loop ──────────────────────────────────────────────────────────────


async def _heartbeat(ws, interval: float, get_seq) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": get_seq()}))
    except Exception:  # noqa: BLE001
        pass


async def _run_gateway() -> None:
    try:
        import websockets
    except ImportError:
        log.error("[discord] websockets not installed — gateway disabled")
        return

    bot = await _api("GET", "/users/@me")
    if not bot:
        log.error("[discord] could not fetch bot user; check DISCORD_BOT_TOKEN")
        return
    bot_id = bot["id"]
    log.info("[discord] bot user: %s (%s)", bot.get("username"), bot_id)

    _conversations.start()

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{_DISCORD_API}/gateway/bot",
                                headers={"Authorization": f"Bot {_token()}"})
        gateway_url = resp.json().get("url", "wss://gateway.discord.gg")

    sequence: int | None = None
    while True:
        try:
            async with websockets.connect(f"{gateway_url}?v=10&encoding=json") as ws:
                log.info("[discord] gateway connected")
                async for raw in ws:
                    data = json.loads(raw)
                    op = data.get("op")
                    if data.get("s") is not None:
                        sequence = data["s"]

                    if op == 10:  # HELLO
                        interval = data["d"]["heartbeat_interval"] / 1000
                        await ws.send(json.dumps({
                            "op": 2,
                            "d": {
                                "token": _token(),
                                "intents": _GATEWAY_INTENTS,
                                "properties": {"os": "linux", "browser": "protoagent",
                                               "device": "protoagent"},
                            },
                        }))
                        asyncio.create_task(_heartbeat(ws, interval, lambda: sequence))
                    elif op == 0:  # DISPATCH
                        t = data.get("t")
                        d = data.get("d") or {}
                        if t == "READY":
                            log.info("[discord] gateway READY (%d guilds)", len(d.get("guilds", [])))
                        elif t == "MESSAGE_CREATE":
                            try:
                                await _handle_message(d, bot_id)
                            except Exception:
                                log.exception("[discord] message handler failed")
                    elif op in (7, 9):  # RECONNECT / INVALID_SESSION
                        log.info("[discord] reconnect requested (op %s)", op)
                        break
        except Exception as e:  # noqa: BLE001
            log.warning("[discord] gateway error: %s; sleeping 5s", e)
            await asyncio.sleep(5)


def start_in_background(
    invoke: InvokeFn,
    *,
    publish: Callable[[str, dict], None] | None = None,
    subscribe: Callable[[], Any] | None = None,
) -> "asyncio.Task | None":
    """Launch the gateway listener as a background task, wiring the agent
    ``invoke(prompt, session_id)`` callable, an optional bus ``publish``, and an
    optional bus ``subscribe`` (enables return-address delivery of reactive
    output to the operator's DM). Returns ``None`` when no ``DISCORD_BOT_TOKEN``
    is set (opt-in)."""
    global _invoke, _publish, _delivery_task
    if not _token():
        log.info("[discord] DISCORD_BOT_TOKEN not set — gateway listener disabled")
        return None
    _invoke = invoke
    _publish = publish
    if subscribe is not None:
        _delivery_task = asyncio.create_task(_delivery_loop(subscribe))
    log.info("[discord] starting gateway listener")
    return asyncio.create_task(_run_gateway())


async def stop() -> None:
    """Stop the delivery loop + conversation sweeper (the gateway task is
    cancelled by the loop)."""
    global _delivery_task
    if _delivery_task is not None:
        _delivery_task.cancel()
        try:
            await _delivery_task
        except asyncio.CancelledError:
            pass
        _delivery_task = None
    await _conversations.stop()
