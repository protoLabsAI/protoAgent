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

## Remaining

| # | Gap | Section | Domain | Notes |
|---|---|---|---|---|
| 1 | **Author a managed MCP server** | Guide | Tools, MCP & plugins | Already covered at a basic level in `guides/mcp.md` (§ Plugin-managed servers, `register_mcp_server`). Only needs a fuller worked example if demand appears — low priority. |
| 2 | **Skills reference** (frontmatter/schema lookup) | Reference | Skills, subagents & workflows | Skills domain has guides + explanation but no Reference page. |
| 3 | **First-skill / Langfuse tutorials** | Tutorial | Skills / Operate | Tutorials are thin (2). Candidates: "Write your first skill", "Set up Langfuse tracing". |

## Stale doc needing a rewrite (not just a banner)

| # | Doc | Issue |
|---|---|---|
| A | `docs/guides/react-tauri-ui.md` | Titled "Migration"; it's the original Gradio→React plan. Banner added; still wants a rewrite into a current **operator-console how-to** (the new [Operator REST API](/reference/operator-api) reference now covers the endpoint map, so the rewrite can focus on console usage). |
