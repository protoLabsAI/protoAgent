# Set up your first agent

Install protoAgent, connect a model, and send your first message. You need a model
endpoint (hosted or local), or a supported Claude or ChatGPT subscription.

## 1. Install and start

**Desktop:** [download protoAgent](https://agent.protolabs.studio/download) for
your operating system and open it. The server and console are bundled.

**Python package:** with uv installed, run:

```bash
uvx --from protolabs-agent protoagent serve
```

Keep the terminal open, then visit <http://localhost:7870>.

::: details Run from a source checkout
You need Git, uv, Python 3.11+, Node 20, and npm 11+. Build the console before
starting the server:

```bash
git clone https://github.com/protoLabsAI/protoAgent.git
cd protoAgent
uv sync --frozen
npm ci
npm run build --workspace @protoagent/web
uv run python -m server
```

If you use nvm, `nvm use` selects the repository's Node version. Node 20 ships an
older npm; run `npm install -g npm@11` if `npm --version` is below 11.

On Windows, keep the checkout near the drive root, such as `C:\src\protoAgent`,
to avoid long dependency paths.
:::

## 2. Choose and name your agent

The setup wizard opens on a fresh instance. Click **Next**, choose **Basic** for
this walkthrough, and give the agent a name. Leave the starting persona as-is;
you can edit it later in **Settings → Identity**.

Other archetypes add a persona and tools for a particular job. Use
[Enable document creation](/guides/python-runtime) for Cowork's desktop
requirements, or [Build with a coding agent](/guides/build-with-a-coding-agent)
for Project Manager setup.

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

To add a CLI coding agent for coding jobs, configure a
[delegate](/guides/coding-agents) after setup.

## 4. Finish and chat

Review your choices and click **Finish**. Once the chat opens, send:

> What time is it in Tokyo?

You should see a `current_time` tool call followed by a reply. Then try:

> Find three recent articles about the A2A protocol and summarize them with links.

This exercises web search, URL fetching, and the model's tool loop. If the reply
fails, read the error in chat and check **Settings → Model → Connections**. If a
tool is unavailable, check **Settings → Tools** and the
[starter-tool reference](/reference/starter-tools).

## Find your configuration

Setup saves the model connection, name, and persona for this instance. To see the
actual paths and where settings came from, run:

```bash
protoagent config explain
# In a source checkout:
uv run python -m server config explain
```

A normal source or package install writes config to
`~/.protoagent/default/config/`, including `langgraph-config.yaml`, `secrets.yaml`,
and `SOUL.md`. Desktop and Docker use different roots; the
[configuration reference](/reference/configuration) explains the layout.

## Changing your mind

Use **Settings → Identity** for the name and persona, **Model → Connections** for
connections, and **Tools** or **Plugins** for capabilities. Adding a connection
does not switch the primary model; follow
[Connect and change models](/guides/model-connections) to select it and save.

## Next steps

- [Use the app](/guides/react-tauri-ui) — chats, settings, and progress.
- [Back up your data](/guides/backup-and-restore) — preserve chats and settings.
- [Fix a problem](/guides/troubleshooting) — recover from a failed connection or task.
- [Work with files and documents](/guides/documents-and-files) — attach files, create documents, and download them.
- [Add documents and media](/guides/ingestion) — give the agent material to recall.
- [Install a plugin](/guides/plugin-registry) — connect more tools and integrations.
- [Write your first skill](/tutorials/first-skill) — teach a reusable procedure.
