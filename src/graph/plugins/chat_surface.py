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
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Literal, Protocol, runtime_checkable

log = logging.getLogger("protoagent.plugins.chat_surface")

NoticeKind = Literal["notify", "question", "review"]
NoticeImportance = Literal["low", "normal", "high"]

# Delivery freshness in seconds, independent of a decision's own deadline.
_NOTICE_LIFETIMES: dict[NoticeKind, float] = {"notify": 3600, "question": 86400, "review": 3600}


@dataclass(frozen=True)
class ChatNotice:
    """Owner-only proactive output; importance is chosen by the raising code path."""

    kind: NoticeKind
    importance: NoticeImportance
    text: str
    link: str
    notice_id: str = ""
    created_at: float = 0.0  # UTC Unix seconds
    expires_at: float = 0.0


# Live chat surfaces and core transports share this registry; stop/unregister removes them.
_notifiers: dict[object, Callable[[ChatNotice], None]] = {}
_notify_lock = threading.Lock()


def register_notice_adapter(key: object, hook: Callable[[ChatNotice], object]) -> None:
    """Register an owner-only transport on the running hub loop without a plugin.

    Re-registering a key replaces its hook. Dispatch is safe from worker threads;
    async hooks run on this loop and synchronous hooks run in a worker. Already
    running deliveries may finish after unregister; queued ones are discarded.
    """
    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task] = set()

    async def deliver(notice: ChatNotice) -> None:
        with _notify_lock:
            if _notifiers.get(key) is not dispatch:
                return
        try:
            if inspect.iscoroutinefunction(hook):
                await hook(notice)
            else:
                result = await asyncio.to_thread(hook, notice)
                if inspect.isawaitable(result):
                    await result
        except Exception:  # noqa: BLE001 — one transport cannot break another
            log.exception("Core notice adapter failed")

    def queue(notice: ChatNotice) -> None:
        with _notify_lock:
            if _notifiers.get(key) is not dispatch:
                return
        task = loop.create_task(deliver(notice))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def dispatch(notice: ChatNotice) -> None:
        loop.call_soon_threadsafe(queue, notice)

    with _notify_lock:
        _notifiers[key] = dispatch


def unregister_notice_adapter(key: object) -> None:
    """Remove a core transport by key; missing keys are harmless."""
    with _notify_lock:
        _notifiers.pop(key, None)


def notify_chat_surfaces(
    text: str, link: str, *, kind: NoticeKind = "notify", importance: NoticeImportance = "normal",
    ttl_seconds: float | None = None, notice_id: str | None = None,
) -> str:
    """Queue optional adapter notifications without waiting for network delivery.

    Safe from a store's worker thread as well as the server loop. An adapter with
    no ``notify`` hook does nothing by default. Calling server code fixes importance
    for its notice type; never expose it as a model-selected tool argument.
    Adapters deliver only to configured owner destinations, never inbound chats.
    The two-argument calling form remains supported for existing plugins. Returns
    the opaque id shared by transports and the retained bus event. Times are UTC
    Unix seconds; ``ttl_seconds`` can shorten, but never extend, the kind's default
    lifetime (notify/review: one hour; question: one day). A producer may supply
    its stable notice id to share identity with its console event.
    """
    lifetime = _NOTICE_LIFETIMES[kind]
    if ttl_seconds is not None:
        if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be finite and non-negative")
        lifetime = min(lifetime, ttl_seconds)
    created_at = time.time()
    notice = ChatNotice(
        kind=kind, importance=importance, text=text, link=link,
        notice_id=notice_id or uuid.uuid4().hex, created_at=created_at, expires_at=created_at + lifetime,
    )
    # The server wires HOST even without chat plugins; publish retains by default.
    from graph.plugins.host import HOST

    try:
        if HOST.publish:
            HOST.publish("notice", asdict(notice))
    except Exception:  # noqa: BLE001 — a bus failure must not suppress adapters
        log.exception("Could not publish notice event")
    with _notify_lock:
        dispatchers = list(_notifiers.values())
    for dispatch in dispatchers:
        try:
            dispatch(notice)
        except Exception:  # noqa: BLE001 — one stopped loop cannot block another surface
            log.exception("Could not queue chat surface notification")
    return notice.notice_id


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
    # Optional: ``async notify(self, text, link, *, kind: NoticeKind,
    # importance: NoticeImportance) -> None`` sends to the instance owner's
    # explicitly configured private destination(s) ONLY. With no owner destination,
    # do nothing; never fall back to a last-seen user, channel, or group, or treat
    # the inbound admin allowlist as notification recipients. Destination resolution
    # belongs to the transport adapter, which alone knows platform identities.
    # Hooks can also accept notice_id, created_at and expires_at keyword metadata.
    # Legacy ``notify(text, link)`` hooks retain the same owner-only contract.
    # Omission is a no-op; synchronous hooks run in a worker thread.


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

    async def _notify(notice: ChatNotice) -> None:
        try:
            hook = getattr(adapter, "notify", None)
            if not callable(hook):
                return
            metadata = asdict(notice)
            metadata.pop("text")
            metadata.pop("link")
            try:
                signature = inspect.signature(hook)
                if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                    metadata = {k: v for k, v in metadata.items() if k in signature.parameters}
                signature.bind(notice.text, notice.link, **metadata)
            except (TypeError, ValueError):
                # Adapt before invoking: a TypeError *inside* a hook must never
                # trigger a second send through the legacy shape.
                metadata = {}
            if inspect.iscoroutinefunction(hook):
                await hook(notice.text, notice.link, **metadata)
            else:
                result = await asyncio.to_thread(hook, notice.text, notice.link, **metadata)
                if inspect.isawaitable(result):
                    await result
        except Exception:  # noqa: BLE001 — notification failure never affects a decision
            log.exception("[%s] proactive notification failed", adapter.id)

    def _queue_notify(notice: ChatNotice) -> None:
        with _notify_lock:
            if notify_key not in _notifiers:
                return
        task = asyncio.create_task(_notify(notice))
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
                _notifiers[notify_key] = lambda notice: loop.call_soon_threadsafe(_queue_notify, notice)
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
