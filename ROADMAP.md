# Roadmap

Where protoAgent is headed, kept honest and light. This is the source of truth for the
marketing site's `/roadmap` page — `scripts/roadmap.py build` parses it into
`sites/marketing/data/roadmap.json`. Group items under `## Planned`, `## In progress`, or
`## Shipped`; each bullet is a short **title** — one-line detail with an optional `(#issue)`
or `(vX.Y.Z)` reference. CI (`roadmap-staleness.yml`, #1945) fails when a Planned or
In-progress ref points at a closed issue — rotate shipped work into `## Shipped`.

## Planned

- **Remote device improvements** — a first-class phone and tablet experience: the mobile console, pairing a device with a code, and reaching your agent from anywhere without fiddling with tokens.
- **Settings, reorganized** — one place for every setting: each field reachable from a real panel (not only a shortcut chip), grouped by what you're trying to do, with clearer wording and safer saves.
- **A smarter command palette** — jump to anything and run any action from one box: better ranking across sessions, settings, plugins and commands, and consistent behavior on desktop and mobile.

## In progress

- **Ollama & Hugging Face listings** — protoAgent as an Ollama community integration and a Hugging Face "Use this model" local app; the listing PRs are open upstream, awaiting maintainer review.

## Shipped

- **Interactive answers** — tables, charts and artifacts render inline in the transcript, with previews that stream in while the agent writes them. (v0.201.0)
- **Real documents in the Artifact panel** — PDF, Word, slides, spreadsheets, images and video render as themselves, not as text dumps. (v0.191.0, v0.196.0, v0.207.0)
- **Trajectory reference log and read API** — recorded message identities/hashes, paged events, per-call reconstruction with explicit missing-content status, and the `/trajectory` command. Search, full-text capture and derived-view compaction are deferred. (#2806)
- **Model in-flight limiter** — per-lane caps on concurrent model calls, with priority classes and queue telemetry. (v0.185.0)
- **Pair agents with a code** — a fleet hub pairs with a remote protoAgent from a one-time code instead of a pasted token. (v0.184.1)
- **Use protoAgent from Zed** — talk to your agent in Zed's Agent Panel over ACP, and file paths in tool results open in your editor. (v0.179.0, v0.176.0)
- **Code pane** — a read-only file and diff viewer beside chat that follows a delegate's edits live. (v0.178.0, v0.190.0)
- **Theme gallery** — 28 theme families, each with a dark and a light variant. (v0.169.0)
- **The fleet deck** — `protoagent fleet` talks to, acts on, and manages every fleet member and hub on the box from the terminal. (v0.166.0)
- **Delegation ledger** — a durable record of who handed what work to whom and what it cost, drawn on the org chart. (v0.161.0, v0.162.0)
- **Federation token follow-ups** — management UI, peer rotation, and fleet integration for ADR 0066 tokens. (v0.160.0)
- **Multi-window desktop chat** — "Open in New Window" spawns a real second desktop window with its own chat surface. (v0.117.0)
- **Plugin Python deps in the desktop app** — opt-in install of a plugin's requires_pip packages inside the frozen desktop build: pure-Python wheels onto the host path, or into the managed Python runtime that also gives the desktop app a working execute_code. (v0.108.0)
- **Production traces → training flywheel** — per-turn trajectory export from production agents into the lab for downstream training-data collection. (#1897)
- **Autonomous operating model** — goals, tasks, scheduling, and watches compose into one self-directed OODA loop, with durable task→goal attribution. (v0.98.0)
- **Media platform** — plugin tools save generated images/audio/video the chat renders inline (signed URLs, bearer-safe), and can return images a vision model actually sees. (v0.98.0)
- **Fleet trace export** — opt-in per-turn trajectory rows (OpenAI chat format + verifiable reward) with a PII-redacting sync pipeline. (v0.97.0)
- **Migrate from Hermes or OpenClaw** — import scripts that carry an existing agent's config, memory, and history into protoAgent. (v0.96.0, v0.93.0)
- **Rewind a chat thread** — jump a conversation back to an earlier message and branch from there. (v0.90.0)
- **Plugin management from the rail** — installed version, "update if available", and uninstall — from the rail context menu and a settings panel. (v0.89.0)
- **One-command install** — a curl | sh bootstrap with an interactive CLI config wizard. (#1520)
