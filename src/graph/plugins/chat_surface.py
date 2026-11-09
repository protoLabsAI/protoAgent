"""A standard for **communication plugins** — chat-ingress surfaces (Discord,
Slack, Telegram, …) — built on the plugin system (ADR 0018/0019, ADR 0029).

Every chat platform differs only in *transport*. The glue — admin-gating, mapping
an inbound message to a stable session/thread, invoking the agent, chunking the
reply to the platform's limit, lifecycle + reconnect-on-save, and the Test route —
is identical. So this module names the transport piece as a small protocol
(``ChatAdapter``) and puts the glue in one helper (``register_chat_surface``).

A new comms plugin then implements **only** the adapter (connect, receive → call
``handle``, send) and a manifest, and its ``register()`` is one line::

    from graph.plugins.chat_surface import register_chat_surface
    def register(registry):
        register_chat_surface(registry, MyAdapter())

The manifest convention (ADR 0029): ``config_section: <id>``, ``secrets: [<token>]``,
``settings: [enabled, <token>, admin_ids]``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable

log = logging.getLogger("protoagent.plugins.chat_surface")

# Only live, configured surfaces participate; stop/reload removes their dispatcher.
_notifiers: dict[object, Callable[[str, str], None]] = {}
_notify_lock = threading.Lock()


def notify_chat_surfaces(text: str, link: str) -> None:
    """Queue optional adapter notifications without waiting for network delivery.

    Safe from a store's worker thread as well as the server loop. An adapter with
    no ``notify(text, link)`` hook does nothing by default.
    """
    with _notify_lock:
        dispatchers = list(_notifiers.values())
    for dispatch in dispatchers:
        try:
            dispatch(text, link)
        except Exception:  # noqa: BLE001 — one stopped loop cannot block another surface
            log.exception("Could not queue chat surface notification")


@dataclass
class InboundMessage:
    """One inbound chat message, normalized across platforms."""

    text: str
    user_id: str  # platform user id — for admin-gating
    channel_id: str  # platform channel/DM id — for the session/thread key
    reply: Callable[[str], Awaitable[None]]  # adapter-provided send-back


@runtime_checkable
class ChatAdapter(Protocol):
    """The transport half of a communication plugin. Implement these; the wirer
    does everything else."""

    id: str  # config section + route suffix, e.g. "telegram"
    chunk_limit: int  # max outbound message length (0 = no chunking)

    def configured(self, cfg: dict) -> bool:
        """True when the credentials needed to connect are present in ``cfg``."""

    async def validate(self, cfg: dict) -> tuple[bool, str | None, str | None]:
        """Verify the credentials (the console Test button). Returns
        ``(ok, identity, error)`` — identity is e.g. the bot's username."""

    async def run(self, handle: Callable[[InboundMessage], Awaitable[None]], *, cfg: dict, host) -> None:
        """Connect and loop: for each inbound message, build an ``InboundMessage``
        (with a ``reply`` that sends back) and ``await handle(msg)``. Runs until
        cancelled. ``host`` exposes ``publish``/``subscribe`` for extras."""

    # Optional: ``outbound_tools(self) -> list`` to also give the agent send tools.
    # Optional: ``async notify(self, text: str, link: str) -> None`` sends an
    # unsolicited message to the adapter's configured destination(s). Omission
    # is a no-op; synchronous hooks are also supported, in a worker thread.


def _chunk(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces ≤ ``limit``, preferring newline then space
    boundaries. ``limit<=0`` → one piece."""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def register_chat_surface(registry, adapter: ChatAdapter) -> None:
    """Wire a ``ChatAdapter`` as a first-party-style communication plugin: the
    inbound→invoke→reply glue, lifecycle + reconnect-on-save, a Test route, and
    any outbound tools. Reads config from the plugin's manifest section
    (``enabled``, credentials, ``admin_ids``)."""
    host = registry.host
    state: dict = {"task": None}
    notify_key = object()
    notifications: set[asyncio.Task] = set()

    async def _notify(text: str, link: str) -> None:
        try:
            hook = getattr(adapter, "notify", None)
            if not callable(hook):
                return
            if inspect.iscoroutinefunction(hook):
                await hook(text, link)
            else:
                result = await asyncio.to_thread(hook, text, link)
                if inspect.isawaitable(result):
                    await result
        except Exception:  # noqa: BLE001 — notification failure never affects a decision
            log.exception("[%s] decision notification failed", adapter.id)

    def _queue_notify(text: str, link: str) -> None:
        with _notify_lock:
            if notify_key not in _notifiers:
                return
        task = asyncio.create_task(_notify(text, link))
        notifications.add(task)
        task.add_done_callback(notifications.discard)

    async def handle(msg: InboundMessage) -> None:
        admin_ids = {str(a).strip() for a in (registry.config.get("admin_ids") or []) if str(a).strip()}
        if admin_ids and str(msg.user_id) not in admin_ids:
            log.info("[%s] ignoring non-admin user %s", adapter.id, msg.user_id)
            return
        session_id = f"{adapter.id}:{msg.channel_id}"  # stable per-conversation thread
        try:
            answer = await host.invoke(msg.text, session_id)
        except Exception:  # noqa: BLE001 — a turn error must not kill the gateway
            log.exception("[%s] invoke failed", adapter.id)
            await msg.reply("⚠️ Something went wrong handling that — try again.")
            return
        for piece in _chunk(answer, adapter.chunk_limit):
            await msg.reply(piece)

    def _should_start(cfg: dict) -> bool:
        return bool(cfg.get("enabled")) and adapter.configured(cfg)

    def _start():
        cfg = registry.config
        if not _should_start(cfg):
            log.info(
                "[%s] not started (enabled=%s, configured=%s)", adapter.id, cfg.get("enabled"), adapter.configured(cfg)
            )
            return None
        if not (host and host.invoke):
            log.warning("[%s] no agent invoke available — not started", adapter.id)
            return None
        state["task"] = asyncio.ensure_future(adapter.run(handle, cfg=dict(cfg), host=host))
        if callable(getattr(adapter, "notify", None)):
            loop = asyncio.get_running_loop()
            with _notify_lock:
                _notifiers[notify_key] = lambda text, link: loop.call_soon_threadsafe(_queue_notify, text, link)
        return state["task"]

    async def _stop():
        with _notify_lock:
            _notifiers.pop(notify_key, None)
        for notification in tuple(notifications):
            notification.cancel()
        if notifications:
            await asyncio.gather(*notifications, return_exceptions=True)
        task = state.get("task")
        if task is not None:
            task.cancel()
            state["task"] = None

    async def _reload(new_config):
        new = (getattr(new_config, "plugin_config", {}) or {}).get(adapter.id, {})
        await _stop()
        registry.config = dict(new)
        _start()

    registry.register_surface(_start, stop=_stop, reload=_reload, name=f"{adapter.id}-gateway")

    # Test route — mounted at the convention path so a console Test button works.
    from fastapi import APIRouter

    router = APIRouter()

    @router.post(f"/api/config/test-{adapter.id}")
    async def _test(body: dict | None = None):
        ok, identity, error = await adapter.validate({**registry.config, **(body or {})})
        return {"ok": ok, "identity": identity, "error": error}

    registry.register_router(router, prefix="")

    tools = adapter.outbound_tools() if hasattr(adapter, "outbound_tools") else []
    if tools:
        registry.register_tools(tools)
