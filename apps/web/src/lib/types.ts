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
    org?: string;
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
  storage?: {
    knowledge_bytes?: number | null;
    telemetry_bytes?: number | null;
    checkpoint_bytes?: number | null;
    skills_bytes?: number | null;
    telemetry_retention_days?: number | null;
  };
  /** User-facing operational alerts (e.g. a live co-located instance sharing
   *  this data root, #706) — the shell banners them under the topbar. */
  warnings?: string[];
  /** Stable per-data-root uid — the TenantGuard keys per-origin client state on it
   *  (a different backend reusing this address must not render this one's chats). */
  instance_uid?: string;
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
    // Console surfaces (ADR 0026): rail views the plugin contributes.
    views?: PluginView[];
  }[];
};

// A plugin-contributed console surface (ADR 0026): a rail icon opening an iframe
// of `path` (served by the plugin), with optional sub-tabs.
export type PluginView = {
  id: string;
  label: string;
  icon?: string; // a lucide-react icon name
  path: string;
  tabs?: { id: string; label: string; path: string }[];
  // "rail" (default) = a left-rail surface; "right" = a right-sidebar panel
  // alongside Notes/Beads/Goals/Schedule (ADR 0026).
  placement?: "rail" | "right";
  // Claim a core surface slot instead of adding a rail icon (ADR 0045). A view with
  // slot:"chat" REPLACES the built-in chat panel — it renders under the core "chat"
  // rail id, stays mounted for the app's lifetime (streaming continuity, #613), and
  // does not get its own rail entry. First enabled claimant wins.
  slot?: "chat";
  // Owning plugin's load state, stamped on by App so the view host can surface a real,
  // actionable error (loaded=false ⇒ the view route isn't serving — missing env / bad
  // deps / mount race; `pluginError` is the loader's exact diagnostic) instead of a
  // blank panel. Optional: present only for runtime-status-sourced views.
  pluginLoaded?: boolean;
  pluginError?: string;
};

// A git-installed plugin (ADR 0027) — a plugins.lock entry enriched with its
// manifest + enabled state for the console Plugins panel.
export type InstalledPlugin = {
  id: string;
  source_url: string;
  requested_ref: string;
  resolved_sha: string;
  installed_at?: string;
  by?: string;
  present: boolean;
  enabled: boolean;
  manifest?: {
    name: string;
    version: string;
    description: string;
    repository?: string;
    homepage?: string;
    capabilities?: Record<string, unknown>;
    requires_env?: string[];
    requires_pip?: string[];
    views?: string[];
    secrets?: string[];
  };
};

// The summary returned right after installing (the review card).
export type PluginInstallSummary = {
  id: string;
  name: string;
  version: string;
  description: string;
  resolved_sha: string;
  source_url: string;
  requires_pip: string[];
  capabilities: Record<string, unknown>;
  contributes: { views: string[]; secrets: string[] };
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
  // Cascade layer this field's shared default lives at (ADR 0047): "agent" (the
  // per-agent leaf) or "host" (the box-shared host-config.yaml). Where the
  // Settings UI writes a "save to default" edit.
  scope: "agent" | "host";
  // Which cascade layer the live value came from: set in the agent leaf, inherited
  // from the host file, or the App (dataclass) default. Drives the
  // inherited-vs-overridden badge + the "reset to inherited" affordance.
  source: "agent" | "host" | "default";
};

export type SettingsGroup = {
  section: string;
  category?: string;
  fields: SettingsField[];
  test?: { endpoint: string };  // ADR 0029 — generic "Test connection" button
};

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

// One provenance feed entry (ADR 0022): an agent-initiated turn + what triggered it.
export type ActivityEntry = {
  id: number;
  created_at: string;
  origin: string;        // scheduler | inbox | webhook | a2a | operator
  trigger: string;       // job id / inbox source (human label), may be ""
  priority: string;      // inbox tier when applicable, else ""
  state: string;
  text: string;
  task_id: string;
};

export type ActivityHistory = {
  context_id: string;
  entries: ActivityEntry[];
  messages: ActivityMessage[];
};

export type GoalState = {
  session_id: string;
  condition: string;
  status: string;
  verifier?: { type?: string } & Record<string, unknown>;
  mode?: "drive" | "monitor";
  iteration?: number;
  max_iterations?: number;
  last_reason?: string;
  last_evidence?: string;
  last_checked?: number | null; // monitor: last out-of-band verifier check (epoch seconds)
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

// A live wired tool (Agent → Tools): its source (core/plugin/mcp) + the subsystem
// category it's grouped under in the console.
export type ToolInfo = {
  name: string;
  description: string;
  source: "core" | "plugin" | "mcp";
  category?: string;
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
  /** A2A task id for this turn — persisted so a stuck `streaming` message can be
   *  reconciled against the server's task state on reload (self-heal). */
  taskId?: string;
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
  // Where turns run: "native" (the built-in LangGraph loop on the model gateway) or
  // "acp:<agent>" (hand each turn to a CLI coding agent over ACP — ADR 0033).
  agent_runtime?: string;
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
  plugins?: {
    enabled?: string[];
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

// Delegate registry (ADR 0025) — the agents & endpoints the agent can talk to.
export type DelegateFieldSpec = {
  key: string;
  label: string;
  kind: string; // text | secret | args | path | number | textarea | select
  required: boolean;
  help: string;
  placeholder: string;
  options: string[];
  default?: unknown;
};
export type DelegateTypeSpec = { type: string; label: string; blurb: string; fields: DelegateFieldSpec[] };
export type DelegateProbe = { ok: boolean | null; latency_ms?: number; error?: string; detail?: string; checked_at?: number };
export type DelegateView = {
  name: string;
  type: string;
  description: string;
  configured: boolean;
  error: string | null;
  has_secret: boolean;
  health?: DelegateProbe;
  [key: string]: unknown;
};

// Fleet (ADR 0042) — many workspace agents on one host, switchable in place.
export type FleetAgent = {
  name: string; // also the instance id; unique, [A-Za-z0-9-_]
  id: string;
  port: number;
  pid: number | null; // null when stopped
  running: boolean;
  bundle: string; // "" for a Basic agent
  a2a?: string; // the agent's own A2A endpoint (focus-independent)
  host?: boolean; // the instance serving this console — can't be stopped/removed from itself
  remote?: boolean; // a REMOTE member (ADR 0042 §I) — proxied by URL, no start/stop from here
  url?: string; // the remote member's base URL
  // App version (pyproject [project].version). Always set on the host (hub) entry;
  // for a remote member it's the last-probed value ("" until the first probe lands).
  // The console flags hub↔remote skew — the proxied /api/* surface has no other versioning.
  version?: string;
};

// The focused agent is the URL slug now (ADR 0042 slug routing) — no server-side 'active'.
export type FleetStatus = { agents: FleetAgent[] };

// Another protoAgent found on the box / LAN (ADR 0042 §I) — a candidate remote delegate.
export type DiscoveredAgent = { name: string; url: string; host: string; port: number };

export type Archetype = {
  id: string; // "basic", or a bundle id e.g. "pm-stack"
  label: string;
  icon: string; // lucide-react icon name
  blurb: string;
  bundle: string | null; // null = Basic; else the bundle git URL
};
