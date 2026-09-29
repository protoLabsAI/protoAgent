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
  KnowledgeChunk,
  MemoryDigestPolicy,
  MemoryHotChunk,
  MemoryInjectionDetail,
  MemoryInjectionRow,
  MemorySessionDigest,
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
  // ignores that signal runs to completion against the FTS index anyway. Both are omitted
  // by the Knowledge surface, which keeps the server default and no cancellation.
  //
  // `prefix` is the third: it widens the query's LAST token to an FTS5 prefix term. The
  // store's index is whole-token (`knowledge/store.py` quotes each token as a phrase), so
  // without it a type-ahead matches nothing until the operator finishes the word they are
  // typing — an empty shortlist mid-word that reads exactly like "no matches". Off by
  // default, and NOT sent by the Knowledge surface: its box searches a query the operator
  // has finished, where a prefix term would only broaden the result silently.
  knowledgeSearch(
    q: string,
    opts: { reviewState?: ReviewState; k?: number; prefix?: boolean; signal?: AbortSignal } = {},
  ) {
    const params = new URLSearchParams({ q });
    if (opts.reviewState) params.set("review_state", opts.reviewState);
    if (opts.k != null) params.set("k", String(opts.k));
    if (opts.prefix) params.set("prefix", "1");
    return request<{
      enabled: boolean;
      query: string;
      results: KnowledgeChunk[];
      stats: Record<string, number>;
    }>(`/api/knowledge/search?${params.toString()}`, { signal: opts.signal });
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
  // toast) can bring them back. `deleted` is the count invalidated.
  deleteKnowledgeBySource(source: string) {
    return request<{ enabled: boolean; deleted: number; error?: string }>(
      "/api/knowledge/delete-by-source",
      { method: "POST", body: { source } },
    );
  },
  restoreKnowledgeBySource(source: string) {
    return request<{ enabled: boolean; restored: number; error?: string }>(
      "/api/knowledge/restore-by-source",
      { method: "POST", body: { source } },
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
  updateMemoryHot(chunkId: number, body: { content: string; heading?: string }) {
    return request<{ enabled: boolean; id: number | null; replaced: boolean }>(
      `/api/memory/hot/${chunkId}`,
      { method: "PUT", body },
    );
  },
  deleteMemoryHot(chunkId: number) {
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
  reviewMemoryChunk(chunkId: number, body: { state: ReviewState; tier?: "private" | "commons" | null }) {
    return request<{ enabled?: boolean; id?: number | null; review_state?: ReviewState | null }>(
      `/api/memory/chunks/${chunkId}/review`,
      { method: "POST", body },
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
