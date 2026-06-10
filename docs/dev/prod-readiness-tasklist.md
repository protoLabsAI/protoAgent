# Prod-readiness task list

**Created:** 2026-06-09 · **Source:** full-app tech-debt audit (backend + frontend + cross-cutting) + DS migration + fork-cleanliness audit. **Triaged 1×1:** 2026-06-09.

Status legend: `[ ]` open · `[~]` in progress · `[x]` done · `[-]` won't-fix/defer.

---

## Sorted backlog (the result of the 1×1 triage)

**🟢 NOW — DONE (8):** `A1 A2 A3 A4 A5 A6 A7 G1`
Doc-truth pass (A1–A6) + prune the stale worktree (G1) **merged in #814**. **A7 was reversed:** rather than untrack `uv.lock`, the project *adopted uv* in **#811** — `pyproject [project.dependencies]` is now the dep source of truth, `uv.lock` is tracked + in sync, and `requirements-*.txt` are kept as readable references.

**🟡 NEXT SPRINT (14):** `E1 E2 · D1 D2 D3 D5 D6 · C1 C5 C6 · B1 B2 · F5 F1`
Suggested order (dependency-aware):
1. **E1** (vitest + pure-logic tests) — gates C3 & D1; do first.
2. **E2** (fleet proxy/discovery tests) — newest network code.
3. **D1** (useChatStream/updateAssistant — needs E1) → **D2 D3** (frontend structure) → **D5 D6**.
4. **C1** (_main split + security-gate extraction) → **C5** (retire peer_consult) → **C6** (except fixes).
5. **B1** (config-triplet → single source) **+ B2** (identity_name CI guard) — together; double-counts as fork hygiene.
6. **F5** (DS `Grid` — reference adoption) → **F1** (token sweep).

**⚪ BACKLOG (11 + 1 blocked):** `C2 C3 C4 · D4 · B3 · F2 F3 F4 F6 F7 · G2` · *(F4b blocked on DS ToolCard #187)*
`C3` gated on E1. `B3` is fork-repo work, after B1 ships the seam. DS phases F2/F3/F4 follow F1/F5.

---

## A. Metadata / docs that lie — 🔴 NOW (one doc PR)

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| A1 | Flip 9 ADR statuses Proposed→Accepted/Shipped + PR refs (esp. **0042 fleet**) | `docs/adr/0033,0034,0035,0042…:3` | S | ✅ Done |
| A2 | Resolve ADR **0040 collision** + rebuild `docs/adr/index.md` (add 0041/0042/0044) | `docs/adr/index.md` | S | ✅ Done |
| A3 | Purge stale `/active/*` docstrings (slug routing superseded, #806) | `graph/fleet/proxy.py:3`, `theme_routes.py:5`, `fleet_routes.py:79`, `server/__init__.py:629` | S | ✅ Done |
| A4 | Fix false `get_github_tools` docstring + audit "appended by get_all_tools" claims | `tools/github_tools.py:12` | S | ✅ Done |
| A5 | Delete dead `cn.ts` | `apps/web/src/lib/cn.ts` | XS | ✅ Done |
| A6 | Fix `src/ext` invariant comment vs shipped `workflows.tsx` | `apps/web/src/ext/index.ts:3` | XS | ✅ Done |
| A7 | ~~Untrack `uv.lock`~~ → **superseded by #811**: adopt uv properly — `pyproject [project.dependencies]` is dep source of truth, `uv.lock` tracked + in sync (`uv lock --check` clean), `requirements-*.txt` = readable references | `pyproject.toml`, `uv.lock` | S | ✅ Done (#811) |

## B. Config + fork hygiene

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| B1 | **Collapse the config triplet to one source of truth.** Fixes 3-file-sync hazard **and** the #1 fork conflict | `graph/config.py` + `config_io.py` + `settings_schema.py` | L | 🟡 Next (w/ B2) |
| B2 | CI guard: fail if `LangGraphConfig.identity_name` default ≠ `"protoagent"` | `graph/config.py:391` | S | 🟡 Next (w/ B1) |
| B3 | Fork-side: gina `briefing_*`→ADR-0019 plugin; both forks `identity_name`→YAML | gina/protoTrader | M | ⚪ Backlog (after B1) |

## C. Backend structure

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| C1 | Break up `_main()` (534 L); extract host-binding **security gate** into a testable fn | `server/__init__.py:274,794` | M | 🟡 Next |
| C2 | Decompose `agent_init.py` (1387 L); fix 67 in-fn imports/load order | `server/agent_init.py` | L | ⚪ Backlog |
| C3 | Factor `_chat_langgraph_stream` (301 L) + `_run_turn_stream` | `server/chat.py:435,152` | M | ⚪ Backlog (gate on E1) |
| C4 | Type `AppState`; funnel 49 mutation sites through owned methods | `runtime/state.py:16` | M | ⚪ Backlog |
| C5 | Retire deprecated `peer_consult` from core toolset | `tools/peer_tools.py:97`, `lg_tools.py:750` | S | 🟡 Next |
| C6 | Fix `CancelledError`-swallowing excepts + annotate 3 unexplained `except: pass` | `cache_warmer.py:149`, `execute_code.py:182`, `server/__init__.py:572`, `a2a.py:242`, `shell.py:81` | S | 🟡 Next |

## D. Frontend structure

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| D1 | Extract `useChatStream(sessionId)` + `store.updateAssistant()` — kill 6× dup | `chat/ChatSurface.tsx:209-690` | M | 🟡 Next (after E1) |
| D2 | Migrate 4 hand-rolled surfaces to react-query | `ActivitySurface`, `PlaybooksSurface`, `KnowledgeStore`, `SetupWizard` | M | 🟡 Next |
| D3 | Decompose `App.tsx` (827 L) → `useRailNav` + icon module; fix 2 `exhaustive-deps` (kill `pluginViewSig` hack) | `app/App.tsx:574,619,568` | M | 🟡 Next |
| D4 | Refactor `SetupWizard` (721 L) to step-config/reducer | `setup/SetupWizard.tsx` | M | ⚪ Backlog |
| D5 | Shared `ApiResult<T>` type + typed event payloads | `lib/api.ts`, `lib/events.ts:10` | S | 🟡 Next |
| D6 | Fix index-as-key in reorderable lists | `WorkflowBuilder.tsx:114`, `tool-renderers.tsx`, `HitlForm.tsx:171` | XS | 🟡 Next |

## E. Tests

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| E1 | Add **vitest**; unit-cover chat-store (`ensureActiveSessions`), `api.ts` SSE parser, `uiStore.migrate` | `apps/web` | M | 🟡 Next (first) |
| E2 | Direct tests for `graph/fleet/proxy.py` + `discovery.py` | newest network code | S | 🟡 Next |

## F. Design-system migration

> **`@protolabsai/ui@0.21.0` shipped 3 of the 4 filed gaps:** #184 `Grid`, #186 `TabBar`, #188 `Empty` — **closed**. #187 `ToolCard` **held for design** (crew answering: do activity/inbox rows share the chat-tool shape; generic vs chat-specific of the 52 classes; status set-once vs live-streaming).

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| F5 | **Adopt DS `Grid` (#184)** → delete `.archetype-grid`/`.subagent-grid`/`.metric-grid` (bump →0.21) | 3 sites | S | 🟡 Next (reference) |
| F1 | DS Phase 1 — token sweep (`muted`/`empty`/`spin`/status → DS) | `theme.css` | M | 🟡 Next |
| F6 | Adopt DS `TabBar` (#186) → retire `.chat-tab*` | `chat/ChatSurface` | M | ⚪ Backlog |
| F7 | Adopt DS `Empty` slots (#188) → retire `.empty-note`/`.fleet-empty` | scattered | S | ⚪ Backlog |
| F2 | DS Phase 2 — forms (`field`/`setting-input` → DS `forms`) | 9 files | M | ⚪ Backlog |
| F3 | DS Phase 3 — panel/card backbone (`.panel`/`.stage-panel` → `Card`+`PanelHeader`) | 24 files | L | ⚪ Backlog |
| F4 | DS Phase 4 — chat renderers (`.markdown-*` prose may stay app-side) | chat | L | ⚪ Backlog |
| F4b | `.tool-*` adoption once DS `ToolCard` lands | chat | L | 🚧 Blocked (#187) |

## G. Cleanup

| ID | Task | Evidence | Effort | Disp. |
|----|------|----------|:------:|:-----:|
| G1 | Prune stale 269 MB ignored worktree | `.claude/worktrees/fleet-ci-fix` | XS | ✅ Done |
| G2 | Move `plugins/coding_agent/` out of `plugins/` (it's a lib now, no manifest) | `plugins/coding_agent/__init__.py:7` | S | ⚪ Backlog |

---

**Not debt (verified — don't re-flag):** backend wire/live-smoke CI is real; gitleaks green; httpx-only HTTP; near-zero markers/`any`/`ts-ignore`; react-query layer + `request<T>` wrapper; chat streaming continuity + self-heal.
