# ADR 0029 — A standard for communication (chat-surface) plugins

**Status:** Accepted (Telegram shipped as the reference)

## Context

Chat-ingress integrations are a recurring *class* of plugin: run the agent as a
Discord bot, a Slack app, a Telegram bot, WhatsApp, etc. Discord shipped first
(ADR 0015/0016 → 0018/0019) and encodes the whole shape — but as a bespoke
surface. Every platform differs **only in transport** (how you connect, receive a
message, and send one). The surrounding glue is identical every time:

- gate inbound by an admin allowlist,
- map a message to a stable per-conversation **session/thread** so LangGraph keeps
  context across turns,
- invoke the agent and **chunk** the reply to the platform's length limit,
- lifecycle + **reconnect-on-save**,
- a **Test connection** route, and the manifest's config/secrets/Settings.

Without a standard, every new comms plugin re-implements that glue (and re-makes
the same mistakes). The user's goal: *"add Slack/Telegram easily."*

## Decision

Name the transport piece as a small protocol and put the glue in one helper —
`graph/plugins/chat_surface.py`.

### D1 — The contract

```python
@dataclass
class InboundMessage:
    text: str
    user_id: str        # for admin-gating
    channel_id: str     # for the session/thread key
    reply: Callable[[str], Awaitable[None]]   # platform send-back

class ChatAdapter(Protocol):
    id: str             # config section + route suffix, e.g. "telegram"
    chunk_limit: int    # platform max message length (0 = no chunking)
    def configured(self, cfg) -> bool: ...
    async def validate(self, cfg) -> tuple[bool, str | None, str | None]: ...   # Test button
    async def run(self, handle, *, cfg, host) -> None: ...   # connect → call handle per msg
    # optional: outbound_tools(self) -> list
```

### D2 — One wirer owns the glue

`register_chat_surface(registry, adapter)` does everything Discord hand-rolls:
builds `handle` = admin-gate → session key → `host.invoke` → chunk → `reply`;
registers the surface with start/stop + reconnect-on-reload; mounts
`POST /api/config/test-<id>`; registers `outbound_tools()` if present. A new comms
plugin's `register()` is therefore one line.

### D3 — Session key = `"<id>:<channel_id>"`

Per-conversation continuity (a Telegram chat, a Slack channel, a Discord DM each
get their own LangGraph thread). Deliberately *not* one shared thread — that would
collapse every conversation together. (Future: route through the
`thread_id_resolver` seam from #571 for custom mapping.)

### D4 — Discord stays bespoke for now

Discord predates this contract and carries platform-specific richness the minimal
adapter doesn't model — slow-response reactions, auto-threading, return-address,
context warming. It keeps working as-is; the standard captures the **common
subset**, and Discord can migrate onto the wirer incrementally (the glue is the
same; its extras layer on top). Telegram is the clean reference adapter (~80 lines
of transport + a manifest — no `surfaces/` module, since the Bot API is just HTTP).

### D5 — Manifest convention

A communication plugin claims `config_section: <id>`, `secrets: [<token>]`, and
`settings: [enabled, <token>, admin_ids]` — so the console renders the Settings
group + wizard step + Test button with no core edit.

## Consequences

- **Adding Slack/WhatsApp/etc. = implement `ChatAdapter` (4 methods) + a manifest +
  a one-line `register()`.** No lifecycle, gating, threading, chunking, or reload code.
- The **console Test button** is still wired per-Discord; a generic "test action"
  affordance in the settings schema (so any comms plugin's Test button calls
  `/api/config/test-<id>` with no console edit) is a follow-up.
- The devkit can scaffold a comms-plugin skeleton (follow-up).

See [Build a communication plugin](../guides/communication-plugins.md),
[Plugins](../guides/plugins.md), ADR
[0018](./0018-plugin-surfaces-routes-subagents.md) /
[0019](./0019-plugin-config-settings-secrets.md).
