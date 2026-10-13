# Connect and change models

Open **Settings → Model → Connections** to add a model source. Then choose a
model from that source for the agent or for one chat. Adding a connection does
not switch the models your agent already uses.

## Add a gateway or local endpoint

1. Choose **Add a connection** and select **OpenAI-compatible endpoint**.
2. Give it an **Id** and a **Name**. The ID is permanent and appears in model
   values; the name is a display label you can change later.
3. Enter the **Base URL** supplied by the endpoint, including its API prefix
   when required, and its **API key**. For an endpoint without authentication,
   leave the key blank.
4. Choose **Add connection**. On its row, choose **Test** to fetch its model list.
5. Find **Primary model** in **Settings → Model**, choose a model under that
   connection, and choose **Save & apply**.
6. Send a short message in a new chat. Confirm a reply before asking for a longer task.

For a local endpoint, its model server must be running. If protoAgent runs on
another machine or in Docker, `localhost` refers to that server or container,
not the computer displaying the console. The connection card currently warns
**No API key** even for keyless endpoints; verify those with **Test** and a chat reply.

The row's **Test** checks the model-list endpoint. A successful list does not
prove a particular model can answer, use tools, or create embeddings. The
setup wizard's **Test connection** makes a model-completion request instead.

## Connect a subscription

1. Choose **Add a connection**, then **Claude subscription** or
   **ChatGPT / Codex subscription**. Choose **Add connection**.
2. On its account card, choose **Sign in with Claude** or
   **Sign in with ChatGPT / Codex**.
3. Follow the flow shown. A device flow asks you to enter a displayed code in
   the opened browser tab and waits for approval. A redirect flow asks you to
   approve in that tab, paste the returned code, and choose **Complete sign-in**.
4. Wait for **Signed in**. Choose **Test**, select the connection's model under
   **Primary model**, and **Save & apply**.
5. Send a short message in a new chat to verify the connection you selected.

Use **Cancel** to end an unfinished sign-in. If no browser tab opens, follow any
link shown in the card. If it has no link, cancel, allow browser popups, and start
sign-in again. Gateway API keys belong to an OpenAI-compatible connection; the
subscription code field accepts the code returned by its sign-in flow.

## Change one chat's model

Use the model control in the composer, or run `/model` and choose a model.
This changes subsequent turns in that tab. It keeps the conversation history
and does not change the agent's primary model or other tabs.

The `/model` card picker uses **Settings → Model → Favorite models**; when that
list is empty, it offers all available models. Add or reorder favorites and
**Save & apply**. Run `/model default` to clear the chat override and follow the
agent’s configured primary model again.

Other model settings can route auxiliary work, compaction, goals, or subagents
to different models. Changing **Primary model** does not automatically replace
every explicitly configured slot. Keep the defaults until you need those routes;
see [Configuration](/reference/configuration#model) for their meanings.

## Update a key or reconnect

Choose **Edit** on a gateway connection, enter the replacement key, and choose
**Save connection**. Leaving the key blank while editing retains the saved key.
Choose **Test**, then verify a chat reply.

For a subscription, **Re-check** refreshes the sign-in status. If signed out,
use its sign-in action again. **Disconnect** removes protoAgent's stored login;
chats using that subscription need a usable connection again. It does not revoke
a vendor CLI's login remotely.

## Remove a connection

Use the row's trash button. If model slots still use it, the dialog lists them
and asks you to choose replacement targets or clear optional slots. The primary
model needs another target. Removing the last unused connection requires a
separate confirmation and can leave the agent without a model source.

If a test or chat fails, keep the error and follow
[Chat fails](/guides/troubleshooting#chat-fails). For a new installation, start
with [Set up your first agent](/tutorials/first-agent).
