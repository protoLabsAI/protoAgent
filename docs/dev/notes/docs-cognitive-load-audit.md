# Documentation cognitive-load audit

2026-09-13. Scope: the repository README, documentation landing pages and navigation,
guide introductions, and closer review of onboarding, console use, plugins,
delegation, fleet, goals, skills, knowledge, and scheduling. The four main documentation
sections contain 89 Markdown files and approximately 149,000 whitespace-delimited
words, including their index pages and code examples. This was a sampled editorial
and source audit, not a line-by-line verification of that entire corpus or a live
usability test. Public documentation has not been rewritten in this pass.

The main problem is that readers must learn how protoAgent is built before they can
find out how to use it. Useful instructions are present, but architecture, historical
explanations, alternate paths, and implementation details compete with the next action.
Some instructions also contradict the current source or other guides.

## 1. Repair the first-run path before polishing its prose

**Priority: first.** A failed instruction creates more cognitive load than a long sentence.

| Evidence | User impact | Change |
|---|---|---|
| [First-agent tutorial](../../tutorials/first-agent.md), lines 14–34, tells readers to install Python dependencies and open the console. [README](../../../README.md), lines 59–80, repeats this. Neither builds the frontend. | A fresh source checkout lacks the promised console. | Include the frontend prerequisites and build, or route app users through a packaged installation. Keep a separate source-install path. |
| [Console guide](../../guides/react-tauri-ui.md), “Run it,” says the repository ships a prebuilt `dist/`. | Readers have no reason to suspect a missing build. | Correct that claim. [hatch_build.py](../../../hatch_build.py) explicitly treats `apps/web/dist` as absent until built; [server/__init__.py](../../../server/__init__.py), around line 1251, warns that `/app` will 404 without it. No files under `apps/web/dist` are tracked. |
| [First-agent tutorial](../../tutorials/first-agent.md), lines 23–33, advertises a Google extra and says `requirements.txt` installs it. | Users are offered an obsolete installation branch. | Remove it: [requirements.txt](../../../requirements.txt) contains `-e .`, and [pyproject.toml](../../../pyproject.toml) has no Google extra. Link to the integration installation guide where needed. |
| The tutorial requires an API key and describes only endpoint/key setup. | Its instructions do not cover the choices now visible in the wizard. | Describe the current connection choices. [SetupWizard.tsx](../../../apps/web/src/setup/SetupWizard.tsx), around lines 888–894, includes subscription sign-in choices; around lines 513–549, it writes a `providers` entry and qualified model name. |
| [Configuration reference](../../reference/configuration.md), opening and `model` table, describes a checkout-local live config and says the client only uses `openai`. [Fleet guide](../../guides/fleet.md), lines 38–41, teaches `PROTOAGENT_CONFIG_DIR` and config-based instance identity. | Users edit or look for the wrong files and learn obsolete configuration rules. | Align with [infra/paths.py](../../../infra/paths.py), particularly `InstancePaths.config_dir` and `_resolve_instance_paths`, and the current provider registry. Lead file-location advice with `protoagent config explain`; the ordinary default config is under `~/.protoagent/default/config/`. |
| [Coding-agent guide](../../guides/coding-agents.md), lines 33–34, says coding agents cannot be configured in Settings. [Delegates guide](../../guides/delegates.md), “Manage in the console,” offers a Coding agent form. | The reader cannot tell which setup route is supported. | Make the UI route canonical and retain YAML as an alternative. [DelegatesSection.tsx](../../../apps/web/src/settings/DelegatesSection.tsx) contains the ACP form. |

These are source-supported discrepancies. The replacement onboarding sequence should
still be walked through on a fresh installation before publication.

## 2. Make the front door describe the app and offer a next action

**Priority: first.** The [docs homepage](../../index.md) leads with “A lean,
A2A-native agent you own,” then LangGraph, git-URL plugins, protocol details,
`cost-v1`, trace metadata, and the release pipeline. Its explanatory section teaches
Diátaxis. Those details help developers evaluating the runtime, but offer little
help to someone trying to start a chat or work with a document.

The [README](../../../README.md) has approximately 1,082 words before its full
Quickstart section, although a short try-it link appears earlier. The feature table
mixes benefits, source locations, tool inventories, configuration caveats, and ADRs
inside individual cells.

Suggested homepage opening:

> Run AI agents on your own machine. Chat, work with documents, and connect tools.
> Add more agents when you need them.

Follow it with task links: **Get started**, **Use the app**, **Connect tools**,
**Run multiple agents**, and **Build plugins**. Keep the existing tutorial/reference/
explanation distinctions underneath; users need not learn the documentation taxonomy
to pick a task. Move protocol and release details to the developer overview.

## 3. Give first-time users one recommended route

**Priority: first.** The archetype selection step in the
[first-agent tutorial](../../tutorials/first-agent.md), line 51, is about 160 words.
It includes plugin installation, five Project Manager fields, coder registration,
merge policy, prerequisites in another guide, and persona editing. The next step
enumerates tools, middleware, database implementations, and storage fallbacks.

Suggested replacement for the archetype instruction:

> Name your agent and choose **Basic** for this walkthrough. Keep the default tools enabled.

Put Cowork and Project Manager setup in linked task guides. Structure the tutorial
around install → connect a model → choose Basic → launch → send one prompt → confirm
a reply. Move storage details to “Find your configuration.” Offer alternate installers
after the recommended path or behind clearly labelled alternatives.

The next steps should include using the app, adding a document, and connecting a tool.
Currently all three links lead to code authoring, deployment, or A2A capability metadata.
Also correct the [tutorial index](../../tutorials/index.md), which promises a “forked,
renamed container” for a tutorial that starts with a source clone.

## 4. Separate app instructions from implementation explanations

**Priority: next.** Several guides change audience within their opening sections:

| Page | What interrupts the user's task | Suggested structure |
|---|---|---|
| [Operator console](../../guides/react-tauri-ui.md) | React/Vite/Tauri packaging, source build, `EventSource`, API endpoints, Playwright advice, and release commands surround ordinary UI instructions. | A “Use the app” guide covering chat, settings, tools, and documents; move building and testing the console to developer documentation. |
| [Delegates](../../guides/delegates.md) | A programmatic plugin-dispatch example and session/policy internals precede the console instructions. | Add a delegate → test it → send work → interpret failure; move plugin dispatch to the SDK documentation. |
| [Managed Python](../../guides/python-runtime.md) | `sys.executable`, frozen binaries, CPython pins, and two ADRs precede installation. | Say what it enables and how to install it first; keep implementation and API details below. |
| [Schedule future work](../../guides/scheduler.md) | “When to read this,” tool signatures, startup helpers, and opt-out configuration precede console management. | Example request → confirm the schedule → edit/cancel → explain execution behavior. |

Retain details that change user decisions. For example, scheduled prompts need to be
self-contained, and a delegate's green health dot does not prove its last job succeeded.
These deserve short explanations beside the relevant controls.

## 5. Remove historical defenses and unsupported assurances

**Priority: next.** Recurring phrases include “the honest answer,” “the model is
simple,” “nothing is aspirational,” “complete and current by construction,” and
explanations of retired implementations. They ask the reader to accept a judgment
instead of supplying the fact needed to act.

| Current copy | Suggested copy |
|---|---|
| [Extend](../../guides/extend.md): “the honest answer to ‘how do I change it’ depends on what you're changing” | “Choose how to customize your agent:” |
| [Plugins](../../guides/plugins.md): reference is “complete and current by construction” | “The reference is generated from source signatures and docstrings.” |
| [Managed Python](../../guides/python-runtime.md): opening explanation of frozen `sys.executable` | “To create Word, Excel, PowerPoint, and PDF files in the desktop app, install the Python runtime from **Settings → Tools**.” |
| [Goal mode](../../guides/goal-mode.md): “a condition plus a verifier that ground-truths whether it's met” | “Give the agent a task and a completion check, such as passing tests. It keeps working until the check passes or a stopping limit is reached.” |

Move the retired `code_with` history in [Coding agents](../../guides/coding-agents.md),
the protocli comparison in [Goal mode](../../guides/goal-mode.md), and the Module
Federation history in [Building a plugin view](../../guides/building-react-plugin-views.md)
to migration notes or their existing ADRs. Keep migration guidance discoverable for
upgraders without putting it before new users' instructions.

## 6. Organize long guides around decisions and outcomes

**Priority: next.** Length alone is not the issue: a reference may need every field.
A how-to should let readers finish one task without studying the whole subsystem.

- [Plugins](../../guides/plugins.md): approximately 6,337 words. Its title suggests
  installation/use, but the body is an authoring manual. Rename it “Build plugins”
  and make “Install a plugin” a separate, direct navigation choice. Separate
  installation from publishing in the existing registry guide.
- [Rooms](../../guides/rooms.md): approximately 4,049 words. Keep the concrete
  `@name` opening; follow it with prerequisites, one/multiple addressees, ordinary
  follow-up behavior, and round limits. Move transcript storage and session-key
  mechanics to an explanation page.
- [Knowledge](../../guides/knowledge.md): approximately 3,277 words. It starts with
  FTS5, RRF, middleware, then a configuration block. Offer problem-oriented routes:
  “Add documents,” “Improve search results,” “Control what the agent remembers,”
  and “Inspect recalled information.” Keep tuning constants in reference material.
- [Command palette](../../guides/command-palette.md): approximately 2,844 words.
  Lead with the shortcut, click alternative, and a few useful searches. Separate
  the full command catalog and plugin-author registration instructions.

## 7. Make labels distinguish tasks before readers open a page

**Priority: next.** [Skills](../../guides/skills.md) and
[Add a custom skill](../../guides/add-a-skill.md) need warning boxes because the
same word refers to loaded instructions and advertised A2A capabilities. Rename
the latter “Advertise a capability to other agents.” Preserve existing URLs.

[Extend](../../guides/extend.md) tells users to fork for persona changes, while
the [console guide](../../guides/react-tauri-ui.md) describes editing the persona
in the app. Offer a decision table: edit behavior/persona in settings; teach a
procedure with a skill; connect existing tools through a plugin or MCP server;
write a plugin for new functionality; fork for core/product changes.

The site navigation's “Getting started” guides are “Fork the template” and
“Customize & deploy.” Move app setup and common app tasks ahead of those. Change
“Operator console (React/Tauri)” to “Use the app,” and “Managed Python runtime”
to a task label such as “Enable document creation.” Keep technical terms inside
the linked pages where they are useful.

## 8. Keep website and in-app help coherent

**Priority: follow-through.** [plugins/docs/corpus.py](../../../plugins/docs/corpus.py)
uses the same Markdown for in-app help and agent documentation tools. Editing it
improves both surfaces. Navigation is generated from
[the VitePress config](../../.vitepress/config.mts) into
[nav.json](../../../plugins/docs/nav.json); update the source and regenerate it.

The [plugin-view quickstart](../../how-to/build-a-plugin-view.md) lives outside
the five sections included by the in-app corpus, even though a public guide links
to it. The [Bundles guide](../../guides/bundles.md) exists and is linked from the
guide index but has no entry in the generated guide sidebar. Resolve these
discoverability gaps as part of the navigation pass. The omitted `plugin-views.md`
sidebar entry is a moved-page stub, not a missing primary guide.

## Delivery order and review criteria

1. **Repair onboarding:** fresh-install instructions, current wizard choices,
   configuration locations, delegate setup, and misleading cross-page claims.
2. **Rebuild entry pages:** product summary, recommended installation, ordinary
   app tasks, and explicit routes for developers and operators.
3. **Edit high-traffic guides:** action first, success condition second, relevant
   limits next, implementation elsewhere. Start with console, delegates, plugins,
   goals, and knowledge.
4. **Align navigation and maintenance:** regenerate in-app navigation, preserve
   URLs/anchors or redirects, and edit generated reference prose at its source.

For each rewritten task page, check whether a reader can identify the prerequisite,
first action, expected result, and recovery step without following another link.
Keep exact UI labels, commands, consequential limits, and useful examples. Remove
repeated benefits, obsolete implementation stories, and assurances without evidence.
Use bold for actions and choices rather than emphasizing most technical nouns.

The [ingestion guide](../../guides/ingestion.md), lines 11–16, already demonstrates
the desired pattern: **Knowledge → Store → Add source**, supported input, then
the expected “Added N chunks” confirmation. The rooms opening and skills guide's
five-step authoring sequence are other useful models to preserve.
