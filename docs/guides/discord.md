# Discord surface

An **optional native Discord surface** ([ADR 0015](/adr/0015-discord-ingress-surface),
[ADR 0016](/adr/0016-discord-ui-config)) — DMs and @-mentions reach the agent,
replies post back. Raw Discord Gateway + REST **v10** over `httpx` + `websockets`
(both already core), no `discord.py`. It's a **standalone plugin**
([`protoLabsAI/discord-plugin`](https://github.com/protoLabsAI/discord-plugin), ADR
0058) — install it at runtime, then give it a bot token. **Off until you do** — when
unset the gateway never starts and the outbound tools aren't registered.

## Connect it in the app

The quickest path — no files, no env vars:

1. **Install the plugin** — open **System → Settings → Plugins → Discover**, find
   **Discord**, and click **Install** (works on every surface, including the desktop
   app). Then enable it.
2. **Create a bot and copy its token** — follow [Bot setup](#bot-setup) below
   (≈2 minutes in Discord's Developer Portal).
3. Under **Installed → Discord → Configure**, paste the **Bot token**, and optionally
   your **Discord user ID(s)** (so only you can talk to it).
4. Click **Test connection** — it verifies the token and shows the bot's name.
   Turn **Enable Discord** on, then **Save & apply** — the gateway connects live,
   no restart.

The token is stored in the per-agent `secrets.yaml` (never committed). The
`DISCORD_BOT_TOKEN` / `DISCORD_ADMIN_IDS` **env vars still work as a fallback**
for Docker/headless deploys.

Two halves (both in the plugin):

- **Inbound gateway** — a persistent listener. A Discord DM is conversational, so
  it invokes the agent as a **chat surface** with a per-conversation `session_id`
  (the LangGraph thread key) — multi-turn memory persists per Discord conversation.
  It also publishes a `discord.message` bus event so the console can surface activity.
- **Outbound tools** — `discord_send` / `discord_read` / `discord_react`, for pushing
  into channels. See [starter tools](/reference/starter-tools).

## Bot setup

Creating the bot is the one part that happens on Discord's side. It takes about
two minutes:

1. **[Discord Developer Portal](https://discord.com/developers/applications)** →
   **New Application** → give it a name (this becomes the bot's name).
2. **Bot** tab → **Reset Token** → **Copy** — this is the token you paste into
   the app (**System → Settings → Discord**, or the setup wizard's Discord step).
   Treat it like a password; never commit or share it. If you ever leak it, come
   back here and **Reset Token** to invalidate the old one.
3. **Privileged Gateway Intents** (same Bot tab) → turn on **Message Content
   Intent**. Without it, messages arrive with empty `content` and the agent
   can't read them — this is the most common "the bot sees nothing" mistake.
4. **Find your own Discord user ID** (so you can lock the bot to just you): in
   Discord, **User Settings → Advanced → Developer Mode** on, then right-click
   your name → **Copy User ID**. Paste it into the app's **Admin user ID(s)**
   field. (Leave it blank to let anyone DM the bot — not recommended for a
   personal assistant.)
5. **Add the bot somewhere it can talk to you** — either:
   - **Just DM it** — DMs work without adding the bot to any server. Simplest.
   - **Invite it to a server**: **OAuth2 → URL Generator** → scope `bot`;
     permissions `Send Messages`, `Read Message History`, `Add Reactions`,
     `Create Public Threads` → open the generated URL and pick your server.
6. Back in the app, **Test connection** (confirms the token + shows the bot
   name), enable Discord, and save. Then DM your bot.

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

Discord is a **standalone external plugin** ([`protoLabsAI/discord-plugin`](https://github.com/protoLabsAI/discord-plugin),
ADR 0018/0019/0058) — the gateway, the `test-discord` route, the outbound tools, and
this `discord` config section are all declared by its `protoagent.plugin.yaml`, never
wired into the core `server/` package. Install it from Settings ▸ Plugins ▸ Discover;
uninstall or `plugins: { disabled: [discord] }` to turn it off — no core edit. See
[Plugins](./plugins.md).

The token, admin list, and on/off toggle are set **in the app** (Settings →
Discord, or the setup wizard) and stored in the per-agent config — `bot_token`
in the gitignored `secrets.yaml`, the rest under the `discord:` section of
`langgraph-config.yaml` (resolved into `plugin_config["discord"]` — a
plugin-declared section, not a typed config field):

| Field (Settings → Discord) | YAML | Purpose |
|---|---|---|
| Enable Discord | `discord.enabled` | Master on/off. Reconnects live on save. |
| Bot token | `discord.bot_token` _(→ secrets.yaml)_ | **Required to enable.** The whole surface (gateway + tools). |
| Admin user ID(s) | `discord.admin_ids` | Discord user IDs allowed to talk to the bot; empty ⇒ anyone. **Lock it to yourself for a personal assistant.** |

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
