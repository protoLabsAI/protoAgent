# Build a communication plugin

A **communication plugin** turns your agent into a chat bot on a platform — Discord,
Slack, Telegram, WhatsApp, … It's a normal [plugin](./plugins.md), but because every
chat platform shares the same shape, there's a standard ([ADR 0029](../adr/0029-communication-plugins-standard.md))
so you only write the **transport**. Everything else — admin-gating, per-conversation
threads, invoking the agent, chunking replies, lifecycle + reconnect, the Test button —
is shared.

## The two pieces

**1. An adapter** implementing `ChatAdapter` (the platform-specific half):

```python
# plugins/telegram/__init__.py
from graph.plugins.chat_surface import InboundMessage, register_chat_surface

class TelegramAdapter:
    id = "telegram"
    chunk_limit = 4096                      # platform message-length cap

    def configured(self, cfg) -> bool:      # do we have creds to connect?
        return bool((cfg.get("bot_token") or "").strip())

    async def validate(self, cfg):          # the console "Test connection" button
        # → (ok, identity_or_None, error_or_None)
        ...

    async def run(self, handle, *, cfg, host):
        # connect, then loop: for each inbound message build an InboundMessage
        # (with a `reply` that sends back) and `await handle(msg)`. Runs until cancelled.
        async def reply(text, _chat=chat_id):
            await send(_chat, text)
        await handle(InboundMessage(text=text, user_id=str(uid),
                                    channel_id=str(chat_id), reply=reply))

def register(registry):
    register_chat_surface(registry, TelegramAdapter())   # ← the whole wiring
```

**2. A manifest** declaring the config surface (the console renders Settings + Test
from this):

```yaml
# plugins/telegram/protoagent.plugin.yaml
id: telegram
name: Telegram
config_section: telegram
config: { enabled: false, admin_ids: [] }
secrets: [bot_token]
settings:
  - { key: enabled,   label: "Enable Telegram", type: bool }
  - { key: bot_token, label: "Bot token",       type: secret }
  - { key: admin_ids, label: "Admin user IDs",  type: string_list }
```

## What `register_chat_surface` does for you

| Concern | Handled by the wirer |
| --- | --- |
| Admin allowlist | `admin_ids` gate on `msg.user_id` (empty = anyone) |
| Conversation memory | session/thread key `"<id>:<channel_id>"` — per-chat continuity |
| Running the agent | `host.invoke(text, session_id)` |
| Long replies | split to `adapter.chunk_limit` (newline/space-aware) |
| Lifecycle | start/stop + **reconnect on Settings save** |
| Test connection | `POST /api/config/test-<id>` → `adapter.validate` |
| Send tools | registers `adapter.outbound_tools()` if you define it |

So your adapter only implements **connect / receive → `handle` / send**.

## Use it

```bash
# enable the bundled Telegram plugin (or install your own from a git URL)
# config.yaml → plugins: { enabled: [telegram] }
```

Set the bot token in **Settings → Telegram**, hit **Test connection**, enable, save —
the gateway reconnects live. Message your bot; replies come back in the same chat,
each chat its own thread.

See [ADR 0029](../adr/0029-communication-plugins-standard.md),
[Plugins](./plugins.md), [Install & publish plugins](./plugin-registry.md).
