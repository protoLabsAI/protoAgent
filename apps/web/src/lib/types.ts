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

export type ChatMessage = {
  id?: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: number;
  status?: "streaming" | "done" | "error";
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
