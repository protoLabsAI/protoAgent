import type {
  ActivityHistory,
  AgentConfig,
  BeadsIssue,
  ChatMessage,
  ConfigPayload,
  GoalState,
  HitlPayload,
  InboxItem,
  NotesWorkspace,
  RuntimeStatus,
  ScheduledJob,
  SetupStatus,
  SettingsGroup,
  SlashCommand,
  Playbook,
  Subagent,
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

type A2AFrame = {
  jsonrpc?: string;
  id?: string;
  result?: {
    kind?: string;
    id?: string;
    taskId?: string;
    contextId?: string;
    status?: {
      state?: string;
      message?: {
        parts?: Array<{
          kind?: string;
          text?: string;
          data?: unknown;
          metadata?: { mimeType?: string };
        }>;
      };
    };
    artifact?: {
      parts?: Array<{ kind?: string; text?: string }>;
    };
    artifacts?: Array<{
      parts?: Array<{ kind?: string; text?: string }>;
    }>;
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
  // free port and injects the base URL here before any page script runs
  // (apps/desktop/src-tauri/src/lib.rs). Prefer it over the legacy fixed port.
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

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
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

/** Pull a structured tool event ({id, name, phase, input|output}) off a frame's parts. */
function toolEventFromParts(parts?: RawPart[]): ToolEvent | null {
  return (dataByMime(parts, TOOL_CALL_MIME) as ToolEvent) || null;
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

async function consumeSse(
  response: Response,
  onFrame: (frame: A2AFrame) => void,
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("streaming response has no body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");

      const data = rawEvent
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) continue;
      onFrame(JSON.parse(data) as A2AFrame);
    }
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

  finishSetup(config: Partial<AgentConfig>, soul: string) {
    return request<{ ok: boolean; message: string }>("/api/config/setup", {
      method: "POST",
      body: { config, soul },
    });
  },

  subagents() {
    return request<{ subagents: Subagent[] }>("/api/subagents");
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
      onDone?: () => void;
    } = {},
  ) {
    const rpcId = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const response = await fetch(apiUrl("/a2a"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: handlers.signal,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: rpcId,
        method: "message/stream",
        params: {
          contextId: sessionId,
          message: {
            role: "user",
            parts: [{ kind: "text", text: message }],
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

      if (result.kind === "task" && result.id) {
        handlers.onTaskId?.(result.id);
        const terminalText = textFromTerminalTask(result);
        if (terminalText) handlers.onText?.(terminalText, false);
      }

      if (result.kind === "status-update") {
        const state = result.status?.state || "";
        const messageText = textFromParts(result.status?.message?.parts);
        handlers.onStatus?.(messageText || state);
        const toolEvent = toolEventFromParts(result.status?.message?.parts);
        if (toolEvent) handlers.onToolCall?.(toolEvent);
        // HITL pause: the turn parked awaiting the operator. Surface the form /
        // question payload so the console can render it (falls back to the text).
        if (state === "input-required") {
          handlers.onInputRequired?.(
            hitlFromParts(result.status?.message?.parts) || { question: messageText },
          );
        }
        if (result.final) handlers.onDone?.();
      }

      if (result.kind === "artifact-update") {
        const text = textFromParts(result.artifact?.parts);
        if (text) handlers.onText?.(text, result.append !== false);
        if (result.lastChunk) handlers.onDone?.();
      }
    });
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
};
