export type RuntimeStatus = {
  setup_complete: boolean;
  graph_loaded: boolean;
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
};

export type ConfigPayload = {
  config: AgentConfig;
  soul: string;
};

export type SetupStatus = {
  setup_complete: boolean;
  presets: string[];
};
