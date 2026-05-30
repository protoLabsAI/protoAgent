// Shared fixtures for the operator-console E2E smoke harness.
//
// The mock server (mock-server.mjs) serves these as the backend API + A2A
// stream so Playwright can exercise the real built frontend deterministically
// — no Python, no langgraph, no model, no network. Specs import the same
// constants to assert against, so the contract can't drift between the two.

export const TOOL_CALL_MIME = "application/vnd.protolabs.tool-call-v1+json";

export const RUNTIME_STATUS = {
  setup_complete: true,
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

export const SLASH_COMMANDS = [
  { name: "goal", description: "Set a goal for this session", usage: "/goal <condition>" },
  { name: "clear", description: "Clear the conversation", usage: "/clear" },
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

/**
 * Build the ordered A2A SSE frames for a streamed turn. The scenario is chosen
 * from the user's prompt text so specs can drive different rendering paths:
 *   - contains "OVERFLOW" → a tool whose input is one very long unbroken token
 *   - contains "MARKDOWN" → a rich-markdown final answer
 *   - otherwise           → a web_search tool with JSON input + a short answer
 */
export function buildFrames({ rpcId, contextId, taskId, prompt }) {
  const text = (prompt || "").toUpperCase();
  const wantOverflow = text.includes("OVERFLOW");
  const wantMarkdown = text.includes("MARKDOWN");

  const toolInput = wantOverflow
    ? JSON.stringify({ token: "x".repeat(400) })
    : JSON.stringify({ max_results: 8, query: "AI coding agents latest news" });
  const toolOutput = wantOverflow
    ? "y".repeat(400)
    : "8 result(s) for 'AI coding agents latest news':\n1. Example — https://example.com/a";
  const answer = wantMarkdown ? MARKDOWN_ANSWER : "Done — found 8 results.";

  const wrap = (result) => ({ jsonrpc: "2.0", id: rpcId, result });
  const toolEvent = (phase, extra) => ({
    id: "run-e2e-1",
    name: "web_search",
    phase,
    ...extra,
  });
  const statusWithTool = (stateText, phase, extra) =>
    wrap({
      kind: "status-update",
      taskId,
      contextId,
      status: {
        state: "working",
        message: {
          role: "agent",
          parts: [
            { kind: "text", text: stateText },
            { kind: "data", data: toolEvent(phase, extra), metadata: { mimeType: TOOL_CALL_MIME } },
          ],
        },
      },
      final: false,
    });

  return [
    wrap({ kind: "task", id: taskId, contextId, status: { state: "submitted" }, artifacts: [] }),
    wrap({
      kind: "status-update",
      taskId,
      contextId,
      status: { state: "working", message: { role: "agent", parts: [{ kind: "text", text: "working…" }] } },
      final: false,
    }),
    statusWithTool(`🔧 web_search: ${toolInput}`, "start", { input: toolInput }),
    statusWithTool(`✅ web_search → ${toolOutput}`, "end", { output: toolOutput }),
    wrap({
      kind: "artifact-update",
      taskId,
      contextId,
      artifact: { artifactId: taskId, parts: [{ kind: "text", text: answer }] },
      append: false,
      lastChunk: true,
    }),
    wrap({ kind: "status-update", taskId, contextId, status: { state: "completed" }, final: true }),
  ];
}
