// Deterministic mock backend for the operator-console E2E harness.
//
// Serves the built SPA (apps/web/dist, base "/app/") AND the subset of the
// operator API + the A2A stream that the console calls — with canned data from
// fixtures.mjs. This lets Playwright drive the *real* compiled frontend with
// zero Python / langgraph / model / network, so the rendering contract (tool
// cards, markdown, slash commands, runtime panel) is tested in isolation.
//
// Run: node e2e/mock-server.mjs [port]   (defaults to 4319)

import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACTIVITY_HISTORY,
  ARCHETYPES,
  buildFrames,
  DELEGATES,
  DELEGATE_TYPES,
  FLEET,
  GOALS,
  INBOX_ITEMS,
  NOTES_WORKSPACE,
  RUNTIME_STATUS,
  SCHEDULER_JOBS,
  SETTINGS_SCHEMA,
  GATEWAY_MODELS,
  settingsRestartRequired,
  SLASH_COMMANDS,
  PLAYBOOKS,
  KNOWLEDGE_CHUNKS,
  SUBAGENTS,
  TELEMETRY_INSIGHTS,
  TELEMETRY_SUMMARY,
  TELEMETRY_TURNS,
  WORKFLOW_RUN_RESULT,
  WORKFLOWS,
} from "./fixtures.mjs";

const PORT = Number(process.argv[2] || process.env.E2E_PORT || 4319);
const DIST = fileURLToPath(new URL("../dist", import.meta.url));

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".map": "application/json; charset=utf-8",
};

function sendJson(res, body, status = 200) {
  const data = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(data);
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
  try {
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

// GET API routes → canned fixtures.
// Git-installed plugins (ADR 0027) — mutable so install/uninstall round-trip in e2e.
let INSTALLED_PLUGINS = [];

// Per-plugin update fixtures, keyed by id — seeds non-default freshness states
// (behind / pinned / errored) for any pre-seeded plugin. After a successful
// `POST /{id}/update` the entry is cleared so the row flips to "up to date".
let PLUGIN_UPDATES = {};

// Playbooks are MUTATED by the promote spec (a skill flips private→commons), so
// serve a working copy that each playbooks test resets via
// POST /api/__test__/playbooks/reset — otherwise the promote leaks into the
// delete test (a commons skill is read-only, so its delete button is gone).
const clonePlaybooks = () => JSON.parse(JSON.stringify(PLAYBOOKS));
let playbooks = clonePlaybooks();

// Knowledge chunks are MUTATED by the promote/forget spec (a chunk flips
// private→commons, then a commons chunk is dropped), so serve a working copy each
// knowledge test resets via POST /api/__test__/knowledge/reset.
const cloneKnowledge = () => JSON.parse(JSON.stringify(KNOWLEDGE_CHUNKS));
let knowledgeChunks = cloneKnowledge();

// Fleet state is the one slice of the mock backend the specs MUTATE (create /
// stop / rename / add-remote). Isolate it PER SPEC so parallel files and serial-
// group retries can't observe each other's writes: every `x-e2e-fleet` request
// header gets its own lazy deep-clone of the FLEET baseline, and a spec resets
// its own scope between tests via POST /api/__test__/fleet/reset. Requests with
// no header share the "default" scope.
const fleetScopes = new Map();
const cloneFleet = (f) => JSON.parse(JSON.stringify(f));
function fleetFor(req) {
  const scope = req.headers["x-e2e-fleet"] || "default";
  if (!fleetScopes.has(scope)) fleetScopes.set(scope, cloneFleet(FLEET));
  return fleetScopes.get(scope);
}

function handleApiGet(pathname, fleet = FLEET) {
  switch (pathname) {
    case "/api/runtime/status":
      return RUNTIME_STATUS;
    case "/api/config":
      return {
        config: { identity: { name: "mock-agent", operator: "" } },
        soul: "# Mock agent\nYou are a helpful test agent.",
      };
    case "/api/subagents":
      return { subagents: SUBAGENTS };
    case "/api/tools":
      return {
        tools: [
          { name: "web_search", description: "Search the web.", source: "core", category: "General" },
          { name: "memory_recall", description: "Search long-term memory.", source: "core", category: "Memory" },
          { name: "echo__ping", description: "Echo ping.", source: "mcp", category: "echo" },
        ],
        count: 3,
      };
    case "/api/chat/commands":
      return { commands: SLASH_COMMANDS };
    case "/api/scheduler/jobs":
      return SCHEDULER_JOBS;
    case "/api/goals":
      return GOALS;
    case "/api/notes/workspace":
      return { workspace: NOTES_WORKSPACE };
    case "/api/tasks/status":
      return { initialized: true };
    case "/api/tasks/issues":
      return {
        issues: [
          {
            id: "bd-1",
            title: "Wire the telemetry rollup",
            status: "in_progress",
            priority: 1,
            issue_type: "task",
            created_at: "2026-06-02T09:00:00Z",
          },
        ],
      };
    case "/api/settings/schema":
      return { groups: SETTINGS_SCHEMA };
    case "/api/delegate-types":
      return DELEGATE_TYPES;
    case "/api/acp-agents":
      return {
        agents: [
          { id: "proto", label: "proto (protoCLI)", command: "proto", args: ["--acp"] },
          { id: "claude", label: "Claude Code", command: "npx", args: ["-y", "@agentclientprotocol/claude-agent-acp"] },
        ],
      };
    case "/api/delegates":
      return DELEGATES;
    case "/api/plugins/installed":
      return { plugins: INSTALLED_PLUGINS };
    case "/api/plugins/catalog":
      // Discover directory (ADR 0059) — official entries with mixed install state.
      return {
        plugins: [
          {
            id: "artifact", name: "Artifact", category: "Generative UI", official: true,
            repo: "https://github.com/protoLabsAI/artifact-plugin",
            tagline: "Render HTML/SVG/Mermaid/React into a sandboxed iframe.",
            bundled: false, installed: false, enabled: false,
          },
          {
            id: "discord", name: "Discord", category: "Communication", official: true,
            repo: "https://github.com/protoLabsAI/discord-plugin",
            tagline: "Run your agent as a Discord bot.",
            bundled: false, installed: true, enabled: true,
          },
        ],
      };
    case "/api/mcp/catalog": {
      // Curated common-MCP-server directory (quick-add picker). `installed` mirrors
      // whatever is already in the runtime roster.
      const configured = new Set(RUNTIME_STATUS.mcp.servers.map((s) => s.name));
      return {
        servers: [
          {
            id: "memory", name: "Memory", category: "Reasoning",
            tagline: "A persistent knowledge-graph memory.", requires: "node", official: true,
            template: { name: "memory", transport: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-memory"] },
            installed: configured.has("memory"),
          },
          {
            id: "filesystem", name: "Filesystem", category: "Files",
            tagline: "Read and write files under a directory you allow.", requires: "node", official: true,
            template: { name: "filesystem", transport: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "${path}"] },
            inputs: [{ key: "path", label: "Allowed directory", placeholder: "/data", required: true }],
            installed: configured.has("filesystem"),
          },
        ],
      };
    }
    case "/api/plugins/updates":
      // Per-plugin freshness (ADR 0027). Console-installed plugins are up to date
      // (their resolved_sha is the latest); the seeded fixtures exercise the other
      // states so the badge renders behind/pinned/error in the e2e.
      return {
        plugins: INSTALLED_PLUGINS.map((p) => {
          const seeded = PLUGIN_UPDATES[p.id];
          if (seeded) return { id: p.id, ...seeded };
          return {
            id: p.id, source_url: p.source_url, requested_ref: p.requested_ref,
            current_sha: p.resolved_sha, latest_sha: p.resolved_sha,
            behind: false, pinned: false, error: null,
          };
        }),
      };
    case "/api/plugins/workflows/list":
      return { workflows: WORKFLOWS };
    case "/api/theme":
      return { theme: null }; // per-agent theme (ADR 0042); null → DS defaults
    case "/api/fleet":
      return { agents: fleet.agents };
    case "/api/fleet/discover":
      // One discoverable sibling on the LAN (not in the fleet) — candidates for
      // add-as-delegate or add-to-fleet (remote member).
      return { discovered: fleet.agents.some((a) => a.name === "remy") ? [] : [
        { name: "remy", url: "http://192.168.5.50:7871", host: "192.168.5.50", port: 7871 },
      ] };
    case "/api/archetypes":
      return { archetypes: ARCHETYPES };
    case "/api/activity":
      return ACTIVITY_HISTORY;
    case "/api/inbox":
      return INBOX_ITEMS;
    case "/api/telemetry/summary":
      return { enabled: true, summary: TELEMETRY_SUMMARY };
    case "/api/telemetry/recent":
      return { enabled: true, turns: TELEMETRY_TURNS };
    case "/api/telemetry/insights":
      return { enabled: true, insights: TELEMETRY_INSIGHTS };
    case "/api/playbooks":
      return { enabled: true, playbooks };
    case "/api/knowledge/search":
      return {
        enabled: true, query: "", results: knowledgeChunks,
        stats: {
          total: knowledgeChunks.length,
          commons: knowledgeChunks.filter((c) => c.tier === "commons").length,
        },
      };
    default:
      return null;
  }
}

// POST /a2a message/stream → SSE of the canned frames for this prompt.
async function handleA2AStream(req, res, body) {
  const params = body.params || {};
  const prompt = (params.message?.parts || [])
    .filter((p) => p.kind === "text" || p.kind === undefined)
    .map((p) => p.text)
    .join("");
  const frames = buildFrames({
    rpcId: body.id ?? "1",
    // Echo the contextId the console sent (it rides on the MESSAGE, like the real server,
    // which mirrors message.context_id back onto every frame). The console now drops frames
    // whose contextId != its sessionId (frameIsForeign, #1399); a mock that didn't echo the
    // real contextId would have all its frames rejected and render nothing.
    contextId: params.message?.contextId || params.contextId || "e2e-ctx",
    taskId: "task-e2e-1",
    prompt,
  });

  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
  // A turn a spec can HOLD OPEN: stream only the opening frames (so the surface
  // enters its "streaming" / steering state) and never the terminal frame, until
  // the client disconnects. Lets the mid-turn steering ✕-cancel e2e (#1103) keep a
  // turn running deterministically instead of racing the ~40ms-gapped frames.
  if (/hold the turn open/i.test(prompt)) {
    for (const frame of frames.slice(0, 2)) {
      res.write(`data: ${JSON.stringify(frame)}\r\n\r\n`);
      await new Promise((r) => setTimeout(r, 40));
    }
    await new Promise((resolve) => req.on("close", resolve));
    return res.end();
  }
  for (const frame of frames) {
    // CRLF frame separator — the a2a-sdk emits SSE with `\r\n\r\n`, not `\n\n`.
    // The mock must mirror that so this e2e exercises the real wire shape: an
    // LF-only mock hid a browser-blanking CRLF parse bug in the client.
    res.write(`data: ${JSON.stringify(frame)}\r\n\r\n`);
    // Small gap so the "working/tool" frames are observably distinct from the
    // terminal artifact (mirrors real tool latency; lets running→done show).
    await new Promise((r) => setTimeout(r, 40));
  }
  res.end();
}

async function serveStatic(pathname, res) {
  // The SPA is built with base "/app/". Map "/app/x" → dist/x, root-level
  // assets pass through, unknown app routes fall back to index.html (SPA).
  let rel = pathname.startsWith("/app/") ? pathname.slice("/app/".length) : pathname.replace(/^\//, "");
  if (rel === "" || rel === "app") rel = "index.html";
  let filePath = normalize(join(DIST, rel));
  if (!filePath.startsWith(DIST)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  try {
    const info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, "index.html");
  } catch {
    filePath = join(DIST, "index.html"); // SPA fallback
  }
  try {
    const data = await readFile(filePath);
    res.writeHead(200, { "content-type": MIME[extname(filePath)] || "application/octet-stream" });
    res.end(data);
  } catch {
    res.writeHead(404).end("not found");
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  // Fleet slug proxy (ADR 0042): /agents/<slug>/<path> is the hub re-proxying the console to a
  // specific agent. The mock strips the /agents/<slug> prefix and serves the same handlers.
  const pathname = url.pathname.replace(/^\/agents\/[^/]+/, "") || url.pathname;

  if (pathname === "/a2a" && req.method === "POST") {
    const body = await readBody(req);
    // GetTask — the reconcile path (self-heal a stuck streaming turn + the cross-agent
    // turn watcher): return a terminal task carrying the final answer as an artifact.
    // Mirrors the REAL a2a-sdk 1.1 wire: proto method name, TASK_STATE_* state, the task
    // FLAT on `result`, member-style `{text}` parts. The legacy `tasks/get` is -32601 on
    // the live server — answering it here is how the self-heal rotted unnoticed.
    if (body?.method === "GetTask") {
      return sendJson(res, {
        jsonrpc: "2.0",
        id: body.id,
        result: {
          id: body.params?.id, contextId: "reconcile",
          status: { state: "TASK_STATE_COMPLETED" },
          artifacts: [{ parts: [{ text: "RECONCILED ANSWER" }] }],
        },
      });
    }
    if (body?.method === "CancelTask") {
      return sendJson(res, {
        jsonrpc: "2.0", id: body.id,
        result: { id: body.params?.id, status: { state: "TASK_STATE_CANCELLED" } },
      });
    }
    if (body?.method === "tasks/get" || body?.method === "tasks/cancel") {
      // The 0.3 names are GONE on the live server — keep the mock honest.
      return sendJson(res, { jsonrpc: "2.0", id: body.id, error: { code: -32601, message: "Method not found" } });
    }
    return handleA2AStream(req, res, body);
  }
  if (pathname === "/api/events" && req.method === "GET") {
    // Server→client SSE push channel (ADR 0003). Hold the connection open so
    // the client's EventSource fires onopen (the "live" indicator), then push
    // one named event to exercise event delivery.
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    res.write(": connected\n\n");
    // Frames are unnamed SSE events carrying the topic in the payload (ADR 0039) — the
    // client routes by topic with wildcard matching.
    const frame = (topic, data) => res.write(`data: ${JSON.stringify({ topic, data })}\n\n`);
    // Push periodically so the unread badge (off-surface), live append (on-surface), and
    // the plugin notification dot (a `boardy.*` event) are all deterministically testable.
    const t = setInterval(() => {
      frame("activity.message", { text: "live activity ping", origin: "scheduler", trigger: "heartbeat", stimulus: "Hourly heartbeat check." });
      frame("inbox.item", { id: 99, priority: "next", source: "mock", text: "live inbox ping" });
      frame("boardy.created", { id: "b1" }); // ADR 0039 — exercises the rail notification dot
    }, 500);
    // goal.achieved (ADR 0039) so the goal toast is testable. Must be an UNNAMED topic-in-payload
    // frame like the others — the client routes via onmessage, not named SSE events. Fire a couple
    // of times early so a slow connect can't miss the one-shot (the toast just needs to appear once).
    const goals = [setTimeout(() => frame("goal.achieved", { condition: "unit tests pass", status: "achieved", mode: "drive" }), 300),
                   setTimeout(() => frame("goal.achieved", { condition: "unit tests pass", status: "achieved", mode: "drive" }), 1200)];
    req.on("close", () => { clearInterval(t); goals.forEach(clearTimeout); });
    return;
  }
  if (pathname.startsWith("/api/")) {
    if (req.method === "GET") {
      // Mid-turn steering: turn-end reconcile reads the still-queued items.
      if (/^\/api\/chat\/sessions\/[^/]+\/steer$/.test(pathname)) return sendJson(res, { pending: [] });
      const payload = handleApiGet(pathname, fleetFor(req));
      if (payload !== null) return sendJson(res, payload);
      return sendJson(res, { detail: "not mocked" }, 404);
    }
    // Mid-turn steering enqueue — accept + echo (the console ignores the body).
    if (/^\/api\/chat\/sessions\/[^/]+\/steer$/.test(pathname) && req.method === "POST") {
      const body = await readBody(req);
      return sendJson(res, { ok: true, id: body.id ?? null, pending: 0 });
    }
    // Mid-turn steering cancel (the ✕ on a queued bubble) — dequeue still-queued.
    // `removed: true` is the happy path the #1103 e2e drives (cancel before drain).
    if (/^\/api\/chat\/sessions\/[^/]+\/steer\/[^/]+$/.test(pathname) && req.method === "DELETE") {
      return sendJson(res, { removed: true, pending: 0 });
    }
    if (pathname === "/api/config/models" && req.method === "POST") {
      // "Get models" (#1386): probe the (form) gateway for its model list. The mock returns a
      // DIFFERENT set than the saved dropdown, so the test can prove the dropdown refreshes.
      return sendJson(res, { models: GATEWAY_MODELS, error: "" });
    }
    if (pathname === "/api/config/test-model" && req.method === "POST") {
      return sendJson(res, { ok: true, error: "" });
    }
    if (pathname === "/api/knowledge/attach" && req.method === "POST") {
      // Chat attachment upload (#1002) — multipart, so DON'T JSON-parse the body.
      // Drain it, then return the small-file "inline" tier with a context block.
      req.resume();
      await new Promise((r) => req.on("end", r));
      return sendJson(res, {
        enabled: true,
        mode: "inline",
        context: "[attachment notes.txt]\nhello from the attached file",
      });
    }
    // POST/PATCH/DELETE writes → generic ok so the UI doesn't error.
    const body = await readBody(req);
    const fleet = fleetFor(req);
    if (pathname === "/api/__test__/fleet/reset" && req.method === "POST") {
      // Per-spec hermeticity: restore this scope's fleet to the baseline.
      fleetScopes.set(req.headers["x-e2e-fleet"] || "default", cloneFleet(FLEET));
      return sendJson(res, { ok: true });
    }
    if (pathname === "/api/__test__/playbooks/reset" && req.method === "POST") {
      // Per-test hermeticity: undo any promote (private→commons) from a prior test.
      playbooks = clonePlaybooks();
      return sendJson(res, { ok: true });
    }
    if (pathname === "/api/__test__/mcp/layered" && req.method === "POST") {
      // Put the MCP roster into "layered" mode (servers carry a tier) so the tier
      // badges + share/unshare surface — exercised by the commons e2e. Keep `echo`
      // (the default fixture other specs assert against — RUNTIME_STATUS is shared
      // across parallel spec files) and ADD the tiered servers.
      RUNTIME_STATUS.mcp.servers = [
        { name: "echo", transport: "stdio", tool_count: 2 },
        { name: "shared-fs", transport: "stdio", tool_count: 1, tier: "commons" },
        { name: "local-fs", transport: "stdio", tool_count: 1, tier: "private" },
      ];
      RUNTIME_STATUS.mcp.tool_count = 4;
      return sendJson(res, { ok: true });
    }
    if (pathname === "/api/__test__/knowledge/reset" && req.method === "POST") {
      knowledgeChunks = cloneKnowledge();
      return sendJson(res, { ok: true });
    }
    if (req.method === "POST" && /^\/api\/knowledge\/\d+\/promote$/.test(pathname)) {
      const id = Number(pathname.split("/").at(-2));
      const c = knowledgeChunks.find((x) => x.id === id);
      if (c) c.tier = "commons"; // promoted: now reads from the commons tier
      return sendJson(res, { enabled: true, promoted: !!c });
    }
    if (req.method === "POST" && /^\/api\/knowledge\/\d+\/forget$/.test(pathname)) {
      const id = Number(pathname.split("/").at(-2));
      const i = knowledgeChunks.findIndex((x) => x.id === id && x.tier === "commons");
      if (i < 0) return sendJson(res, { enabled: true, forgotten: false, error: "no commons chunk with that id" });
      knowledgeChunks.splice(i, 1); // removed from the commons
      return sendJson(res, { enabled: true, forgotten: true });
    }
    if (pathname === "/api/settings") {
      // ADR 0047: a layer-aware save — "agent" (per-agent leaf, default) or "host"
      // (box-shared host-config.yaml). The mock just echoes which layer it wrote.
      const layer = body.layer === "host" ? "host" : "agent";
      return sendJson(res, {
        ok: true,
        messages: [`config saved (${layer})`, "reloaded • model=protolabs/reasoning"],
        restart_required: settingsRestartRequired(body.updates),
      });
    }
    if (pathname === "/api/settings/reset") {
      // ADR 0047 reset-to-inherited: pop the given keys from the agent leaf.
      const keys = Array.isArray(body.keys) ? body.keys : [];
      return sendJson(res, { ok: true, messages: [`reset ${keys.length} setting(s) to inherited`] });
    }
    if (/^\/api\/plugins\/workflows\/[^/]+\/run$/.test(pathname)) {
      return sendJson(res, WORKFLOW_RUN_RESULT);
    }
    if (pathname === "/api/plugins/workflows/save") {
      return sendJson(res, { saved: true, name: "demo" });
    }
    if (req.method === "DELETE" && /^\/api\/plugins\/workflows\/[^/]+$/.test(pathname)) {
      return sendJson(res, { deleted: true });
    }
    // Fleet (ADR 0042) — mutate this scope's fleet so create/start/stop/activate/remove round-trip.
    if (pathname === "/api/fleet" && req.method === "POST") {
      const name = String(body.name || "").trim();
      if (!/^[A-Za-z0-9-_]+$/.test(name)) return sendJson(res, { detail: "invalid name" }, 400);
      // Ids are opaque + immutable (name-<4hex>); the name is the editable display label.
      const agent = { name, id: `${name}-ab12`, port: 7899, pid: 5000, running: true, bundle: body.bundle || "" };
      fleet.agents.push(agent);
      return sendJson(res, { ok: true, agent, installed: [] });
    }
    if (pathname === "/api/fleet/remotes" && req.method === "POST") {
      const name = String(body.name || "").trim();
      if (fleet.agents.some((a) => a.name === name)) return sendJson(res, { detail: "an agent named " + name + " already exists" }, 400);
      const agent = { name, id: `${name}-re01`, port: null, pid: null, running: true, bundle: "", remote: true, url: body.url, a2a: `${body.url}/a2a` };
      fleet.agents.push(agent);
      return sendJson(res, { ok: true, agent });
    }
    if (req.method === "DELETE" && /^\/api\/fleet\/remotes\/[^/]+$/.test(pathname)) {
      const ident = decodeURIComponent(pathname.split("/").pop());
      const a = fleet.agents.find((x) => x.remote && (x.id === ident || x.name === ident));
      if (!a) return sendJson(res, { detail: "no remote member" }, 400);
      fleet.agents = fleet.agents.filter((x) => x !== a);
      return sendJson(res, { ok: true, id: a.id, name: a.name });
    }
    if (req.method === "PATCH" && /^\/api\/fleet\/[^/]+$/.test(pathname)) {
      const ident = decodeURIComponent(pathname.split("/").pop());
      const a = fleet.agents.find((x) => x.id === ident || x.name === ident);
      if (!a) return sendJson(res, { detail: "no such agent" }, 400);
      if (fleet.agents.some((x) => x.name === body.name && x.id !== a.id))
        return sendJson(res, { detail: "an agent with that name already exists" }, 400);
      a.name = String(body.name || "").trim();
      return sendJson(res, { ok: true, id: a.id, name: a.name });
    }
    if (pathname === "/api/fleet/down" && req.method === "POST") {
      fleet.agents.forEach((a) => { a.running = false; a.pid = null; });
      return sendJson(res, { ok: true, stopped: fleet.agents.map((a) => a.name) });
    }
    {
      const m = pathname.match(/^\/api\/fleet\/([^/]+)\/(start|stop|activate)$/);
      if (m && req.method === "POST") {
        const a = fleet.agents.find((x) => x.name === m[1] || x.id === m[1]);
        if (!a) return sendJson(res, { detail: "no such agent" }, 400);
        if (m[2] === "start") { a.running = true; a.pid = 5001; return sendJson(res, { ok: true, agent: a }); }
        if (m[2] === "stop") { a.running = false; a.pid = null; return sendJson(res, { ok: true, name: a.name, stopped: true }); }
        // activate: ensure-running + keep-warm (no server-side active pointer — slug routing).
        if (!a.host) a.running = true;
        return sendJson(res, { ok: true, evicted: [] });
      }
    }
    if (req.method === "DELETE" && /^\/api\/fleet\/[^/]+$/.test(pathname)) {
      const name = decodeURIComponent(pathname.split("/").pop());
      fleet.agents = fleet.agents.filter((a) => a.name !== name && a.id !== name);
      return sendJson(res, { ok: true, name, removed: [name] });
    }
    if (req.method === "POST" && /^\/api\/playbooks\/\d+\/promote$/.test(pathname)) {
      const id = Number(pathname.split("/").at(-2));
      const p = playbooks.find((x) => x.id === id);
      if (p) p.tier = "commons"; // promoted: now reads from the commons tier
      return sendJson(res, { enabled: true, promoted: true, name: p?.name });
    }
    if (req.method === "POST" && /^\/api\/playbooks\/\d+\/forget$/.test(pathname)) {
      const id = Number(pathname.split("/").at(-2));
      const i = playbooks.findIndex((x) => x.id === id && x.tier === "commons");
      if (i < 0) return sendJson(res, { enabled: true, forgotten: false, error: "no commons skill with that id" });
      const [p] = playbooks.splice(i, 1); // removed from the commons → no agent reads it
      return sendJson(res, { enabled: true, forgotten: true, name: p.name });
    }
    if (req.method === "DELETE" && /^\/api\/playbooks\/\d+$/.test(pathname)) {
      return sendJson(res, { enabled: true, deleted: true });
    }
    {
      const m = pathname.match(/^\/api\/plugins\/([^/]+)\/enabled$/);
      if (m) {
        return sendJson(res, {
          ok: true, enabled: !!body.enabled, reloaded: true,
          // Enable hot-mounts the view router live (#822) → no restart. Only DISABLE of a
          // view plugin (boardy) leaves a stale route → restart recommended.
          restart_recommended: !body.enabled && m[1] === "boardy",
        });
      }
    }
    if (pathname === "/api/mcp/servers" && req.method === "POST") {
      const name = body.name || "server";
      RUNTIME_STATUS.mcp.servers = RUNTIME_STATUS.mcp.servers
        .filter((s) => s.name !== name)
        .concat({ name, transport: body.transport || "stdio", tool_count: 1 });
      return sendJson(res, { ok: true, name, servers: RUNTIME_STATUS.mcp.servers.map((s) => s.name) });
    }
    if (pathname === "/api/mcp/servers/import" && req.method === "POST") {
      let added = ["imported"];
      try {
        const d = JSON.parse(body.raw || "{}");
        added = d.mcpServers ? Object.keys(d.mcpServers) : [d.name || "imported"];
      } catch { return sendJson(res, { detail: "invalid JSON" }, 400); }
      return sendJson(res, { ok: true, added, servers: added });
    }
    {
      const mp = pathname.match(/^\/api\/mcp\/servers\/([^/]+)\/promote$/);
      if (mp && req.method === "POST") {
        const name = decodeURIComponent(mp[1]);
        RUNTIME_STATUS.mcp.servers = RUNTIME_STATUS.mcp.servers.map((s) => (s.name === name ? { ...s, tier: "commons" } : s));
        return sendJson(res, { ok: true, promoted: true, name });
      }
      const mf = pathname.match(/^\/api\/mcp\/servers\/([^/]+)\/forget$/);
      if (mf && req.method === "POST") {
        const name = decodeURIComponent(mf[1]);
        RUNTIME_STATUS.mcp.servers = RUNTIME_STATUS.mcp.servers.map((s) => (s.name === name ? { ...s, tier: "private" } : s));
        return sendJson(res, { ok: true, forgotten: true, name });
      }
      const m = pathname.match(/^\/api\/mcp\/servers\/([^/]+)$/);
      if (m && req.method === "DELETE") {
        return sendJson(res, { ok: true, servers: [] });
      }
    }
    if (pathname === "/api/plugins/install") {
      const id = (String(body.url || "").replace(/\.git$/, "").split("/").pop()) || "ext_plugin";
      const sha = "abc1234567def8900000000000000000000abcd";
      INSTALLED_PLUGINS = INSTALLED_PLUGINS.filter((p) => p.id !== id).concat({
        id, source_url: body.url, requested_ref: body.ref || "", resolved_sha: sha,
        present: true, enabled: true,   // install AUTO-ENABLES + runs it (trust-by-default)
        manifest: { name: id, version: "0.1.0", description: "installed via console", requires_pip: [], views: [] },
      });
      // The new plugin joins the runtime roster too (install → reload), so it shows as a
      // row in the Installed list (and, being lock-backed, gets an Uninstall button).
      RUNTIME_STATUS.plugins = RUNTIME_STATUS.plugins.filter((p) => p.id !== id).concat({
        id, name: id, version: "0.1.0", enabled: true, loaded: true, tools: [], skills: 0,
      });
      return sendJson(res, {
        installed: {
          id, name: id, version: "0.1.0", description: "installed via console",
          resolved_sha: sha, source_url: body.url, requires_pip: [], capabilities: {},
          contributes: { views: [], secrets: [] },
        },
        enabled: [id],
        reloaded: true,
        restart_recommended: false,
        enable_error: null,
      });
    }
    {
      const m = pathname.match(/^\/api\/plugins\/([^/]+)\/update$/);
      if (m && req.method === "POST") {
        const id = decodeURIComponent(m[1]);
        const sha = "fed9876543210abcdef0000000000000000fed98"; // the "latest" sha
        INSTALLED_PLUGINS = INSTALLED_PLUGINS.map((p) =>
          p.id === id ? { ...p, resolved_sha: sha } : p,
        );
        // Updated to latest → drop the seeded "behind" state so the badge flips.
        delete PLUGIN_UPDATES[id];
        return sendJson(res, {
          ok: true, id, version: "0.2.0", resolved_sha: sha,
          reloaded: true, restart_recommended: false,
        });
      }
    }
    if (req.method === "DELETE" && /^\/api\/plugins\/[^/]+$/.test(pathname)) {
      const id = decodeURIComponent(pathname.split("/").pop());
      INSTALLED_PLUGINS = INSTALLED_PLUGINS.filter((p) => p.id !== id);
      RUNTIME_STATUS.plugins = RUNTIME_STATUS.plugins.filter((p) => p.id !== id);
      return sendJson(res, { ok: true });
    }
    return sendJson(res, { ok: true });
  }
  // Plugin-served pages (ADR 0026) — a tiny listener page so the e2e can assert
  // the console's post-load init handshake (token + theme via postMessage).
  if (pathname.startsWith("/plugins/") && req.method === "GET") {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(
      `<!doctype html><html><body data-bridge="pending">` +
      `<p id="p">${pathname}</p><script>` +
      `window.addEventListener("message",function(e){var m=e.data||{};` +
      `if(m.type!=="protoagent:init")return;` +
      `document.body.setAttribute("data-bridge",m.token?"authed":"anon");});` +
      // Like the real plugin-kit: announce readiness so the console (re-)sends the
      // bearer + theme, closing the race where load-time init beats our listener.
      `try{parent&&parent!==window&&parent.postMessage({type:"protoagent:ready"},"*");}catch(_){}` +
      `</script></body></html>`,
    );
    return;
  }
  if (req.method !== "GET") {
    return sendJson(res, { detail: "method not allowed" }, 405);
  }
  return serveStatic(pathname, res);
});

server.listen(PORT, "127.0.0.1", () => {
  // Playwright's webServer waits on this readiness line / the port.
  console.log(`[e2e mock] serving on http://127.0.0.1:${PORT}/app/`);
});
