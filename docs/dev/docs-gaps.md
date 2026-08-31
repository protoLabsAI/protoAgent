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

## Remaining

_All audit gaps are filled._

- Langfuse-tracing tutorial: **intentionally not written** — `guides/observability.md`
  already covers it step-by-step; a tutorial would duplicate it.
- The console guide's Layout section describes surfaces by behavior, not geometry (the IA is
  mid-evolution — utility bar + bottom panel landed in #1176/#1178; ADR 0056 "dockable
  views" is Proposed). Fine today; re-check that section if the dockable-view work lands.
