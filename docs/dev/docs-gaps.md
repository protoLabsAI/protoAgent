# Docs gaps — tracked follow-ups

Internal (not published; `docs/dev/**` is `srcExclude`d). Captured during the
Diátaxis→domain reorg pass; updated as gaps are filled. Each item: what's missing,
target Diátaxis section, and target domain (from the 9-domain taxonomy).

## Done (filled in the gap-fill pass)

| Gap | Page shipped |
|---|---|
| Ingestion pipeline | `docs/guides/ingestion.md` |
| Knowledge & memory how-to (RAG tuning) | `docs/guides/knowledge.md` |
| Command palette (⌘K) — was shipped, not just proposed | `docs/guides/command-palette.md` |
| Mid-turn steering | `docs/explanation/steering.md` |
| Operator REST API reference | `docs/reference/operator-api.md` |
| Skills loaded chip / `skills.announce` | already in `docs/guides/skills.md` |
| Operator-console rewrite (was the Gradio→React migration plan) | `docs/guides/react-tauri-ui.md` |
| Skills reference (frontmatter/schema lookup) | `docs/reference/skills.md` |
| "Write your first skill" tutorial | `docs/tutorials/first-skill.md` |
| Managed-MCP-server worked example | `docs/guides/mcp.md` (§ Plugin-managed servers) |

## Remaining

_All audit gaps are filled._

- Langfuse-tracing tutorial: **intentionally not written** — `guides/observability.md`
  already covers it step-by-step; a tutorial would duplicate it.
- The console guide's Layout section describes surfaces by behavior, not geometry (the IA is
  mid-evolution — utility bar + bottom panel landed in #1176/#1178; ADR 0056 "dockable
  views" is Proposed). Fine today; re-check that section if the dockable-view work lands.
