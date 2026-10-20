# Set up your first agent

Install protoAgent, connect a model, and send your first message. You need a model
endpoint (hosted or local), or a supported Claude or ChatGPT subscription.

## 1. Install and start

**Recommended: desktop app.** [Download protoAgent](https://agent.protolabs.studio/download)
for your operating system. The server and console are bundled; you do not need
Python or Node to chat.

- **macOS:** open the `.dmg`, drag protoAgent to Applications, then launch it.
- **Windows:** run the installer, then launch protoAgent from the Start Menu.
  See [Windows installation help](/guides/windows-desktop) for the unsigned-build prompt.
- **Linux:** make the downloaded AppImage executable and run it, or install the
  `.deb` with your package manager. The download page lists supported systems.

The setup wizard should open. If the app will not open, start with
[App won't open](/guides/troubleshooting#app-won-t-open).

::: details Alternative: run the Python package in a browser
Use this route if you prefer starting the server from a terminal.

**Python package:** with uv installed, run:

```bash
uvx --from protolabs-agent protoagent serve
```

Keep the terminal open, then visit <http://localhost:7870>. Run the same command
when you want to start it again. If port 7870 is taken, add `--port 7880` and open
that port instead.
:::

::: details Run from a source checkout
Use a checkout to develop or change protoAgent. You need Git, uv, Python 3.11+,
Node 20, and npm 11+. On Windows, choose a short checkout path such as
`C:\src\protoAgent` to avoid long dependency paths. Build the console before
starting the server:

```bash
git clone https://github.com/protoLabsAI/protoAgent.git
cd protoAgent
uv sync --frozen
```

If you use nvm, run `nvm use` now. Check `npm --version`; if it is below 11,
run `npm install -g npm@11` before continuing.

```bash
npm ci
npm run build --workspace @protoagent/web
uv run python -m server
```

:::

## 2. Choose and name your agent

The setup wizard opens on a fresh instance. Click **Next**, choose **Basic** for
this walkthrough, and give the agent a name. Leave the starting persona as-is;
you can edit it later in **Settings → Identity**.

## 3. Connect a model

In **Brain**, choose a connection:

- **Gateway model:** enter an OpenAI-compatible base URL and any required API key.
  For example, use `https://api.openai.com/v1` for OpenAI or
  `http://localhost:4000/v1` for a local LiteLLM gateway. Choose a model from the
  fetched list (**Probe** loads the gateway models).
- **Claude subscription** or **ChatGPT / Codex subscription:** follow the sign-in
  controls, then choose an available model. These routes use a subscription login
  instead of an API key.

Click **Test connection**. Continue when the wizard reports that the model
responded. If it fails, check the endpoint URL, credentials, and selected model;
for a local endpoint, confirm its server is running.

For sign-in or connection errors, see [Chat fails](/guides/troubleshooting#chat-fails).

<!-- docs-media: setup-model-test -->
<figure class="docs-media"><img src="/media/setup-model-test/v1.png" alt="The setup wizard shows Claude subscription selected, a model name, and a successful real connection test." loading="lazy" style="max-width:100%;height:auto"><figcaption>Run Test connection before continuing. This example uses a Claude subscription; gateway setup uses the same test step.</figcaption></figure>
<!-- /docs-media -->

## 4. Finish and chat

Review your choices and click **Finish**. Once the chat opens, send:

> Say hello in one sentence.

A reply confirms that setup is complete and the selected model can answer.
Continue to [Use protoAgent](/guides/use-protoagent) for everyday tasks.

**Optional: try a tool.** Ask “What time is it in Tokyo?” With the starter tools
available, you should see a `current_time` tool call followed by a reply.
A failed tool does not necessarily mean the model connection failed. Read the
error in chat, check **Settings → Tools**, and use
[Troubleshooting](/guides/troubleshooting) for the matching symptom.

<!-- docs-media: first-reply -->
<figure class="docs-media"><img src="/media/first-reply/v1.png" alt="Docs demo answers “Say hello in one sentence.” The completed reply appears above the chat composer." loading="lazy" style="max-width:100%;height:auto"><figcaption>A completed model reply confirms that the connection can answer. This capture uses an isolated demo agent.</figcaption></figure>
<!-- /docs-media -->

## Find your configuration

Setup saves the model connection, name, and persona for this instance. You do
not need to edit configuration files to finish setup.

For desktop data locations, see [Find your data](/guides/backup-and-restore#find-your-data).
For the Python-package route above, open a second terminal and run:

```bash
uvx --from protolabs-agent protoagent config explain
```

In a source checkout, use `uv run python -m server config explain`. These
commands report the paths and settings for the same environment used to start
the server. See [Configuration](/reference/configuration) for field details.

## Changing your mind

Use **Settings → Identity** for the name and persona, **Model → Connections** for
connections, and **Tools** or **Plugins** for capabilities. Adding a connection
does not switch the primary model; follow
[Connect and change models](/guides/model-connections) to select it and save.

## Next steps

Continue with [Use protoAgent](/guides/use-protoagent) to learn chat, files, and tools.

- [Work with files and documents](/guides/documents-and-files) — attach a file or create a document.
- [Back up your data](/guides/backup-and-restore) — preserve chats and settings.
