# Docs gaps — tracked follow-ups

Internal (not published; `docs/dev/**` is `srcExclude`d). Captured during the
Diátaxis→domain reorg pass; updated as gaps are filled. Each item: what's missing,
target Diátaxis section, and target domain (from the 9-domain taxonomy).

## Done (filled in the gap-fill pass)

| Gap | Page shipped |
|---|---|
| Ingestion pipeline | `docs/guides/ingestion.md` |
| Knowledge & memory how-to (RAG tuning) | `docs/guides/knowledge.md` |
| Command palette (⌘⇧K) — was shipped, not just proposed | `docs/guides/command-palette.md` |
| Mid-turn steering | `docs/explanation/steering.md` |
| Operator REST API reference | `docs/reference/operator-api.md` |
| Skill progressive disclosure (`<available_skills>` index + `load_skill`, ADR 0060) | `docs/guides/skills.md` |
| Operator-console rewrite (was the Gradio→React migration plan) | `docs/guides/react-tauri-ui.md` |
| Skills reference (frontmatter/schema lookup) | `docs/reference/skills.md` |
| "Write your first skill" tutorial | `docs/tutorials/first-skill.md` |
| Managed-MCP-server worked example | `docs/guides/mcp.md` (§ Plugin-managed servers) |

## Filled 2026-08 (the plugin-docs pass, #3057)

The Diátaxis audit that produced this file grouped plugins under "Tools, MCP & plugins" and
found no gap — because it checked whether pages *existed*, not whether the tiers did. Plugins
had five guides and **zero reference pages, no tutorial, and no explanation page**, while the
architecture story sat in 83 ADRs. That is the largest gap this file ever missed, so the
lesson is recorded with it: audit by tier per domain, not by page count.

| Gap | Page shipped |
|---|---|
| Plugin manifest schema | `docs/reference/plugin-manifest.md` (generated) |
| `register(registry)` surface | `docs/reference/plugin-registry-api.md` (generated) |
| `graph.sdk` | `docs/reference/plugin-sdk-api.md` (generated) |
| Plugin testkit | `docs/reference/plugin-testkit.md` (generated) |
| Plugin CLI | `docs/reference/plugin-cli.md` (generated) |
| View-bridge wire protocol | `docs/reference/plugin-view-bridge.md` |
| Event bus topic catalog | `docs/reference/plugin-events.md` (generated) |
| "Build your first plugin" tutorial | `docs/tutorials/first-plugin.md` |
| Plugin architecture explanation | `docs/explanation/plugin-architecture.md` |
| No author-facing entry point | `docs/guides/extend.md` + a top-level **Extend** nav entry |

Seven of those regenerate from the source (`scripts/gen_plugin_api.py`) and
`tests/test_plugin_api_reference.py` + `tests/test_plugin_view_bridge_docs.py` fail CI when
any of them drifts — so this particular kind of rot is now a build error rather than a
follow-up in this file.

## Documentation audit — 2026-10-09

Reviewed the main entry points, tutorials, guide navigation, and operational
references against the current implementation. Changes focused on helping a
reader complete a task: prerequisites, commands, expected results, and recovery.

| Finding | Change |
| --- | --- |
| Source setup used an unavailable CLI and omitted the console build | Corrected README, first-agent, and development commands; added `guides/build-console.md` |
| First-run docs described retired wizard controls | Documented the current archetype, identity, connection test, and finish flow |
| Console usage mixed user tasks with frontend implementation | Separated `guides/react-tauri-ui.md` from console build and test instructions |
| First-tool tutorial required core edits | Replaced it with a scaffolded plugin and executable examples |
| Skill paths and loading behavior contradicted the runtime | Corrected disk roots and on-demand loading in guides and references |
| Plugin-view quickstart was outside the indexed corpus | Moved it into guides, added navigation, and preserved the old page's links |
| Container examples persisted the wrong config directory | Standardized the bundled image examples on `/sandbox` persistence |
| Instance, scheduler, and store references described obsolete scoping | Documented environment-based roots, private stores, and explicit overrides |
| Goals, delegates, and scheduling led with internals | Put operator procedures and results first; corrected goal verifier creation paths |
| Introductions repeated taxonomy, incidents, or implementation history | Cut repetition from README, PROTO, landing pages, and affected guides |
| Rendered section links did not match heading IDs | Added stable anchors for tool references and affected guide sections |

Validation: documentation tests and lint gate; docs and console builds; generated
API/nav checks; rendered local section-link checks; a fresh isolated source server;
and the first-tool and first-plugin tutorials copied into fresh scaffolds. Model-backed chat,
native desktop packaging, and a live Docker deployment were not exercised.

## App-user follow-up — 2026-10-09

Audience: people using the app. Prioritize task completion and recovery before
runtime or plugin-author internals.

| Gap | Status |
| --- | --- |
| No direct route to preserving chats, credentials, and settings | Added `guides/backup-and-restore.md`, with desktop locations, stopped-copy steps, restore verification, and rollback |
| Existing backup instructions copied live Windows data or used container paths on the host | Corrected Windows recovery and deployment guidance; documented named-volume backup and restore |
| Failure guidance scattered across technical pages | Added `guides/troubleshooting.md`, routing from symptoms to connection, runtime, plugin, file-access, and data-location checks |
| Plugin installation guide required reading publishing internals | Rewrote `guides/plugin-registry.md` around app controls; moved author material to `guides/publish-a-plugin.md` and preserved old section links |
| Snapshot versus full backup remained hard to discover | Linked both from app tasks, added export/import procedures, and removed a duplicated knowledge section |
| Rooms, knowledge, and palette still mix routine use with internals | Rewrote their task guides, added `guides/manage-memory.md`, and moved contracts to `explanation/rooms.md`, `reference/knowledge.md`, and `reference/command-palette.md`; retained old section URLs |
| Phone access assumed a fixed port and hid the generated token | Rewrote `guides/phone-access.md` around the app's pairing controls, displayed addresses, retained tokens, and recovery; corrected pairing labels and CLI examples |
| Connecting a model and selecting it were conflated | Added `guides/model-connections.md`: connection setup, subscription flows, list tests versus completion tests, primary models, favorites, per-chat overrides, and recovery |
| Runtime setup did not explain the document workflow | Added `guides/documents-and-files.md`: attachments, server work folders, generation, downloads, revisions, and failure recovery |
| Snapshot docs overstated memory and credential exclusions | Rewrote the app guide, added `reference/agent-snapshots.md`, and shortened the duplicated CLI material; documented exported learned facts, recognized-secret limits, and unreported knowledge omissions |
| Console guide still exposed implementation details in routine tasks | Simplified file previews and editor controls; added conversation export and document tasks |
| Packaged execution and stopped-copy recovery lacked evidence | Exercised the installed macOS sidecar in temporary data, including setup, managed runtime provisioning, real document tools and downloads, stopped backup/restore, and a new post-restore turn |

Validation: 199 documentation and memory/room behavior tests; 39 browser checks
against the mock backend; production docs build; synchronized in-app navigation;
12,140 rendered local links, including 2,999 section links, with no missing
targets. All 31 prior section URLs on the rewritten task guides still resolve. Exercised the Docker archive/restore commands on temporary
volumes with a SQLite conversation fixture, config, credentials, and hidden files;
verified integrity and restarted a fixture service on the restored volume. This
checks the backup procedure, not a full protoAgent Docker deployment or a native
app restore. Temporary containers and volumes were removed.

## Final verification pass — 2026-10-09

The editorial and source discrepancies tracked above are addressed. The new
model, document, and snapshot pages appear in the site and in-app navigation.
Incognito guidance now states that explicit attachments still reach the server.
The artifact plugin README no longer directs users to a retired installation tab.

- 511 focused Python tests passed; two were skipped. These cover documentation,
  provider/OAuth routes, snapshots, runtime management, Cowork, and artifacts.
- A further 187 pairing and documentation tests passed after correcting the
  phone-access instructions.
- 65 additional Chromium checks passed against the mock backend: model controls,
  settings, snapshot review/import, runtime controls, artifact selection, and pairing.
  These supplement the earlier 39 setup and memory checks.
- The installed macOS **0.199.0** frozen server booted with isolated roots and
  discovery disabled. A local deterministic model fixture completed the wizard
  connection test and drove the real agent graph through `execute_code` and
  `save_file_artifact`. Managed Python and the document baseline were installed
  from their pinned downloads. Word, Excel, PowerPoint, and PDF files had valid
  container formats and downloaded through the artifact routes.
- A stopped whole-folder backup of that temporary instance restored intact SQLite
  stores, the persisted chat, credentials, artifacts, and setup state. The restored
  server answered a new turn. The native desktop window was not launched.
- A full Docker application recovery also passed with the freshly pulled amd64
  image, revision `89085636a15d882da777a426bb07ff6a0557ff73` (image digest
  `sha256:7174b3f4c5a608f550d25d740d8db0f2bc9e3c6e31923b3d1ab3f744aa8065d0`).
  The app completed setup and a persisted chat against the local model fixture.
  Its stopped-volume archive restored into a new volume with intact databases,
  credentials, setup state, and chat; the restored app answered a new turn.
  Temporary containers and volumes were removed.
- The production docs build, lint gate, generated API/nav checks, and diff
  whitespace check passed. All 12,519 rendered local links and 3,013 section
  links resolved across 229 pages; all 71 checked prior section URLs remain.
- A synthetic knowledge-store export confirmed that `fact` entries can travel
  while designated memory domains and always-on entries are excluded.

### Remaining release verification

These require accounts or platforms outside this workspace. They are validation
follow-ups, rather than missing instructions. Do not treat source review,
Chromium fixtures, or the frozen server check as proof of these native flows.

| Check | Evidence still needed |
| --- | --- |
| Real model accounts | Complete each supported subscription sign-in in the app, test the selected model, send a tool-using chat, and verify reconnect after expiry; repeat with a real hosted gateway |
| Native macOS window | From a fresh app profile, finish setup, install the runtime, create and download a document, export a chat, quit from the tray, and verify stopped-folder restore through the window |
| Windows and Linux desktop | Repeat fresh setup, runtime provisioning, downloads, tray quit, and same-platform restore on supported builds |

Cross-platform raw data moves remain explicitly unvalidated in the backup guide.
Prefer definition snapshots when moving to a new platform.

## Ongoing checks

- Keep commands, wizard labels, store paths, and defaults aligned with source.
- Check rendered section links after changing headings; a successful docs build
  does not catch missing fragments.
- Langfuse already has a step-by-step guide in `guides/observability.md`; avoid a
  duplicate tutorial unless it serves a distinct learning goal.
- Historical ADRs remain decision records. Put current procedures in guides and
  current contracts in reference pages.
