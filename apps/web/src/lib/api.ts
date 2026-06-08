import type {
  ActivityHistory,
  AgentConfig,
  BeadsIssue,
  ChatMessage,
  ConfigPayload,
  DelegateProbe,
  DelegateTypeSpec,
  DelegateView,
  GoalState,
  HitlPayload,
  InboxItem,
  InstalledPlugin,
  PluginInstallSummary,
  KnowledgeChunk,
  NotesWorkspace,
  RuntimeStatus,
  ScheduledJob,
  SetupStatus,
  SettingsGroup,
  SlashCommand,
  Playbook,
  Subagent,
  ToolInfo,
  TelemetryInsights,
  TelemetrySummary,
  TelemetryTurn,
  ToolEvent,
  WorkflowRunResult,
  WorkflowSummary,
} from "./types";

type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
};

type A2APart = {
  kind?: string;
  text?: string;
  data?: unknown;
  metadata?: { mimeType?: string };
};
type A2AStatus = {
  state?: string;
  message?: { parts?: A2APart[] };
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
      artifact?: { parts?: A2APart[] };
      append?: boolean;
      lastChunk?: boolean;
    };
    // ── A2A 0.3 (back-compat) ──
    kind?: string;
    id?: string;
    taskId?: string;
    contextId?: string;
    status?: A2AStatus;
    artifact?: { parts?: A2APart[] };
    artifacts?: Array<{ parts?: A2APart[] }>;
    append?: boolean;
    lastChunk?: boolean;
    final?: boolean;
  };
  error?: {
    message?: string;
  };
};

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

export function apiUrl(path: string) {
  if (/^https?:\/\//.test(path)) return path;
  const base = defaultApiBase();
  return base ? `${base}${path.startsWith("/") ? path : `/${path}`}` : path;
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

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = applyAuth(new Headers(options.headers));
  let body: BodyInit | undefined;
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }

  const response = await fetch(apiUrl(path), {
    ...options,
    headers,
    body,
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail || detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || "request failed");
  }

  return (await response.json()) as T;
}

function textFromParts(parts?: Array<{ kind?: string; text?: string }>) {
  return (parts || [])
    .filter((part) => (part.kind === undefined || part.kind === "text") && part.text)
    .map((part) => part.text)
    .join("");
}

const TOOL_CALL_MIME = "application/vnd.protolabs.tool-call-v1+json";
const HITL_MIME = "application/vnd.protolabs.hitl-v1+json";

type RawPart = {
  kind?: string;
  data?: unknown;
  content?: { $case?: string; value?: unknown };
  metadata?: { mimeType?: string };
};

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
function toolEventFromParts(parts?: RawPart[]): ToolEvent | null {
  const d = dataByMime(parts, TOOL_CALL_MIME) as
    | { toolCallId?: string; name?: string; phase?: string; args?: string; result?: string }
    | null;
  if (!d) return null;
  return {
    id: d.toolCallId || "",
    name: d.name || "",
    phase: d.phase === "started" ? "start" : "end",
    input: d.args,
    output: d.result,
  };
}

/** Pull the HITL form/question payload off an input-required frame's parts. */
function hitlFromParts(parts?: RawPart[]): HitlPayload | null {
  return (dataByMime(parts, HITL_MIME) as HitlPayload) || null;
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
function drainSseBuffer(buffer: string, onFrame: (frame: A2AFrame) => void): string {
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

  deletePlaybook(id: number) {
    return request<{ enabled: boolean; deleted: boolean; error?: string }>(
      `/api/playbooks/${id}`,
      { method: "DELETE" },
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

  // Verify a Discord bot token by fetching its identity. Blank falls back to the
  // saved token. Returns the bot username on success ("Connected as <bot>").
  testDiscord(botToken: string) {
    return request<{ ok: boolean; error: string; bot_user: string | null }>("/api/config/test-discord", {
      method: "POST",
      body: { bot_token: botToken },
    });
  },

  // Google surface (ADR 0017). status → {configured, connected, email}.
  googleStatus() {
    return request<{ configured: boolean; connected: boolean; email: string | null; error?: string }>(
      "/api/config/google/status",
    );
  },
  // Runs the OAuth consent (opens the operator's browser) — long-lived until they
  // approve. Returns the connected account email on success.
  googleConnect() {
    return request<{ ok: boolean; email?: string; error?: string }>("/api/config/google/connect", {
      method: "POST",
      body: {},
    });
  },

  finishSetup(config: Partial<AgentConfig>, soul: string) {
    return request<{ ok: boolean; message: string }>("/api/config/setup", {
      method: "POST",
      body: { config, soul },
    });
  },

  subagents() {
    return request<{ subagents: Subagent[] }>("/api/subagents");
  },

  tools() {
    return request<{ tools: ToolInfo[]; count: number }>("/api/tools");
  },

  runSubagent(body: {
    session_id: string;
    type: string;
    description: string;
    prompt: string;
    emit_skill: boolean;
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
      emit_skill: boolean;
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

  addSchedule(body: { prompt: string; schedule: string; job_id?: string }) {
    return request<{ job: ScheduledJob }>("/api/scheduler/jobs", {
      method: "POST",
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

  workflows() {
    return request<{ workflows: WorkflowSummary[] }>("/api/workflows");
  },

  runWorkflow(name: string, inputs: Record<string, unknown>) {
    return request<WorkflowRunResult>(`/api/workflows/${encodeURIComponent(name)}/run`, {
      method: "POST",
      body: { inputs },
    });
  },

  saveWorkflow(recipe: Record<string, unknown>) {
    return request<{ saved: boolean; name: string; path?: string }>("/api/workflows", {
      method: "POST",
      body: recipe,
    });
  },

  deleteWorkflow(name: string) {
    return request<{ deleted: boolean }>(`/api/workflows/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
  },

  saveSettings(updates: Record<string, unknown>) {
    return request<{ ok: boolean; messages: string[]; restart_required: string[] }>("/api/settings", {
      method: "POST",
      body: { updates },
    });
  },

  chat(message: string, sessionId: string) {
    return request<{ response: string; messages: ChatMessage[] }>("/api/chat", {
      method: "POST",
      body: { message, session_id: sessionId },
    });
  },

  // Retire a chat session server-side: harvest its history into knowledge (if
  // enabled) then purge its checkpoints. Fire-and-forget on tab delete.
  deleteChatSession(sessionId: string) {
    return request<{ deleted: boolean; harvested: boolean }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  },

  async streamChat(
    message: string,
    sessionId: string,
    handlers: {
      signal?: AbortSignal;
      onTaskId?: (taskId: string) => void;
      onStatus?: (status: string) => void;
      onText?: (text: string, append: boolean) => void;
      onToolCall?: (evt: ToolEvent) => void;
      onInputRequired?: (payload: HitlPayload) => void;
      // Terminal failure (A2A `TASK_STATE_FAILED`) — e.g. the model rejected the
      // turn (bad API key → 401). Carries the gateway's error text. Without this
      // the failure only flashed in the transient status line and the turn
      // looked like a silent "no response".
      onFailed?: (message: string) => void;
      onDone?: () => void;
    } = {},
  ) {
    // Desktop (WKWebView) can't read a streaming SSE body via fetch (see
    // isDesktopWebview) — the turn would render as a blank assistant bubble. Take
    // the non-streaming `/api/chat` path: one request, full reply, render once.
    // No token-by-token streaming or tool-call cards here, but the turn always
    // shows. Browsers fall through to the streaming `/a2a` path below.
    if (isDesktopWebview()) {
      try {
        const res = await fetch(apiUrl("/api/chat"), {
          method: "POST",
          headers: applyAuth(new Headers({ "Content-Type": "application/json" })),
          signal: handlers.signal,
          body: JSON.stringify({ message, session_id: sessionId }),
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
        handlers.onFailed?.(err instanceof Error ? err.message : String(err));
      } finally {
        handlers.onDone?.();
      }
      return;
    }

    const rpcId = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const response = await fetch(apiUrl("/a2a"), {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      signal: handlers.signal,
      // A2A 1.0 (a2a-sdk): the streaming RPC is `SendStreamingMessage` (0.3's
      // `message/stream` is gone → -32601 Method not found, the cause of a
      // never-resolving spinner). Message uses ROLE_USER, member-discriminated
      // parts (`{text}`, not `{kind,text}`), and carries messageId + contextId.
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: rpcId,
        method: "SendStreamingMessage",
        params: {
          message: {
            role: "ROLE_USER",
            parts: [{ text: message }],
            messageId: rpcId,
            contextId: sessionId,
          },
        },
      }),
    });

    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }

    await consumeSse(response, (frame) => {
      if (frame.error?.message) throw new Error(frame.error.message);
      const result = frame.result;
      if (!result) return;

      // A2A 1.0 nests the event (task / statusUpdate / artifactUpdate); fall
      // back to the flat 0.3 `kind`-tagged shape.
      const task = result.task ?? (result.kind === "task" ? result : undefined);
      const statusUpdate =
        result.statusUpdate ?? (result.kind === "status-update" ? result : undefined);
      const artifactUpdate =
        result.artifactUpdate ?? (result.kind === "artifact-update" ? result : undefined);

      if (task?.id) {
        handlers.onTaskId?.(task.id);
        const terminalText = textFromTerminalTask(task);
        if (terminalText) handlers.onText?.(terminalText, false);
      }

      if (statusUpdate) {
        const state = statusUpdate.status?.state || "";
        const parts = statusUpdate.status?.message?.parts;
        const messageText = textFromParts(parts);
        handlers.onStatus?.(messageText || state);
        const toolEvent = toolEventFromParts(parts);
        if (toolEvent) handlers.onToolCall?.(toolEvent);
        // HITL pause: the turn parked awaiting the operator (0.3 `input-required`
        // / 1.0 `TASK_STATE_INPUT_REQUIRED`). Surface the form/question payload.
        if (state === "input-required" || state === "TASK_STATE_INPUT_REQUIRED") {
          handlers.onInputRequired?.(hitlFromParts(parts) || { question: messageText });
        }
        if (state === "failed" || state === "TASK_STATE_FAILED") {
          handlers.onFailed?.(messageText || "the turn failed");
        }
      }

      if (artifactUpdate) {
        const text = textFromParts(artifactUpdate.artifact?.parts);
        if (text) handlers.onText?.(text, artifactUpdate.append !== false);
      }
    });
    // The SSE stream closing is the canonical "turn complete" signal in A2A 1.0
    // (terminal-by-state, no `final` flag) — resolve the spinner here.
    handlers.onDone?.();
  },

  cancelTask(taskId: string) {
    return request<{ result?: unknown; error?: unknown }>("/a2a", {
      method: "POST",
      body: {
        jsonrpc: "2.0",
        id: `cancel-${Date.now()}`,
        method: "tasks/cancel",
        params: { id: taskId },
      },
    });
  },

  // Reconcile a turn against the server's durable task (A2A tasks/get). Used to
  // self-heal a chat message stuck in `streaming` after the stream was
  // interrupted (reload, network blip, a stale tab) — the server task is the
  // source of truth. Returns the normalized state + the final answer text (empty
  // until terminal).
  async getTask(taskId: string): Promise<{ state: string; text: string }> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      body: { jsonrpc: "2.0", id: `get-${Date.now()}`, method: "tasks/get", params: { id: taskId } },
    });
    const result = res.result;
    const task = (result?.task ?? (result?.kind === "task" ? result : result)) as
      | NonNullable<A2AFrame["result"]>
      | undefined;
    if (!task) return { state: "", text: "" };
    const state = (task.status?.state || "").toString();
    return { state, text: textFromTerminalTask(task) };
  },

  // Notes + Beads are agent-global (one persistent store each) — no project
  // scope. The project / allowed-dirs list is purely the filesystem fence.
  getNotes() {
    return request<{ workspace: NotesWorkspace }>("/api/notes/workspace");
  },

  saveNotes(workspace: NotesWorkspace) {
    return request<{ ok: boolean }>("/api/notes/workspace", {
      method: "POST",
      body: { workspace },
    });
  },

  beadsStatus() {
    return request<{ initialized: boolean }>("/api/beads/status");
  },

  initBeads() {
    return request<{ initialized: boolean; already_initialized?: boolean }>("/api/beads/init", {
      method: "POST",
      body: {},
    });
  },

  beadsIssues() {
    return request<{ issues: BeadsIssue[] }>("/api/beads/issues");
  },

  createIssue(issue: {
    title: string;
    type?: string;
    priority?: number;
    description?: string;
    assignee?: string;
  }) {
    return request<{ issue: BeadsIssue }>("/api/beads/issues", {
      method: "POST",
      body: { ...issue },
    });
  },

  updateIssue(
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
    return request<{ issue: BeadsIssue }>(`/api/beads/issues/${encodeURIComponent(issueId)}`, {
      method: "PATCH",
      body: { ...update },
    });
  },

  closeIssue(issueId: string, reason?: string) {
    return request<{ issue: BeadsIssue }>(`/api/beads/issues/${encodeURIComponent(issueId)}/close`, {
      method: "POST",
      body: { reason },
    });
  },

  deleteIssue(issueId: string) {
    return request<{ deleted?: string; project_path?: string }>(
      `/api/beads/issues/${encodeURIComponent(issueId)}`,
      { method: "DELETE" },
    );
  },

  // Delegate registry (ADR 0025) — the agents & endpoints the agent can talk to.
  delegateTypes() {
    return request<{ types: DelegateTypeSpec[] }>("/api/delegate-types");
  },
  delegates() {
    return request<{ delegates: DelegateView[] }>("/api/delegates");
  },
  // Git-installed plugins (ADR 0027). install fetches code only (does NOT enable).
  installedPlugins() {
    return request<{ plugins: InstalledPlugin[] }>("/api/plugins/installed");
  },
  installPlugin(url: string, ref?: string, force?: boolean) {
    return request<{ installed: PluginInstallSummary; restart_required: boolean }>(
      "/api/plugins/install",
      { method: "POST", body: { url, ref: ref || undefined, force: force || undefined } },
    );
  },
  uninstallPlugin(id: string) {
    return request<{ ok: boolean }>(`/api/plugins/${encodeURIComponent(id)}`, { method: "DELETE" });
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
