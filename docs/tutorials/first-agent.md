# Spin up your first agent

About 5 minutes. You need Python 3.11+ and an OpenAI-compatible API key (OpenAI direct, LiteLLM gateway, Anthropic-via-gateway, Ollama, anything that speaks the OpenAI REST shape).

No forking, no `sed`, no Docker for your first run. That's all in [Customize & deploy](/guides/customize-and-deploy) once you've decided this template works for you.

## 1. Get the code

```bash
git clone https://github.com/protoLabsAI/protoAgent.git my-agent
cd my-agent
```

## 2. Install dependencies & run

Dependencies live in `pyproject.toml` (the single source of truth), so both
modern `uv` and classic `pip` just work.

**uv (recommended)** — creates the venv, installs the core deps, runs the server:

```bash
uv sync && uv run python -m server          # core, serves the React console (--ui console)
# Add the Google surface with the extra:
#   uv sync --extra google && uv run python -m server
# Re-running and already synced? `uv run --no-sync python -m server`.
```

**pip** — `requirements.txt` installs the core + Google surface set:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt            # == pip install -e .[google]
python -m server
```

You should see:

```
LangGraph agent initialized (setup wizard not complete — graph not compiled. Open the UI to finish setup.)
Starting protoagent on http://0.0.0.0:7870
```

## 3. Open the setup wizard

Visit <http://localhost:7870> in a browser. Because `config/.setup-complete` doesn't exist yet, you'll land in the wizard instead of the chat UI.

Walk through the four steps:

1. **Connect to your model.** Paste your API base URL (`https://api.openai.com/v1` for OpenAI direct, `http://localhost:4000/v1` for a local LiteLLM gateway) and API key. Click **Test connection & fetch models** — the dropdown fills with whatever the endpoint actually exposes. Pick one.
2. **Name your agent.** Short lowercase slug (e.g. `product-director`). Pick a persona preset — **Generic Assistant** is the safe default; **Research** / **Coding** / **Blank** are the alternatives — and click **Load preset into SOUL.md**. Edit the loaded text if you want to make it specific to your agent.
3. **Tools & middleware.** A broad starter set is enabled by default — four keyless general (`current_time`, `calculator`, `web_search`, `fetch_url`), two HITL (`ask_human`, `request_user_input`), four GitHub read tools, four notes, five memory (`memory_ingest`, `memory_recall`, `memory_list`, `memory_stats`, `daily_log`), three scheduler, four tasks, plus inbox/peer when configured. (See [Starter tools](/reference/starter-tools) for the full list; drop any via `tools.disabled`.) Leave **Audit**, **Memory**, **Knowledge**, and **Scheduler** middleware on — the template ships a working sqlite + FTS5 store under `/sandbox/knowledge/agent.db` and a sqlite-backed scheduler under `/sandbox/scheduler/<agent_name>/jobs.db`, both with `~/.protoagent/...` fallbacks outside Docker.
4. **Optional — you, security, autostart.** Your name makes the agent address you directly. A2A auth token blank for local dev, set it before you expose the port. "Launch this agent automatically on login" installs a macOS LaunchAgent so the server is up after every reboot without remembering to `python -m server`.

Hit **Launch agent**. The wizard closes, the chat UI appears, and the Configuration drawer on the right is now populated with your choices.

## 4. Try it

In the chat box:

> What time is it in Tokyo?

The agent calls `current_time`, returns an ISO-8601 timestamp, and explains what it found.

Then:

> Find three recent articles about the A2A protocol and summarize them.

The agent calls `web_search`, then `fetch_url` on the top results, and hands back a synthesis. That round-trip exercises the full tool loop + LLM call + streaming response path.

## What just happened

- Your answers were written to `config/langgraph-config.yaml` (human-readable — peek at it).
- The persona preset was written to `config/SOUL.md`.
- A `config/.setup-complete` marker was created so the next boot goes straight to chat.
- The agent card at <http://localhost:7870/.well-known/agent-card.json> now reflects your agent name.
- If you checked autostart, `~/Library/LaunchAgents/ai.protolabs.<name>.plist` was installed and `launchctl load`-ed.

## Changing your mind

- **Any field** — open the Configuration drawer on the right side of the chat UI. Every wizard field is there, plus a few advanced ones (temperature, max_tokens, max_iterations, knowledge store settings).
- **The whole wizard** — expand the drawer's "Re-run setup wizard" accordion and click **Run wizard now**. Your current values pre-fill every step.
- **Autostart** — toggle it off in the wizard or the drawer; the LaunchAgent is removed and the plist file deleted.

## Where to go next

- [Write your first tool](/tutorials/first-tool) — wire a custom LangChain tool into the loop
- [Customize & deploy](/guides/customize-and-deploy) — fork the template, rename throughout, ship a GHCR image
- [Add a custom skill](/guides/add-a-skill) — expose the new behaviour on the A2A agent card
