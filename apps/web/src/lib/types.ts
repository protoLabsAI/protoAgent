export type RuntimeStatus = {
  setup_complete: boolean;
  graph_loaded: boolean;
  project: {
    path: string;
    allowed_dirs?: string[];
  };
  model: null | {
    provider: string;
    name: string;
    api_base: string;
    api_key_configured: boolean;
    temperature: number | null;
    max_tokens: number | null;
    max_iterations: number | null;
  };
  identity: null | {
    name: string;
    operator: string;
  };
  middleware: Record<string, boolean>;
  knowledge: {
    enabled: boolean;
    configured_path: string | null;
    resolved_path: string | null;
    top_k?: number | null;
  };
  scheduler: {
    enabled: boolean;
    backend: string;
  };
  goal: {
    enabled: boolean;
    controller_loaded: boolean;
    max_iterations?: number | null;
  };
  cache_warmer: {
    enabled: boolean;
    loaded: boolean;
    interval_seconds?: number | null;
  };
  skills?: {
    enabled: boolean;
    count: number;
    top_k?: number | null;
  };
  mcp?: {
    enabled: boolean;
    servers: { name: string; transport: string; tool_count: number }[];
    tool_count: number;
  };
  plugins?: {
    id: string;
    name: string;
    version?: string;
    enabled: boolean;
    loaded: boolean;
    tools: string[];
    skills: number;
    error?: string;
  }[];
};

export type SlashCommand = {
  name: string;
  description: string;
  usage?: string;
};

export type SettingsField = {
  key: string;
  label: string;
  type: "string" | "number" | "bool" | "select" | "string_list" | "secret";
  section: string;
  description?: string;
  restart: boolean;
  options: string[];
  default?: unknown;
  value?: unknown; // absent for secrets
  is_set?: boolean; // secrets only
  minimum?: number;
  maximum?: number;
};

export type SettingsGroup = { section: string; category?: string; fields: SettingsField[] };

export type WorkflowSummary = {
  name: string;
  description: string;
  inputs: { name: string; required: boolean; default?: unknown }[];
  steps: { id: string; subagent: string; depends_on: string[] }[];
};

export type WorkflowRunResult = {
  output: string;
  steps: Record<string, string>;
  failed: string[];
};

export type InboxItem = {
  id: number;
  created_at: string;
  priority: "now" | "next" | "later";
  source: string | null;
  text: string;
  dedup_key: string | null;
  delivered_at: string | null;
};

export type ActivityMessage = { role: "user" | "assistant"; content: string };

export type ActivityHistory = {
  context_id: string;
  messages: ActivityMessage[];
};

export type GoalState = {
  session_id: string;
  condition: string;
  status: string;
  verifier?: { type?: string } & Record<string, unknown>;
  iteration?: number;
  max_iterations?: number;
  last_reason?: string;
  started_at?: number;
  finished_at?: number | null;
};

export type ScheduledJob = {
  id: string;
  prompt: string;
  schedule: string;
  agent_name?: string;
  created_at?: string;
  next_fire?: string | null;
  last_fire?: string | null;
  enabled?: boolean;
};

export type Subagent = {
  name: string;
  description: string;
  enabled: boolean;
  tools: string[];
  default_tools: string[];
  max_turns: number;
  default_max_turns: number;
  allow_skill_emission: boolean;
};

export type ToolCall = {
  id: string;
  name: string;
  input?: string;
  output?: string;
  status: "running" | "done" | "error";
  /** Client wall-clock when the start frame arrived (ms epoch). */
  startedAt?: number;
  /** Elapsed start→end, stamped client-side when the end frame arrives. */
  durationMs?: number;
  /** id of the enclosing `task` tool, if this call ran inside a subagent. */
  parentId?: string;
};

/** Wire shape of a single tool event streamed over the A2A tool-call DataPart. */
export type ToolEvent = {
  id: string;
  name: string;
  phase: "start" | "end";
  input?: string;
  output?: string;
};

export type ChatMessage = {
  id?: string;
  role: "user" | "assistant" | "system";
  content: string;
  toolCalls?: ToolCall[];
  createdAt?: number;
  status?: "streaming" | "done" | "error";
};

// HITL (human-in-the-loop) request surfaced when a turn pauses as input-required
// — a `request_user_input` JSON-schema form (kind "form", multi-step = wizard) or
// an `ask_human` free-text question.
export type HitlFormStep = {
  schema: Record<string, unknown>; // JSON Schema (draft-07) of the step's fields
  uiSchema?: Record<string, unknown>;
  title?: string;
  description?: string;
};
export type HitlPayload = {
  kind?: "form" | "approval";
  title?: string;
  description?: string;
  steps?: HitlFormStep[];
  question?: string; // ask_human shape
  detail?: string; // approval shape — the command/action being approved
};

export type NotesWorkspace = {
  version: number;
  workspaceVersion: number;
  activeTabId: string;
  tabOrder: string[];
  tabs: Record<
    string,
    {
      id: string;
      name: string;
      content: string;
      permissions: {
        agentRead: boolean;
        agentWrite: boolean;
      };
      metadata: Record<string, unknown>;
    }
  >;
};

export type BeadsIssue = {
  id: string;
  title: string;
  status?: string;
  description?: string;
  priority?: number | string;
  issue_type?: string;
  type?: string;
  assignee?: string;
  created_at?: string;
  updated_at?: string;
  closed_at?: string | null;
};

export type AgentConfig = {
  model: {
    provider: string;
    name: string;
    api_base: string;
    api_key?: string;
    temperature: number;
    max_tokens: number;
    max_iterations: number;
  };
  subagents: {
    researcher: {
      enabled: boolean;
      tools: string[];
      max_turns: number;
    };
  };
  middleware: {
    knowledge: boolean;
    audit: boolean;
    memory: boolean;
    scheduler: boolean;
  };
  knowledge: {
    db_path: string;
    embed_model: string;
    top_k: number;
  };
  identity: {
    name: string;
    operator: string;
  };
  auth: {
    token: string;
  };
  discord?: {
    enabled: boolean;
    bot_token?: string;
    admin_ids: string[];
  };
  google?: {
    enabled: boolean;
    client_id: string;
    client_secret?: string;
    tz: string;
  };
  runtime: {
    autostart_on_boot: boolean;
  };
  operator?: {
    allowed_dirs: string[];
  };
};

export type ConfigPayload = {
  config: AgentConfig;
  soul: string;
};

export type SetupStatus = {
  setup_complete: boolean;
  presets: string[];
};

// Telemetry (ADR 0006 Slice 3) — mirrors /api/telemetry/* (telemetry_store.py).
export type TelemetrySummary = {
  turns: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  llm_calls: number;
  tool_calls: number;
  avg_duration_ms: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
  success_rate: number;
  cache_hit_ratio: number;
  by_model: { model: string; turns: number; cost_usd: number; total_tokens: number }[];
};

export type TelemetryTurn = {
  task_id: string;
  session_id: string;
  state: string;
  success: number;
  model: string;
  models?: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  duration_ms: number;
  llm_calls: number;
  tool_calls: number;
  created_at: string;
  ended_at: string;
};

export type TelemetryInsights = {
  turns: number;
  flagged: (TelemetryTurn & { reasons: string[] })[];
  flagged_count: number;
  levers: {
    cache: { hit_ratio: number; read_tokens: number; est_savings_usd: number };
    routing: { by_model: { model: string; turns: number; cost_usd: number; total_tokens: number }[] };
    success_rate: number;
  };
  unproven_levers: string[];
};

// Playbooks (skills surface, ADR 0009) — mirrors /api/playbooks (skills.db).
export type Playbook = {
  id: number;
  name: string;
  description: string;
  tools_used: string[];
  source: string;        // "disk" (pinned SKILL.md) | "emitted" (agent-learned)
  confidence: number;
  last_used: string | null;
  created_at: string | null;
};

// One row from the knowledge store (knowledge/store.py chunks table), as the
// searchable Knowledge → Store view consumes it (ADR 0020).
export type KnowledgeChunk = {
  id: number;
  heading: string;
  content: string;
  preview: string;
  domain: string;
  source: string | null;
  source_type: string | null;
  finding_type: string | null;
  created_at: string | null;
};
