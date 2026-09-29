import type {
  SnapshotImportPlan,
  SnapshotImportResult,
  SnapshotReview,
  AcpAgent,
  ActivityHistory,
  AgentConfig,
  Archetype,
  ArchetypePreview,
  BackgroundJobDTO,
  BrowseListing,
  ChatBundleManifest,
  FsProject,
  ManagedProjects,
  Task,
  ChatMessage,
  ComponentSpec,
  ConfigPayload,
  ContextWindow,
  ConsumedSteer,
  DelegateProbe,
  DelegateTypeSpec,
  DelegateView,
  DiagnosticsLogs,
  DiagnosticsTask,
  DiscoveredAgent,
  FleetAgent,
  RemoteAuth,
  FleetStatus,
  FlagsPayload,
  GoalState,
  HitlPayload,
  InboxItem,
  MentionTarget,
  MessageAuthor,
  RoomReply,
  CatalogPlugin,
  McpCatalogEntry,
  InstalledPlugin,
  PluginDepsNeeded,
  PluginInstallSummary,
  PluginUpdate,
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
  PublishedLink,
  NodeRuntimePayload,
  PythonRuntimePayload,
  RuntimeStatus,
  ScheduledJob,
  SecretsStatus,
  SecretsTestResult,
  SetupStatus,
  SettingsGroup,
  SlashCommand,
  SoulVersion,
  Playbook,
  ReviewState,
  Subagent,
  ToolInfo,
  FleetTelemetry,
  TelemetryInsights,
  TelemetrySummary,
  TelemetryTurn,
  ToolEvent,
  TurnUsage,
  WatchState,
  WorkflowPausedRun,
  WorkflowRecipe,
  WorkflowRunRecord,
  WorkflowRunResult,
  WorkflowRunSummary,
  VerifierCatalog,
  WorkflowSummary,
} from "./types";
import { cancelBody, type PairKind } from "./agentPairing";
import { notifyAuthRequired } from "./auth";
import { errMsg } from "./format";
import type { WatchCreateBody } from "../chat/watchForm";
import { authToken, currentSlug, apiUrl, memberPath, applyAuth } from "./api/routing";
import {
  ApiError,
  isMemberScoped,
  parseErrorBody,
  request,
  requestForm,
  memberRequest,
} from "./api/http";
import {
  consumedSteersFromParts,
  consumeSse,
  drainSseBuffer,
  makeA2ADispatcher,
  textFromTerminalTask,
  type A2AFrame,
  type DurableChatSession,
  type DurableChatTurn,
  type RawPart,
  type TurnStreamHandlers,
} from "./api/a2aStream";

// #3808: the slug-routing, HTTP and A2A-stream layers live in ./api/*. Re-export every name
// that used to be defined here so no importer (or `vi.mock("../lib/api")`) has to change.
export { agentHref, apiUrl, authToken, currentSlug, isHostConsole, memberPath } from "./api/routing";
export {
  ApiError,
  is401,
  isAgentNotRunning,
  isAgentUnreachable,
  isColdStart,
  isMemberScoped,
  parseErrorBody,
} from "./api/http";
export {
  artifactAppends,
  componentFromParts,
  consumedSteersFromParts,
  contextFromParts,
  costFromMeta,
  drainSseBuffer,
  frameIsForeign,
  hitlFromParts,
  replayDurableChatTurn,
  roomReplyFromParts,
  textFromParts,
  type DurableChatSession,
  type DurableChatTurn,
  type TurnStreamHandlers,
} from "./api/a2aStream";

/** Boot hook (ADR 0042 slug routing → #806): a window opening `/app/agent/<slug>/` ensures
 * its agent is RUNNING — `POST /api/fleet/<name>/activate` resumes a cold agent from its
 * checkpoint and touches it for keep-N-warm LRU. Every slug navigation is a full page load
 * (FleetSwitcher navigates), so this one boot call covers switch, reload and new-window.
 * Fire-and-forget: the shell's queries already retry through the resume window, and any
 * failure (non-fleet backend, unknown slug) just leaves today's behavior. The slug IS the
 * agent's `id`, and activate resolves id-or-name, so this goes straight there — it used to
 * fetch the whole fleet first only to map the id back to a display name. */
export async function activateSlugAgent(): Promise<void> {
  const slug = currentSlug();
  if (slug === "host") return;
  try {
    await api.activateAgent(slug); // hub control-plane path — never slug-scoped
  } catch {
    // best-effort — the proxy 502s + query retries surface a truly unreachable agent
  }
}

/** True inside the desktop (Tauri/WKWebView) shell. WKWebView does NOT deliver a
 * `text/event-stream` body through `fetch()` — neither via `body.getReader()` nor
 * a buffered `clone().text()` (both come back empty) — so the streaming chat turn
 * renders as a blank assistant bubble. In that environment we route the chat turn
 * through the non-streaming `/api/chat` endpoint instead, which returns ordinary
 * JSON that WKWebView handles fine (it's how the rest of the console already talks
 * to the sidecar). Browsers keep the streaming `/a2a` path. */
export function isDesktopWebview(): boolean {
  try {
    const { protocol, hostname } = window.location;
    return protocol === "tauri:" || protocol === "file:" || hostname === "tauri.localhost";
  } catch {
    return false;
  }
}

/** A typed view of the bits of the Tauri `core` API the desktop streaming path uses,
 * read off the `window.__TAURI__` global (the shell sets `withGlobalTauri: true`), so
 * the shared web bundle needs no `@tauri-apps/api` dependency. Null outside the shell. */
type TauriChannel<T> = { onmessage: (msg: T) => void };
type TauriCore = {
  invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
  Channel: new <T>() => TauriChannel<T>;
};
function tauriCore(): TauriCore | null {
  try {
    return (window as unknown as { __TAURI__?: { core?: TauriCore } }).__TAURI__?.core ?? null;
  } catch {
    return null;
  }
}

/** GET /api/fs/file (ADR 0112). `text` is lines start..end (1-based, inclusive); null when
 *  the file is binary. `truncated` = a server cap cut the read — page with start/end. */
export type FsFile = {
  project: string;
  path: string;
  size: number;
  /** null for a binary file (no lines to count). */
  line_count: number | null;
  start: number | null;
  end: number | null;
  /** A server cap cut the read: lines past `end` (page with start/end) and/or single lines
   *  past the per-line cap (each cut line ends with " … [line truncated]"). */
  truncated: boolean;
  language: string;
  binary: boolean;
  text: string | null;
};

export type FsDiffFile = {
  path: string;
  status: "M" | "A" | "D" | "R" | "?";
  additions: number;
  deletions: number;
  binary: boolean;
  denied: boolean;
  /** A rename's previous path (status "R"). */
  old_path?: string;
  /** An untracked file over the server's 256 KB cap — listed, content not in `patch`. */
  too_large?: boolean;
};

/** GET /api/fs/diff (ADR 0112) — the working tree vs HEAD, untracked files included. */
export type FsDiff = {
  project: string;
  is_git: boolean;
  head?: string;
  branch?: string;
  files: FsDiffFile[];
  patch: string;
  truncated?: boolean;
};

/** A reachable address in a pairing-start answer. For a PHONE code `url` is the full
 *  `…/app/#pair=<code>` link and `qr` its rendered SVG; for an AGENT code (ADR 0113) `url` is
 *  the bare base URL a hub pairs against and there is no QR — the code is typed, not scanned. */
export type PairHost = { host: string; kind: "tailnet" | "lan"; url: string; qr?: string | null };
export type PairAddress = { host: string; kind: "tailnet" | "lan" };
/** A row of `GET /api/devices`. `kind` says whether the client is a phone/tablet or another
 *  agent's hub (ADR 0113 D3) — fixed by the code it claimed; absent (pre-0113) reads `device`. */
export type PairedDevice = {
  id: string;
  name: string;
  created_at: number;
  last_seen_at: number | null;
  kind?: PairKind;
};
export type PairingStart =
  | {
      ok: true;
      /** Which code this is — echoed by the server; absent from pre-ADR-0113 servers (a phone code). */
      kind?: PairKind;
      /** A phone code is 32 url-safe chars; an agent code is `XXXXX-XXXXX`. */
      code: string;
      expires_at: number;
      ttl: number;
      hosts: PairHost[];
      /** Agent codes only: this agent's display name, so the hint can say who's pairing. */
      name?: string;
    }
  /** Nothing reachable — `available` is what the server COULD bind to (ADR 0087 D6).
   *  `authConfigured` is THIS SERVER's answer to "do I have a token", which a client cannot
   *  infer: localStorage holding one says nothing about what the server accepts. */
  | { ok: false; error: string; available: PairAddress[]; bind: string; authConfigured: boolean };

export const api = {
  runtimeStatus() {
    return request<RuntimeStatus>("/api/runtime/status");
  },

  // ── Device pairing (ADR 0087) ──
  // The CLAIM half deliberately lives outside this object (lib/pairing.ts): `request`
  // attaches the operator bearer, and claiming runs precisely when there isn't one.
  devices() {
    return request<{ devices: PairedDevice[] }>("/api/devices");
  },
  /** Start pairing.
   *
   * Read directly rather than through `request` because the 409 body is MEANINGFUL: a
   * loopback-bound instance reports the addresses it *could* be reached on so the panel can
   * offer to bind there. `request` collapses every non-2xx into a thrown string, which would
   * throw that payload away — and the alternative (returning 200 with ok:false) would weaken
   * a correct status code for client convenience. */
  async pairingStart(kind: PairKind = "device"): Promise<PairingStart> {
    const headers = applyAuth(new Headers());
    headers.set("Content-Type", "application/json");
    const res = await fetch(apiUrl("/api/pairing/start"), {
      method: "POST",
      headers,
      // `kind` is fixed at MINT time (ADR 0113 D2) — the claimer can't relabel a phone code
      // as an agent, so this body is the only place the kind is ever chosen.
      body: JSON.stringify({ kind }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) return { ok: true, ...data } as PairingStart;
    return {
      ok: false,
      error: String(data?.error || `${res.status} ${res.statusText}`),
      available: Array.isArray(data?.available) ? data.available : [],
      bind: String(data?.bind || ""),
      // Absent (an older server) is treated as NOT configured, so the flow mints rather than
      // assuming — minting a second token is recoverable; writing a bind with no token is not.
      authConfigured: data?.auth_configured === true,
    };
  },
  /** Drop this dialog's pending code. Scoped by kind (see `cancelBody`): an unscoped cancel
   *  would also kill a code of the OTHER kind that another dialog is still showing. */
  pairingCancel(kind: PairKind) {
    return request<{ ok: boolean }>("/api/pairing/cancel", { method: "POST", body: cancelBody(kind) });
  },
  revokeDevice(id: string) {
    return request<{ ok: boolean }>(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Short-lived HMAC token for the SSE EventSource, which can't send an
  // Authorization header. Bearer-gated; in open mode the server returns "" and
  // accepts a tokenless /api/events. events.ts fetches this before each
  // (re)connect.
  //
  // Signed by the HUB (host:true), NOT slug-routed. The proxied `/api/events`
  // is validated at the HUB's auth middleware FIRST (before it forwards to the
  // member), so the token must carry the hub's signature or the stream 401s at
  // the hub for every non-host member on a bearer-gated hub. The hub then
  // forwards with the member's own credential attached, so the member accepts it
  // downstream (its SSE branch falls through to the bearer check).
  sseToken() {
    return request<{ token: string }>("/api/sse-token", { host: true });
  },
  // SSE token for a SPECIFIC fleet member (Fleet Activity streams each member's
  // /agents/<slug>/api/events). Best-effort: open-mode instances need none, so a
  // failure resolves to "" and the caller connects tokenless.
  sseTokenFor(slug: string): Promise<{ token: string }> {
    return fetch(memberPath(slug, "/api/sse-token"), { headers: applyAuth(new Headers()) })
      .then((r) => (r.ok ? (r.json() as Promise<{ token: string }>) : { token: "" }))
      .catch(() => ({ token: "" }));
  },

  // Gracefully restart the server process (POST /api/restart) — the server drains and
  // re-execs; the console reconnects via the boot gate. Always targets the HOST (the
  // process you're connected to), never a slug-routed agent.
  restart() {
    return request<{ ok: boolean; restarting: boolean }>("/api/restart", { method: "POST", host: true });
  },

  // Managed Node runtime (ADR 0085) — status + one-click provisioning of node/npx for
  // the npx-based ACP agents + MCP servers. HOST-targeted (the box-shared runtime lives
  // on the server process, not a slug-routed agent), like restart().
  nodeRuntime() {
    return request<NodeRuntimePayload>("/api/runtime/node", { host: true });
  },
  installNodeRuntime(force = false) {
    return request<{ ok: boolean } & NodeRuntimePayload>(
      `/api/runtime/node/install${force ? "?force=true" : ""}`,
      { method: "POST", host: true },
    );
  },

  // Managed Python runtime (ADR 0094) — status + one-click provisioning of the
  // execute_code child interpreter for the packaged desktop app. HOST-targeted like
  // nodeRuntime(): the box-shared runtime lives on the server process.
  pythonRuntime() {
    return request<PythonRuntimePayload>("/api/runtime/python", { host: true });
  },
  installPythonRuntime(force = false) {
    return request<{ ok: boolean } & PythonRuntimePayload>(
      `/api/runtime/python/install${force ? "?force=true" : ""}`,
      { method: "POST", host: true },
    );
  },

  // The HUB's runtime status — NEVER slug-routed. The TenantGuard keys on the hub's
  // `instance_uid` (the real tenant of this origin), which is STABLE across agent
  // swaps. The slug-routed runtimeStatus() returns the FOCUSED agent's uid, which
  // changes on every switch and would wrongly wipe the chat view each time.
  hostRuntimeStatus() {
    return request<RuntimeStatus>("/api/runtime/status", { host: true });
  },

  // Background subagent jobs (ADR 0050) — the focused agent's registry. Read-only;
  // the UtilityBar pill + jobs dialog hydrate from this, then track live via the
  // `background.{started,completed}` bus events.
  background() {
    return request<{ enabled: boolean; jobs: BackgroundJobDTO[] }>("/api/background");
  },

  // One background job's full row by id (ADR 0070 D4). This is the ONLY place the
  // FULL result text is fetchable — the `background.completed` bus event and the
  // drained <task-notification> both carry truncated previews.
  backgroundJob(jobId: string) {
    return request<BackgroundJobDTO>(`/api/background/${encodeURIComponent(jobId)}`);
  },

  // Stop a running background job (ADR 0051) — cancels its detached A2A turn.
  stopBackground(jobId: string) {
    return request<{ ok: boolean; status?: string; detail?: string }>(
      `/api/background/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  },

  // Delete a FINISHED background job's entry (housekeeping). Running jobs are kept.
  deleteBackground(jobId: string) {
    return request<{ ok: boolean; deleted?: boolean }>(
      `/api/background/${encodeURIComponent(jobId)}`,
      { method: "DELETE" },
    );
  },

  // Delete all FINISHED background jobs (clears the stacked-up history).
  clearFinishedBackground() {
    return request<{ ok: boolean; cleared?: number }>("/api/background/clear", { method: "POST" });
  },

  trajectoryEvents(sessionId: string, limit = 20) {
    return request<{ found: boolean; events: import("./types").TrajectoryEvent[]; total: number }>(
      `/api/trajectory/${encodeURIComponent(sessionId)}?limit=${limit}`,
    );
  },

  trajectoryCall(sessionId: string, n: number) {
    return request<import("./types").TrajectoryCall & { reason?: string }>(
      `/api/trajectory/${encodeURIComponent(sessionId)}/call/${n}`,
    );
  },

  telemetrySummary(since?: string) {
    const q = since ? `?since=${encodeURIComponent(since)}` : "";
    return request<{ enabled: boolean; summary: TelemetrySummary | null }>(
      `/api/telemetry/summary${q}`,
    );
  },

  telemetryRecent(limit = 50) {
    // `langfuse_trace_url_template` carries a `{trace_id}` placeholder (null when
    // Langfuse isn't configured) — see telemetry/traceUrl.ts.
    // `tracing_enabled` says whether Langfuse is on at all (#3017), so a blank Trace
    // cell can say "tracing is off" instead of implying this turn simply wasn't
    // traced. Optional: an older backend omits it and the surface stays as it was.
    return request<{
      enabled: boolean;
      turns: TelemetryTurn[];
      langfuse_trace_url_template?: string | null;
      tracing_enabled?: boolean;
    }>(`/api/telemetry/recent?limit=${limit}`);
  },

  telemetryInsights() {
    return request<{ enabled: boolean; insights: TelemetryInsights | null }>(
      "/api/telemetry/insights",
    );
  },

  // Hub-side fleet telemetry rollup (ADR 0006 fleet extension). Read-only; a
  // single-box install answers with `fleet: false` and just the host member.
  telemetryFleet() {
    return request<FleetTelemetry>("/api/telemetry/fleet");
  },

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

  // #1701 Slice 2: redeem a plugin composer-form — POST the field values back to the
  // plugin's on_submit. Returns a reply note, or the next form for a multi-step wizard.
  submitChatCommandForm(body: { callback_id: string; session_id: string; answers: Record<string, unknown> }) {
    return request<{ reply?: string | null; form?: HitlPayload; callback_id?: string }>(
      "/api/chat/commands/submit",
      { method: "POST", body },
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

  // Chat attachment — extract + TIER a dropped file (FormData: `file` + `session_id`).
  // Returns a ready-to-prepend `context` block (full text for small docs, a lede +
  // retrieval note for large docs indexed under the session) so a big doc never
  // gets dumped into the turn.
  attachToChat(form: FormData) {
    return requestForm<{
      enabled: boolean;
      mode?: "inline" | "indexed";
      name?: string;
      source_type?: string;
      chars?: number;
      chunks?: number;
      context?: string;
    }>("/api/knowledge/attach", form);
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

  setupStatus() {
    return request<SetupStatus>("/api/config/setup-status");
  },

  config() {
    return request<ConfigPayload>("/api/config");
  },

  soulPreset(name: string) {
    return request<{ name: string; content: string }>(`/api/config/presets/${encodeURIComponent(name)}`);
  },

  // SOUL.md version history (#1691): every persona save archives the outgoing text.
  soulHistory() {
    return request<{ versions: SoulVersion[] }>("/api/config/soul/history");
  },
  soulVersion(id: string) {
    return request<{ id: string; content: string }>(`/api/config/soul/history/${encodeURIComponent(id)}`);
  },
  restoreSoulVersion(id: string) {
    return request<{ ok: boolean; messages: string[]; restored: string }>(
      `/api/config/soul/history/${encodeURIComponent(id)}/restore`,
      { method: "POST" },
    );
  },

  models(apiBase: string, apiKey: string, provider = "") {
    return request<{ models: string[]; error: string }>("/api/config/models", {
      method: "POST",
      // `provider` (ADR 0097): a native OAuth provider lists the subscription
      // account's models instead of the gateway's; blank = gateway.
      body: { api_base: apiBase, api_key: apiKey, provider },
    });
  },

  /** The provider registry (ADR 0106) — every configured CONNECTION, keys redacted.
   *  `in_use_by` names the slots routing through each one, so the delete guard is
   *  legible before the operator tries it. */
  providers() {
    return request<{
      providers: {
        id: string;
        type: string;
        label?: string;
        base_url?: string;
        display: string;
        has_key: boolean;
        in_use_by: string[];
        // The same dependencies `in_use_by` names, structured so the panel can offer a
        // repoint/clear per row (bd-v6xy). `kind`: slot | favorite | subagent. `clearable`
        // is false for `model.name` only (the lead model must always resolve). A favorites
        // entry is ONE row whose `value` is the matching favorite list. Optional so a
        // pre-bd-neiz backend (or a test fixture) that omits it still types.
        in_use?: {
          key: string;
          value: string | string[];
          kind: "slot" | "favorite" | "subagent";
          clearable: boolean;
        }[];
      }[];
    }>("/api/config/providers");
  },

  addProvider(body: { id: string; type: string; label?: string; base_url?: string; api_key?: string }) {
    return request<{ ok: boolean; id: string }>("/api/config/providers", { method: "POST", body });
  },

  /** Label / endpoint / key only. There is no id or type here on purpose: both are
   *  identity, and an id lives inside stored model values that a rename cannot reach. */
  updateProvider(id: string, body: { label?: string; base_url?: string; api_key?: string }) {
    return request<{ ok: boolean; id: string }>(`/api/config/providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
    });
  },

  /** 409 with the referencing slots named when the connection is still in use.
   *
   *  `releases` (bd-v6xy) repoints or clears each blocking reference in the SAME request
   *  that removes the connection — `<other_pid>:<model>` to repoint, `null` to clear
   *  (favorites: drop those prefixed `<pid>:`). It is sent as the JSON body ONLY when
   *  provided; the bare `removeProvider(id, confirmLast)` call stays byte-identical (no
   *  body), preserving the old refuse-if-in-use behaviour. */
  removeProvider(id: string, confirmLast = false, releases?: Record<string, string | null>) {
    const query = confirmLast ? "?confirm_last=true" : "";
    return request<{ ok: boolean; removed: string; released?: string[] }>(
      `/api/config/providers/${encodeURIComponent(id)}${query}`,
      releases ? { method: "DELETE", body: { releases } } : { method: "DELETE" },
    );
  },

  /** That connection's own model list — its endpoint, or its subscription account. */
  providerModels(id: string) {
    return request<{ models: string[]; error: string }>(
      `/api/config/providers/${encodeURIComponent(id)}/models`,
      { method: "POST" },
    );
  },

  /** Sign-in status for the native OAuth providers (ADR 0097) — "✓ signed in" or a
   *  sign-in hint per provider, so the setup UX never asks for a key it doesn't need. */
  oauthStatus() {
    return request<{
      providers: { provider: string; signed_in: boolean; source: string; detail: string; hint: string }[];
    }>("/api/config/oauth-status");
  },

  /** Begin an in-console OAuth sign-in (ADR 0097). `mode: "device"` (Codex) returns a
   *  user_code + verification_uri to poll; `mode: "redirect"` (Claude) returns an
   *  authorize_url to open and complete with the pasted code. */
  oauthStart(provider: string) {
    return request<{
      flow_id: string;
      mode: "device" | "redirect";
      user_code?: string;
      verification_uri?: string;
      interval?: number;
      authorize_url?: string;
    }>("/api/config/oauth/start", { method: "POST", body: { provider } });
  },
  /** Poll a Codex device sign-in until the user approves. `graph_reloaded` (#2458):
   *  a completed sign-in on a graphless server rebuilt the graph inline. */
  oauthPoll(flowId: string) {
    return request<{ status: "pending" | "complete" | "error"; error?: string; graph_reloaded?: boolean; graph_reload_error?: string }>(
      "/api/config/oauth/poll",
      { method: "POST", body: { flow_id: flowId } },
    );
  },
  /** Complete a Claude sign-in with the pasted `code#state`. */
  oauthComplete(flowId: string, code: string) {
    return request<{ status: "complete" | "error"; error?: string; graph_reloaded?: boolean; graph_reload_error?: string }>(
      "/api/config/oauth/complete",
      { method: "POST", body: { flow_id: flowId, code } },
    );
  },
  /** Abandon an in-progress sign-in server-side (#2440) — so wizard Cancel truly cancels
   *  the flow, not just the browser timer. */
  oauthCancel(flowId: string) {
    return request<{ ok: boolean; cancelled: boolean }>(
      "/api/config/oauth/cancel",
      { method: "POST", body: { flow_id: flowId } },
    );
  },
  /** Disconnect a native OAuth provider (#2440): best-effort remote revoke + delete
   *  protoAgent's own credential + suppress auto-reconnect until the next sign-in. */
  oauthDisconnect(provider: string) {
    return request<{ provider: string; removed: boolean; revoked: boolean; note: string; graph_unloaded?: boolean }>(
      "/api/config/oauth/disconnect",
      { method: "POST", body: { provider } },
    );
  },

  // ── Agent snapshot (ADR 0091 Slice 1) ──
  /** Review WITHOUT building the download: what would be stripped, what the target must
   *  re-supply, what the pattern sweep matched. The export is meant to leave the machine,
   *  so the console shows this first rather than handing over a zip nobody has read. */
  snapshotReview() {
    return request<SnapshotReview>("/api/agent/export", { method: "POST", body: { dry_run: true } });
  },
  /** The snapshot itself. Returns the Blob plus the server's filename — the name carries the
   *  agent + timestamp, and re-deriving it client-side would drift from the artifact. */
  async exportSnapshot(): Promise<{ blob: Blob; filename: string; definitionSha256: string }> {
    const res = await fetch(apiUrl("/api/agent/export"), {
      method: "POST",
      headers: applyAuth(new Headers({ "content-type": "application/json" })),
      body: JSON.stringify({ dry_run: false }),
    });
    if (!res.ok) throw new Error(`export failed: ${res.status}`);
    const disposition = res.headers.get("content-disposition") || "";
    const match = /filename="([^"]+)"/.exec(disposition);
    return {
      blob: await res.blob(),
      filename: match?.[1] || "agent-snapshot.zip",
      definitionSha256: res.headers.get("x-snapshot-definition-sha256") || "",
    };
  },

  /** Inspect a snapshot WITHOUT applying it (ADR 0091 D3). Returns the plan: which plugins
   *  would be installed and run, which capabilities the config grants, which credentials the
   *  new agent needs. Writes nothing — the console shows this before asking for consent. */
  snapshotPlan(file: File) {
    const form = new FormData();
    form.append("file", file);
    return requestForm<SnapshotImportPlan>("/api/agent/import", form);
  },
  /** Apply a snapshot. `acknowledged` asserts the operator has SEEN the plan — applying
   *  installs and runs the plugin code it names, so this is never sent implicitly. */
  snapshotImport(file: File, opts: { name: string; secrets: Record<string, string> }) {
    const form = new FormData();
    form.append("file", file);
    form.append("name", opts.name);
    form.append("acknowledged", "true");
    if (Object.keys(opts.secrets).length) form.append("secrets_json", JSON.stringify(opts.secrets));
    return requestForm<SnapshotImportResult>("/api/agent/import", form);
  },

  // Real completion probe — the true auth check (unlike `models`, which only
  // Download all telemetry as CSV (carries the bearer; returns a Blob to save).
  async exportTelemetry(): Promise<Blob> {
    const res = await fetch(apiUrl("/api/telemetry/export"), {
      headers: applyAuth(new Headers()),
    });
    if (!res.ok) throw new Error(`export failed: ${res.status}`);
    return res.blob();
  },

  // lists). Blank fields fall back to the saved config (Settings re-test).
  testModel(apiBase: string, apiKey: string, model: string, provider = "") {
    return request<{ ok: boolean; error: string }>("/api/config/test-model", {
      method: "POST",
      // `provider` (ADR 0097): a native OAuth provider tests through the subscription
      // (a real streamed turn), ignoring api_base/api_key; blank = gateway.
      body: { api_base: apiBase, api_key: apiKey, model, provider },
    });
  },

  // Generic plugin "Test connection" (ADR 0029) — POST the group's fields (short
  // keys) to the plugin's test route. Blank/omitted fields fall back to the saved
  // config. Returns {ok, identity, error}. Used by any group with a `test` endpoint.
  testConfig(endpoint: string, fields: Record<string, unknown>) {
    return request<{ ok: boolean; identity: string | null; error: string | null }>(endpoint, {
      method: "POST",
      body: fields,
    });
  },

  // External secrets manager (ADR 0080) — status / force-a-refresh / connection test.
  // Test runs against the SAVED config (unsaved form edits don't ride along yet).
  secretsStatus() {
    return request<SecretsStatus>("/api/secrets/status");
  },
  secretsSync() {
    return request<SecretsStatus>("/api/secrets/sync", { method: "POST", body: {} });
  },
  secretsTest() {
    return request<SecretsTestResult>("/api/secrets/test", { method: "POST", body: {} });
  },


  // `requires_tools` is the picked archetype's capability contract (ADR 0100) — the
  // host-side twin of what createAgent records on a member's workspace.yaml, so a
  // wizard-installed archetype gets the same contract banner. Always sent (an empty
  // list clears a stale record from an earlier wizard run).
  finishSetup(config: Partial<AgentConfig>, soul: string, requiresTools: string[] = []) {
    return request<{ ok: boolean; message: string }>("/api/config/setup", {
      method: "POST",
      body: { config, soul, requires_tools: requiresTools },
    });
  },

  // Merge-apply a config patch (+ optional SOUL.md) on the live agent, then reload.
  // Partial config is merged into the live YAML (not a replace), so passing just
  // `{ identity: { name } }` is safe. Pass null to skip either.
  applyConfig(config: Partial<AgentConfig> | null, soul: string | null) {
    return request<{ ok: boolean; messages: string[] }>("/api/config", {
      method: "POST",
      body: { config, soul },
    });
  },

  subagents() {
    return request<{ subagents: Subagent[] }>("/api/subagents");
  },

  tools() {
    // `count` = wired (enabled) tools; `disabled` = the RAW tools.disabled denylist —
    // the base a row toggle edits, so stale names (no live tool) survive a save.
    return request<{ tools: ToolInfo[]; count: number; disabled: string[] }>("/api/tools");
  },

  runSubagent(body: {
    session_id: string;
    type: string;
    description: string;
    prompt: string;
  }) {
    return request<{ ok: boolean; session_id: string; output: string }>("/api/subagents/run", {
      method: "POST",
      body,
    });
  },

  runSubagentBatch(body: {
    session_id: string;
    tasks: Array<{
      type?: string;
      subagent_type?: string;
      description: string;
      prompt: string;
    }>;
  }) {
    return request<{ ok: boolean; session_id: string; output: string }>("/api/subagents/batch", {
      method: "POST",
      body,
    });
  },

  schedules() {
    return request<{ jobs: ScheduledJob[]; backend: string }>("/api/scheduler/jobs");
  },

  addSchedule(body: { prompt: string; schedule: string; job_id?: string; timezone?: string }) {
    return request<{ job: ScheduledJob }>("/api/scheduler/jobs", {
      method: "POST",
      body,
    });
  },

  updateSchedule(jobId: string, body: { prompt: string; schedule: string; timezone?: string }) {
    return request<{ job: ScheduledJob }>(`/api/scheduler/jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body,
    });
  },

  cancelSchedule(jobId: string) {
    return request<{ canceled: boolean }>(`/api/scheduler/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
    });
  },

  goals() {
    return request<{ goals: GoalState[]; enabled: boolean }>("/api/goals");
  },

  // One goal's full detail — the status dict + the durable plan artifact (`.plan.md`, the
  // agent's "orient" world-model it maintains via `update_goal_plan`, ADR 0079). `plan` is
  // "" when the goal hasn't recorded one. Powers the goal detail drawer.
  goalDetail(sessionId: string) {
    return request<{ enabled: boolean; goal: GoalState | null; plan: string }>(
      `/api/goals/${encodeURIComponent(sessionId)}`,
    );
  },

  // Clear (stop) a goal. `closeTasks` also closes the goal's session-scoped task backlog
  // (ADR 0079) — the "Stop goal" action. Returns how many tasks were closed.
  clearGoal(sessionId: string, closeTasks = false) {
    const q = closeTasks ? "?close_tasks=true" : "";
    return request<{ cleared: boolean; tasks_closed?: number }>(
      `/api/goals/${encodeURIComponent(sessionId)}${q}`,
      { method: "DELETE" },
    );
  },

  // Goal lifecycle (ADR 0079) — re-arm: extend an active goal's iteration budget
  // (`add_iterations`), or reactivate a terminal one and kick a fresh drive turn (the backend
  // resets the loop + enqueues the turn). `resumed` is true when a terminal goal was
  // reactivated. A no-op (active goal, no added budget) comes back HTTP 400.
  rearmGoal(sessionId: string, body: { add_iterations?: number }) {
    return request<{ ok: boolean; message?: string; resumed?: boolean; kicked?: boolean; error?: string }>(
      `/api/goals/${encodeURIComponent(sessionId)}/rearm`,
      { method: "POST", body },
    );
  },

  // Operator goal-set (ADR 0066) — the trusted operator channel. `/api` is operator-tier by
  // the ADR 0066 path ceiling, so this accepts ANY verifier type (unlike the plugin-only SDK
  // path). A rejected verifier / disabled goal mode comes back as HTTP 400 (request() throws,
  // so the caller's onError surfaces the reason); the happy path returns {ok:true, message}.
  // Optional completion-contract fields (ADR 0073) shape the drive-loop continuation
  // prompt each turn — the verifier still decides DONE. All optional and backward-compatible.
  setGoal(body: {
    session_id: string;
    condition: string;
    verifier: unknown;
    outcome?: string;
    constraints?: string[];
    boundaries?: string[];
    stop_when?: string;
    max_iterations?: number;
    // `false` = don't kick a headless drive turn; the caller drives the goal from a chat tab
    // instead (the console panel path). Omitted/true = the pre-tab behavior (auto-start).
    kick?: boolean;
  }) {
    return request<{ ok: boolean; message?: string; kicked?: boolean; error?: string }>("/api/goals", {
      method: "POST",
      body,
    });
  },

  // Detach-continue (ADR 0079): keep an ACTIVE goal driving in the background after the chat
  // tab that was streaming it is closed. 400 when the session has no active goal.
  resumeGoal(sessionId: string) {
    return request<{ ok: boolean; kicked?: boolean; error?: string }>(
      `/api/goals/${encodeURIComponent(sessionId)}/resume`,
      { method: "POST" },
    );
  },

  // Watches (ADR 0067) — passive verifier-only objectives, many at once, keyed by id. The
  // panel invalidates this on the `watch.*` bus pushes (created/checked/met/expired/stalled)
  // instead of polling — same pattern as goals.
  watches() {
    return request<{ watches: WatchState[]; enabled: boolean }>("/api/watches");
  },

  // What a goal or watch can be checked WITH, from every source (ADR 0028/0067). The
  // creators build their verifier pickers from this instead of a hardcoded list — a UI-side
  // copy of a server registry drifts, and this one had already lost the whole `plugin` class.
  verifiers() {
    return request<VerifierCatalog>("/api/verifiers");
  },

  // Operator watch-create. This is the TRUSTED channel (ADR 0066 path ceiling), so unlike
  // the agent's plugin-only `create_watch` it accepts command/test/ci/data verifiers; a
  // rejected spec comes back 400 → `request` throws, which the caller surfaces as a toast.
  // `body` is the raw object — `request` serializes it (and sets the JSON content type).
  // Pre-stringifying here double-encodes it, which the mock server happily accepts and
  // FastAPI's `body: dict` rejects with a 422.
  createWatch(body: WatchCreateBody) {
    return request<{ ok: boolean; message?: string }>("/api/watches", {
      method: "POST",
      body,
    });
  },

  clearWatch(id: string) {
    return request<{ cleared: boolean }>(`/api/watches/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  chatCommands() {
    return request<{ commands: SlashCommand[] }>("/api/chat/commands");
  },

  /** Who the operator can address with `@<name>` (#3042) — the same resolver the chat
   *  dispatcher routes with, so the popover can't offer an unreachable target. */
  chatMentions() {
    return request<{ mentions: MentionTarget[] }>("/api/chat/mentions");
  },

  settingsSchema(host = false) {
    return request<{ groups: SettingsGroup[] }>("/api/settings/schema", { host });
  },

  activity() {
    return request<ActivityHistory>("/api/activity");
  },

  inbox(floor: "now" | "next" | "later" = "later", includeDelivered = false) {
    const q = `?floor=${floor}&include_delivered=${includeDelivered}`;
    return request<{ items: InboxItem[] }>(`/api/inbox${q}`);
  },

  deliverInbox(id: number) {
    return request<{ ok: boolean; delivered: number }>(`/api/inbox/${id}/deliver`, {
      method: "POST",
      body: {},
    });
  },

  // Workflows are an opt-in plugin (plugins/workflows) — it serves /api/plugins/workflows.
  workflows() {
    return request<{ workflows: WorkflowSummary[] }>("/api/plugins/workflows/list");
  },

  runWorkflow(name: string, inputs: Record<string, unknown>) {
    return request<WorkflowRunResult>(`/api/plugins/workflows/${encodeURIComponent(name)}/run`, {
      method: "POST",
      body: { inputs },
    });
  },

  // The Studio's run shape: validated up front (a bad request rejects here), then the
  // DAG executes detached — poll workflowRun(run_id) for the live per-step record.
  startWorkflow(name: string, inputs: Record<string, unknown>) {
    return request<{ started: boolean; run_id: string }>(
      `/api/plugins/workflows/${encodeURIComponent(name)}/start`,
      { method: "POST", body: { inputs } },
    );
  },

  // One run's full record — live polling target while it executes, history inspector after.
  workflowRun(runId: string) {
    return request<WorkflowRunRecord>(`/api/plugins/workflows/runs/${encodeURIComponent(runId)}`);
  },

  // Run history — summaries of every recorded run (any status), newest first.
  workflowRunHistory(limit = 50) {
    return request<{ runs: WorkflowRunSummary[] }>(`/api/plugins/workflows/runs/all?limit=${limit}`);
  },

  // The full recipe document — what the builder loads to EDIT (prompts, gates, output).
  workflowRecipe(name: string) {
    return request<{ recipe: WorkflowRecipe }>(
      `/api/plugins/workflows/${encodeURIComponent(name)}/recipe`,
    );
  },

  // Save's checks as data (never a 400) — the builder's live validation.
  validateWorkflow(recipe: Record<string, unknown>) {
    return request<{ errors: string[] }>("/api/plugins/workflows/validate", {
      method: "POST",
      body: recipe,
    });
  },

  // Resume a paused run detached (the Studio timeline's shape): prechecked up front,
  // then poll workflowRun(run_id) — the sync resumeWorkflow below returns the final
  // result directly and stays for the Pending Gates cards.
  resumeWorkflowBackground(
    runId: string,
    body: { action: "approve" | "edit" | "reject"; edits?: { prompt?: string } },
  ) {
    return request<{ resumed: boolean; run_id: string }>(
      `/api/plugins/workflows/runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body: { ...body, background: true } },
    );
  },

  saveWorkflow(recipe: Record<string, unknown>) {
    return request<{ saved: boolean; name: string; path?: string }>("/api/plugins/workflows/save", {
      method: "POST",
      body: recipe,
    });
  },

  deleteWorkflow(name: string) {
    return request<{ deleted: boolean }>(`/api/plugins/workflows/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
  },

  // Paused workflow runs (F3) — runs parked at a `gate: human` step, awaiting operator
  // approval. The "Pending Gates" section polls this on mount + after each action.
  workflowRuns() {
    return request<{ runs: WorkflowPausedRun[] }>("/api/plugins/workflows/runs");
  },

  // Continue a paused run from its gated step: approve (original prompt), edit
  // (`edits.prompt` runs verbatim), or reject (step marked failed, DAG continues).
  // Resolves with the run's final output (or a paused envelope if a downstream gate hits).
  resumeWorkflow(
    runId: string,
    body: { action: "approve" | "edit" | "reject"; edits?: { prompt?: string } },
  ) {
    return request<WorkflowRunResult>(
      `/api/plugins/workflows/runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body },
    );
  },

  // Save a flat {key: value} payload to a cascade layer (ADR 0047): "agent" (the
  // per-agent leaf, default) or "host" (the box-shared host-config.yaml). Secrets
  // are refused on the host layer server-side.
  saveSettings(
    updates: Record<string, unknown>,
    layer: "agent" | "host" = "agent",
    host = false,
  ) {
    return request<{ ok: boolean; messages: string[]; restart_required: string[] }>("/api/settings", {
      method: "POST",
      body: { updates, layer },
      host,
    });
  },

  // Reset-to-inherited (ADR 0047): pop the given keys from the agent leaf so each
  // falls back to the Host/App layer.
  resetSettings(keys: string[]) {
    return request<{ ok: boolean; messages: string[] }>("/api/settings/reset", {
      method: "POST",
      body: { keys },
    });
  },

  // --- Fleet (ADR 0042) — many workspace agents on one host ------------------
  fleet() {
    return request<FleetStatus>("/api/fleet");
  },
  // Persist the fleet roster's DISPLAY order (ADR 0042 hub control-plane; backend from
  // PR #3200, issue #3197). `order` is the COMPLETE permutation of the live roster
  // (host + local + remote) by immutable FleetAgent.id — stable ids, NEVER an editable
  // name/label, so a rename can't perturb order. The path lives under /api/fleet, so
  // `isHubPath` keeps it HUB-scoped (never slug-routed) like every other fleet control
  // call. The server validates the permutation under its state lock and 400s a duplicate /
  // unknown / missing / malformed list WITHOUT touching the saved order; on success it
  // echoes the applied order, and a subsequent GET /api/fleet returns members in it,
  // reconciled as members are later added or removed.
  reorderFleet(order: string[]) {
    return request<{ ok: boolean; order: string[] }>("/api/fleet/order", {
      method: "PUT",
      body: { order },
    });
  },
  flags() {
    return request<FlagsPayload>("/api/flags");
  },
  discoverAgents() {
    return request<{ discovered: DiscoveredAgent[] }>("/api/fleet/discover");
  },
  archetypes() {
    return request<{ archetypes: Archetype[] }>("/api/archetypes");
  },
  archetypePreview(id: string) {
    return request<ArchetypePreview>(`/api/archetypes/${encodeURIComponent(id)}/preview`);
  },
  createAgent(body: {
    name: string;
    bundle?: string | null;
    soul?: string;
    port?: number;
    start?: boolean;
    shared_skills?: boolean;
    // Operator-supplied bundle-seed values (#2041): `inputs` fill the bundle's MCP
    // `${input}` placeholders (an entry seeds ENABLED when its required inputs are here),
    // `secrets` carry values for the bundle's declared secrets. Omitted → env-only.
    inputs?: Record<string, string>;
    secrets?: { key: string; value: string }[];
    // Answers to the bundle's declared config_inputs prompts (#2934), keyed by dotted
    // config path — written into the member's config at those keys. Omitted → defaults.
    config_inputs?: Record<string, string | boolean>;
    // The picked archetype's capability contract (#2277), persisted to the member's
    // workspace.yaml so it can warn at boot when its toolset doesn't cover the persona.
    requires_tools?: string[];
  }) {
    return request<{ ok: boolean; agent: FleetAgent; installed: string[] }>("/api/fleet", {
      method: "POST",
      body,
    });
  },
  // The fleet control plane resolves an agent by `id` OR display `name`, id first. Callers
  // should pass the **id**: display names are editable — a member can even rename itself from
  // its own Identity panel, where sibling names aren't knowable — so only the id is unique.
  startAgent(ident: string) {
    return request<{ ok: boolean; agent: FleetAgent }>(`/api/fleet/${encodeURIComponent(ident)}/start`, {
      method: "POST",
    });
  },
  stopAgent(ident: string) {
    return request<{ ok: boolean; name: string; stopped: boolean }>(`/api/fleet/${encodeURIComponent(ident)}/stop`, {
      method: "POST",
    });
  },
  /** Pair this hub with a remote protoAgent (ADR 0113 D1): the HUB's server redeems a code
   *  minted on the remote (its Settings ▸ Devices ▸ Pair an agent, or `protoagent pair`) and
   *  stores the per-device token it gets back — the token never reaches this browser. Adds the
   *  member, or re-tokens it in place when that URL is already one (`action: "retokened"` —
   *  the Re-pair path; id, slug and open windows survive). Errors carry the server's `detail`
   *  (400: bad url/name, invalid/expired code; 502: unreachable or not a pairing protoAgent). */
  pairRemote(body: { url: string; code: string; name?: string; allow_insecure?: boolean }) {
    return request<{
      ok: boolean;
      agent: FleetAgent;
      reachable?: boolean;
      version?: string;
      auth?: RemoteAuth;
      action?: "added" | "retokened";
    }>("/api/fleet/remotes/pair", { method: "POST", body });
  },
  addRemoteAgent(body: { name: string; url: string; token?: string; allow_insecure?: boolean }) {
    // Register a remote protoAgent as a SWITCHABLE fleet member (ADR 0042 §I) —
    // it gets a slug window; the hub reverse-proxies its console + A2A. The server
    // probes it at register time and returns `reachable`/`version` so the caller can
    // warn up front (registration is NOT rejected for an unreachable peer — deferred
    // registration is intentional; a peer can come online later).
    return request<{ ok: boolean; agent: FleetAgent; reachable?: boolean; version?: string; auth?: RemoteAuth }>("/api/fleet/remotes", {
      method: "POST",
      body,
    });
  },
  updateRemoteAgent(ident: string, body: { name?: string; url?: string; token?: string; allow_insecure?: boolean }) {
    // Edit a remote member in place (ADR 0042 §I) — omitted fields keep their value;
    // token:"" clears the stored bearer. The id/slug is unchanged, so open windows survive.
    // The server re-probes and returns fresh {reachable, version, auth}. A url on a NEW origin
    // with no token in the same body clears the stored token (it was issued by the old host)
    // and the answer says `token_cleared: true` (ADR 0113).
    return request<{
      ok: boolean;
      agent: FleetAgent;
      reachable?: boolean;
      version?: string;
      auth?: RemoteAuth;
      token_cleared?: boolean;
    }>(
      `/api/fleet/remotes/${encodeURIComponent(ident)}`,
      { method: "PATCH", body },
    );
  },
  removeRemoteAgent(ident: string) {
    return request<{ ok: boolean; id: string; name: string }>(`/api/fleet/remotes/${encodeURIComponent(ident)}`, {
      method: "DELETE",
    });
  },
  renameAgent(ident: string, name: string) {
    // Display rename only — the id (URL slug + data scope) is immutable.
    return request<{ ok: boolean; id: string; name: string }>(`/api/fleet/${encodeURIComponent(ident)}`, {
      method: "PATCH",
      body: { name },
    });
  },
  removeAgent(ident: string, purge = false) {
    return request<{ ok: boolean; name: string; removed: string[] }>(
      `/api/fleet/${encodeURIComponent(ident)}${purge ? "?purge=true" : ""}`,
      { method: "DELETE" },
    );
  },
  activateAgent(ident: string) {
    // #806: ensure-running + keep-N-warm touch (no server-side active pointer since slug routing).
    return request<{ ok: boolean; evicted: string[] }>(`/api/fleet/${encodeURIComponent(ident)}/activate`, {
      method: "POST",
    });
  },
  fleetDown() {
    return request<{ ok: boolean; stopped: string[] }>("/api/fleet/down", { method: "POST" });
  },
  // Fire a one-shot message at a specific fleet member (Fleet Room broadcast). Goes to the
  // member's A2A endpoint through the hub proxy (/agents/<slug>/a2a) — NOT /api/chat —
  // because the streaming A2A turn publishes `turn.usage` to the member's event bus, which
  // is what surfaces "<member> finished a turn" in the activity feed (the non-streaming
  // /api/chat path publishes nothing). Fire-and-forget: the A2A task is durable, so we send
  // the request, then cancel the response stream — the turn keeps running server-side and
  // emits its bus event. `slug` is the member id, "host" for this instance.
  sendToAgent(slug: string, message: string, sessionId?: string): Promise<void> {
    const rpcId = `flr-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const body = {
      jsonrpc: "2.0",
      id: rpcId,
      method: "SendStreamingMessage",
      params: {
        message: {
          role: "ROLE_USER",
          parts: [{ text: message }],
          messageId: rpcId,
          contextId: sessionId ?? `fleet-room-${Date.now()}`,
        },
      },
    };
    return fetch(memberPath(slug, "/a2a"), {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      body: JSON.stringify(body),
    }).then((res) => {
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      void res.body?.cancel().catch(() => {}); // durable task runs on + emits turn.usage
    });
  },

  // Member diagnostics (#3168) — bounded, redacted, read-only reads for an EXPLICITLY chosen
  // member (`slug` = its id, or "host" for this instance), reached through the hub's
  // /agents/<slug>/* proxy regardless of the focused window. The Fleet Room drawer (#3169)
  // drives these; the server owns the caps + redaction, so the client only presents the
  // returned snapshot. Snapshot reads — no live SSE following of the log stream.
  memberDiagnosticsLogs(slug: string, lines?: number): Promise<DiagnosticsLogs> {
    // `lines` is left to the server default when omitted; an out-of-range value is CLAMPED
    // (never a 422) and reported back on `note`, so the drawer can always render an answer.
    const q = typeof lines === "number" ? `?lines=${encodeURIComponent(String(lines))}` : "";
    return memberRequest<DiagnosticsLogs>(slug, `/api/diagnostics/logs${q}`);
  },
  memberDiagnosticsTask(slug: string, taskId: string): Promise<DiagnosticsTask> {
    return memberRequest<DiagnosticsTask>(slug, `/api/diagnostics/tasks/${encodeURIComponent(taskId)}`);
  },

  // Per-agent theme (ADR 0042). The blob is opaque — the DS ThemePanel owns its schema; the
  // server just round-trips JSON. These auto-route to the focused agent via the active prefix
  // (host → /api/theme, peer → /active/api/theme).
  getTheme() {
    return request<{ theme: unknown | null }>("/api/theme");
  },
  saveTheme(theme: unknown) {
    return request<{ ok: boolean }>("/api/theme", { method: "PUT", body: { theme } });
  },
  resetTheme() {
    return request<{ ok: boolean }>("/api/theme", { method: "DELETE" });
  },

  chat(message: string, sessionId: string, model?: string) {
    return request<{ response: string; messages: ChatMessage[] }>("/api/chat", {
      method: "POST",
      body: { message, session_id: sessionId, ...(model ? { model } : {}) },
    });
  },

  // Retire a chat session server-side: purge its checkpoints, optionally
  // harvesting the conversation into knowledge first and/or forgetting what it
  // already wrote to memory (the delete dialog's two opt-in switches, #3493).
  // Callers await this durable commit before dropping the local tab so a failed
  // tombstone write remains visible and retryable.
  deleteChatSession(sessionId: string, harvest = false, forget = false) {
    return request<{ deleted: boolean; harvested: boolean; forgotten?: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}?harvest=${harvest}${forget ? "&forget=true" : ""}`,
      { method: "DELETE" },
    );
  },

  /** Wipe durable history but keep the tab/id reusable. Unlike retirement this
   * deliberately does not tombstone the id, so its next turn can be discovered. */
  clearChatSession(sessionId: string, harvest = false, forget = false) {
    return request<{ deleted: boolean; harvested: boolean; forgotten?: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}?harvest=${harvest}${forget ? "&forget=true" : ""}&retire=false`,
      { method: "DELETE" },
    );
  },

  // Bounded discovery + turn reads for ADR 0104 recovery. The index carries no
  // transcript content; callers fetch turns only for sessions missing locally.
  chatSessions(limit = 50) {
    return request<{ sessions: DurableChatSession[]; reason?: string }>(
      `/api/chat/sessions?limit=${Math.max(1, Math.min(limit, 200))}`,
    );
  },

  chatSessionTurns(sessionId: string, limit = 50) {
    return request<{ turns: DurableChatTurn[]; reason?: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/turns?limit=${Math.max(1, Math.min(limit, 200))}`,
    );
  },

  // Compact a chat session server-side (#1527): archive the raw history into
  // searchable memory, summarize it, and rewrite the LangGraph checkpoint to
  // [summary, recent tail] so the agent keeps context at lower token cost. The
  // checkpoint is the agent's REAL context, so this must be server-side — a
  // client-only trim would leave the agent's context untouched. `refused` (never
  // lossy: nothing could be archived) means the server left the thread intact.
  compactChatSession(sessionId: string) {
    return request<{
      summary: string;
      archived_chunks: number;
      kept: number;
      removed: number;
      archived: boolean;
      refused: boolean;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/compact`, { method: "POST", body: {} });
  },

  // Export a chat session as self-contained Markdown (#2158 P1). Read-only — never
  // touches the checkpoint (unlike compact/rewind). Secrets are scrubbed server-side
  // (graph/export_op) and the kinds found come back in `redactions` so the caller can
  // tell the operator what was removed. `title` names the document heading + file.
  exportChatSession(sessionId: string, title?: string) {
    const q = title ? `?title=${encodeURIComponent(title)}` : "";
    return request<{
      found: boolean;
      markdown: string;
      message_count: number;
      redactions: string[];
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/export${q}`, { method: "GET" });
  },

  // Build the structured chat-bundle for the pre-publish review (#2179 P2, #2682) —
  // read-only, sends nothing anywhere. Behind the `chat.publish` developer flag.
  fetchPublishPreview(sessionId: string, title?: string) {
    const q = title ? `?title=${encodeURIComponent(title)}` : "";
    return request<{
      found: boolean;
      manifest: ChatBundleManifest | null;
      message_count: number;
      redactions: string[];
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/publish/preview${q}`, { method: "GET" });
  },

  // Publish a chat thread to the hosted viewer (#2179 P2, #2683). Rebuilds the bundle
  // server-side fresh — never sends the client-side preview back up — so `published:
  // false` with `reason: "not_configured"` is the expected state until the hosted
  // service (#2685) exists and an operator points `publish.endpoint_url` at it.
  publishChatSession(sessionId: string, title?: string) {
    return request<{
      published: boolean;
      link_id?: string | null;
      public_url?: string;
      revoke_token?: string;
      expires_at?: string | null;
      redactions?: string[];
      artifact_notes?: string[];
      reason?: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/publish`, { method: "POST", body: { title } });
  },

  // Everything this instance has published (#2684) — never includes the revoke token,
  // which stays server-internal (presented to the hosted service by the revoke call
  // below, never sent back to the browser after the initial publish).
  publishedLinks() {
    return request<{ links: PublishedLink[] }>("/api/chat/publish/links");
  },
  // `ok: false` with `reason: "not_configured"` when no revoke endpoint is set — same
  // honest-state shape as publishing itself, not an error status.
  revokePublishedLink(id: string) {
    return request<{ ok: boolean; reason?: string; error?: string }>(
      `/api/chat/publish/links/${encodeURIComponent(id)}/revoke`,
      { method: "POST" },
    );
  },

  // `/btw` (#2180): ask a side question about this session's context WITHOUT changing
  // it. The server runs an incognito turn on a fresh ephemeral thread seeded with the
  // main thread's messages; the main thread's checkpoint is never written. The answer is
  // rendered as an EPHEMERAL client-side note (it never goes back to the server as a real
  // turn), so the side exchange leaves no trace in the conversation.
  asideChatSession(sessionId: string, question: string) {
    return request<{
      found: boolean;
      answer: string;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/aside`, { method: "POST", body: { question } });
  },

  // Rewind a chat session server-side (#1535): discard every message AFTER the
  // target and rewrite the LangGraph checkpoint in place, rolling the agent's live
  // context back to that point. The checkpoint is the agent's REAL context, so this
  // must be server-side — a client-only truncate would leave the agent's memory
  // intact. Intentionally DESTRUCTIVE (no archive) but never corrupting. `content`
  // is the visible bubble's text: the console's client-side message ids never appear
  // in the checkpoint, so the server locates the message by its rendered content.
  // `before: true` = exclusive cut (#2491): the target itself is discarded too —
  // Regenerate rewinds to just before the last user message so its resend REPLACES
  // the turn instead of appending a duplicate pair.
  forkChatSession(sessionId: string, newSessionId: string, messageId: string, content?: string, occurrence?: number) {
    return request<{
      found: boolean;
      kept: number;
      discarded: number;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/fork`, {
      method: "POST",
      body: { new_session_id: newSessionId, message_id: messageId, content, occurrence },
    });
  },
  rewindChatSession(sessionId: string, messageId: string, content?: string, occurrence?: number, before?: boolean) {
    return request<{
      found: boolean;
      kept: number;
      removed: number;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/rewind`, {
      method: "POST",
      body: { message_id: messageId, content, occurrence, before },
    });
  },

  async streamChat(
    message: string,
    sessionId: string,
    handlers: TurnStreamHandlers = {},
    opts: {
      images?: { b64: string; mime: string; name: string }[];
      model?: string;
      reasoningEffort?: string;
      bypassPermissions?: boolean;
      // Incognito thread (ADR 0069 D3b): the flag is PER MESSAGE server-side, so the
      // console stamps it on EVERY send while the thread's toggle is on — a mixed
      // thread would leak earlier incognito content into a later turn's summary.
      incognito?: boolean;
      // This message ANSWERS a pending HITL form/question/approval (#1560): the server
      // resumes the parked interrupt with it (Command(resume=…)) instead of running a
      // fresh turn. Unmarked messages sent while a form is pending are held server-side
      // until the form resolves.
      hitlResume?: boolean;
      // How this message showed in the transcript, when that is not simply its text. The
      // server ignores both; they ride the message into the task's durable history so a
      // chat rebuilt from it (ADR 0104) draws the same user bubble: `hidden` = none (an
      // approval/dismissal resume, a regenerate, a goal kickoff), `display` = the bubble
      // text when the sent text differs from it (attachment context prepended).
      hidden?: boolean;
      display?: string;
      // Stream to a SPECIFIC fleet member (Fleet Room DM) instead of THIS window's agent:
      // the turn runs on that member via the hub proxy (/agents/<slug>/a2a). "host" = this
      // instance. Omitted → normal chat with the focused agent (apiUrl slug-routing).
      agentSlug?: string;
    } = {},
  ) {
    // DM target (opts.agentSlug) streams to that member through the hub proxy; a normal
    // chat routes to THIS window's agent via apiUrl().
    const a2aTarget = opts.agentSlug ? memberPath(opts.agentSlug, "/a2a") : apiUrl("/a2a");
    const chatTarget = opts.agentSlug ? memberPath(opts.agentSlug, "/api/chat") : apiUrl("/api/chat");
    // One A2A SendStreamingMessage body + one frame dispatcher, shared by the desktop
    // (Tauri-relayed) and browser (fetch SSE) paths so both decode turns identically.
    const rpcId = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const buildBody = () => ({
      jsonrpc: "2.0",
      id: rpcId,
      method: "SendStreamingMessage",
      params: {
        message: {
          role: "ROLE_USER",
          parts: [
            { text: message },
            ...(opts.images || []).map((img) => ({ raw: img.b64, mediaType: img.mime, filename: img.name })),
          ],
          messageId: rpcId,
          contextId: sessionId,
          // Per-turn overrides ride the A2A message metadata (server/chat.py reads them):
          // the tab's chosen model + the /effort reasoning level + incognito (ADR 0069 D3b —
          // per-message server-side, stamped on every send while the thread toggle is on).
          // `hidden` / `display` are for the durable transcript only (see opts above).
          ...((opts.model ||
            opts.reasoningEffort ||
            opts.bypassPermissions ||
            opts.incognito ||
            opts.hitlResume ||
            opts.hidden ||
            opts.display !== undefined)
            ? {
                metadata: {
                  ...(opts.model ? { model: opts.model } : {}),
                  ...(opts.reasoningEffort ? { reasoning_effort: opts.reasoningEffort } : {}),
                  ...(opts.bypassPermissions ? { bypass_permissions: true } : {}),
                  ...(opts.incognito ? { incognito: true } : {}),
                  ...(opts.hitlResume ? { hitl_resume: true } : {}),
                  ...(opts.hidden ? { hidden: true } : {}),
                  ...(opts.display !== undefined ? { display: opts.display } : {}),
                },
              }
            : {}),
        },
      },
    });
    const dispatchFrame = makeA2ADispatcher(sessionId, handlers);

    // Desktop: WKWebView can't read a streaming SSE body via fetch, so relay the /a2a
    // SSE through the Tauri shell (Rust reqwest → IPC Channel) and parse frames with the
    // SAME drainSseBuffer + dispatchFrame as the browser — real token-by-token + tool-card
    // streaming. Falls back to the non-streaming `/api/chat` path if the native command
    // is unavailable or fails, so it never regresses below the old render-once behavior.
    if (isDesktopWebview()) {
      try {
        const core = tauriCore();
        if (!core) throw new Error("Tauri core API unavailable (withGlobalTauri off?)");
        const channel = new core.Channel<string>();
        let buf = "";
        channel.onmessage = (chunk) => {
          buf += chunk;
          buf = drainSseBuffer(buf, dispatchFrame);
        };
        const tok = authToken();
        await core.invoke("chat_stream", {
          url: a2aTarget,
          body: buildBody(),
          auth: tok ? `Bearer ${tok}` : null,
          onEvent: channel,
        });
        handlers.onDone?.();
        return;
      } catch (err) {
        console.warn("[desktop] native chat stream failed; falling back to /api/chat:", err);
      }
      try {
        const res = await fetch(chatTarget, {
          method: "POST",
          headers: applyAuth(new Headers({ "Content-Type": "application/json" })),
          signal: handlers.signal,
          body: JSON.stringify({
            message,
            session_id: sessionId,
            ...(opts.model ? { model: opts.model } : {}),
            // The non-streaming fallback must carry incognito too — dropping it here
            // would silently persist a thread the operator marked private.
            ...(opts.incognito ? { incognito: true } : {}),
            // …and hitl_resume (#1560) — dropping it would make the server HOLD the
            // operator's own form answer behind the form it answers (deadlock).
            ...(opts.hitlResume ? { hitl_resume: true } : {}),
          }),
        });
        if (!res.ok) {
          const raw = await res.text().catch(() => "");
          const { detail } = parseErrorBody(raw, `${res.status} ${res.statusText}`);
          handlers.onFailed?.(detail);
          return;
        }
        const data = (await res.json()) as { response?: string };
        const reply = (data.response || "").trim();
        if (reply) handlers.onText?.(reply, false);
        else handlers.onFailed?.("the turn returned no content");
      } catch (err) {
        handlers.onFailed?.(errMsg(err));
      } finally {
        handlers.onDone?.();
      }
      return;
    }

    const response = await fetch(a2aTarget, {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      signal: handlers.signal,
      // A2A 1.0 streaming RPC `SendStreamingMessage`; body built by buildBody()
      // (shared with the desktop path) — ROLE_USER, member-discriminated parts,
      // messageId + contextId, optional image parts + per-tab model metadata.
      body: JSON.stringify(buildBody()),
    });

    if (!response.ok) {
      // token-gated chat turn (#873) — but a member-scoped 401 is the focused remote's bad
      // token, not the hub's, so don't hijack the hub AuthGate (the boot gate owns that).
      // A DM (opts.agentSlug) is member-scoped by construction.
      if (response.status === 401 && !opts.agentSlug && !isMemberScoped("/a2a")) notifyAuthRequired();
      throw new Error(`${response.status} ${response.statusText}`);
    }

    await consumeSse(response, dispatchFrame);
    // The SSE stream closing is the canonical "turn complete" signal in A2A 1.0
    // (terminal-by-state, no `final` flag) — resolve the spinner here.
    handlers.onDone?.();
  },

  cancelTask(taskId: string) {
    // A2A 1.0 (a2a-sdk 1.1): proto method name + the version header — `tasks/cancel`
    // is -32601 Method not found on the live server (same rot class as the eval
    // harness's; the mock now mirrors the 1.0 wire so this can't rot silently again).
    return request<{ result?: unknown; error?: unknown }>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: {
        jsonrpc: "2.0",
        id: `cancel-${Date.now()}`,
        method: "CancelTask",
        params: { id: taskId },
      },
    });
  },

  /** Desktop in-app updater (Tauri). `checkUpdate` returns the available build's
   * version + notes (the changelog from latest.json) or null (up to date / not
   * desktop). Updater failures reject so an explicit tray request can surface them;
   * ambient callers decide whether to stay quiet. */
  async checkUpdate(): Promise<{ version: string; current: string; notes: string } | null> {
    const core = tauriCore();
    if (!core) return null;
    return (await core.invoke<{ version: string; current: string; notes: string } | null>("updater_check")) ?? null;
  },
  /** The LAUNCH check's stored outcome (#2203) — the shell runs one update check in
   * parallel with engine startup; this pulls its result (a state read, no network).
   * `done: false` while the check is still in flight. Null outside the shell or on an
   * older shell without the command — callers must treat null as "no launch check". */
  async launchUpdateResult(): Promise<{
    done: boolean;
    update: { version: string; current: string; notes: string } | null;
  } | null> {
    const core = tauriCore();
    if (!core) return null;
    try {
      return (
        (await core.invoke<{ done: boolean; update: { version: string; current: string; notes: string } | null }>(
          "updater_launch_result",
        )) ?? null
      );
    } catch {
      return null; // older shell without the command — UpdateNotice falls back to its timers
    }
  },
  /** Destructively consume the latest tray request retained by Rust across the
   * webview's boot/listener race. Only the primary `main` window can receive it. */
  async consumeUpdateRequest(): Promise<number | null> {
    const core = tauriCore();
    if (!core) return null;
    try {
      return (await core.invoke<number | null>("updater_consume_request")) ?? null;
    } catch {
      return null; // older shell without the durable request command
    }
  },
  async ackUpdateRequest(requestId: number): Promise<void> {
    const core = tauriCore();
    if (!core) return;
    try {
      await core.invoke("updater_ack_request", { requestId });
    } catch {
      // Older shell: the request may replay after a webview reload, but id de-duping
      // still prevents duplicates within this mount.
    }
  },
  async installUpdate(
    expectedVersion: string,
    onProgress: (e: { chunkLength: number; contentLength: number | null }) => void,
  ): Promise<
    | { status: "superseded"; update: { version: string; current: string; notes: string } }
    | { status: "upToDate" }
  > {
    const core = tauriCore();
    if (!core) throw new Error("Tauri core API unavailable");
    const channel = new core.Channel<{ chunkLength: number; contentLength: number | null }>();
    channel.onmessage = onProgress;
    // Resolves only if install fails — on success the Rust command relaunches the app.
    return await core.invoke<
      | { status: "superseded"; update: { version: string; current: string; notes: string } }
      | { status: "upToDate" }
    >("updater_install", { expectedVersion, onProgress: channel });
  },

  // Mid-turn steering: queue a user message into a RUNNING turn (folded in at the
  // next model call by SteeringMiddleware) without stopping the stream. The client
  // `id` lets the turn-end reconcile tell consumed from arrived-too-late.
  steerChat(sessionId: string, id: string, text: string) {
    return request<{ ok: boolean; id: string | null; pending: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/steer`,
      { method: "POST", body: { id, text } },
    );
  },
  // Items still queued for the session — read at turn-end: anything here arrived
  // after the turn's last model call and wasn't folded in (re-send as a new turn).
  // `drained` names ids a turn actually folded in: absence from `pending` alone can't
  // tell a message the agent READ from one that never arrived (the queue is in-memory,
  // and the live boundary marker is best-effort), and the console must not guess between
  // settling a message the agent never saw and re-offering one it already used. Absent
  // from an older server, which reads as "can't say" rather than "not read".
  pendingSteer(sessionId: string) {
    return request<{ pending: { id: string; text: string }[]; drained?: string[] }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/steer`,
    );
  },
  // Cancel a still-queued steer (the ✕ on a pending bubble) before it folds into
  // the turn. `removed: false` means it was already drained — the agent will act
  // on it, so the caller settles it into the thread rather than dropping it.
  cancelSteer(sessionId: string, id: string) {
    return request<{ removed: boolean; pending: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/steer/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },
  serverTurnInterject(sessionId: string, taskId: string, id: string, text: string) {
    return request<{ ok: boolean; id?: string | null; pending: number; reason?: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/server-turns/${encodeURIComponent(taskId)}/interject`,
      { method: "POST", body: { id, text } },
    );
  },
  // Abort ONE running foreground subagent delegation (the Stop on a running `task`
  // tool card, Tier 2) — cancels just that subagent, NOT the whole turn: the lead
  // continues with a 'cancelled' result. `delegationId` is the `task` tool-call id.
  // `cancelled: false` means it already finished / wasn't running (too late).
  cancelDelegation(sessionId: string, delegationId: string) {
    return request<{ cancelled: boolean; running: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/delegations/${encodeURIComponent(delegationId)}/cancel`,
      { method: "POST" },
    );
  },

  // Reconcile a turn against the server's durable task (A2A GetTask). Used to
  // self-heal a chat message stuck in `streaming` after the stream was
  // interrupted (reload, network blip, a stale tab) — the server task is the
  // source of truth. Returns the normalized state + the final answer text (empty
  // until terminal).
  //
  // A2A 1.0: the method is `GetTask` (+ A2A-Version header) and the unary result
  // is the task FLAT on `result` with TASK_STATE_* states. The old `tasks/get`
  // was Method-not-found against a2a-sdk 1.1 — which made this self-heal finalize
  // a still-running turn instantly with empty state (caught live 2026-06-09).
  async getTask(taskId: string): Promise<{ state: string; text: string }> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: { jsonrpc: "2.0", id: `get-${Date.now()}`, method: "GetTask", params: { id: taskId } },
    });
    const result = res.result;
    const task = (result?.task ?? (result?.kind === "task" ? result : result)) as
      | NonNullable<A2AFrame["result"]>
      | undefined;
    if (!task) return { state: "", text: "" };
    const state = (task.status?.state || "").toString();
    return { state, text: textFromTerminalTask(task) };
  },

  /** A turn's state plus the interjection ids its DURABLE history records as folded in.
   *
   *  The steering queue is in-memory (graph/steering.py), so "no longer queued" cannot tell
   *  a message the agent read from one a restart dropped. The executor's steer-consumed
   *  marker is written into the task's history, which survives both — so this is what lets
   *  the console settle an interjection on proof instead of inference. One GetTask, because
   *  the reconcile needs the state anyway. */
  async taskSteerState(taskId: string): Promise<{ state: string; consumed: string[] }> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: { jsonrpc: "2.0", id: `steer-get-${Date.now()}`, method: "GetTask", params: { id: taskId } },
    });
    const result = res.result;
    const task = (result?.task ?? (result?.kind === "task" ? result : result)) as
      | NonNullable<A2AFrame["result"]>
      | undefined;
    if (!task) return { state: "", consumed: [] };
    const history = ((task as { history?: Array<{ parts?: RawPart[] }> }).history || []) as Array<{
      parts?: RawPart[];
    }>;
    return {
      state: (task.status?.state || "").toString(),
      consumed: history.flatMap((entry) => consumedSteersFromParts(entry.parts) ?? []).map((item) => item.id),
    };
  },

  // Reattach to an IN-FLIGHT turn after an agent switch / reload (Swap & Resume
  // S1): A2A `SubscribeToTask` — served by the backend and forwarded by the
  // fleet proxy all along; the console just never called it. The server replays
  // a Task snapshot first (whose durable history carries everything emitted
  // while nobody was subscribed — replayed via replayTaskSnapshot), then the
  // same live frames SendStreamingMessage emits. Stream close = turn complete.
  // A TERMINAL task is rejected by the server (UnsupportedOperation) — callers
  // catch and fall back to replayTask() below.
  async resumeTask(taskId: string, sessionId: string, handlers: TurnStreamHandlers = {}) {
    const dispatchFrame = makeA2ADispatcher(sessionId, handlers);
    const body = {
      jsonrpc: "2.0",
      id: `resub-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      method: "SubscribeToTask",
      params: { id: taskId },
    };
    // Desktop: same WKWebView limitation as streamChat — relay the SSE through
    // the Tauri shell; any A2A streaming body rides the same command.
    if (isDesktopWebview()) {
      const core = tauriCore();
      if (!core) throw new Error("Tauri core API unavailable");
      const channel = new core.Channel<string>();
      let buf = "";
      channel.onmessage = (chunk) => {
        buf += chunk;
        buf = drainSseBuffer(buf, dispatchFrame);
      };
      const tok = authToken();
      await core.invoke("chat_stream", {
        url: apiUrl("/a2a"),
        body,
        auth: tok ? `Bearer ${tok}` : null,
        onEvent: channel,
      });
      handlers.onDone?.();
      return;
    }
    const response = await fetch(apiUrl("/a2a"), {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      signal: handlers.signal,
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    // A JSON-RPC rejection (e.g. resubscribing a TERMINAL task) comes back as a
    // plain JSON body, not an SSE stream — surfacing it as a throw is what routes
    // the caller onto the snapshot-replay fallback.
    if (!(response.headers.get("content-type") || "").includes("text/event-stream")) {
      const payload = (await response.json().catch(() => null)) as { error?: { message?: string } } | null;
      throw new Error(payload?.error?.message || "resubscribe rejected");
    }
    await consumeSse(response, dispatchFrame);
    handlers.onDone?.();
  },

  // Fetch the durable task and replay its snapshot (accumulated text + history
  // tool/reasoning frames) through the SAME dispatcher the streams use — the
  // catch-up path when the turn already ended while nobody was watching.
  // Returns the task state ("" when the task is gone).
  async replayTask(taskId: string, sessionId: string, handlers: TurnStreamHandlers = {}): Promise<string> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: { jsonrpc: "2.0", id: `get-${Date.now()}`, method: "GetTask", params: { id: taskId } },
    });
    const result = res.result;
    if (!result) return "";
    const task = (result.task ?? (result.kind === "task" ? result : result)) as NonNullable<A2AFrame["result"]>;
    if (!task?.status) return "";
    const dispatch = makeA2ADispatcher(sessionId, handlers);
    // GetTask results aren't context-stamped frames — wrap as a task frame; the
    // dispatcher's foreign-frame guard passes frames without a contextId.
    dispatch({ result: { task } } as A2AFrame);
    return (task.status?.state || "").toString();
  },

  // Tasks are agent-global (one persistent store) — no project scope. (Notes moved
  // to the first-party `notes` plugin, ADR 0034 S4 — it owns its own data route.)
  tasksStatus() {
    return request<{ initialized: boolean }>("/api/tasks/status");
  },

  initTasks() {
    return request<{ initialized: boolean; already_initialized?: boolean }>("/api/tasks/init", {
      method: "POST",
      body: {},
    });
  },

  tasks() {
    return request<{ issues: Task[] }>("/api/tasks/issues");
  },

  createTask(issue: {
    title: string;
    type?: string;
    priority?: number;
    description?: string;
    assignee?: string;
  }) {
    return request<{ issue: Task }>("/api/tasks/issues", {
      method: "POST",
      body: { ...issue },
    });
  },

  updateTask(
    issueId: string,
    update: {
      title?: string;
      description?: string;
      status?: string;
      priority?: number;
      type?: string;
      assignee?: string;
    },
  ) {
    return request<{ issue: Task }>(`/api/tasks/issues/${encodeURIComponent(issueId)}`, {
      method: "PATCH",
      body: { ...update },
    });
  },

  closeTask(issueId: string, reason?: string) {
    return request<{ issue: Task }>(`/api/tasks/issues/${encodeURIComponent(issueId)}/close`, {
      method: "POST",
      body: { reason },
    });
  },

  deleteTask(issueId: string) {
    return request<{ deleted?: string; project_path?: string }>(
      `/api/tasks/issues/${encodeURIComponent(issueId)}`,
      { method: "DELETE" },
    );
  },

  // Delegate registry (ADR 0025) — the agents & endpoints the agent can talk to.
  delegateTypes() {
    return request<{ types: DelegateTypeSpec[] }>("/api/delegate-types");
  },
  // The canonical ACP coding-agent catalog (single source — runtime/acp_agents.py).
  acpAgents() {
    return request<{ agents: AcpAgent[] }>("/api/acp-agents");
  },
  delegates() {
    return request<{ delegates: DelegateView[]; can_share?: boolean }>("/api/delegates");
  },
  // Git-installed plugins (ADR 0027). install fetches code only (does NOT enable).
  installedPlugins() {
    // `bundles` = the lock's bundles[] registry verbatim (#2718) — the authoritative
    // installed-bundle list (a bundle whose members were all removed individually
    // still has a row and is still uninstallable). Optional: absent on older backends.
    // `deps_installing`: the dependency install this server is running (one per environment
    // at a time), so every tab can show it busy. Null when idle; absent on older backends.
    return request<{
      plugins: InstalledPlugin[];
      bundles?: { id: string; name?: string }[];
      deps_installing?: { id: string; target?: string; since?: number } | null;
    }>("/api/plugins/installed");
  },
  // The curated official-plugin directory (Discover, ADR 0059), merged with install
  // state. One-click install posts each entry's `repo` to installPlugin().
  pluginCatalog() {
    return request<{ plugins: CatalogPlugin[] }>("/api/plugins/catalog");
  },
  // Install AUTO-ENABLES + runs the plugin (trust-by-default): `enabled` lists the
  // ids now in plugins.enabled; `reloaded` whether the hot-reload landed; `enable_error`
  // is set if the install succeeded but the enable-reload failed (enable it manually
  // then). `load_errors` (#2716) maps enabled ids that FAILED to import on that reload —
  // in plugins.enabled but not running — optional so older backends parse fine.
  installPlugin(
    url: string,
    ref?: string,
    force?: boolean,
    // Bundle create-time seed values (#2041/#2118/#2934): `inputs` fill the bundle's MCP
    // `${input}` placeholders, `secrets` its declared secrets, `config_inputs` its
    // declared config prompts (written at their dotted config paths) — same body shapes
    // as POST /api/fleet. Omitted → env-only / declared-default seeding.
    seed?: {
      inputs?: Record<string, string>;
      secrets?: { key: string; value: string }[];
      config_inputs?: Record<string, string | boolean>;
    },
  ) {
    return request<{
      installed: PluginInstallSummary;
      enabled: string[];
      reloaded: boolean;
      restart_recommended: boolean;
      enable_error: string | null;
      load_errors?: Record<string, string>;
      // Packages the just-installed plugins still need HERE (install never pips — ADR 0027
      // D4). The console asks once and, on confirm, calls installPluginDeps. Optional so
      // older backends parse fine.
      deps_needed?: PluginDepsNeeded[];
      // Consent gate (ADR 0071 D3, #2721): set INSTEAD of the fields above when the
      // source needs a one-time "this runs code" confirm — nothing was fetched.
      // Ack via ackPluginSource, then retry the install.
      needs_ack?: boolean;
      source?: string;
    }>(
      "/api/plugins/install",
      {
        method: "POST",
        body: {
          url,
          ref: ref || undefined,
          force: force || undefined,
          inputs: seed?.inputs,
          secrets: seed?.secrets,
          config_inputs: seed?.config_inputs,
        },
      },
    );
  },
  // One-time source consent (ADR 0071 D3, #2721): persists the exact normalized repo
  // into plugins.sources.acked (trustAll also flips plugins.trust_unverified — the
  // dialog's "don't ask again"). The caller retries its install afterwards.
  ackPluginSource(url: string, trustAll?: boolean) {
    return request<{ ok: boolean; acked: string | null; trust_all: boolean }>("/api/plugins/ack", {
      method: "POST",
      body: { url, trust_all: trustAll || undefined },
    });
  },
  // Server-side directory listing behind the path pickers. Deliberately the SERVER's
  // filesystem: the console may be configuring a different machine, and the browser's
  // own pickers can't produce an absolute path on it.
  browseDir(opts: { path?: string; files?: boolean; hidden?: boolean } = {}) {
    const qs = new URLSearchParams();
    if (opts.path) qs.set("path", opts.path);
    if (opts.files) qs.set("files", "true");
    if (opts.hidden) qs.set("hidden", "true");
    const q = qs.toString();
    return request<BrowseListing>(`/api/fs/browse${q ? `?${q}` : ""}`);
  },
  // `{project name: absolute root}` for the LIVE fs fence — the same registry the fs tools
  // resolve through (not /api/projects, which the fence can shadow). Backs the tool cards'
  // "open in editor" links, which join a tool's project-relative path onto its root.
  fsRoots() {
    return request<{ roots: Record<string, string> }>("/api/fs/roots");
  },
  // The code pane (ADR 0112): one file's text through the SAME fence read_file uses, and the
  // project's read-only working-tree diff vs HEAD. Both are GETs with no side effects.
  fsFile(project: string, path: string, range: { start?: number; end?: number } = {}) {
    const qs = new URLSearchParams({ project, path });
    if (range.start) qs.set("start", String(range.start));
    if (range.end) qs.set("end", String(range.end));
    return request<FsFile>(`/api/fs/file?${qs.toString()}`);
  },
  fsDiff(project: string) {
    return request<FsDiff>(`/api/fs/diff?${new URLSearchParams({ project }).toString()}`);
  },
  // The artifact plugin's chip metadata (#3617): for each id still in the store, its lifetime
  // version count and the oldest version it still keeps. An evicted/deleted id is absent.
  artifactRefs(ids: string[]) {
    return request<{
      artifacts: Record<string, { title: string; kind: string; version_count: number; oldest: number }>;
    }>(`/api/plugins/artifact/refs?${new URLSearchParams({ ids: ids.join(",") }).toString()}`);
  },
  // "Continue in Zed": offer this chat to the next agent thread the operator starts in Zed
  // under `project`'s root (no project = any folder). The protoagent-acp shim claims it on
  // session/new and continues the same A2A context. 120 s TTL, one-shot, latest wins.
  editorHandoff(body: { session_id: string; project?: string; path?: string; line?: number; title?: string }) {
    return request<{ id: string; expires_at: string; root: string | null }>("/api/editor/handoff", {
      method: "POST",
      body,
    });
  },
  uninstallPlugin(id: string) {
    // `superseded_by_bundled` (the bundled version) = only the ignored old copy of a
    // plugin that now ships with protoAgent was removed; the built-in keeps running.
    return request<{ ok: boolean; superseded_by_bundled?: string; restart_recommended?: boolean }>(
      `/api/plugins/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },
  // Pip-install a plugin's declared requires_pip (the code-exec step `install`
  // deliberately skips) — previously CLI-only.
  installPluginDeps(id: string) {
    // needs_ack (#2743): deps-install re-checks source trust like install does — the
    // caller renders the same confirm dialog and retries after POST /api/plugins/ack.
    // `failed` (#3450): optional deps that didn't install — they fail soft, so an empty
    // `installed` alone can't distinguish "nothing to do" from "everything failed".
    return request<{ ok?: boolean; installed?: string[]; failed?: string[]; refresh?: "none" | "plugin" | "full"; needs_ack?: boolean; source?: string }>("/api/plugins/install-deps", {
      method: "POST",
      body: { id },
    });
  },
  // Run a setup STEP a plugin registered for its setup-gap banner — a `plugin_setup` action's
  // button ("Download the CLI", "Install Chrome"). The host runs only the callable it holds for
  // exactly this (plugin, step); `pending` means it started long work the gap reports on. A core
  // route OUTSIDE /api/plugins/<id>/, which a plugin's manifest may exempt from the auth gate.
  runPluginSetupStep(plugin: string, step: string) {
    return request<{ ok: boolean; message?: string; pending?: boolean }>(
      `/api/plugin-setup/${encodeURIComponent(plugin)}/${encodeURIComponent(step)}`,
      { method: "POST" },
    );
  },
  fsProjects() {
    return request<{ enabled: boolean; projects: FsProject[] }>("/api/settings/filesystem-projects");
  },
  // The ADR 0095 managed-projects registry. Read-only by design: the fs-projects POST
  // above REPLACES `filesystem.projects`, so writing back a registry-derived list here
  // would silently materialize the projection and sever the registry link.
  managedProjects() {
    return request<ManagedProjects>("/api/projects");
  },
  // `replace: true` because this editor genuinely IS a replace-list editor — the form
  // holds every root and removing a row is how you delete one. The server refuses an
  // unacknowledged removal (409) precisely so that callers which DIDN'T mean to replace
  // — a script posting one folder to "add" it — can't strip the fence silently (#2556).
  setFsProjects(projects: FsProject[]) {
    return request<{ ok: boolean; projects: FsProject[]; removed?: FsProject[] }>(
      "/api/settings/filesystem-projects",
      {
        method: "POST",
        body: { projects, replace: true },
      },
    );
  },
  // Per-plugin freshness (ADR 0027). The backend TTL-caches the ls-remote probe,
  // so polling is cheap; each row carries behind/pinned/error. `bundles` (#2718,
  // ADR 0049 D4) is the same status per installed bundle — behind there means the
  // bundle REPO's manifest moved (member pins may move with it on update). Optional
  // so older backends parse fine.
  pluginUpdates() {
    return request<{ plugins: PluginUpdate[]; bundles?: PluginUpdate[] }>("/api/plugins/updates");
  },
  // Bundle-level update (#2718): re-resolves the bundle's ref (release-tag pins move
  // to the newest semver), re-pins every member, retires members the new manifest
  // dropped, hot-reloads. The declared enable set re-applies WITHOUT undoing an
  // operator's explicit disable.
  updateBundle(id: string) {
    return request<{
      installed: PluginInstallSummary;
      enabled: string[];
      reloaded: boolean;
      restart_recommended: boolean;
      enable_error: string | null;
      load_errors: Record<string, string>;
      removed_members: string[];
      retire_error: string | null;
    }>(`/api/plugins/bundles/${encodeURIComponent(id)}/update`, { method: "POST" });
  },
  // One-action bundle removal (#2718): exclusively-owned members + the lock row;
  // members shared with another bundle (or re-installed directly) are kept.
  uninstallBundle(id: string, purge?: boolean) {
    return request<{
      ok: boolean;
      removed_members: string[];
      kept: string[];
      reloaded: boolean;
      reload_error: string | null;
    }>(`/api/plugins/bundles/${encodeURIComponent(id)}${purge ? "?purge=true" : ""}`, { method: "DELETE" });
  },
  // Re-clone every locked plugin that's missing on disk (fresh clone / restored
  // data dir). Fetches at the lock's resolved_sha; already-enabled plugins come
  // up live via the same hot-reload the enable toggle uses. "superseded" = the locked
  // copy's source is retired by a bundled plugin of the same id — nothing to fetch.
  syncPlugins() {
    return request<{
      plugins: { id: string; status: "present" | "installed" | "failed" | "superseded"; error?: string }[];
      reloaded: boolean;
      reload_error: string | null;
    }>("/api/plugins/sync", { method: "POST" });
  },
  // Pull the latest code at the plugin's recorded ref + hot-reload (same path as
  // enable). Returns whether the live reload landed and if a restart is still
  // recommended (a view/route plugin can't swap its mounted router in place).
  updatePlugin(id: string) {
    return request<{ ok: boolean; id: string; version?: string; resolved_sha?: string; reloaded: boolean; restart_recommended: boolean }>(
      `/api/plugins/${encodeURIComponent(id)}/update`,
      { method: "POST" },
    );
  },
  setPluginEnabled(id: string, enabled: boolean) {
    return request<{ deps_missing?: string[]; ok: boolean; enabled: boolean; reloaded: boolean; restart_recommended: boolean }>(
      `/api/plugins/${encodeURIComponent(id)}/enabled`,
      { method: "POST", body: { enabled } },
    );
  },
  addMcpServer(entry: Record<string, unknown>) {
    return request<{ ok: boolean; name: string; servers: string[] }>(
      "/api/mcp/servers",
      { method: "POST", body: entry },
    );
  },
  removeMcpServer(name: string) {
    return request<{ ok: boolean; servers: string[] }>(
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
  },
  importMcpServers(raw: string) {
    return request<{ ok: boolean; added: string[]; servers: string[] }>(
      "/api/mcp/servers/import",
      { method: "POST", body: { raw } },
    );
  },
  mcpCatalog() {
    return request<{ servers: McpCatalogEntry[] }>("/api/mcp/catalog");
  },
  promoteMcpServer(name: string) {
    return request<{ ok: boolean; promoted: boolean; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}/promote`,
      { method: "POST" },
    );
  },
  forgetMcpServer(name: string) {
    return request<{ ok: boolean; forgotten: boolean; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}/forget`,
      { method: "POST" },
    );
  },
  createDelegate(entry: Record<string, unknown>) {
    return request<{ ok: boolean; message: string; delegates: DelegateView[] }>("/api/delegates", {
      method: "POST",
      body: entry,
    });
  },
  updateDelegate(name: string, entry: Record<string, unknown>) {
    return request<{ ok: boolean; message: string; delegates: DelegateView[] }>(
      `/api/delegates/${encodeURIComponent(name)}`,
      { method: "PUT", body: entry },
    );
  },
  deleteDelegate(name: string) {
    return request<{ ok: boolean; message: string; delegates: DelegateView[] }>(
      `/api/delegates/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
  },
  testDelegate(entry: Record<string, unknown>) {
    return request<DelegateProbe>("/api/delegates/test", { method: "POST", body: entry });
  },
};

/** Full report body for the chat report card → document viewer (ADR 0070 D4).
 *
 *  Fetches the job by id (`GET /api/background/{id}` — the only route that carries the
 *  untruncated result). Falls back to the legacy list-and-filter ONLY on a 404: a
 *  pre-ADR-0070 server has no by-id route (its router answers 404), and on a current
 *  server a 404 means the job row was deleted — which the list fallback resolves to the
 *  same "no longer available" placeholder. Any other failure (401/500/network) is real
 *  and propagates so the viewer shows its error state instead of a misleading placeholder. */
export async function loadBackgroundReport(jobId: string): Promise<string> {
  const gone =
    "_The full report is no longer available — it may have been cleared from the Background agents panel._";
  try {
    return (await api.backgroundJob(jobId)).result || gone;
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) throw err;
    // Old server (no by-id route) or deleted row — the list answers both.
    const listed = await api.background().catch(() => null);
    return listed?.jobs.find((j) => j.id === jobId)?.result || gone;
  }
}
