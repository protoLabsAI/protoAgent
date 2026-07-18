import type {
  AcpAgent,
  ActivityHistory,
  AgentConfig,
  Archetype,
  ArchetypePreview,
  BackgroundJobDTO,
  FsProject,
  Task,
  ChatMessage,
  ComponentSpec,
  ConfigPayload,
  ContextWindow,
  DelegateProbe,
  DelegateTypeSpec,
  DelegateView,
  DiscoveredAgent,
  FleetAgent,
  FleetStatus,
  FlagsPayload,
  GoalState,
  HitlPayload,
  InboxItem,
  CatalogPlugin,
  McpCatalogEntry,
  InstalledPlugin,
  PluginInstallSummary,
  PluginUpdate,
  KnowledgeChunk,
  MemoryHotChunk,
  MemoryInjectionDetail,
  MemoryInjectionRow,
  MemorySessionDigest,
  NodeRuntimePayload,
  RuntimeStatus,
  ScheduledJob,
  SecretsStatus,
  SecretsTestResult,
  SetupStatus,
  SettingsGroup,
  SlashCommand,
  SoulVersion,
  Playbook,
  Subagent,
  ToolInfo,
  TelemetryInsights,
  TelemetrySummary,
  TelemetryTurn,
  ToolEvent,
  TurnUsage,
  WatchState,
  WorkflowRunResult,
  WorkflowSummary,
} from "./types";

import { notifyAuthRequired } from "./auth";
import { errMsg } from "./format";

type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  /** Pin to the HUB (never slug-route) — for origin-level reads like the tenant uid
   * that must NOT follow the focused agent. */
  host?: boolean;
};

type A2APart = {
  kind?: string;
  text?: string;
  data?: unknown;
  metadata?: { mimeType?: string };
};
// A Message / Artifact carries an optional `metadata` map — since protolabs-a2a 0.3.0
// that's where the SDK extensions (cost-v1, tool-call-v1) ride, keyed by extension URI.
type A2AMessage = { parts?: A2APart[]; metadata?: Record<string, unknown> };
type A2AArtifact = { parts?: A2APart[]; metadata?: Record<string, unknown> };
type A2AStatus = {
  state?: string;
  message?: A2AMessage;
};
type A2AFrame = {
  jsonrpc?: string;
  id?: string;
  result?: {
    // A2A 1.0 streaming frames nest the payload under task / statusUpdate /
    // artifactUpdate; A2A 0.3 used a flat `kind`-tagged result. We read both.
    task?: {
      id?: string;
      contextId?: string;
      status?: A2AStatus;
    };
    statusUpdate?: {
      taskId?: string;
      contextId?: string;
      status?: A2AStatus;
      final?: boolean;
    };
    artifactUpdate?: {
      taskId?: string;
      contextId?: string;
      artifact?: A2AArtifact;
      append?: boolean;
      lastChunk?: boolean;
    };
    // ── A2A 0.3 (back-compat) ──
    kind?: string;
    id?: string;
    taskId?: string;
    contextId?: string;
    status?: A2AStatus;
    artifact?: A2AArtifact;
    artifacts?: A2AArtifact[];
    append?: boolean;
    lastChunk?: boolean;
    final?: boolean;
  };
  error?: {
    message?: string;
  };
};

/**
 * Defense-in-depth for streaming (follow-up to the subagent-stream-isolation fix #1394).
 *
 * The a2a SDK stamps EVERY frame it emits — `task`, `statusUpdate`, `artifactUpdate` — with
 * the originating `contextId`, and a single console turn streams exactly ONE context (the
 * `sessionId` it sent as the message `contextId`; the server echoes it back unchanged). So a
 * frame carrying a DIFFERENT contextId is cross-talk from a concurrent turn or a detached
 * background job and must never be rendered into this turn's message. Returns true for such a
 * foreign frame. A frame with no contextId (an older server / the A2A 0.3 flat shape that
 * omits it) is never treated as foreign — the guard degrades to a no-op rather than dropping
 * legitimate output.
 */
export function frameIsForeign(frame: A2AFrame, expectedContextId: string): boolean {
  const r = frame.result;
  if (!r) return false;
  const cid = r.task?.contextId ?? r.statusUpdate?.contextId ?? r.artifactUpdate?.contextId ?? r.contextId;
  return !!cid && cid !== expectedContextId;
}

function defaultApiBase() {
  if (typeof window === "undefined") return "";
  let savedBase = "";
  try {
    savedBase = window.localStorage.getItem("protoagent.apiBase") || "";
  } catch {
    savedBase = "";
  }
  if (savedBase) return savedBase.replace(/\/$/, "");

  // The Tauri desktop shell boots its bundled server on a dynamically-chosen
  // free port and hands it to the webview two ways (lib.rs): a `window` global,
  // and `?__apiPort=` on the URL. The URL is always visible to the page (the
  // global sometimes isn't, in which case we'd otherwise fall back to a dead
  // legacy port → "Load failed"). Try the URL first, then the global.
  try {
    const p = new URLSearchParams(window.location.search).get("__apiPort");
    if (p && /^\d+$/.test(p)) return `http://127.0.0.1:${p}`;
  } catch {
    /* no-op */
  }
  const injected = (window as unknown as { __PROTOAGENT_API_BASE__?: string })
    .__PROTOAGENT_API_BASE__;
  if (injected) return injected.replace(/\/$/, "");

  const { hostname, protocol } = window.location;
  if (protocol === "tauri:" || protocol === "file:" || hostname === "tauri.localhost") {
    return "http://127.0.0.1:7870";
  }
  return "";
}

// Fleet slug routing (ADR 0042). The focused agent lives in the URL — /app/agent/<slug>/ —
// so each console window targets its own agent: deterministic, survives reload, and two
// agents can be open in two windows at once. apiUrl() reads that slug and routes agent-level
// calls through the hub's per-agent proxy (/agents/<slug>/api/*). `host` (or no slug) = this
// instance, talking to /api directly. Hub control-plane paths (the fleet itself) are never
// scoped — they're served by the supervisor.
export function currentSlug(): string {
  try {
    const m = window.location.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : "host";
  } catch {
    return "host";
  }
}

/** True when this window is the host console (the un-suffixed root or the reserved
 *  `host` slug) — the only console allowed to edit the box-shared Global defaults
 *  (ADR 0047 §7.7). A workspace console sees those fields read-only. */
export function isHostConsole(): boolean {
  return currentSlug() === "host";
}

/** URL of the console focused on `slug` (for navigation / opening a new window). */
export function agentHref(slug: string): string {
  const base = import.meta.env.BASE_URL || "/"; // "/app/"
  return slug === "host" ? base : `${base}agent/${encodeURIComponent(slug)}/`;
}

/** Boot hook (ADR 0042 slug routing → #806): a window opening `/app/agent/<slug>/` ensures
 * its agent is RUNNING — `POST /api/fleet/<name>/activate` resumes a cold agent from its
 * checkpoint and touches it for keep-N-warm LRU. Every slug navigation is a full page load
 * (FleetSwitcher navigates), so this one boot call covers switch, reload and new-window.
 * Fire-and-forget: the shell's queries already retry through the resume window, and any
 * failure (non-fleet backend, unknown slug) just leaves today's behavior. The slug is the
 * agent's `id`; activate wants its `name` — map via the hub's fleet status. */
export async function activateSlugAgent(): Promise<void> {
  const slug = currentSlug();
  if (slug === "host") return;
  try {
    const fleet = await api.fleet(); // hub control-plane path — never slug-scoped
    const agent = fleet.agents.find((a) => a.id === slug || a.name === slug);
    if (!agent || agent.host) return;
    await api.activateAgent(agent.name);
  } catch {
    // best-effort — the proxy 502s + query retries surface a truly unreachable agent
  }
}

function isHubPath(path: string) {
  // The fleet control plane is served by the supervisor itself — never scoped to an agent.
  return path.startsWith("/api/fleet") || path.startsWith("/api/archetypes");
}
function isAgentPath(path: string) {
  // Everything that drives the focused AGENT: its console API, its A2A brain (streaming chat),
  // its OpenAI-compat endpoint, and its plugin VIEW content. /api/fleet stays on the hub.
  //
  // `/plugins/` is the registry's DEFAULT router prefix — plugin views served there (e.g.
  // agent_browser → /plugins/agent_browser/panel) are the focused agent's, so a fleet member's
  // view must proxy to it. Custom-prefix plugins serve their view at /api/plugins/<id>/… (already
  // covered by the /api/ clause). Without /plugins/ here, a member's default-prefix view iframe
  // hits the hub origin instead of the member → 404 (the agent_browser/project_board panels).
  //
  // `/media/` is the core media store (#1929 `registry.save_media` → `GET /media/<file>?sig=…`)
  // — a media file a member's tool generated lives on the MEMBER, so its console view must
  // proxy there too (#1946). The hub-side proxy is a catch-all (fleet_routes.py
  // `/agents/{slug}/{path:path}`), so no server change is needed.
  return (
    (path.startsWith("/api/") && !isHubPath(path)) ||
    path.startsWith("/plugins/") ||
    path.startsWith("/media/") ||
    path.startsWith("/a2a") ||
    path.startsWith("/v1")
  );
}

export function apiUrl(path: string, opts?: { host?: boolean }) {
  if (/^https?:\/\//.test(path)) return path;
  // Agent-level paths route through the focused agent's proxy, keyed by the URL slug.
  // `opts.host` forces the HUB (no slug routing) — for origin-level reads (the tenant
  // uid) that must stay on the hub regardless of which agent is focused.
  let p = path;
  const slug = currentSlug();
  if (!opts?.host && slug !== "host" && isAgentPath(path)) {
    p = `/agents/${encodeURIComponent(slug)}${path}`;
  }
  const base = defaultApiBase();
  return base ? `${base}${p.startsWith("/") ? p : `/${p}`}` : p;
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

/** Operator bearer token, set in localStorage (`protoagent.authToken`). Sent on
 * every fetch-based API + A2A call so a token-configured deployment's console
 * authenticates against the server guard. Blank ⇒ no header — the default
 * local/desktop case (no token) stays open. (The `/api/events` EventSource is
 * exempt server-side since EventSource can't set headers.) */
export function authToken(): string {
  try {
    return window.localStorage.getItem("protoagent.authToken") || "";
  } catch {
    return "";
  }
}

function applyAuth(headers: Headers): Headers {
  const t = authToken();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  return headers;
}

/** An HTTP error from `request()` that carries the status code, so callers (and the
 *  QueryClient's retry policy) can react to it without parsing the message. */
export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

/** Cold start: the backend isn't answering *yet*, but will be shortly — retry through
 *  it instead of flashing an error. Two shapes:
 *   - HTTP 409 / 502: a just-switched-to fleet agent (the member isn't running yet —
 *     `activate` is still spawning it) or its hub proxy (booting, not bound).
 *   - A fetch that threw before any response (no ApiError status): the LOCAL desktop
 *     sidecar isn't bound to its port yet during the ~12s first-launch boot. WKWebView
 *     surfaces this as `TypeError: Load failed` — which is exactly why the tasks/notes
 *     panels showed "Load failed" and had to be reloaded on a fresh desktop start.
 *  A genuinely-down backend just keeps the panels in their loading state until the
 *  shell's boot-gate ("isn't responding") takes over — same as before. */
export function isColdStart(error: unknown): boolean {
  if (error instanceof ApiError) return error.status === 409 || error.status === 502;
  return true; // no HTTP response at all ⇒ not reachable yet (desktop sidecar booting)
}

/** The fleet proxy's "agent isn't running/registered" signal (ADR 0042): a 409 from a
 *  slug-routed call. Distinct from `isColdStart` (which also rides 502/no-response) — this
 *  is specifically "the focused fleet agent is down", used to offer a return-to-host recovery
 *  once it persists past a normal spawn window instead of the generic "isn't responding" gate. */
export function isAgentNotRunning(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

/** True for a 401 from request() — retrying can't help until the operator supplies
 *  a token (#873); the AuthGate owns recovery. */
export function is401(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

/** The fleet proxy's "can't reach the member" signal (ADR 0042 §I): a 502 from a
 *  slug-routed call. A REMOTE member never 409s (it isn't a local process the hub can find
 *  "not running") — it 502s when its box is offline or its URL is wrong. Distinct from
 *  `isAgentNotRunning` (409) so the boot gate can offer the same return-to-host recovery for a
 *  dead remote that it does for a down local peer. */
export function isAgentUnreachable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 502;
}

/** A request is MEMBER-scoped when it's slug-routed to the focused agent (not the hub). A 401
 *  from one is that member's credential problem — a wrong/missing stored token for a REMOTE —
 *  NOT the hub's, so it must not trip the global AuthGate (which prompts for, and would
 *  overwrite, the HUB token). `host:true` and the host window are always hub-scoped. Exported
 *  for unit testing (it gates whether a 401 reaches `notifyAuthRequired`). */
export function isMemberScoped(path: string, host?: boolean): boolean {
  return !host && currentSlug() !== "host" && isAgentPath(path);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { host, ...init } = options;  // `host` is ours (routing), not a fetch RequestInit field
  const headers = applyAuth(new Headers(init.headers));
  let body: BodyInit | undefined;
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.body);
  }

  const response = await fetch(apiUrl(path, { host }), {
    ...init,
    headers,
    body,
  });

  if (!response.ok) {
    // Read the body ONCE — calling response.json() then response.text() on the same
    // response throws "body stream already read" (a second error that masks the real
    // one). Read text, then best-effort parse a JSON {detail}.
    const raw = await response.text().catch(() => "");
    let detail = `${response.status} ${response.statusText}`;
    try {
      detail = (JSON.parse(raw) as { detail?: string }).detail || raw || detail;
    } catch {
      detail = raw || detail;
    }
    // Wrong/expired/missing bearer on a token-gated deployment — surface the
    // token prompt (#873) instead of leaving per-panel 401 cards as the only signal.
    // But a MEMBER-scoped 401 is the focused remote's bad token, not the hub's — don't
    // hijack the hub AuthGate; the boot gate / fleet panel own that recovery.
    if (response.status === 401 && !isMemberScoped(path, host)) notifyAuthRequired();
    throw new ApiError(response.status, detail || "request failed");
  }

  return (await response.json()) as T;
}

// Multipart sibling of `request` for file uploads (the ingestion engine). Never
// sets Content-Type — the browser adds the multipart boundary itself — but reuses
// the same auth + slug routing + 401 handling.
async function requestForm<T>(path: string, form: FormData, opts: { host?: boolean } = {}): Promise<T> {
  const headers = applyAuth(new Headers());
  const response = await fetch(apiUrl(path, { host: opts.host }), {
    method: "POST",
    headers,
    body: form,
  });
  if (!response.ok) {
    // Read the body ONCE (a Response stream can't be read twice — calling
    // .json() then .text() throws "body stream already read", which masked the
    // real HTTP detail and skipped the 401 AuthGate). Mirror `request`.
    const raw = await response.text().catch(() => "");
    let detail = `${response.status} ${response.statusText}`;
    try {
      detail = (JSON.parse(raw) as { detail?: string }).detail || raw || detail;
    } catch {
      detail = raw || detail;
    }
    if (response.status === 401 && !isMemberScoped(path, opts.host)) notifyAuthRequired();
    throw new ApiError(response.status, detail || "request failed");
  }
  return (await response.json()) as T;
}

export function textFromParts(parts?: Array<{ kind?: string; text?: string }>) {
  return (parts || [])
    .filter((part) => (part.kind === undefined || part.kind === "text") && part.text)
    .map((part) => part.text)
    .join("");
}

/** Does this artifact-update frame APPEND to the artifact (vs REPLACE it)?
 *
 *  The A2A `append` bool has NO wire presence at its default: the SDK serializes
 *  frames with proto3 semantics, so `append=false` is OMITTED from the JSON —
 *  the key is simply absent on every replace frame, including the terminal
 *  last-chunk frame that re-sends the full canonical answer (#1709). Per the
 *  A2A spec an absent/false `append` means REPLACE, so only an explicit `true`
 *  may be read as append — `append !== false` treated the terminal replace as
 *  an append and rendered every streamed answer twice. */
export function artifactAppends(update: { append?: boolean; [key: string]: unknown }): boolean {
  return update.append === true;
}

const HITL_MIME = "application/vnd.protolabs.hitl-v1+json";
const COMPONENT_MIME = "application/vnd.protolabs.component-v1+json";
const REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json";
const CONTEXT_MIME = "application/vnd.protolabs.context-v1+json";

// The two protolabs-a2a SDK extensions we consume ride the message/artifact METADATA
// map keyed by their extension URI (protolabs-a2a 0.3.0) — they are no longer MIME-typed
// DataParts in `parts[]`, so a generic A2A client stops rendering telemetry as content.
// The template-local extensions (hitl / component / reasoning / context) are unaffected
// and stay DataParts.
const TOOL_CALL_EXT_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1";
const COST_EXT_URI = "https://proto-labs.ai/a2a/ext/cost-v1";

type RawPart = {
  kind?: string;
  data?: unknown;
  content?: { $case?: string; value?: unknown };
  metadata?: { mimeType?: string };
};

/** A metadata map as it arrives on a Message or Artifact — extension payloads keyed by URI. */
type ExtMetadata = Record<string, unknown> | undefined;

/** Read an extension payload out of a metadata map by its extension URI. */
function extByUri(metadata: ExtMetadata, uri: string): unknown {
  const value = metadata?.[uri];
  return value && typeof value === "object" ? value : null;
}

/** Read a custom DataPart's payload iff its `metadata.mimeType` matches `mime`.
 *
 * Accepts every encoding the fleet emits: A2A 1.0 member-discriminated
 * (`content.$case === "data"`, payload under `content.value`), 1.0 flattened
 * proto-JSON (top-level `data`), and legacy 0.3 (`kind: "data"` + `data`). The
 * discriminator is always `metadata.mimeType` — `kind` is not required (1.0
 * dropped it), so this keeps matching after the a2a-sdk migration. */
function dataByMime(parts: RawPart[] | undefined, mime: string): unknown {
  const part = (parts || []).find((p) => p.metadata?.mimeType === mime);
  if (!part) return null;
  if (part.content && part.content.$case === "data") return part.content.value ?? null;
  return part.data ?? null;
}

/** Pull a structured tool event off a frame's parts and map the A2A 1.0 wire
 * payload (`{toolCallId, name, phase: "started"|"completed", args, result}`)
 * onto the frontend `ToolEvent` (`{id, name, phase: "start"|"end", input,
 * output}`).
 *
 * The field rename is load-bearing: casting the raw payload straight to
 * `ToolEvent` left `id`/`input`/`output` undefined and `phase` never `"start"`.
 * With `id` undefined, `onToolCall`'s `findIndex(c => c.id === evt.id)` matched
 * the FIRST card on every event, so all of a turn's tool calls collapsed into a
 * single ever-overwriting card — the "only one tool at a time" symptom. */
function toolEventFromMeta(metadata: ExtMetadata): ToolEvent | null {
  const d = extByUri(metadata, TOOL_CALL_EXT_URI) as
    | {
        toolCallId?: string;
        name?: string;
        phase?: string;
        args?: string;
        result?: string;
        error?: string;
        parentToolCallId?: string;
      }
    | null;
  if (!d) return null;
  return {
    id: d.toolCallId || "",
    name: d.name || "",
    phase: d.phase === "started" ? "start" : "end",
    input: d.args,
    // A "failed" end carries the error text in `error`; fall back to it for the body.
    output: d.result ?? d.error,
    error: d.phase === "failed" || Boolean(d.error),
    // Set only for a subagent's own tool calls → nest under the `task` card by id.
    ...(d.parentToolCallId ? { parentId: d.parentToolCallId } : {}),
  };
}

/** Pull the HITL form/question payload off an input-required frame's parts. */
/** Decode a component-v1 DataPart (ADR 0051) → a {component, props} spec, or null. */
export function componentFromParts(parts?: RawPart[]): ComponentSpec | null {
  const d = dataByMime(parts, COMPONENT_MIME) as
    | { component?: string; props?: Record<string, unknown> }
    | undefined;
  if (!d || typeof d.component !== "string") return null;
  return { component: d.component, props: (d.props as Record<string, unknown>) || {} };
}

export function hitlFromParts(parts?: RawPart[]): HitlPayload | null {
  return (dataByMime(parts, HITL_MIME) as HitlPayload) || null;
}

/** Pull a streamed reasoning ("thinking") delta off a working frame's parts. */
function reasoningFromParts(parts?: RawPart[]): string | null {
  const d = dataByMime(parts, REASONING_MIME) as { text?: string } | null;
  return d?.text || null;
}

/** Decode the terminal cost-v1 extension (A2A ext) → this turn's token usage + cost, or null.
 * Read off the artifact's METADATA keyed by the cost-v1 extension URI (protolabs-a2a 0.3.0),
 * not a DataPart. Wire shape: `{ usage: {input_tokens, output_tokens, cache_read_input_tokens,
 * cache_creation_input_tokens}, costUsd?, durationMs? }`. The snake_case `usage` fields are
 * mapped to the camelCase `TurnUsage` the console renders; totalTokens is derived. */
export function costFromMeta(metadata: ExtMetadata): TurnUsage | null {
  const d = extByUri(metadata, COST_EXT_URI) as
    | {
        usage?: {
          input_tokens?: number;
          output_tokens?: number;
          cache_read_input_tokens?: number;
          cache_creation_input_tokens?: number;
        };
        costUsd?: number;
        durationMs?: number;
      }
    | null;
  if (!d || !d.usage) return null;
  const inputTokens = Number(d.usage.input_tokens || 0);
  const outputTokens = Number(d.usage.output_tokens || 0);
  return {
    inputTokens,
    outputTokens,
    totalTokens: inputTokens + outputTokens,
    cacheReadTokens: Number(d.usage.cache_read_input_tokens || 0),
    cacheCreationTokens: Number(d.usage.cache_creation_input_tokens || 0),
    ...(typeof d.costUsd === "number" ? { costUsd: d.costUsd } : {}),
    ...(typeof d.durationMs === "number" ? { durationMs: d.durationMs } : {}),
  };
}

/** Decode the terminal context-v1 DataPart (#1372) → the turn's context-window fill +
 * compaction threshold, or null. `compactionAtTokens` / `maxTokens` are present only when the
 * server could resolve a token denominator (token-based trigger); otherwise the meter shows
 * the raw size. */
export function contextFromParts(parts?: RawPart[]): ContextWindow | null {
  const d = dataByMime(parts, CONTEXT_MIME) as
    | {
        contextTokens?: number;
        compactionAtTokens?: number;
        maxTokens?: number;
        trigger?: string;
        enabled?: boolean;
      }
    | null;
  if (!d || typeof d.contextTokens !== "number") return null;
  return {
    contextTokens: d.contextTokens,
    ...(typeof d.compactionAtTokens === "number" ? { compactionAtTokens: d.compactionAtTokens } : {}),
    ...(typeof d.maxTokens === "number" ? { maxTokens: d.maxTokens } : {}),
    ...(typeof d.trigger === "string" ? { trigger: d.trigger } : {}),
    ...(typeof d.enabled === "boolean" ? { enabled: d.enabled } : {}),
  };
}

function textFromTerminalTask(result: NonNullable<A2AFrame["result"]>) {
  return (result.artifacts || [])
    .flatMap((artifact) => artifact.parts || [])
    .filter((part) => (part.kind === undefined || part.kind === "text") && part.text)
    .map((part) => part.text)
    .join("");
}

// Parse complete SSE events (blank-line-delimited) out of a buffer, dispatching
// each frame. Returns the unconsumed remainder. Shared by the streaming +
// buffered paths so both decode frames identically.
//
// The event boundary is a blank line whose line ending VARIES: the a2a-sdk
// emits CRLF (`\r\n\r\n`); the SSE spec also allows LF (`\n\n`) or CR (`\r\r`).
// Scanning for `\n\n` only — which we used to do — never matched the CRLF
// stream, so the browser parsed zero frames and chat rendered a blank bubble
// (the agent had replied). Match any blank-line boundary, and split data lines
// on any line ending. The regex matches on the raw buffer (not a normalized
// copy), so a boundary split across two fetch chunks still reassembles correctly.
export function drainSseBuffer(buffer: string, onFrame: (frame: A2AFrame) => void): string {
  const BOUNDARY = /\r\n\r\n|\n\n|\r\r/;
  let match = BOUNDARY.exec(buffer);
  while (match) {
    const rawEvent = buffer.slice(0, match.index);
    buffer = buffer.slice(match.index + match[0].length);
    match = BOUNDARY.exec(buffer);

    const data = rawEvent
      .split(/\r\n|\r|\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (data) onFrame(JSON.parse(data) as A2AFrame);
  }
  return buffer;
}

async function consumeBuffered(
  response: Response,
  onFrame: (frame: A2AFrame) => void,
): Promise<void> {
  // Await the whole body, then parse every frame at once. Loses token-by-token
  // streaming but always renders the turn — the fallback for environments that
  // don't expose a readable fetch stream.
  const text = await response.text();
  drainSseBuffer(text.endsWith("\n\n") ? text : `${text}\n\n`, onFrame);
}

async function consumeSse(
  response: Response,
  onFrame: (frame: A2AFrame) => void,
): Promise<void> {
  // WKWebView (the desktop shell) doesn't reliably expose a readable stream on a
  // fetch response — `response.body` can be null, or the reader can throw before
  // the first chunk — which left the desktop chat with NO response at all (the
  // agent replied, but the SSE never rendered). Clone up front so we can fall
  // back to a buffered read (the clone keeps its own body once we lock the
  // original via getReader()).
  let fallback: Response | null = null;
  try {
    fallback = response.clone();
  } catch {
    fallback = null;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    return consumeBuffered(fallback ?? response, onFrame);
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let streamed = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      streamed = true;
      buffer += decoder.decode(value, { stream: true });
      buffer = drainSseBuffer(buffer, onFrame);
    }
  } catch (err) {
    // Reader threw. If we never saw a chunk and have a clone, retry buffered;
    // otherwise a mid-stream failure is real — propagate it.
    if (streamed || !fallback) throw err;
    return consumeBuffered(fallback, onFrame);
  }

  // Reader completed but delivered nothing (WKWebView can hand back a reader
  // that immediately reports `done` without ever surfacing the buffered body) —
  // render via the buffered fallback so the turn isn't silently lost.
  if (!streamed && fallback) {
    return consumeBuffered(fallback, onFrame);
  }
}

export const api = {
  runtimeStatus() {
    return request<RuntimeStatus>("/api/runtime/status");
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

  telemetrySummary(since?: string) {
    const q = since ? `?since=${encodeURIComponent(since)}` : "";
    return request<{ enabled: boolean; summary: TelemetrySummary | null }>(
      `/api/telemetry/summary${q}`,
    );
  },

  telemetryRecent(limit = 50) {
    return request<{ enabled: boolean; turns: TelemetryTurn[] }>(
      `/api/telemetry/recent?limit=${limit}`,
    );
  },

  telemetryInsights() {
    return request<{ enabled: boolean; insights: TelemetryInsights | null }>(
      "/api/telemetry/insights",
    );
  },

  playbooks() {
    return request<{ enabled: boolean; playbooks: Playbook[] }>("/api/playbooks");
  },

  knowledgeSearch(q: string) {
    return request<{
      enabled: boolean;
      query: string;
      results: KnowledgeChunk[];
      stats: Record<string, number>;
    }>(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
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
  memorySessions() {
    return request<{ sessions: MemorySessionDigest[] }>("/api/memory/sessions");
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

  models(apiBase: string, apiKey: string) {
    return request<{ models: string[]; error: string }>("/api/config/models", {
      method: "POST",
      body: { api_base: apiBase, api_key: apiKey },
    });
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
  testModel(apiBase: string, apiKey: string, model: string) {
    return request<{ ok: boolean; error: string }>("/api/config/test-model", {
      method: "POST",
      body: { api_base: apiBase, api_key: apiKey, model },
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


  finishSetup(config: Partial<AgentConfig>, soul: string) {
    return request<{ ok: boolean; message: string }>("/api/config/setup", {
      method: "POST",
      body: { config, soul },
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

  clearGoal(sessionId: string) {
    return request<{ cleared: boolean }>(`/api/goals/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
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
  }) {
    return request<{ ok: boolean; message?: string; error?: string }>("/api/goals", {
      method: "POST",
      body,
    });
  },

  // Watches (ADR 0067) — passive verifier-only objectives, many at once, keyed by id. The
  // panel invalidates this on the `watch.*` bus pushes (created/checked/met/expired/stalled)
  // instead of polling — same pattern as goals.
  watches() {
    return request<{ watches: WatchState[]; enabled: boolean }>("/api/watches");
  },

  clearWatch(id: string) {
    return request<{ cleared: boolean }>(`/api/watches/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  chatCommands() {
    return request<{ commands: SlashCommand[] }>("/api/chat/commands");
  },

  settingsSchema() {
    return request<{ groups: SettingsGroup[] }>("/api/settings/schema");
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

  // Save a flat {key: value} payload to a cascade layer (ADR 0047): "agent" (the
  // per-agent leaf, default) or "host" (the box-shared host-config.yaml). Secrets
  // are refused on the host layer server-side.
  saveSettings(updates: Record<string, unknown>, layer: "agent" | "host" = "agent") {
    return request<{ ok: boolean; messages: string[]; restart_required: string[] }>("/api/settings", {
      method: "POST",
      body: { updates, layer },
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
  }) {
    return request<{ ok: boolean; agent: FleetAgent; installed: string[] }>("/api/fleet", {
      method: "POST",
      body,
    });
  },
  startAgent(name: string) {
    return request<{ ok: boolean; agent: FleetAgent }>(`/api/fleet/${encodeURIComponent(name)}/start`, {
      method: "POST",
    });
  },
  stopAgent(name: string) {
    return request<{ ok: boolean; name: string; stopped: boolean }>(`/api/fleet/${encodeURIComponent(name)}/stop`, {
      method: "POST",
    });
  },
  addRemoteAgent(body: { name: string; url: string; token?: string }) {
    // Register a remote protoAgent as a SWITCHABLE fleet member (ADR 0042 §I) —
    // it gets a slug window; the hub reverse-proxies its console + A2A. The server
    // probes it at register time and returns `reachable`/`version` so the caller can
    // warn up front (registration is NOT rejected for an unreachable peer — deferred
    // registration is intentional; a peer can come online later).
    return request<{ ok: boolean; agent: FleetAgent; reachable?: boolean; version?: string }>("/api/fleet/remotes", {
      method: "POST",
      body,
    });
  },
  updateRemoteAgent(ident: string, body: { name?: string; url?: string; token?: string }) {
    // Edit a remote member in place (ADR 0042 §I) — omitted fields keep their value;
    // token:"" clears the stored bearer. The id/slug is unchanged, so open windows survive.
    // The server re-probes and returns fresh {reachable, version}.
    return request<{ ok: boolean; agent: FleetAgent; reachable?: boolean; version?: string }>(
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
  removeAgent(name: string, purge = false) {
    return request<{ ok: boolean; name: string; removed: string[] }>(
      `/api/fleet/${encodeURIComponent(name)}${purge ? "?purge=true" : ""}`,
      { method: "DELETE" },
    );
  },
  activateAgent(name: string) {
    // #806: ensure-running + keep-N-warm touch (no server-side active pointer since slug routing).
    return request<{ ok: boolean; evicted: string[] }>(`/api/fleet/${encodeURIComponent(name)}/activate`, {
      method: "POST",
    });
  },
  fleetDown() {
    return request<{ ok: boolean; stopped: string[] }>("/api/fleet/down", { method: "POST" });
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
  // harvesting the conversation into knowledge first (the delete dialog's
  // opt-in checkbox). Fire-and-forget on tab delete.
  deleteChatSession(sessionId: string, harvest = false) {
    return request<{ deleted: boolean; harvested: boolean }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}?harvest=${harvest}`,
      { method: "DELETE" },
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

  // Rewind a chat session server-side (#1535): discard every message AFTER the
  // target and rewrite the LangGraph checkpoint in place, rolling the agent's live
  // context back to that point. The checkpoint is the agent's REAL context, so this
  // must be server-side — a client-only truncate would leave the agent's memory
  // intact. Intentionally DESTRUCTIVE (no archive) but never corrupting. `content`
  // is the visible bubble's text: the console's client-side message ids never appear
  // in the checkpoint, so the server locates the message by its rendered content.
  rewindChatSession(sessionId: string, messageId: string, content?: string, occurrence?: number) {
    return request<{
      found: boolean;
      kept: number;
      removed: number;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/rewind`, {
      method: "POST",
      body: { message_id: messageId, content, occurrence },
    });
  },

  async streamChat(
    message: string,
    sessionId: string,
    handlers: {
      signal?: AbortSignal;
      onTaskId?: (taskId: string) => void;
      onStatus?: (status: string) => void;
      onText?: (text: string, append: boolean) => void;
      onReasoning?: (delta: string) => void;
      onToolCall?: (evt: ToolEvent) => void;
      onComponent?: (spec: ComponentSpec) => void;
      // This turn's token usage + cost — lifted off the terminal cost-v1 DataPart.
      onCost?: (usage: TurnUsage) => void;
      // This turn's context-window fill + compaction threshold — terminal context-v1 DataPart.
      onContext?: (ctx: ContextWindow) => void;
      onInputRequired?: (payload: HitlPayload) => void;
      // Terminal failure (A2A `TASK_STATE_FAILED`) — e.g. the model rejected the
      // turn (bad API key → 401). Carries the gateway's error text. Without this
      // the failure only flashed in the transient status line and the turn
      // looked like a silent "no response".
      onFailed?: (message: string) => void;
      onDone?: () => void;
    } = {},
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
    } = {},
  ) {
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
          ...((opts.model || opts.reasoningEffort || opts.bypassPermissions || opts.incognito || opts.hitlResume)
            ? {
                metadata: {
                  ...(opts.model ? { model: opts.model } : {}),
                  ...(opts.reasoningEffort ? { reasoning_effort: opts.reasoningEffort } : {}),
                  ...(opts.bypassPermissions ? { bypass_permissions: true } : {}),
                  ...(opts.incognito ? { incognito: true } : {}),
                  ...(opts.hitlResume ? { hitl_resume: true } : {}),
                },
              }
            : {}),
        },
      },
    });
    const dispatchFrame = (frame: A2AFrame) => {
      if (frame.error?.message) throw new Error(frame.error.message);
      const result = frame.result;
      if (!result) return;
      // Drop any frame stamped with a different contextId than this turn's — cross-talk from
      // a concurrent turn or background job can't leak into this message (see frameIsForeign).
      if (frameIsForeign(frame, sessionId)) return;
      const task = result.task ?? (result.kind === "task" ? result : undefined);
      const statusUpdate = result.statusUpdate ?? (result.kind === "status-update" ? result : undefined);
      const artifactUpdate = result.artifactUpdate ?? (result.kind === "artifact-update" ? result : undefined);
      if (task?.id) {
        handlers.onTaskId?.(task.id);
        const terminalText = textFromTerminalTask(task);
        if (terminalText) handlers.onText?.(terminalText, false);
      }
      if (statusUpdate) {
        const state = statusUpdate.status?.state || "";
        const parts = statusUpdate.status?.message?.parts;
        const messageText = textFromParts(parts);
        const reasoning = reasoningFromParts(parts);
        if (reasoning) handlers.onReasoning?.(reasoning);
        // A reasoning-only frame carries no status text; don't let it clobber the
        // transient status line with the bare working state.
        if (!reasoning) handlers.onStatus?.(messageText || state);
        // tool-call-v1 rides the status MESSAGE's metadata (URI-keyed), not its parts.
        const toolEvent = toolEventFromMeta(statusUpdate.status?.message?.metadata);
        if (toolEvent) handlers.onToolCall?.(toolEvent);
        const component = componentFromParts(parts);
        if (component) handlers.onComponent?.(component);
        if (state === "input-required" || state === "TASK_STATE_INPUT_REQUIRED") {
          handlers.onInputRequired?.(hitlFromParts(parts) || { question: messageText });
        }
        if (state === "failed" || state === "TASK_STATE_FAILED") {
          handlers.onFailed?.(messageText || "the turn failed");
        }
      }
      if (artifactUpdate) {
        const aParts = artifactUpdate.artifact?.parts;
        const text = textFromParts(aParts);
        if (text) handlers.onText?.(text, artifactAppends(artifactUpdate));
        // The terminal answer artifact carries cost-v1 in its URI-keyed METADATA and
        // context-v1 as a DataPart (a2a_impl executor) — surface this turn's spend and
        // its context-window fill.
        const usage = costFromMeta(artifactUpdate.artifact?.metadata);
        if (usage) handlers.onCost?.(usage);
        const ctx = contextFromParts(aParts);
        if (ctx) handlers.onContext?.(ctx);
      }
    };

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
          url: apiUrl("/a2a"),
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
        const res = await fetch(apiUrl("/api/chat"), {
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
          let detail = `${res.status} ${res.statusText}`;
          try {
            const p = (await res.json()) as { detail?: string };
            if (p?.detail) detail = p.detail;
          } catch {
            /* keep status text */
          }
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

    const response = await fetch(apiUrl("/a2a"), {
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
      if (response.status === 401 && !isMemberScoped("/a2a")) notifyAuthRequired();
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
   * desktop / offline). `installUpdate` downloads + installs + relaunches, streaming
   * download progress. Both go through the Rust `updater_*` commands via the Tauri
   * global (withGlobalTauri); they no-op outside the desktop shell. */
  async checkUpdate(): Promise<{ version: string; current: string; notes: string } | null> {
    const core = tauriCore();
    if (!core) return null;
    try {
      return (await core.invoke<{ version: string; current: string; notes: string } | null>("updater_check")) ?? null;
    } catch {
      return null; // not in Tauri / no manifest / offline — stay quiet
    }
  },
  async installUpdate(
    onProgress: (e: { chunkLength: number; contentLength: number | null }) => void,
  ): Promise<void> {
    const core = tauriCore();
    if (!core) throw new Error("Tauri core API unavailable");
    const channel = new core.Channel<{ chunkLength: number; contentLength: number | null }>();
    channel.onmessage = onProgress;
    // Resolves only if install fails — on success the Rust command relaunches the app.
    await core.invoke("updater_install", { onProgress: channel });
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
  pendingSteer(sessionId: string) {
    return request<{ pending: { id: string; text: string }[] }>(
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
    return request<{ delegates: DelegateView[] }>("/api/delegates");
  },
  // Git-installed plugins (ADR 0027). install fetches code only (does NOT enable).
  installedPlugins() {
    return request<{ plugins: InstalledPlugin[] }>("/api/plugins/installed");
  },
  // The curated official-plugin directory (Discover, ADR 0059), merged with install
  // state. One-click install posts each entry's `repo` to installPlugin().
  pluginCatalog() {
    return request<{ plugins: CatalogPlugin[] }>("/api/plugins/catalog");
  },
  // Install AUTO-ENABLES + runs the plugin (trust-by-default): `enabled` lists the
  // ids now live; `reloaded` whether the hot-reload landed; `enable_error` is set if
  // the install succeeded but the enable-reload failed (enable it manually then).
  installPlugin(url: string, ref?: string, force?: boolean) {
    return request<{
      installed: PluginInstallSummary;
      enabled: string[];
      reloaded: boolean;
      restart_recommended: boolean;
      enable_error: string | null;
    }>(
      "/api/plugins/install",
      { method: "POST", body: { url, ref: ref || undefined, force: force || undefined } },
    );
  },
  uninstallPlugin(id: string) {
    return request<{ ok: boolean }>(`/api/plugins/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
  // Pip-install a plugin's declared requires_pip (the code-exec step `install`
  // deliberately skips) — previously CLI-only.
  installPluginDeps(id: string) {
    return request<{ ok: boolean; installed: string[] }>("/api/plugins/install-deps", {
      method: "POST",
      body: { id },
    });
  },
  fsProjects() {
    return request<{ enabled: boolean; projects: FsProject[] }>("/api/settings/filesystem-projects");
  },
  setFsProjects(projects: FsProject[]) {
    return request<{ ok: boolean; projects: FsProject[] }>("/api/settings/filesystem-projects", {
      method: "POST",
      body: { projects },
    });
  },
  // Per-plugin freshness (ADR 0027). The backend TTL-caches the ls-remote probe,
  // so polling is cheap; each row carries behind/pinned/error.
  pluginUpdates() {
    return request<{ plugins: PluginUpdate[] }>("/api/plugins/updates");
  },
  // Re-clone every locked plugin that's missing on disk (fresh clone / restored
  // data dir). Fetches at the lock's resolved_sha; already-enabled plugins come
  // up live via the same hot-reload the enable toggle uses.
  syncPlugins() {
    return request<{
      plugins: { id: string; status: "present" | "installed" | "failed"; error?: string }[];
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
    return request<{ ok: boolean; enabled: boolean; reloaded: boolean; restart_recommended: boolean }>(
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
