// Shared fixtures for the operator-console E2E smoke harness.
//
// The mock server (mock-server.mjs) serves these as the backend API + A2A
// stream so Playwright can exercise the real built frontend deterministically
// — no Python, no langgraph, no model, no network. Specs import the same
// constants to assert against, so the contract can't drift between the two.

export const TOOL_CALL_MIME = "application/vnd.protolabs.tool-call-v1+json";

export const RUNTIME_STATUS = {
  setup_complete: true,
  instance_uid: "mock-uid-1", // TenantGuard keys per-origin client state on this
  graph_loaded: true,
  project: { path: "/tmp/e2e-project", allowed_dirs: ["/tmp/e2e-project"] },
  model: {
    provider: "openai",
    name: "protolabs/reasoning",
    api_base: "https://api.proto-labs.ai/v1",
    api_key_configured: true,
    temperature: 0.2,
    max_tokens: 2048,
    max_iterations: 8,
  },
  identity: { name: "protoAgent", operator: "e2e" },
  middleware: { knowledge: true, audit: true, memory: false, scheduler: true },
  knowledge: { enabled: true, configured_path: "/tmp/k.db", resolved_path: "/tmp/k.db", top_k: 5 },
  scheduler: { enabled: true, backend: "local" },
  goal: { enabled: true, controller_loaded: true, max_iterations: 6 },
  cache_warmer: { enabled: false, loaded: false, interval_seconds: null },
  // Surfaced in the Runtime panel — the extensibility features.
  skills: { enabled: true, count: 3, top_k: 4 },
  mcp: {
    enabled: true,
    servers: [{ name: "echo", transport: "stdio", tool_count: 2 }],
    tool_count: 2,
  },
  plugins: [
    { id: "demo", name: "Demo Plugin", version: "1.0.0", enabled: true, loaded: true, tools: ["demo_tool"], skills: 1 },
    { id: "zzz-off", name: "Zzz Disabled", version: "0.1.0", enabled: false, loaded: false, tools: [], skills: 0 },
    // Workflows is an opt-in plugin (lean core); enabled here so the gated Studio surface shows.
    { id: "workflows", name: "Workflows", version: "0.1.0", enabled: true, loaded: true, tools: ["run_workflow", "save_workflow"], skills: 0 },
    // A plugin that contributes a console view (ADR 0026) → a dynamic rail icon + iframe.
    {
      id: "boardy", name: "Boardy", version: "0.1.0", enabled: true, loaded: true, tools: [], skills: 0,
      views: [
        {
          id: "board", label: "Board", icon: "LayoutDashboard", path: "/plugins/boardy/board",
          tabs: [
            { id: "open", label: "Open", path: "/plugins/boardy/board?tab=open" },
            { id: "done", label: "Done", path: "/plugins/boardy/board?tab=done" },
          ],
        },
        { id: "stats", label: "Stats", icon: "BarChart3", path: "/plugins/boardy/stats" },
        // A right-rail panel (ADR 0026 placement:"right") — sits with Notes/Beads/Goals.
        { id: "scratch", label: "Scratch", icon: "FileText", path: "/plugins/boardy/scratch", placement: "right" },
      ],
    },
  ],
};

export const SUBAGENTS = [
  {
    name: "researcher",
    description: "Researches a topic and reports findings",
    enabled: true,
    tools: ["web_search", "fetch_url"],
    default_tools: ["web_search", "fetch_url"],
    max_turns: 6,
    default_max_turns: 6,
    allow_skill_emission: true,
  },
];

export const ACTIVITY_HISTORY = {
  context_id: "system:activity",
  entries: [
    { id: 1, created_at: "2026-06-05T09:00:00+00:00", origin: "scheduler", trigger: "daily-brief", priority: "", state: "completed", text: "3 PRs merged overnight, CI green.", task_id: "t1" },
    { id: 2, created_at: "2026-06-05T09:05:00+00:00", origin: "inbox", trigger: "ci", priority: "now", state: "completed", text: "Build failed on main — investigating.", task_id: "t2" },
  ],
  messages: [
    { role: "user", content: "morning standup" },
    { role: "assistant", content: "3 PRs merged overnight, CI green." },
  ],
};

export const INBOX_ITEMS = {
  items: [
    { id: 1, created_at: "2026-05-30T09:00:00+00:00", priority: "now", source: "ci", text: "build failed on main", dedup_key: null, delivered_at: null },
    { id: 2, created_at: "2026-05-30T09:05:00+00:00", priority: "next", source: "webhook", text: "new signup: acme.co", dedup_key: null, delivered_at: null },
  ],
};

// Fleet (ADR 0042) — mutable so create/start/stop/remove round-trip in e2e. The host (this
// instance) self-registers (host:true); `active: null` = the host is focused.
export const FLEET = {
  agents: [
    { name: "main", id: "main", port: 7871, pid: 3000, running: true, bundle: "", host: true },
    { name: "ava", id: "ava", port: 7890, pid: 4001, running: true, bundle: "" },
    { name: "roxy", id: "roxy", port: 7891, pid: null, running: false, bundle: "pm-stack" },
  ],
  active: null,
};

export const ARCHETYPES = [
  { id: "basic", label: "Basic", icon: "Sparkles", blurb: "A plain agent — add tools later.", bundle: null },
  { id: "pm-stack", label: "Project Manager", icon: "LayoutGrid", blurb: "PM tools + board.", bundle: "https://github.com/x/pm-stack" },
];

export const WORKFLOWS = [
  {
    name: "research-and-brief",
    description: "Research a topic, then write a brief.",
    inputs: [
      { name: "topic", required: true },
      { name: "depth", required: false, default: "deep" },
    ],
    steps: [
      { id: "gather", subagent: "researcher", depends_on: [] },
      { id: "brief", subagent: "researcher", depends_on: ["gather"] },
    ],
  },
];

export const WORKFLOW_RUN_RESULT = {
  output: "## Brief on AI\n\nKey findings…",
  steps: {
    gather: "raw research notes",
    brief: "## Brief on AI\n\nKey findings…",
  },
  failed: [],
};

export const SLASH_COMMANDS = [
  { name: "goal", description: "Set a goal for this session", usage: "/goal <condition>" },
  { name: "clear", description: "Clear the conversation", usage: "/clear" },
  // Workflows surface as slash commands too (ADR 0002) — server lists them.
  { name: "research-and-brief", description: "Research a topic, then write a brief.", usage: "/research-and-brief <topic> [depth]" },
];

export const SCHEDULER_JOBS = {
  backend: "local",
  jobs: [
    {
      id: "job-1",
      prompt: "Summarize overnight activity",
      schedule: "0 9 * * *",
      agent_name: "protoAgent",
      enabled: true,
      next_fire: "2026-05-30T09:00:00Z",
    },
  ],
};

export const GOALS = {
  enabled: true,
  goals: [
    {
      session_id: "operator-default",
      condition: "All tests pass",
      status: "in_progress",
      iteration: 1,
      max_iterations: 6,
    },
  ],
};

export const NOTES_WORKSPACE = {
  version: 1,
  workspaceVersion: 1,
  activeTabId: "tab-1",
  tabOrder: ["tab-1"],
  tabs: {
    "tab-1": {
      id: "tab-1",
      name: "Notes",
      content: "e2e note",
      permissions: { agentRead: true, agentWrite: true },
      metadata: {},
    },
  },
};

// Settings schema the Settings surface renders. Exercises every input type
// plus a restart-flagged field.
// Each field carries the ADR 0047 cascade tags: `scope` (where a "save to default"
// edit is written — "agent" leaf or the box-shared "host" file) and `source` (where
// the live value came from — "agent" set / "host" inherited / "default" App default).
export const SETTINGS_SCHEMA = [
  {
    section: "Model",
    category: "Agent",
    fields: [
      // model.name is host-scoped + inherited from the host layer (inheritance badge).
      { key: "model.name", label: "Primary model", type: "select", section: "Model", restart: false, description: "", options: ["protolabs/reasoning", "protolabs/fast"], value: "protolabs/reasoning", default: "protolabs/reasoning", scope: "host", source: "host" },
      // host-scoped but overridden in this agent (overridden-here badge + reset link).
      { key: "model.temperature", label: "Temperature", type: "number", section: "Model", restart: false, description: "", options: [], value: 0.2, default: 0.2, minimum: 0, maximum: 2, scope: "host", source: "agent" },
      { key: "model.api_key", label: "API key", type: "secret", section: "Model", restart: false, description: "Stored in secrets.yaml.", options: [], value: "", is_set: true, scope: "agent", source: "agent" },
    ],
  },
  {
    section: "Routing",
    category: "Agent",
    fields: [
      // App default (inherited-from-default badge).
      { key: "routing.aux_model", label: "Auxiliary (fast) model", type: "string", section: "Routing", restart: false, description: "Cheap alias for aux calls.", options: [], value: "protolabs/fast", default: "", scope: "host", source: "default" },
      // a plain per-agent setting — no badge.
      { key: "routing.fallback_models", label: "Fallback models", type: "string_list", section: "Routing", restart: false, description: "", options: [], value: [], default: [], scope: "agent", source: "agent" },
    ],
  },
  {
    section: "Compaction",
    category: "System",
    fields: [
      { key: "compaction.enabled", label: "Enable compaction", type: "bool", section: "Compaction", restart: false, description: "", options: [], value: true, default: true, scope: "host", source: "host" },
    ],
  },
  {
    section: "Runtime",
    category: "System",
    fields: [
      { key: "runtime.autostart_on_boot", label: "Autostart on boot", type: "bool", section: "Runtime", restart: true, description: "Install/remove the boot LaunchAgent.", options: [], value: false, default: false, scope: "agent", source: "agent" },
    ],
  },
];

/** restart_required for a flat updates payload, per the schema. */
export function settingsRestartRequired(updates) {
  const flagged = new Set();
  for (const g of SETTINGS_SCHEMA) for (const f of g.fields) if (f.restart) flagged.add(f.key);
  return Object.keys(updates || {}).filter((k) => flagged.has(k));
}

const MARKDOWN_ANSWER = [
  "## Summary",
  "",
  "Here are the **key** findings:",
  "",
  "- First point",
  "- Second point",
  "",
  "```js",
  "const x = 1;",
  "```",
].join("\n");

const DEFAULT_SEARCH_OUTPUT = [
  "8 result(s) for 'AI coding agents latest news':",
  "1. First Result — https://example.com/a",
  "   A snippet about coding agents.",
  "2. Second Result — https://example.com/b",
  "   Another snippet.",
].join("\n");

// Map a prompt keyword to a tool scenario so specs drive each renderer path.
// Each scenario's input is an object (rendered as key/value fields) and output
// matches the real starter-tool string format the per-tool renderer expects.
function scenarioFor(prompt) {
  const t = (prompt || "").toUpperCase();
  if (t.includes("CALC"))
    return { name: "calculator", input: { expression: "19 * 23" }, output: "19 * 23 = 437", answer: "19 × 23 = 437." };
  if (t.includes("TIME"))
    return {
      name: "current_time",
      input: { timezone: "Asia/Tokyo" },
      output: "2026-05-29T21:00:00+09:00 (Asia/Tokyo)\nHuman: Thursday, May 29 2026, 21:00:00 JST",
      answer: "It is 21:00 in Tokyo.",
    };
  if (t.includes("FETCH"))
    return {
      name: "fetch_url",
      input: { url: "https://example.com" },
      output: "[200] https://example.com\n\nExample Domain. This domain is for use in examples.",
      answer: "Fetched example.com.",
    };
  if (t.includes("SUBAGENT"))
    return {
      answer: "Delegated research to a subagent and summarized.",
      // Explicit nested order: the child web_search starts while the `task`
      // tool is still running, so the client groups it under the task.
      events: [
        { id: "task-1", name: "task", phase: "start", input: JSON.stringify({ description: "Research coding agents", prompt: "Find the major coding agents." }) },
        { id: "child-1", name: "web_search", phase: "start", input: JSON.stringify({ query: "coding agents" }) },
        { id: "child-1", name: "web_search", phase: "end", output: "1 result(s) for 'coding agents':\n1. Example — https://example.com/x\n   A snippet." },
        { id: "task-1", name: "task", phase: "end", output: "Subagent finished: found 1 result." },
      ],
    };
  if (t.includes("NOEND"))
    // A tool whose `end` frame never arrives (e.g. a workflow card whose end
    // races with the terminal `done`). The turn still completes — the client
    // must flip the lingering card running→done, not spin forever.
    return {
      answer: "Workflow finished.",
      events: [
        { id: "wf-1", name: "workflow:research-and-brief", phase: "start", input: JSON.stringify({ topic: "quantum computing" }) },
      ],
    };
  if (t.includes("TOOLERR"))
    return { name: "web_search", input: { query: "x" }, output: "Error: DuckDuckGo search failed: rate limited", answer: "Search failed." };
  if (t.includes("OVERFLOW"))
    return { name: "web_search", input: { token: "x".repeat(400) }, output: "y".repeat(400), answer: "done" };
  if (t.includes("STREAM"))
    return {
      name: "web_search",
      input: { query: "stream" },
      output: DEFAULT_SEARCH_OUTPUT,
      // Streamed token-by-token as append:true deltas, then reconciled by the
      // terminal append:false frame (mirrors the real output-streaming path).
      streamChunks: ["Testing ", "catches bugs ", "before users do."],
      answer: "Testing catches bugs before users do.",
    };
  if (t.includes("MARKDOWN"))
    return { name: "web_search", input: { query: "md" }, output: DEFAULT_SEARCH_OUTPUT, answer: MARKDOWN_ANSWER };
  return { name: "web_search", input: { max_results: 8, query: "AI coding agents latest news" }, output: DEFAULT_SEARCH_OUTPUT, answer: "Done — found 8 results." };
}

/**
 * Build the ordered A2A SSE frames for a streamed turn. The tool scenario is
 * chosen from the prompt text (see scenarioFor) so specs can exercise every
 * tool-value renderer + the overflow/markdown paths off one mock server.
 */
export function buildFrames({ rpcId, contextId, taskId, prompt }) {
  const scenario = scenarioFor(prompt);
  const wrap = (result) => ({ jsonrpc: "2.0", id: rpcId, result });

  // A status-update frame carrying optional text + a tool-call DataPart.
  // Emit the REAL a2a-sdk tool-call-v1 wire payload, not the frontend ToolEvent
  // shape. The backend sends `{toolCallId, name, phase: "started"|"completed",
  // args, result}`; the fixtures author events in the readable frontend shape
  // (`{id, phase: "start"|"end", input, output}`), so map here. (An LF-style gap
  // — fixtures in the frontend shape — is exactly what hid the client's field-
  // mapping bug from CI; the mock must mirror the wire shape so the e2e guards it.)
  const toWire = (ev) => ({
    toolCallId: ev.id,
    name: ev.name,
    phase: ev.phase === "start" ? "started" : "completed",
    args: ev.input,
    result: ev.output,
  });
  const statusFrame = (text, toolEvent) =>
    wrap({
      kind: "status-update",
      taskId,
      contextId,
      status: {
        state: "working",
        message: {
          role: "agent",
          parts: [
            { kind: "text", text },
            ...(toolEvent ? [{ kind: "data", data: toWire(toolEvent), metadata: { mimeType: TOOL_CALL_MIME } }] : []),
          ],
        },
      },
      final: false,
    });

  // Single-tool scenarios synthesize a start/end pair; multi-tool scenarios
  // (e.g. SUBAGENT) provide their own ordered event list.
  const toolEvents =
    scenario.events || [
      { id: "run-e2e-1", name: scenario.name, phase: "start", input: JSON.stringify(scenario.input) },
      { id: "run-e2e-1", name: scenario.name, phase: "end", output: scenario.output },
    ];

  const frames = [
    wrap({ kind: "task", id: taskId, contextId, status: { state: "submitted" }, artifacts: [] }),
    statusFrame("working…", null),
  ];
  for (const ev of toolEvents) {
    const text = ev.phase === "start" ? `🔧 ${ev.name}: ${ev.input ?? ""}` : `✅ ${ev.name} → ${ev.output ?? ""}`;
    frames.push(statusFrame(text, ev));
  }
  // Stream the answer as append:true deltas when the scenario asks for it,
  // then always send the authoritative append:false terminal artifact.
  for (const chunk of scenario.streamChunks || []) {
    frames.push(
      wrap({
        kind: "artifact-update",
        taskId,
        contextId,
        artifact: { artifactId: taskId, parts: [{ kind: "text", text: chunk }] },
        append: true,
        lastChunk: false,
      }),
    );
  }
  frames.push(
    wrap({
      kind: "artifact-update",
      taskId,
      contextId,
      artifact: { artifactId: taskId, parts: [{ kind: "text", text: scenario.answer }] },
      append: false,
      lastChunk: true,
    }),
    wrap({ kind: "status-update", taskId, contextId, status: { state: "completed" }, final: true }),
  );
  return frames;
}

// Telemetry fixtures (ADR 0006 Slice 3) — shape mirrors /api/telemetry/*.
export const TELEMETRY_SUMMARY = {
  turns: 3,
  input_tokens: 12000,
  output_tokens: 1800,
  total_tokens: 13800,
  cache_read_input_tokens: 7200,
  cache_creation_input_tokens: 0,
  cost_usd: 0.2154,
  llm_calls: 7,
  tool_calls: 4,
  avg_duration_ms: 4200,
  p50_duration_ms: 3800,
  p95_duration_ms: 8100,
  success_rate: 0.6667,
  cache_hit_ratio: 0.6,
  by_model: [
    { model: "claude-opus-4-8", turns: 2, cost_usd: 0.21, total_tokens: 12000 },
    { model: "claude-haiku-4-5", turns: 1, cost_usd: 0.0054, total_tokens: 1800 },
  ],
};

export const TELEMETRY_TURNS = [
  {
    task_id: "task-3", session_id: "s1", state: "completed", success: 1,
    model: "claude-opus-4-8", input_tokens: 6000, output_tokens: 900, total_tokens: 6900,
    cache_read_input_tokens: 3600, cache_creation_input_tokens: 0, cost_usd: 0.12,
    duration_ms: 8100, llm_calls: 3, tool_calls: 2,
    created_at: "2026-06-01T05:00:00+00:00", ended_at: "2026-06-01T05:00:08+00:00",
  },
  {
    task_id: "task-2", session_id: "s1", state: "failed", success: 0,
    model: "claude-haiku-4-5", input_tokens: 1800, output_tokens: 0, total_tokens: 1800,
    cache_read_input_tokens: 0, cache_creation_input_tokens: 0, cost_usd: 0.0054,
    duration_ms: 700, llm_calls: 1, tool_calls: 0,
    created_at: "2026-06-01T04:59:00+00:00", ended_at: "2026-06-01T04:59:01+00:00",
  },
];

export const TELEMETRY_INSIGHTS = {
  turns: 3,
  flagged_count: 1,
  flagged: [
    {
      ...TELEMETRY_TURNS[0],
      reasons: ["cost 0.12 ≥ 5× median 0.012", "latency 8100ms ≥ 5× median 700ms"],
    },
  ],
  levers: {
    cache: { hit_ratio: 0.6, read_tokens: 7200, est_savings_usd: 0.0972 },
    routing: {
      by_model: [
        { model: "claude-opus-4-8", turns: 2, cost_usd: 0.21, total_tokens: 12000 },
      ],
    },
    success_rate: 0.6667,
  },
  unproven_levers: [],
};

// Playbooks fixtures (ADR 0009) — shape mirrors /api/playbooks (skills.db).
export const PLAYBOOKS = [
  {
    id: 1, name: "web-research", description: "Plan → search → read → synthesize → cite.",
    tools_used: ["web_search", "fetch_url"], source: "disk", confidence: 1.0,
    last_used: "2026-06-01T18:00:00+00:00", created_at: "2026-05-29T00:00:00+00:00",
  },
  {
    id: 2, name: "pr-triage-flow", description: "Learned: how to triage a PR review backlog.",
    tools_used: ["github_get_pr", "github_list_issues"], source: "emitted", confidence: 0.62,
    last_used: "2026-05-31T12:00:00+00:00", created_at: "2026-05-30T00:00:00+00:00",
  },
];

// Knowledge → Store (ADR 0020): chunks from the knowledge base (findings, notes).
export const KNOWLEDGE_CHUNKS = [
  {
    id: 11, heading: "Release cadence", content: "Releases are cut manually via workflow_dispatch.",
    preview: "Release cadence: Releases are cut manually via workflow_dispatch.",
    domain: "process", source: "daily-log", source_type: "log", finding_type: "fact",
    created_at: "2026-06-03T12:00:00+00:00",
  },
  {
    id: 12, heading: "", content: "The gateway alias for the live model is protolabs/reasoning.",
    preview: "The gateway alias for the live model is protolabs/reasoning.",
    domain: "general", source: null, source_type: null, finding_type: null,
    created_at: "2026-06-02T09:00:00+00:00",
  },
];

// Delegate registry (ADR 0025) — types schema + a sample roster for the panel.
export const DELEGATE_TYPES = {
  types: [
    {
      type: "a2a", label: "A2A agent", blurb: "A fleet peer over the A2A protocol.",
      fields: [
        { key: "url", label: "URL", kind: "text", required: true, help: "", placeholder: "https://peer/a2a", options: [], default: null },
        { key: "auth.scheme", label: "Auth scheme", kind: "select", required: false, help: "", placeholder: "", options: ["", "bearer", "apiKey"], default: null },
        { key: "auth.token", label: "Auth token", kind: "secret", required: false, help: "Stored in secrets.yaml.", placeholder: "", options: [], default: null },
      ],
    },
    {
      type: "openai", label: "Model endpoint", blurb: "Ask another OpenAI-compatible model.",
      fields: [
        { key: "url", label: "Base URL", kind: "text", required: true, help: "", placeholder: "https://api/v1", options: [], default: null },
        { key: "model", label: "Model", kind: "text", required: true, help: "", placeholder: "protolabs/reasoning", options: [], default: null },
        { key: "api_key", label: "API key", kind: "secret", required: false, help: "", placeholder: "", options: [], default: null },
      ],
    },
    {
      type: "acp", label: "Coding agent (ACP)", blurb: "A CLI coding agent over ACP.",
      fields: [
        { key: "command", label: "Command", kind: "text", required: true, help: "", placeholder: "proto", options: [], default: null },
        { key: "args", label: "Args", kind: "args", required: false, help: "", placeholder: "--acp", options: [], default: null },
        { key: "workdir", label: "Workdir", kind: "path", required: true, help: "", placeholder: "~/dev/repo", options: [], default: null },
      ],
    },
  ],
};

export const DELEGATES = {
  delegates: [
    {
      name: "opus", type: "openai", description: "Heavy reasoning model.",
      configured: true, error: null, has_secret: true,
      url: "https://api.proto-labs.ai/v1", model: "protolabs/reasoning",
      health: { ok: true, latency_ms: 42, detail: "endpoint reachable" },
    },
  ],
};
