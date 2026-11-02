Your local agent, handing real coding work to Claude Code and Codex.

A private, plugin-extensible desktop agent. It plans and remembers, and it gives the coding to the CLI agents you already use, over the Agent Client Protocol. Your chats, memory and tasks stay in SQLite on your disk. No analytics, tracking or telemetry — what it does call out to.

    

protoAgent hands a coding task to Claude Code. The diff pane fills in as the files change, and the tests pass.

Get it running

What you need: a Claude Pro/Max or ChatGPT Plus/Pro plan, an API key (OpenAI, OpenRouter, …), or a local model (Ollama / LM Studio). Or nothing, to just explore.

Download the desktop app for macOS, Windows, or Linux.

On first run, click Next on the welcome screen, connect and test a model (or choose Explore without a model), choose Basic and name your agent, then review your choices and click Finish. The first-agent tutorial covers installation, connection testing, and your first chat.

macOS builds are Apple Silicon only. Windows and Linux builds are unsigned; expect an OS warning on first launch.

Other ways to run it
Run the Python package

With uv installed, run the published package:

uvx --from protolabs-agent protoagent serve

Open http://localhost:7870 and follow the setup wizard. Port 7870 busy? Add --port 7871 and open http://localhost:7871 instead.

From source, step by step

You need Git, uv, Python 3.11+, Node 20, and npm 11+. A source checkout needs a console build; published desktop and Python packages include it.

git clone https://github.com/protoLabsAI/protoAgent.git
cd protoAgent
uv sync --frozen
npm ci
npm run build --workspace @protoagent/web
uv run python -m server

If you use nvm, nvm use selects Node 20. Run npm install -g npm@11 if npm --version is below 11. Open http://localhost:7870 and complete setup. If /app returns 404, check that apps/web/dist/index.html exists and rebuild.

For frontend development, use scripts/dev.sh for the backend on port 7871 and npm run dev --workspace @protoagent/web for the frontend. See Build and test the console and the repository instructions in PROTO.md.

The protoagent command

Install the CLI to manage an instance, including a running desktop hub:

uv tool install protolabs-agent
protoagent --help
protoagent fleet

In a checkout, use uv run python -m server <subcommand>. The CLI guide covers lifecycle, plugins, workspaces, config, and the terminal fleet deck.

One-command install (Docker)

With Docker running:

curl -fsSL https://raw.githubusercontent.com/protoLabsAI/protoAgent/main/scripts/install.sh | sh

The installer starts the published image, preserves its data volume on updates, and offers a setup wizard. See Deploy with Docker.

Run headless

After configuring the model, serve the OpenAI-compatible API and A2A without a console:

protoagent serve --ui none
curl http://localhost:7870/.well-known/agent-card.json

See Run headless for setup, API requests, and authentication.

Watch it
Your agent drives Claude Code / Codex	An autonomous dev team	A private desktop agent
		
delegate_to hands a coding task to a CLI coding agent over ACP, streams its work into your chat, and brings the result back. Spawn CLI coding agents →	The Project Manager archetype runs the pipeline: brief → board card → disposable worktree → pull request → CI gates → merge. Build with a coding agent →	Runs on your machine. Plugins installed from any git URL add tools and console views. Download →
Repository layout

Runtime Python code lives in src/, with unchanged import names such as graph and server. apps/ contains the web console and desktop shell; plugins/ contains bundled plugins. Tests, development scripts, documentation and deployment files live in tests/, scripts/, docs/ and deploy/. uv sync installs the application editably, so each checkout's CLI uses its own source and assets.

What you can do
Task	Guide
Chat, change settings, and inspect work	Use the app
Preserve or recover chats and settings	Back up and restore
Resolve a failed connection or task	Troubleshooting
Create Word, Excel, PowerPoint, and PDF files on desktop	Enable document creation
Hand coding work to Claude Code, Codex, or another CLI agent	CLI coding agents
Run a board-driven development team	Build with a coding agent
Add documents the agent can recall	Ingest documents and media
Run several agents in one console	Fleet
Teach a reusable procedure	Skills

Watch a coding agent work: its plan, narration, every tool call, and what each turn cost.

Chats, memory, knowledge, and tasks stay on your disk. Model requests go to the provider you choose; tools and integrations can make their own network calls. Open the command palette with ⌘⇧K / Ctrl-Shift-K to jump to a surface or setting.

Tracing is optional. See Network egress.

To locate the active config and stores, run protoagent config explain. A normal source or package install uses ~/.protoagent/default/; desktop and Docker have their own roots. See Configuration.

Plugins

The agent walks through its own plugin system in Atlas, with the code that does each step shown below.

Install tools, integrations, skills, and console views from a git URL:

protoagent plugin install https://github.com/you/your-plugin

Plugins run with the server's privileges. Review their code before enabling them. The bundled agent_browser plugin opens websites, fills forms, and captures screenshots or PDFs. Follow Browser automation to set up its browser runtime.

Browse the plugin directory, then use Install and manage plugins. To build your own, start with Extend protoAgent or the first-plugin tutorial.

Architecture

A FastAPI server exposes the operator API and A2A. A LangGraph runtime owns the model and tool loop; plugins add capabilities, and delegates hand work to other agents over A2A, OpenAI-compatible HTTP, or ACP.

See Architecture, Starter tools, and A2A extensions for runtime details.

Build your own agent

Change the persona in Settings → Identity and add skills or plugins to customize a running agent. To change the core or ship your own product, use the GitHub template and follow Customize and deploy. protoLabsAI/roxy is an example fork.

Release pipeline

The optional pipeline builds Docker images on main. To release, dispatch prepare-release.yml, merge its version-bump PR, and push the version tag. Desktop and Python publishing have separate steps. Follow the release guide when enabling or running the pipeline.

Contributing

Read CONTRIBUTING.md for issues and pull requests, and PROTO.md for development rules and required checks.

License

MIT. Bundled first-party plugins also carry their own licenses.