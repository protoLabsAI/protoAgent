/**
 * Knowledge + memory: search/curation/ingest, the memory inspector (ADR 0069), prompt
 * snapshots, and playbooks (skills).
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  HistoryEntry,
  HistorySearchHit,
  HistorySessionRow,
  KnowledgeChunk,
  KnowledgeCollection,
  MemoryDigestPolicy,
  MemoryHotChunk,
  MemoryInjectionDetail,
  MemoryInjectionRow,
  MemoryRecord,
  MemorySessionDigest,
  MemoryStats,
  PromptBreakdown,
  PromptCall,
  PromptRetention,
  PromptTaskResponse,
  Playbook,
  ReviewState,
} from "../types";
import { request, requestForm } from "./http";

export const knowledgeApi = {
  playbooks() {
    return request<{ enabled: boolean; playbooks: Playbook[] }>("/api/playbooks");
  },

  // `reviewState` (ADR 0108 D7) narrows a LISTING (empty q) or a search to rows with
  // that operator verdict — the console's "pending review" queue. Omitted = all rows.
  //
  // `k` and `signal` exist for the ⌘K knowledge provider (app/palette/knowledgeSearch.ts),
  // which needs both and cannot fake either. `k` because the route does NOT clamp it the
  // way its siblings do (`chat_routes.py`: `max(1, min(int(limit), 200))`) — the caller is
  // the only ceiling on how many rows come back, and a palette shortlist wants a handful,
  // not the server's thirty (the provider asks for one over its own cap, since an
  // over-full page is the only signal that route gives that there are more matches).
  // `signal` because the DS palette aborts a superseded keystroke, and a request that
  // ignores that signal runs to completion against the FTS index anyway. The ⌘K palette
  // is not the only caller now: the Knowledge collections browser (ADR 0125 P2 slice 6)
  // also passes `k` (to read a whole collection's chunks, not the server's default page)
  // and `signal` (to abort a superseded scoped search); the recall paths still omit both.
  //
  // `prefix` is the third: it widens the query's LAST token to an FTS5 prefix term. The
  // store's index is whole-token (`knowledge/store.py` quotes each token as a phrase), so
  // without it a type-ahead matches nothing until the operator finishes the word they are
  // typing — an empty shortlist mid-word that reads exactly like "no matches". Off by
  // default, and NOT sent by the Knowledge surface: its box searches a query the operator
  // has finished, where a prefix term would only broaden the result silently.
  //
  // `collection` (ADR 0125 P2 slice 6) scopes the browse/search to one collection
  // bucket (the `listCollections` rail); an unknown one yields an empty result, not
  // an error. Omitted = every collection.
  knowledgeSearch(
    q: string,
    opts: { reviewState?: ReviewState; collection?: string; k?: number; prefix?: boolean; signal?: AbortSignal } = {},
  ) {
    const params = new URLSearchParams({ q });
    if (opts.reviewState) params.set("review_state", opts.reviewState);
    if (opts.collection) params.set("collection", opts.collection);
    if (opts.k != null) params.set("k", String(opts.k));
    if (opts.prefix) params.set("prefix", "1");
    return request<{
      enabled: boolean;
      query: string;
      results: KnowledgeChunk[];
      stats: Record<string, number>;
    }>(`/api/knowledge/search?${params.toString()}`, { signal: opts.signal });
  },

  // The collection buckets behind the Knowledge browser (GET /api/knowledge/collections,
  // ADR 0125 P2 slice 6): one row per collection with its chunk/source counts and newest
  // ingest time. Chunks with no collection group under the knowledge_ingest default
  // ("general"); agent memory never appears (it lives in the memories table).
  // `enabled:false` = the knowledge store is off.
  listCollections() {
    return request<{ enabled: boolean; collections: KnowledgeCollection[] }>(
      "/api/knowledge/collections",
    );
  },

  // Knowledge chunk CRUD — operator curation of the store (add a fact, fix a
  // stale one, drop a wrong one). Edit replaces the chunk (new id): the server
  // adds the revision first, then deletes the old row, so it works on every
  // ADR 0031 backend and a hybrid store re-embeds on the way in.
  addKnowledgeChunk(body: { content: string; domain?: string; heading?: string }) {
    return request<{ enabled: boolean; id: number | null }>(
      "/api/knowledge/chunks",
      { method: "POST", body },
    );
  },
  updateKnowledgeChunk(id: number, body: { content: string; domain?: string; heading?: string; source?: string | null }) {
    return request<{ enabled: boolean; id: number | null; replaced: boolean }>(
      `/api/knowledge/chunks/${id}`,
      { method: "PUT", body },
    );
  },
  deleteKnowledgeChunk(id: number) {
    return request<{ enabled: boolean; deleted: boolean }>(
      `/api/knowledge/chunks/${id}`,
      { method: "DELETE" },
    );
  },
  // Promote a private chunk into the shared commons (ADR 0041 / bd-2wu) — only
  // meaningful when knowledge is layered; the route hints with promoted:false otherwise.
  promoteKnowledgeChunk(id: number) {
    return request<{ enabled: boolean; promoted: boolean; error?: string }>(
      `/api/knowledge/${id}/promote`,
      { method: "POST" },
    );
  },
  // Forget a chunk FROM the commons (the inverse of promote), by its commons-tier id.
  forgetKnowledgeChunk(id: number) {
    return request<{ enabled: boolean; forgotten: boolean; error?: string }>(
      `/api/knowledge/${id}/forget`,
      { method: "POST" },
    );
  },
  // Bulk delete-by-source (#1770) — remove a whole ingest (all chunks sharing one
  // `source`) in one call. It's a reversible SOFT delete: the chunks leave recall
  // immediately but survive a grace window, so `restoreKnowledgeBySource` (the Undo
  // toast) can bring them back. `deleted` is the count invalidated. An optional
  // collection restricts both deletion and Undo to one document bucket.
  deleteKnowledgeBySource(source: string, collection?: string) {
    return request<{ enabled: boolean; deleted: number; error?: string }>(
      "/api/knowledge/delete-by-source",
      { method: "POST", body: { source, ...(collection !== undefined ? { collection } : {}) } },
    );
  },
  restoreKnowledgeBySource(source: string, collection?: string) {
    return request<{ enabled: boolean; restored: number; error?: string }>(
      "/api/knowledge/restore-by-source",
      { method: "POST", body: { source, ...(collection !== undefined ? { collection } : {}) } },
    );
  },
  deleteKnowledgeByCollection(collection: string) {
    return request<{ enabled: boolean; deleted: number; error?: string }>(
      "/api/knowledge/delete-by-collection", { method: "POST", body: { collection } },
    );
  },
  restoreKnowledgeByCollection(collection: string) {
    return request<{ enabled: boolean; restored: number; error?: string }>(
      "/api/knowledge/restore-by-collection", { method: "POST", body: { collection } },
    );
  },
  // Document ingestion engine — extract a file/URL/YouTube into the KB (chunked,
  // enriched, embedded). FormData carries `file` OR `url` OR `text`, plus `domain`.
  ingestKnowledge(form: FormData) {
    return requestForm<{
      enabled: boolean;
      ids: number[];
      chunks: number;
      title: string | null;
      source_type: string;
      chars: number;
    }>("/api/knowledge/ingest", form);
  },

  // Dry-run an ingest (#1801) — extract + count chunks for a file/URL/text WITHOUT
  // persisting anything, so the upload dialog can show what's about to be ingested
  // (chunk count, token estimate, a text snippet) and gate it behind a Confirm.
  // Same FormData shape as `ingestKnowledge` minus `domain` (chosen at confirm time).
  previewKnowledgeIngest(form: FormData) {
    return requestForm<{
      enabled: boolean;
      chunks: number;
      chars: number;
      approx_tokens: number;
      title: string | null;
      source_type: string;
      source: string;
      snippet: string;
      truncated: boolean;
    }>("/api/knowledge/ingest/preview", form);
  },

  // --- Memory inspector (ADR 0069 D7) — the delivery-layer audit surface -----
  // Session summaries: the files behind the <prior_sessions> digest.
  // `sessionId` = the chat being viewed, so the row for it is reported the way
  // the agent sees it (a session is never a "prior" session of itself).
  memorySessions(sessionId = "") {
    const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
    return request<{ sessions: MemorySessionDigest[]; digest_policy?: MemoryDigestPolicy }>(
      `/api/memory/sessions${q}`,
    );
  },
  memorySession(sessionId: string) {
    return request<{ session: MemorySessionDigest }>(
      `/api/memory/sessions/${encodeURIComponent(sessionId)}`,
    );
  },
  deleteMemorySession(sessionId: string) {
    return request<{ deleted: boolean; session_id: string }>(
      `/api/memory/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  },
  // Hot memory: the domain="hot" chunks; the newest slice under the budget
  // injects every turn (rows carry `injecting` when the backend can tell).
  memoryHot() {
    return request<{ enabled: boolean; chunks: MemoryHotChunk[] }>("/api/memory/hot");
  },
  updateMemoryHot<T extends number | string>(chunkId: T, body: { content: string; heading?: string }) {
    return request<{ enabled: boolean; id: T | null; replaced: boolean }>(
      `/api/memory/hot/${chunkId}`,
      { method: "PUT", body },
    );
  },
  deleteMemoryHot(chunkId: number | string) {
    return request<{ enabled: boolean; deleted: boolean }>(`/api/memory/hot/${chunkId}`, {
      method: "DELETE",
    });
  },
  // Operator review verdict (ADR 0108 D7): confirm / reject / re-open (pending) one
  // memory row. `tier` MUST ride along on a layered store — ids are per-backend and the
  // route only refuses a commons id when told it is one; without it the verdict would
  // land on whatever PRIVATE row shares the number. 400 = bad state or a commons row,
  // 404 = unknown id, 501 = an ADR 0031 backend without verdicts, enabled:false = the
  // store is off. Every non-2xx surfaces as an ApiError carrying the server `detail`.
  reviewMemoryChunk(chunkId: number | string, body: { state: ReviewState; tier?: "private" | "commons" | null }) {
    return request<{ enabled?: boolean; id?: number | string | null; review_state?: ReviewState | null }>(
      `/api/memory/chunks/${chunkId}/review`,
      { method: "POST", body },
    );
  },
  // Core memories have their own id sequence (ADR 0125); never review them as chunks.
  memoryReview(id: number | string, state: ReviewState) {
    // Hot-list handles can be prefixed; the dedicated route takes the table id.
    const memoryId = String(id).replace(/^memory:/, "");
    return request<{ enabled?: boolean; id?: number | string | null; review_state?: ReviewState | null }>(
      `/api/memory/memories/${memoryId}/review`,
      { method: "POST", body: { state } },
    );
  },
  // --- Review pass (ADR 0125 P3) — the operator's curation of migrated memories ---
  // The core-memory listing (GET /api/memory/memories): every MemoryStore row, filtered
  // by the review tab's chips. `reviewState`/`kind`/`provenance` are exact-match filters
  // (omit = all). `includeSuperseded` brings the retired rows (whose Revert undoes a wrong
  // automatic supersession) into view. `limit`/`offset` page the (audit-complete) listing;
  // `total` is the count under the same filters. `enabled:false` = the memory store is off.
  memoryMemories(
    opts: {
      kind?: string;
      reviewState?: string;
      provenance?: string;
      legacyDomain?: string;
      includeSuperseded?: boolean;
      limit?: number;
      offset?: number;
    } = {},
  ) {
    const q = new URLSearchParams();
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.reviewState) q.set("review_state", opts.reviewState);
    if (opts.provenance) q.set("provenance", opts.provenance);
    if (opts.legacyDomain) q.set("legacy_domain", opts.legacyDomain);
    if (opts.includeSuperseded) q.set("include_superseded", "true");
    if (opts.limit != null) q.set("limit", String(opts.limit));
    if (opts.offset != null) q.set("offset", String(opts.offset));
    const qs = q.toString();
    return request<{ enabled: boolean; memories: MemoryRecord[]; total: number }>(
      `/api/memory/memories${qs ? `?${qs}` : ""}`,
    );
  },
  // Edit one memory's content/heading/subject/kind (PATCH). A non-rejected row is
  // confirmed by the edit (an operator write); a rejected row keeps its verdict. 400 on a
  // bad kind / empty content, 404 for an unknown id. `kind` is one of profile|standing|fact|decision.
  editMemory(id: number, body: { content?: string; heading?: string; subject?: string; kind?: string }) {
    return request<{ enabled: boolean; memory: MemoryRecord | null }>(
      `/api/memory/memories/${id}`,
      { method: "PATCH", body },
    );
  },
  // Forget a memory outright (DELETE). An always-on row is refused (400 — curate it on the
  // Hot panel); 404 for an unknown id. The review verdict (confirm/reject) rides the shared
  // `api.memoryReview` instead — rejecting keeps the row for audit.
  deleteMemory(id: number) {
    return request<{ enabled: boolean; deleted: boolean }>(
      `/api/memory/memories/${id}`,
      { method: "DELETE" },
    );
  },
  // Undo one automatic supersession (ADR 0125 P3-A): the retired row goes live again and the
  // row that replaced it is stamped superseded instead — both survive for audit. 404 when the
  // row has no revertible supersession.
  revertMemory(id: number) {
    return request<{ enabled: boolean; id?: number; reverted: boolean }>(
      `/api/memory/memories/${id}/revert`,
      { method: "POST" },
    );
  },
  // Shared-profile candidates (GET): the newest profile memory per subject, each flagged
  // `already_shared` when a live shared row of the same kind exists. `enabled:false` = the
  // shared profile is off (the whole candidates section hides).
  memoryProfileCandidates() {
    return request<{ enabled: boolean; candidates?: MemoryRecord[] }>(
      "/api/memory/profile-candidates",
    );
  },
  // Promote one profile/standing memory into the shared profile (POST). The server copies it
  // in as a confirmed operator row once per subject; a subject already shared answers 409 (the
  // ApiError carries status 409 so the caller can refresh the candidate list). 400 = not a
  // promotable kind / no subject / inactive row, 404 = unknown id.
  promoteMemory(id: number) {
    return request<{ enabled?: boolean; id?: number }>(
      `/api/memory/memories/${id}/promote`,
      { method: "POST" },
    );
  },
  // Injection record (ADR 0069 D6): which memory entered which turn.
  memoryInjections(sessionId = "", limit = 50) {
    const q = new URLSearchParams();
    if (sessionId) q.set("session_id", sessionId);
    q.set("limit", String(limit));
    return request<{ injections: MemoryInjectionRow[] }>(`/api/memory/injections?${q}`);
  },
  // One record's ids RESOLVED to their content, grouped for the detail dialog
  // (past conversations · memories · docs). Chunks that no longer resolve come
  // back marked `unavailable`.
  memoryInjectionDetail(id: number) {
    return request<MemoryInjectionDetail>(`/api/memory/injections/${id}`);
  },

  // History (ADR 0125 D6): the conversation `history` table (archives, retired-thread
  // summaries, background-job reports) behind the HistoryStore — the Memory inspector's
  // History tab. Never the Knowledge browser, which lists chunks only. `enabled:false`
  // = history is off (middleware.knowledge disabled).
  memoryHistorySessions() {
    return request<{ enabled: boolean; sessions: HistorySessionRow[] }>(
      "/api/memory/history/sessions",
    );
  },
  // Search runs the SAME query normalization `session_search` uses (natural text →
  // literal AND terms), so the tab finds what the agent's recall would.
  memoryHistorySearch(q: string, limit = 50) {
    const params = new URLSearchParams({ q, limit: String(limit) });
    return request<{ enabled: boolean; query: string; results: HistorySearchHit[] }>(
      `/api/memory/history/search?${params.toString()}`,
    );
  },
  memoryHistoryEntry(id: number) {
    return request<{ enabled: boolean; entry: HistoryEntry | null }>(
      `/api/memory/history/entries/${id}`,
    );
  },
  // Deletes reuse the chat-delete forget semantics (#3957): a single entry is a hard
  // delete of exactly that row (any kind); a whole session keeps background-job reports,
  // like the chat delete's forget. `deleted` is a boolean (entry) / a count (session).
  deleteMemoryHistoryEntry(id: number) {
    return request<{ enabled: boolean; deleted: boolean }>(
      `/api/memory/history/entries/${id}`,
      { method: "DELETE" },
    );
  },
  deleteMemoryHistorySession(sessionId: string) {
    return request<{ enabled: boolean; session_id: string; deleted: number }>(
      `/api/memory/history/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  },
  // The memory/history size split (#4557): the chunk store (memory + knowledge) and
  // history, each its own per-bucket count map.
  memoryStats() {
    return request<MemoryStats>("/api/memory/stats");
  },

  // Prompt snapshots (#2243): every captured model call of one turn, in call
  // order — the "View prompt" dialog's payload. 404s when the task has none.
  // #2388 P3: also carries `subagents` (calls nested under this turn's delegations)
  // and `prev` (the previous turn's last call — the diff anchor), both additive.
  promptsForTask(taskId: string) {
    return request<PromptTaskResponse>(`/api/prompts/${encodeURIComponent(taskId)}`);
  },
  // The TRUE next-call preview (#2388 P3): speculatively runs the dynamic layer
  // (incl. retrieval) without a model call or an injection-log write. Explicit
  // route — the speculation isn't free, so it's never the default.
  promptPreview(sessionId = "") {
    const q = new URLSearchParams();
    if (sessionId) q.set("session_id", sessionId);
    return request<{ enabled: boolean; call: PromptCall | null; reason?: string }>(
      `/api/prompts/preview?${q}`,
    );
  },
  // What the session's HISTORY is made of (#2843): checkpoint-sized categories,
  // per-tool totals, biggest blocks. Independent of prompts.capture (reads the
  // checkpoint, not the snapshot store) and cheap — no speculative retrieval.
  promptBreakdown(sessionId: string) {
    const q = new URLSearchParams({ session_id: sessionId });
    return request<{ found: boolean; reason?: string; breakdown?: PromptBreakdown }>(
      `/api/prompts/breakdown?${q}`,
    );
  },
  // The most recent captured call of one session (backs /prompt). `call` is
  // null when nothing has been captured yet; `enabled:false` = capture off (and
  // then there is no `retention` block — #3019's effective-window report).
  promptLast(sessionId = "") {
    const q = new URLSearchParams();
    if (sessionId) q.set("session_id", sessionId);
    return request<{ enabled: boolean; call: PromptCall | null; retention?: PromptRetention }>(
      `/api/prompts/last?${q}`,
    );
  },

  // Skills CRUD — author/edit operator skills. A create/edit writes a real
  // SKILL.md under the user-skills root (durable + exportable) and re-indexes it
  // live; editing a learned skill materializes it as a durable SKILL.md.
  createPlaybook(body: {
    name: string;
    description: string;
    prompt_template: string;
    tools_used?: string[];
    user_facing?: boolean;
    slash?: string;
  }) {
    return request<{ enabled: boolean; id: number | null; skill: Playbook | null }>(
      "/api/playbooks",
      { method: "POST", body },
    );
  },
  // Fetch one skill WITH its full prompt_template (the list omits it) to pre-fill the editor.
  getPlaybook(id: number) {
    return request<{ enabled: boolean; skill: Playbook | null }>(`/api/playbooks/${id}`);
  },
  updatePlaybook(
    id: number,
    body: {
      name: string;
      description: string;
      prompt_template: string;
      tools_used?: string[];
      user_facing?: boolean;
      slash?: string;
    },
  ) {
    return request<{ enabled: boolean; id: number | null; skill: Playbook | null }>(
      `/api/playbooks/${id}`,
      { method: "PUT", body },
    );
  },

  deletePlaybook(id: number) {
    return request<{ enabled: boolean; deleted: boolean; error?: string }>(
      `/api/playbooks/${id}`,
      { method: "DELETE" },
    );
  },

  // Promote a private skill into the shared commons (ADR 0041) — only meaningful
  // when the index is layered; the route reports promoted:false with a hint otherwise.
  promotePlaybook(id: number) {
    return request<{ enabled: boolean; promoted: boolean; name?: string; error?: string }>(
      `/api/playbooks/${id}/promote`,
      { method: "POST" },
    );
  },

  // Forget a skill FROM the shared commons (ADR 0041) — the inverse of promote, on a
  // COMMONS-tier id. Layered-only; reports forgotten:false with a hint otherwise.
  forgetPlaybook(id: number) {
    return request<{ enabled: boolean; forgotten: boolean; name?: string; error?: string }>(
      `/api/playbooks/${id}/forget`,
      { method: "POST" },
    );
  },
};
