<p align="center">
  <img src="docs/public/social-preview.png" alt="protoAgent" width="640">
</p>

<h3 align="center">Your local agent, handing real coding work to Claude Code and Codex.</h3>

<p align="center">
  A private, plugin-extensible desktop agent. It plans and remembers, and it gives the coding
  to the CLI agents you already use, over the Agent Client Protocol. Your chats, memory and
  tasks stay in SQLite on your disk. No analytics, tracking or telemetry — <a href="./docs/explanation/network-egress.md">what it does call out to</a>.
</p>

<p align="center">
  <a href="https://agent.protolabs.studio/download"><img src="https://img.shields.io/badge/download-macOS%20%C2%B7%20Windows%20%C2%B7%20Linux-9b87f2" alt="Download for macOS, Windows, Linux"></a>
  <a href="https://pypi.org/project/protolabs-agent/"><img src="https://img.shields.io/pypi/v/protolabs-agent?label=PyPI" alt="PyPI: protolabs-agent"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://agent.protolabs.studio/docs/"><img src="https://img.shields.io/badge/docs-agent.protolabs.studio-9b87f2" alt="Docs"></a>
  <a href="https://github.com/protoLabsAI/protoAgent/actions/workflows/checks.yml"><img src="https://github.com/protoLabsAI/protoAgent/actions/workflows/checks.yml/badge.svg" alt="Checks"></a>
</p>

<p align="center">
  <img src="docs/public/readme/hero-code-pane.gif" alt="protoAgent console: a chat tab sends a task to Claude Code, which plans, edits calc.py and test_calc.py, and runs pytest (3 passed) while the Diff pane beside it fills in with the changes" width="800">
</p>

<p align="center"><em>protoAgent hands a coding task to Claude Code. The diff pane fills in as the files change, and the tests pass.</em></p>

## Get it running

[Download the desktop app](https://agent.protolabs.studio/download) for macOS,
Windows, or Linux, then follow the setup wizard.

With [uv](https://docs.astral.sh/uv/) installed, you can also run the published package:

```bash
uvx --from protolabs-agent protoagent serve
```

Open <http://localhost:7870>. Connect a model endpoint or sign in to a supported
Claude or ChatGPT subscription, choose **Basic**, name your agent, and finish setup.
The [first-agent tutorial](./docs/tutorials/first-agent.md) covers installation,
connection testing, and your first chat.

## Watch it

| Your agent drives Claude Code / Codex | An autonomous dev team | A private desktop agent |
| --- | --- | --- |
| <a href="docs/public/readme/hero-code-pane.gif"><img src="docs/public/readme/hero-code-pane-poster.png" alt="Claude Code's finished run in a protoAgent chat tab: todo list done, pytest 3 passed, and the Diff pane showing the changes to calc.py and test_calc.py" width="280"></a> | <img src="docs/public/readme/board-builds.gif" alt="Project board: a card in In Review with auto-merge pending moves to Done with its PR link, and a backlog card whose dependencies closed is flagged to promote" width="280"> | <img src="docs/public/readme/install-plugin.gif" alt="Settings, Plugins: the terminal plugin is installed from its git URL, a Terminal icon appears in the rail with no restart, and the shell runs a command" width="280"> |
| `delegate_to` hands a coding task to a CLI coding agent over ACP, streams its work into your chat, and brings the result back. [Spawn CLI coding agents →](./docs/guides/coding-agents.md) | The **Project Manager** archetype runs the pipeline: brief → board card → disposable worktree → pull request → CI gates → merge. [Build with a coding agent →](./docs/guides/build-with-a-coding-agent.md) | Runs on your machine. Plugins installed from any git URL add tools and console views. [Download →](https://agent.protolabs.studio/download) |

## From source, step by step

You need Git, uv, Python 3.11+, Node 20, and npm 11+. A source checkout needs a
console build; published desktop and Python packages include it.

```bash
git clone https://github.com/protoLabsAI/protoAgent.git
cd protoAgent
uv sync --frozen
npm ci
npm run build --workspace @protoagent/web
uv run python -m server
```

If you use nvm, `nvm use` selects Node 20. Run `npm install -g npm@11` if
`npm --version` is below 11. Open <http://localhost:7870> and complete setup.
If `/app` returns 404, check that `apps/web/dist/index.html` exists and rebuild.

For frontend development, use `scripts/dev.sh` for the backend on port 7871 and
`npm run dev --workspace @protoagent/web` for the frontend. See
[Build and test the console](./docs/guides/build-console.md) and the repository
instructions in [PROTO.md](./PROTO.md).

## What you can do

| Task | Guide |
| --- | --- |
| Chat, change settings, and inspect work | [Use the app](./docs/guides/react-tauri-ui.md) |
| Preserve or recover chats and settings | [Back up and restore](./docs/guides/backup-and-restore.md) |
| Resolve a failed connection or task | [Troubleshooting](./docs/guides/troubleshooting.md) |
| Create Word, Excel, PowerPoint, and PDF files on desktop | [Enable document creation](./docs/guides/python-runtime.md) |
| Hand coding work to Claude Code, Codex, or another CLI agent | [CLI coding agents](./docs/guides/coding-agents.md) |
| Run a board-driven development team | [Build with a coding agent](./docs/guides/build-with-a-coding-agent.md) |
| Add documents the agent can recall | [Ingest documents and media](./docs/guides/ingestion.md) |
| Run several agents in one console | [Fleet](./docs/guides/fleet.md) |
| Teach a reusable procedure | [Skills](./docs/guides/skills.md) |

Chats, memory, knowledge, and tasks stay on your disk. Model requests go to the
provider you choose; tools and integrations can make their own network calls.
Open the command palette with **⌘⇧K** / **Ctrl-Shift-K** to jump to a surface or setting.

Tracing is optional. See [Network egress](./docs/explanation/network-egress.md).

To locate the active config and stores, run `protoagent config explain`. A normal
source or package install uses `~/.protoagent/default/`; desktop and Docker have
their own roots. See [Configuration](./docs/reference/configuration.md).

## The `protoagent` command

Install the CLI to manage an instance, including a running desktop hub:

```bash
uv tool install protolabs-agent
protoagent --help
protoagent fleet
```

In a checkout, use `uv run python -m server <subcommand>`. The
[CLI guide](./docs/guides/cli.md) covers lifecycle, plugins, workspaces, config,
and the terminal fleet deck.

## One-command install (Docker)

With Docker running:

```bash
curl -fsSL https://raw.githubusercontent.com/protoLabsAI/protoAgent/main/scripts/install.sh | sh
```

The installer starts the published image, preserves its data volume on updates,
and offers a setup wizard. See [Deploy with Docker](./docs/guides/deploy-docker.md).

## Run headless

After configuring the model, serve the OpenAI-compatible API and A2A without a console:

```bash
protoagent serve --ui none
curl http://localhost:7870/.well-known/agent-card.json
```

See [Run headless](./docs/guides/headless.md) for setup, API requests, and authentication.

## Plugins

Install tools, integrations, skills, and console views from a git URL:

```bash
protoagent plugin install https://github.com/you/your-plugin
```

Plugins run with the server's privileges. Review their code before enabling them.
Browse the [plugin directory](https://agent.protolabs.studio/plugins), then use
[Install and manage plugins](./docs/guides/plugin-registry.md). To build your own,
start with [Extend protoAgent](./docs/guides/extend.md) or the
[first-plugin tutorial](./docs/tutorials/first-plugin.md).

## Architecture

A FastAPI server exposes the operator API and A2A. A LangGraph runtime owns the
model and tool loop; plugins add capabilities, and delegates hand work to other
agents over A2A, OpenAI-compatible HTTP, or ACP.

See [Architecture](./docs/explanation/architecture.md),
[Starter tools](./docs/reference/starter-tools.md), and
[A2A extensions](./docs/reference/extensions.md) for runtime details.

## Build your own agent

Change the persona in **Settings → Identity** and add skills or plugins to
customize a running agent. To change the core or ship your own product, use the
[GitHub template](https://github.com/new?template_name=protoAgent&template_owner=protoLabsAI)
and follow [Customize and deploy](./docs/guides/customize-and-deploy.md).
[protoLabsAI/roxy](https://github.com/protoLabsAI/roxy) is an example fork.

### Release pipeline

The optional pipeline builds Docker images on `main`. To release, dispatch
`prepare-release.yml`, merge its version-bump PR, and push the version tag.
Desktop and Python publishing have separate steps. Follow the
[release guide](./docs/guides/releasing.md) when enabling or running the pipeline.

## Contributing

Read [CONTRIBUTING.md](./CONTRIBUTING.md) for issues and pull requests, and
[PROTO.md](./PROTO.md) for development rules and required checks.

## License

[MIT](./LICENSE). Bundled first-party plugins also carry their own licenses.
