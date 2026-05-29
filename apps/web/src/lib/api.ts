import type {
  AgentConfig,
  BeadsIssue,
  ChatMessage,
  ConfigPayload,
  NotesWorkspace,
  RuntimeStatus,
  SetupStatus,
  Subagent,
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
        parts?: Array<{ kind?: string; text?: string }>;
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

  const { hostname, protocol } = window.location;
  if (protocol === "tauri:" || protocol === "file:" || hostname === "tauri.localhost") {
    return "http://127.0.0.1:7870";
  }
  return "";
}

function apiUrl(path: string) {
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

  chat(message: string, sessionId: string) {
    return request<{ response: string; messages: ChatMessage[] }>("/api/chat", {
      method: "POST",
      body: { message, session_id: sessionId },
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

  getNotes(projectPath: string) {
    const params = new URLSearchParams({ project_path: projectPath });
    return request<{ workspace: NotesWorkspace }>(`/api/notes/workspace?${params}`);
  },

  saveNotes(projectPath: string, workspace: NotesWorkspace) {
    return request<{ ok: boolean }>("/api/notes/workspace", {
      method: "POST",
      body: { project_path: projectPath, workspace },
    });
  },

  beadsStatus(projectPath: string) {
    const params = new URLSearchParams({ project_path: projectPath });
    return request<{ initialized: boolean }>(`/api/beads/status?${params}`);
  },

  initBeads(projectPath: string) {
    return request<{ initialized: boolean; already_initialized?: boolean }>("/api/beads/init", {
      method: "POST",
      body: { project_path: projectPath },
    });
  },

  beadsIssues(projectPath: string) {
    const params = new URLSearchParams({ project_path: projectPath });
    return request<{ issues: BeadsIssue[] }>(`/api/beads/issues?${params}`);
  },

  createIssue(
    projectPath: string,
    issue: {
      title: string;
      type?: string;
      priority?: number;
      description?: string;
      assignee?: string;
    },
  ) {
    return request<{ issue: BeadsIssue }>("/api/beads/issues", {
      method: "POST",
      body: { project_path: projectPath, ...issue },
    });
  },

  updateIssue(
    projectPath: string,
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
      body: { project_path: projectPath, ...update },
    });
  },

  closeIssue(projectPath: string, issueId: string, reason?: string) {
    return request<{ issue: BeadsIssue }>(`/api/beads/issues/${encodeURIComponent(issueId)}/close`, {
      method: "POST",
      body: { project_path: projectPath, reason },
    });
  },

  deleteIssue(projectPath: string, issueId: string) {
    const params = new URLSearchParams({ project_path: projectPath });
    return request<{ deleted?: string; project_path?: string }>(
      `/api/beads/issues/${encodeURIComponent(issueId)}?${params}`,
      { method: "DELETE" },
    );
  },
};
