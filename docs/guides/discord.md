# Connect Discord

Install the [Discord plugin](https://github.com/protoLabsAI/discord-plugin) to
message the agent from Discord. You need permission to install a bot in a server,
a bot token, and a running protoAgent. Keep a separate bot for each agent.

## Bot setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. On **Bot**, create or reset its token and keep it private. Enable **Message
   Content Intent**, which this plugin requests for its gateway connection.
3. Configure the app for installation to your server with the `bot` scope and
   the permissions needed to send messages, read message history, add reactions,
   and create public threads. Use its installation link to add it to the server.
   Discord’s [installation guide](https://docs.discord.com/developers/quick-start/getting-started)
   describes the current portal controls.
4. To restrict who can use the bot, enable Discord’s Developer Mode and copy
   your user ID for the plugin’s **Admin user IDs** field.

See Discord’s [privileged-intent guidance](https://docs.discord.com/developers/gateway/getting-started-with-privileged-intent-review)
if the gateway refuses the requested intent. Reset a leaked token in the portal
and replace it in protoAgent.

## Connect it in the app

1. Open **Settings → Plugins → Discover**, install **Discord**, and enable the plugin.
2. Open **Installed → Discord → Configure**. Enter **Bot token** and your
   **Admin user IDs**. An empty admin list allows other users to contact the bot.
3. Choose **Test connection** and confirm the expected bot name. This checks
   the token; it does not prove message delivery.
4. Turn **Enable Discord** on and **Save & apply**. The gateway connects without
   restarting the app.
5. Mention the bot in a channel it can access, or send it a DM. Confirm a reply.

## Use conversations and reminders

DMs continue without a mention. Channel conversations begin with a mention;
follow-ups from the same user continue within the configured timeout. A new
channel conversation can create a thread for replies.

Sending a DM records a return address for proactive output such as reminders.
The agent must stay running. Check the saved task in
[Schedule](/guides/scheduler) and test one short reminder before depending on it.

## Fix a missing reply

- If **Test connection** fails, check the bot token and network access.
- If the token works but messages do not arrive, check the gateway intent,
  plugin enable switch, admin ID list, and channel permissions.
- If multiple agents use the same bot, give each a separate bot/token.
- If the model itself fails, use [Chat fails](/guides/troubleshooting#chat-fails).

Keep the error from the plugin or agent log. For configuration, timeout,
return-address and retention details, see [Discord reference](/reference/discord).
For two operators’ agents talking in a channel, follow
[Discord peer channels](/guides/discord-peer-channels).

<span id="earlier-section-links"></span>

<a id="discord-surface"></a>
<a id="conversation-model"></a>
<a id="peer-channels-—-cross-operator-agent-chat"></a>
<a id="configuration"></a>
<a id="proactive-delivery-return-address"></a>
<a id="one-bot-per-agent"></a>
<a id="long-window-context"></a>

Configuration and delivery details are in [Discord reference](/reference/discord); peer coordination is in [Discord peer channels](/guides/discord-peer-channels).
