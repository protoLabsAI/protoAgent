# Discord configuration and delivery

Use [Connect Discord](/guides/discord) for installation and a first reply; use [peer channels](/guides/discord-peer-channels) for agent-to-agent coordination. Configuration belongs to the external Discord plugin.

## Conversation model

- **DMs** always continue (no mention needed). **Channel** messages start a
  conversation on an @-mention; follow-ups in the same channel from the same
  user continue it within the timeout window.
- The `conversation_id` is the agent's `session_id` (surface-tagged
  `discord-dm:…` / `discord-channel-…:…` for provenance in traces), so the
  LangGraph thread stays keyed across turns.
- **Burst debounce** — a rapid run of messages is coalesced into one invocation
  after a few seconds of silence (reply attaches to the last).
- **Slow-response reactions** — fast replies leave the channel clean; only when a
  turn is slow does a 👀 land on the message(s), swapped to ✅ on completion.
  (DMs never get reactions — the typing indicator is signal enough.)
- **Auto-thread** — the first reply in a new channel conversation opens a thread
  (24h auto-archive) so long answers don't clutter the channel.

## Configuration

Discord is a **standalone external plugin** ([`protoLabsAI/discord-plugin`](https://github.com/protoLabsAI/discord-plugin),
ADR 0018/0019/0058) — the gateway, the `test-discord` route, the outbound tools, and
this `discord` config section are all declared by its `protoagent.plugin.yaml`, never
wired into the core `server/` package. Install it from Settings ▸ Plugins ▸ Discover;
uninstall or `plugins: { disabled: [discord] }` to turn it off — no core edit. See
[Plugins](/guides/plugins.md).

The token, admin list, and on/off toggle are set **in the app** (Settings → Plugins → Installed → Discord → Configure) and stored in the per-agent config — `bot_token`
in the gitignored `secrets.yaml`, the rest under the `discord:` section of
`langgraph-config.yaml` (resolved into `plugin_config["discord"]` — a
plugin-declared section, not a typed config field):

| Field (Settings → Plugins → Installed → Discord → Configure) | YAML | Purpose |
|---|---|---|
| Enable Discord | `discord.enabled` | Master on/off. Reconnects live on save. |
| Bot token | `discord.bot_token` _(→ secrets.yaml)_ | **Required to enable.** The whole surface (gateway + tools). |
| Admin user ID(s) | `discord.admin_ids` | Discord user IDs allowed to talk to the bot; empty ⇒ anyone. **Lock it to yourself for a personal assistant.** |
| Peer channel IDs | `discord.peer_channel_ids` | Channels shared with another operator's agent ([peer channels](/guides/discord-peer-channels)). Empty ⇒ off. |
| Peer bot user IDs | `discord.peer_bot_ids` | The other operator's bot user IDs trusted in those channels. |
| Peer turn budget | `discord.peer_max_turns` | Consecutive peer replies before ⏸ (default 6). |
| Peer turn tools | `discord.peer_tools` | Tool allowlist for peer-triggered turns (default read-only trio; empty ⇒ none). |

The matching **env vars are a fallback** for Docker/headless deploys (the
in-app config takes precedence when set):

| Env var | Default | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | Enables the surface when no in-app token is set. |
| `DISCORD_ADMIN_IDS` | _(unset)_ | CSV of Discord user IDs (overridden by the in-app admin list). |
| `DISCORD_CHANNEL_CONVERSATION_TIMEOUT_S` | `300` | Channel conversation-continuity window. |
| `DISCORD_DM_CONVERSATION_TIMEOUT_S` | `900` | DM conversation-continuity window. |
| `DISCORD_BURST_DEBOUNCE_S` | `3` | Silence before a message burst is flushed. |
| `DISCORD_SLOW_REACTION_S` | `4` | Grace window before the 👀 "still working" reaction. |
| `DISCORD_PEER_CONVERSATION_TIMEOUT_S` | `900` | Peer-channel conversation-continuity window. |
| `DISCORD_PEER_COOLDOWN_S` | `3` | Minimum gap between the bot's replies to a peer agent. |
| `DISCORD_RETURN_ADDRESS_PATH` | _(instance-scoped default)_ | Override the return-address store location. |

## Proactive delivery (return address)

When you DM the agent, it records that DM channel as your **return address**.
Scheduler-fired and proactive turns have no originating caller — so reactive
output that lands in the Activity thread (a fired reminder, an inbox `now` item,
a scheduled briefing) is **forwarded to your Discord DM**. That's what makes
"remind me in 30 minutes" actually arrive somewhere.

- Capture is automatic + idempotent on any DM; only **DM** channels are stored
  (a guild channel isn't a private inbox). Override the file location with
  `DISCORD_RETURN_ADDRESS_PATH`.
- Delivery is opt-in by usage: until you've DM'd the bot once, there's no address
  and nothing is forwarded. Your live Discord replies aren't affected (they use
  per-conversation contexts, not the Activity thread — no double-posting).

## One bot per agent

Use a separate bot/token per agent so message routing and identity remain unambiguous. Do not share one token across
agents; give each its own bot (cf. [multiple instances](/guides/multi-instance)).

## Long-window context

Every Discord exchange is logged to a small SQLite turn store (separate from the
knowledge DB; `DISCORD_LOG_PATH` to override). When a conversation has gone cold
(the continuity window expired) or the process restarted, the next message is
**warmed** with the last few turns for that `(channel, user)` — prepended as a
`<recent_conversation>` block — so continuity survives timeouts and restarts.
It's best-effort: if the store can't init, the gateway just runs without warming.
