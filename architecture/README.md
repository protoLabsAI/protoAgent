# protoAgent architecture model

A [LikeC4](https://likec4.dev) model of protoAgent itself, written for the
[Atlas plugin](https://github.com/protoLabsAI/atlas-plugin): drill-down views from the system
context down to components, two step-by-step flows, and presenter tours. It covers the whole runtime
at the top levels and goes deep on the **plugin ecosystem and the plugin SDK**.

Every element links to the code that defines it (`link /src/...#Lnn`, project-root relative, so the
links open in the console's code pane). Where an ADR governs an element, it is in `metadata { adr }`.

| File | Holds |
|---|---|
| `likec4.config.json` | The project config (JSON only; Atlas reads no `.ts` config) |
| `spec.c4` | Element kinds and tags |
| `model.c4` | System context, the three subsystems, containers, core components |
| `plugins.c4` | The plugin ecosystem, what a plugin contributes, the plugin SDK, the console view bridge, first-party plugin repos |
| `views.c4` | Every view and the flows (`chat_turn_flow`, `plugin_lifecycle`, `sdk_tool_call`) |
| `atlas.tours.json` | Tours: `overview` and `plugins` |

**Views:** System context → protoAgent subsystems → Interfaces / Agent core / Platform services →
Console, Server, Agent graph, **Plugin ecosystem** → **What a plugin contributes**, **Plugin SDK**,
**Console to plugin view bridge**, First-party plugin repos.

![The plugin ecosystem view](screenshots/proto_agent_platform_plugins_detail.png)

## Viewing it

Install the Atlas plugin and open its rail view. Atlas reads `architecture/` from the project root by
default (its `model_dir` setting), so there's nothing to configure. Ask the agent to walk you through
it: Atlas gives the agent tools to move the view, highlight elements, step through a flow, and run a
tour (`tour="plugins"`).

Without a console, the Atlas repo's dev server renders the model:

```bash
cd atlas-plugin/web && npm ci && npm run build:view
node dev/server.mjs --model /path/to/protoAgent/architecture --project-root /path/to/protoAgent
# http://localhost:4720/plugins/atlas/view#view=plugin_lifecycle
```

## Validating it

```bash
node atlas-plugin/vendor/atlas-engine.mjs validate architecture   # {"ok":true,...}
```

That runs LikeC4's checks only. The agent's `atlas_validate` tool also runs Atlas's lint: no view
over 12 nodes, a scoped view for every parent, code links that resolve, no sequence-view nesting,
and sound tours. Keep it at `Engine: OK` / `Lint: clean`. The rules are in Atlas's
[authoring guide](https://github.com/protoLabsAI/atlas-plugin/blob/main/docs/authoring.md).

When code moves, the code links move with it: a renamed file fails `code-link-missing`. Line
anchors (`#Lnn`) aren't checked, so re-point them when you change the file they point into.
