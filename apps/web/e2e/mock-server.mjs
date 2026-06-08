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
  buildFrames,
  DELEGATES,
  DELEGATE_TYPES,
  GOALS,
  INBOX_ITEMS,
  NOTES_WORKSPACE,
  RUNTIME_STATUS,
  SCHEDULER_JOBS,
  SETTINGS_SCHEMA,
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

function handleApiGet(pathname) {
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
          { name: "echo__ping", description: "Echo ping.", source: "mcp", category: "MCP" },
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
    case "/api/beads/status":
      return { initialized: true };
    case "/api/beads/issues":
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
    case "/api/delegates":
      return DELEGATES;
    case "/api/plugins/installed":
      return { plugins: INSTALLED_PLUGINS };
    case "/api/workflows":
      return { workflows: WORKFLOWS };
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
      return { enabled: true, playbooks: PLAYBOOKS };
    case "/api/knowledge/search":
      return {
        enabled: true, query: "", results: KNOWLEDGE_CHUNKS,
        stats: { total: KNOWLEDGE_CHUNKS.length },
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
    contextId: params.contextId || "e2e-ctx",
    taskId: "task-e2e-1",
    prompt,
  });

  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });
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
  const { pathname } = url;

  if (pathname === "/a2a" && req.method === "POST") {
    const body = await readBody(req);
    // tasks/get — the reconcile path (self-heal a stuck streaming turn): return a
    // terminal task carrying the final answer as an artifact.
    if (body?.method === "tasks/get") {
      return sendJson(res, {
        jsonrpc: "2.0",
        id: body.id,
        result: {
          id: body.params?.id, contextId: "reconcile", kind: "task",
          status: { state: "completed" },
          artifacts: [{ parts: [{ kind: "text", text: "RECONCILED ANSWER" }] }],
        },
      });
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
    // Push an activity.message periodically so both the unread badge (while off
    // the surface) and live append (while on it) are deterministically testable.
    const t = setInterval(() => {
      res.write('event: activity.message\ndata: {"text":"live activity ping","origin":"scheduler","trigger":"heartbeat"}\n\n');
      res.write('event: inbox.item\ndata: {"id":99,"priority":"next","source":"mock","text":"live inbox ping"}\n\n');
    }, 500);
    req.on("close", () => clearInterval(t));
    return;
  }
  if (pathname.startsWith("/api/")) {
    if (req.method === "GET") {
      const payload = handleApiGet(pathname);
      if (payload !== null) return sendJson(res, payload);
      return sendJson(res, { detail: "not mocked" }, 404);
    }
    // POST/PATCH/DELETE writes → generic ok so the UI doesn't error.
    const body = await readBody(req);
    if (pathname === "/api/settings") {
      return sendJson(res, {
        ok: true,
        messages: ["config saved", "reloaded • model=protolabs/reasoning"],
        restart_required: settingsRestartRequired(body.updates),
      });
    }
    if (/^\/api\/workflows\/[^/]+\/run$/.test(pathname)) {
      return sendJson(res, WORKFLOW_RUN_RESULT);
    }
    if (req.method === "DELETE" && /^\/api\/playbooks\/\d+$/.test(pathname)) {
      return sendJson(res, { enabled: true, deleted: true });
    }
    {
      const m = pathname.match(/^\/api\/plugins\/([^/]+)\/enabled$/);
      if (m) {
        return sendJson(res, {
          ok: true, enabled: !!body.enabled, reloaded: true,
          // a view plugin (boardy) recommends a restart; others don't
          restart_recommended: !!body.enabled && m[1] === "boardy",
        });
      }
    }
    if (pathname === "/api/plugins/install") {
      const id = (String(body.url || "").replace(/\.git$/, "").split("/").pop()) || "ext_plugin";
      const sha = "abc1234567def8900000000000000000000abcd";
      INSTALLED_PLUGINS = INSTALLED_PLUGINS.filter((p) => p.id !== id).concat({
        id, source_url: body.url, requested_ref: body.ref || "", resolved_sha: sha,
        present: true, enabled: false,
        manifest: { name: id, version: "0.1.0", description: "installed via console", requires_pip: [], views: [] },
      });
      return sendJson(res, {
        installed: {
          id, name: id, version: "0.1.0", description: "installed via console",
          resolved_sha: sha, source_url: body.url, requires_pip: [], capabilities: {},
          contributes: { views: [], secrets: [] },
        },
        restart_required: true,
      });
    }
    if (req.method === "DELETE" && /^\/api\/plugins\/[^/]+$/.test(pathname)) {
      const id = decodeURIComponent(pathname.split("/").pop());
      INSTALLED_PLUGINS = INSTALLED_PLUGINS.filter((p) => p.id !== id);
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
