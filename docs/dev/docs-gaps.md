# Docs gaps — tracked follow-ups

Internal (not published; `docs/dev/**` is `srcExclude`d). Captured during the
Diátaxis→domain reorg pass. Each item: what's missing, target Diátaxis section, and
target domain (from the 9-domain taxonomy used across the sidebars).

## Missing pages (shipped code, no user doc)

| # | Gap | Section | Domain | Notes |
|---|---|---|---|---|
| 1 | **Ingestion pipeline** — `POST /api/knowledge/ingest`, "Add source" UI, txt/md/html/pdf/web/YouTube + audio/video STT | Guide | Knowledge & memory | `ingestion/` pkg + `KnowledgeIngestMiddleware`; only mentioned in passing |
| 2 | **Command palette (⌘K)** | Guide | Console & UI | ADR 0057; only a passing mention in `react-tauri-ui.md` |
| 3 | **Mid-turn steering** — submit a message while a turn runs; folds in at next model call | Explanation | Agent core & runtime | shipped (steering middleware); undocumented for users |
| 4 | **Operator REST API** (`/api/*`) reference | Reference | Console & UI | `operator_api/` has no endpoint reference; only A2A/metrics documented |
| 5 | **Knowledge & memory how-to** | Guide | Knowledge & memory | domain has explanation but **no guide** — e.g. "wire a knowledge store / RAG tuning" |
| 6 | **Author a managed MCP server** | Guide | Tools, MCP & plugins | `mcp_servers/` (e.g. Google) — no authoring guide |
| 7 | **Skills loaded chip / `skills.announce`** | (done) | Skills | documented in `guides/skills.md` already — no action |

## Stale doc needing a rewrite (not just a banner)

| # | Doc | Issue |
|---|---|---|
| A | `docs/guides/react-tauri-ui.md` | Titled "Migration"; it's the original Gradio→React plan. Banner added this pass; needs a rewrite into a current **operator-console how-to** (REST map + console usage), dropping the "keep Gradio for now" slices. |

## Coverage gaps by domain (which Diátaxis cells are empty)

- **Knowledge & memory** — has Explanation, but **no Tutorial / Guide / Reference**.
- **Console & UI** — has Guides, but **no Reference** (operator REST API) and no Tutorial.
- **Skills, subagents & workflows** — no Reference page (frontmatter/schema lookup could be one).

Tutorials are thin (2). Candidate additions: "Write your first skill" (Skills), "Set up
Langfuse tracing" (Operate & deploy).
