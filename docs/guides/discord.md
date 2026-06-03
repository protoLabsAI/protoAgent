# Discord surface

An **optional native Discord surface** ([ADR 0015](/adr/0015-discord-ingress-surface)) —
DMs and @-mentions reach the agent, replies post back. Self-contained: raw
Discord Gateway + REST **v10** over `httpx` + `websockets` (both already core),
no `discord.py`. **Off unless `DISCORD_BOT_TOKEN` is set** — when unset the
gateway never starts and the outbound tools aren't registered.

Two halves:

- **Inbound gateway** (`surfaces/discord/`) — a persistent listener. A Discord
  DM is conversational, so it invokes the agent as a **chat surface** with a
  per-conversation `session_id` (the LangGraph thread key) — multi-turn memory
  persists per Discord conversation. It also publishes a `discord.message` bus
  event so the console can surface Discord activity.
- **Outbound tools** (`tools/discord_tools.py`) — `discord_send` / `discord_read`
  / `discord_react`, for pushing into channels. See [starter tools](/reference/starter-tools).

## Bot setup

1. **[Discord Developer Portal](https://discord.com/developers/applications)** →
   New Application → name it.
2. **Bot** tab → copy the token → this is your `DISCORD_BOT_TOKEN`
   (put it in `config/secrets.yaml` or the env — never commit it).
3. **Privileged Gateway Intents** → enable **Message Content Intent** (otherwise
   messages arrive with empty `content` and the agent can't read them).
4. **OAuth2 → URL Generator** → scopes `bot`; permissions `Send Messages`,
   `Read Message History`, `Add Reactions`, `Create Public Threads`.
5. Open the generated URL to add the bot to a server — or just **DM the bot**
   (DMs work without a server).

The gateway requests these intents: `GUILDS | GUILD_MESSAGES |
GUILD_MESSAGE_REACTIONS | DIRECT_MESSAGES | MESSAGE_CONTENT`.

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

| Env var | Default | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | **Required.** Enables the whole surface (gateway + tools). |
| `DISCORD_ADMIN_IDS` | _(unset)_ | CSV of Discord user IDs. When set, only those users are answered; unset ⇒ anyone. **Default-closed is recommended for a personal assistant.** |
| `DISCORD_CHANNEL_CONVERSATION_TIMEOUT_S` | `300` | Channel conversation-continuity window. |
| `DISCORD_DM_CONVERSATION_TIMEOUT_S` | `900` | DM conversation-continuity window. |
| `DISCORD_BURST_DEBOUNCE_S` | `3` | Silence before a message burst is flushed. |
| `DISCORD_SLOW_REACTION_S` | `4` | Grace window before the 👀 "still working" reaction. |
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

Discord's gateway permits **one concurrent connection per token**. A second
listener on the same token evicts the first — so don't share a token across
agents; give each its own bot (cf. [multiple instances](/guides/multi-instance)).

## Long-window context

Every Discord exchange is logged to a small SQLite turn store (separate from the
knowledge DB; `DISCORD_LOG_PATH` to override). When a conversation has gone cold
(the continuity window expired) or the process restarted, the next message is
**warmed** with the last few turns for that `(channel, user)` — prepended as a
`<recent_conversation>` block — so continuity survives timeouts and restarts.
It's best-effort: if the store can't init, the gateway just runs without warming.
