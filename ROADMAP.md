# Roadmap

Where protoAgent is headed, kept honest and light. This is the source of truth for the
marketing site's `/roadmap` page — `scripts/roadmap.py build` parses it into
`sites/marketing/data/roadmap.json`. Group items under `## Planned`, `## In progress`, or
`## Shipped`; each bullet is a short **title** — one-line detail with an optional `(#issue)`
or `(vX.Y.Z)` reference.

## Planned

- **One-command install** — a `curl | sh` bootstrap with an interactive CLI config wizard. (#1520)
- **Migrate from Hermes** — a script that imports an existing Hermes agent into protoAgent. (#1515)
- **Migrate from OpenClaw** — a script that imports an existing OpenClaw agent into protoAgent. (#1514)
- **Federation token follow-ups** — management UI, peer rotation, and fleet integration for ADR 0066 tokens. (#1504)
- **Rewind a chat thread** — jump a conversation back to an earlier message and branch from there. (#1535)

## In progress

- **Plugin management from the rail** — uninstall a plugin from the rail context menu and a plugin-management settings panel. (#1522)
- **Plugin version + update** — show the installed version inline with an "update if available" action. (#1521)
- **Live Work panel** — reflect goal, task, and schedule changes as they happen, without a manual refresh. (#1537)

## Shipped

- **/compact** — summarize and archive a long chat thread in a single command. (v0.78.0)
- **Developer flags** — gate pre-release work behind local feature flags, with a Settings ▸ Developer panel. (v0.78.0)
- **Watches** — supervise many external conditions at once as a first-class primitive. (v0.78.0)
