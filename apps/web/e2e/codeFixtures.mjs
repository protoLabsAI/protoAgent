// Code pane fixtures (ADR 0112) — the mock backend's answers for GET /api/fs/roots,
// /api/fs/file and /api/fs/diff, shaped exactly like the server contract (operator_api/
// browse_routes.py). One project, "app", with a real-looking source file, a long file for
// the scroll test, a secret-like file (403), a binary (binary:true) and a working-tree diff.

const SERVER_TS = `import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { loadConfig } from "./config";
import { log } from "./log";

// A tiny HTTP front door for the agent. Every request is authenticated before routing:
// a missing or wrong bearer is a 401, never a fall-through to the handler.

export type Handler = (req: IncomingMessage, res: ServerResponse) => Promise<void>;

const routes = new Map<string, Handler>();

export function route(path: string, handler: Handler): void {
  routes.set(path, handler);
}

function bearer(req: IncomingMessage): string | null {
  const h = req.headers.authorization || "";
  return h.startsWith("Bearer ") ? h.slice(7) : null;
}

export function authorize(req: IncomingMessage, token: string): boolean {
  // Constant-time compare — a plain === leaks the prefix length through timing.
  const got = bearer(req);
  if (!got || got.length !== token.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++) diff |= got.charCodeAt(i) ^ token.charCodeAt(i);
  return diff === 0;
}

export async function handle(req: IncomingMessage, res: ServerResponse, token: string) {
  if (!authorize(req, token)) {
    res.writeHead(401, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: "unauthorized" }));
    return;
  }
  const url = new URL(req.url || "/", "http://localhost");
  const handler = routes.get(url.pathname);
  if (!handler) {
    res.writeHead(404).end();
    return;
  }
  try {
    await handler(req, res);
  } catch (err) {
    log.error("handler failed", { path: url.pathname, err });
    res.writeHead(500).end();
  }
}

export function start(port = 7870) {
  const cfg = loadConfig();
  const server = createServer((req, res) => void handle(req, res, cfg.token));
  server.listen(port, "127.0.0.1", () => log.info(\`listening on :\${port}\`));
  return server;
}
`;

const BIG_TS = Array.from(
  { length: 3000 },
  (_, i) => `export const row${i + 1} = ${i + 1}; // generated row ${i + 1}`,
).join("\n") + "\n";

const HUGE_TS = Array.from(
  { length: 20000 },
  (_, i) => `export const huge${i + 1} = { id: ${i + 1}, name: "row ${i + 1}" }; // generated`,
).join("\n") + "\n";

const README = "# app\n\nA fixture project for the console's code pane e2e.\n";

export const CODE_FILES = {
  app: {
    "src/server.ts": { text: SERVER_TS, language: "ts" },
    "src/big.ts": { text: BIG_TS, language: "ts" },
    "src/huge.ts": { text: HUGE_TS, language: "ts" },
    "README.md": { text: README, language: "md" },
    "assets/logo.png": { binary: true, size: 18_342 },
  },
};

export const CODE_ROOTS = { roots: { app: "/home/op/dev/app", docs: "/home/op/dev/docs" } };

const SECRET = /(^|\/)(\.env(\.(?!example$|sample$|template$)[^/]*)?|[^/]*\.pem|[^/]*\.key|secrets\.ya?ml|\.netrc)$/i;

/** GET /api/fs/file → `{status, body}`. */
export function fsFileResponse(params) {
  const project = params.get("project") || "";
  const path = params.get("path") || "";
  const files = CODE_FILES[project];
  if (!files) return { status: 400, body: { detail: { code: "bad_path", reason: "unknown project" } } };
  if (path.startsWith("/") || path.split("/").includes("..")) {
    return { status: 400, body: { detail: { code: "bad_path", reason: "outside the project" } } };
  }
  if (SECRET.test(path)) return { status: 403, body: { detail: { code: "denied", reason: "secret-like path" } } };
  const f = files[path];
  if (!f) return { status: 404, body: { detail: "not found" } };
  if (f.binary) {
    return {
      status: 200,
      body: { project, path, size: f.size, line_count: 0, start: 0, end: 0, truncated: false, language: "text", binary: true, text: null },
    };
  }
  const lines = f.text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  const lineCount = lines.length;
  const start = Math.max(1, Number(params.get("start")) || 1);
  const end = Math.min(lineCount, Number(params.get("end")) || lineCount);
  const text = lines.slice(start - 1, end).join("\n") + (end === lineCount ? "\n" : "");
  return {
    status: 200,
    body: {
      project,
      path,
      size: f.text.length,
      line_count: lineCount,
      start,
      end,
      truncated: false,
      language: f.language || "text",
      binary: false,
      text,
    },
  };
}

// Built line by line: a blank CONTEXT line is a single space, which an editor's
// trim-trailing-whitespace would silently eat out of a template literal.
const DIFF_PATCH = [
  "diff --git a/src/server.ts b/src/server.ts",
  "index 3b18e51..a9c0f2d 100644",
  "--- a/src/server.ts",
  "+++ b/src/server.ts",
  "@@ -21,8 +21,13 @@ function bearer(req: IncomingMessage): string | null {",
  " }",
  " ",
  " export function authorize(req: IncomingMessage, token: string): boolean {",
  "-  return bearer(req) === token;",
  "+  // Constant-time compare — a plain === leaks the prefix length through timing.",
  "+  const got = bearer(req);",
  "+  if (!got || got.length !== token.length) return false;",
  "+  let diff = 0;",
  "+  for (let i = 0; i < got.length; i++) diff |= got.charCodeAt(i) ^ token.charCodeAt(i);",
  "+  return diff === 0;",
  " }",
  " ",
  " export async function handle(req: IncomingMessage, res: ServerResponse, token: string) {",
  "   if (!authorize(req, token)) {",
  "diff --git a/README.md b/README.md",
  "index 1f2e3d4..5a6b7c8 100644",
  "--- a/README.md",
  "+++ b/README.md",
  "@@ -1,3 +1,3 @@",
  " # app",
  " ",
  "-A fixture project.",
  "+A fixture project for the console's code pane e2e.",
  "diff --git a/notes/todo.md b/notes/todo.md",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/notes/todo.md",
  "@@ -0,0 +1,2 @@",
  "+- constant-time token compare",
  "+- rate-limit the auth failures",
  "",
].join("\n");

export const CODE_DIFF = {
  project: "app",
  is_git: true,
  head: "a9c0f2d4e1b7",
  branch: "feat/constant-time-auth",
  files: [
    { path: "src/server.ts", status: "M", additions: 6, deletions: 1, binary: false, denied: false },
    { path: "README.md", status: "M", additions: 1, deletions: 1, binary: false, denied: false },
    { path: "notes/todo.md", status: "?", additions: 2, deletions: 0, binary: false, denied: false },
    { path: ".env", status: "M", additions: 0, deletions: 0, binary: false, denied: true },
  ],
  patch: DIFF_PATCH,
  truncated: false,
};

/** GET /api/fs/diff → `{status, body}`. */
export function fsDiffResponse(params) {
  const project = params.get("project") || "";
  if (project === "app") return { status: 200, body: CODE_DIFF };
  if (project === "docs") return { status: 200, body: { project, is_git: false, files: [], patch: "" } };
  return { status: 400, body: { detail: { code: "bad_path", reason: "unknown project" } } };
}
