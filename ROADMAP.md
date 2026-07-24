# Roadmap

Where protoAgent is headed, kept honest and light. This is the source of truth for the
marketing site's `/roadmap` page — `scripts/roadmap.py build` parses it into
`sites/marketing/data/roadmap.json`. Group items under `## Planned`, `## In progress`, or
`## Shipped`; each bullet is a short **title** — one-line detail with an optional `(#issue)`
or `(vX.Y.Z)` reference. CI (`roadmap-staleness.yml`, #1945) fails when a Planned or
In-progress ref points at a closed issue — rotate shipped work into `## Shipped`.

## Planned

- **Signed Windows installer** — Authenticode-sign the desktop setup.exe — Windows leaves the notify-me gate when it ships. (#1689)
- **Multi-window desktop chat** — "Open in New Window" spawns a real second desktop window with its own chat surface. (#1706)
- **Federation token follow-ups** — management UI, peer rotation, and fleet integration for ADR 0066 tokens. (#1504)
- **Backend-agnostic tracing** — generic OTLP / OpenInference trace export instead of hard-coding the Langfuse SDK. (#1884)
- **Ollama & Hugging Face listings** — register protoAgent as an Ollama community integration and a Hugging Face "Use this model" local app. (#1990)

## In progress


## Shipped

- **Plugin Python deps in the desktop app** — opt-in install of a plugin's requires_pip packages inside the frozen desktop build: pure-Python wheels onto the host path, or into the managed Python runtime that also gives the desktop app a working execute_code. (v0.108.0)
- **Production traces → training flywheel** — per-turn trajectory export from production agents into the lab for downstream training-data collection. (#1897)
- **Autonomous operating model** — goals, tasks, scheduling, and watches compose into one self-directed OODA loop, with durable task→goal attribution. (v0.98.0)
- **Media platform** — plugin tools save generated images/audio/video the chat renders inline (signed URLs, bearer-safe), and can return images a vision model actually sees. (v0.98.0)
- **Fleet trace export** — opt-in per-turn trajectory rows (OpenAI chat format + verifiable reward) with a PII-redacting sync pipeline. (v0.97.0)
- **Migrate from Hermes or OpenClaw** — import scripts that carry an existing agent's config, memory, and history into protoAgent. (v0.96.0, v0.93.0)
- **Rewind a chat thread** — jump a conversation back to an earlier message and branch from there. (v0.90.0)
- **Plugin management from the rail** — installed version, "update if available", and uninstall — from the rail context menu and a settings panel. (v0.89.0)
- **One-command install** — a curl | sh bootstrap with an interactive CLI config wizard. (#1520)
