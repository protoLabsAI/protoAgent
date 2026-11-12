/**
 * Background job sessions S4 (ADR 0129 D6, #5110) — Work decision access against the
 * REAL backend, not mocked endpoint fixtures.
 *
 * This spec boots the actual Python server (`python -m server --ui console`, instance
 * and box roots fully isolated in temp dirs per PROTO.md, the `scripts/live_smoke.py`
 * pattern) against an in-process fake OpenAI gateway — the model is the ONLY faked
 * boundary. Two inbox now-items each fire on their own isolated `job:` execution
 * session; the fake model parks each turn on a real `request_decision` graph
 * interrupt. The console then proves, over the real `/api/decisions` routes and real
 * SSE events:
 *
 *  - both job-session decisions are listed fleet-wide (no origin chat, never opened);
 *  - the approved Needs-you marker (the Decisions utility pill, bd-y3d8m surface)
 *    stays visible from ANOTHER chat, with no layout change;
 *  - answering one from Work resumes ONLY its owning job (the fake gateway counts
 *    resumed completions per job) while the sibling decision stays pending;
 *  - informative states read as neutral human copy — no error banners.
 *
 * Opt-in (needs a Python runtime with the repo's deps — CI's web-e2e shards have
 * none and SKIP this): from apps/web,
 *
 *   PROTOAGENT_E2E_PYTHON=../../.venv/bin/python \
 *     npx playwright test e2e/background-job-decisions-live.spec.ts
 *
 * The reproducible pre-release checklist lives in
 * docs/dev/background-job-session-verification.md.
 */
import { type ChildProcess, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const PYTHON = process.env.PROTOAGENT_E2E_PYTHON ?? "";
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const MODEL = "protolabs/reasoning";

/** One SSE chunk in the OpenAI streaming shape. */
function chunk(delta: Record<string, unknown>, finish: string | null = null): string {
  return `data: ${JSON.stringify({
    id: "e2e-1",
    object: "chat.completion.chunk",
    model: MODEL,
    choices: [{ index: 0, delta, finish_reason: finish }],
  })}\n\n`;
}

interface Gateway {
  server: Server;
  port: number;
  /** Serialized message lists of RESUMED completions (a request_decision tool result present). */
  resumes: string[];
  /** Serialized message lists of completions answered with the request_decision tool call. */
  parks: string[];
}

/**
 * The faked model boundary: an OpenAI-compatible gateway. A completion whose messages
 * carry an `[E2E-DECISION <name>]` marker is answered with a `request_decision` tool
 * call (the turn then parks on the REAL graph interrupt); once the operator's answer
 * comes back as a tool result, it completes the turn. Everything else gets a plain
 * completion, and `/v1/embeddings` serves zero vectors so no middleware needs creds.
 */
function startGateway(): Promise<Gateway> {
  const gateway: Partial<Gateway> = { resumes: [], parks: [] };
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const raw: Buffer[] = [];
    req.on("data", (piece) => raw.push(piece));
    req.on("end", () => {
      const url = req.url ?? "";
      if (req.method === "GET" && url.includes("/models")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ object: "list", data: [{ id: MODEL, object: "model" }] }));
        return;
      }
      let body: { stream?: boolean; input?: unknown; messages?: { role?: string }[] } = {};
      try {
        body = JSON.parse(Buffer.concat(raw).toString("utf8") || "{}");
      } catch {
        body = {};
      }
      if (req.method === "POST" && url.includes("/embeddings")) {
        const inputs = Array.isArray(body.input) ? body.input : [body.input ?? ""];
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({
          object: "list",
          model: MODEL,
          data: inputs.map((_, index) => ({ object: "embedding", index, embedding: new Array(64).fill(0) })),
          usage: { prompt_tokens: 1, total_tokens: 1 },
        }));
        return;
      }
      if (!(req.method === "POST" && url.includes("/chat/completions"))) {
        res.writeHead(404).end();
        return;
      }
      const messages = body.messages ?? [];
      const flat = JSON.stringify(messages);
      const marker = /E2E-DECISION (alpha|beta)/.exec(flat)?.[1] ?? "";
      const resumed = messages.some((message) => message.role === "tool");
      let toolCall: { name: string; arguments: string } | null = null;
      let answer: string;
      if (marker && !resumed) {
        gateway.parks!.push(flat);
        toolCall = {
          name: "request_decision",
          arguments: JSON.stringify({
            question: `Ship the ${marker} release?`,
            options: ["Approve", "Hold"],
          }),
        };
        answer = "";
      } else if (marker) {
        gateway.resumes!.push(flat);
        answer = `<output>Rolling the ${marker} release.</output>`;
      } else {
        answer = "<output>ok</output>";
      }
      const usage = { prompt_tokens: 7, completion_tokens: 4, total_tokens: 11 };
      if (body.stream) {
        res.writeHead(200, { "content-type": "text/event-stream" });
        if (toolCall) {
          res.write(chunk({
            role: "assistant",
            tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: toolCall.name, arguments: "" } }],
          }));
          res.write(chunk({ tool_calls: [{ index: 0, function: { arguments: toolCall.arguments } }] }));
          res.write(chunk({}, "tool_calls"));
        } else {
          res.write(chunk({ role: "assistant", content: "" }));
          res.write(chunk({ content: answer }));
          res.write(chunk({}, "stop"));
        }
        res.write("data: [DONE]\n\n");
        res.end();
        return;
      }
      const message = toolCall
        ? { role: "assistant", content: null, tool_calls: [{ id: "call_1", type: "function", function: toolCall }] }
        : { role: "assistant", content: answer };
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({
        id: "e2e-1",
        object: "chat.completion",
        model: MODEL,
        choices: [{ index: 0, message, finish_reason: toolCall ? "tool_calls" : "stop" }],
        usage,
      }));
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      gateway.server = server;
      gateway.port = typeof address === "object" && address ? address.port : 0;
      resolve(gateway as Gateway);
    });
  });
}

function startBackend(gatewayPort: number): { proc: ChildProcess; port: number; log: string[] } {
  const home = mkdtempSync(path.join(tmpdir(), "pa-e2e-home-"));
  const box = mkdtempSync(path.join(tmpdir(), "pa-e2e-box-"));
  mkdirSync(path.join(home, "config"), { recursive: true });
  writeFileSync(
    path.join(home, "config", "langgraph-config.yaml"),
    `model:\n  name: ${MODEL}\n  api_base: http://127.0.0.1:${gatewayPort}/v1\n` +
      "middleware:\n  knowledge: false\n  scheduler: false\n",
  );
  const port = 42000 + Math.floor(Math.random() * 20000);
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    OPENAI_API_KEY: "fake-e2e-key",
    PROTOAGENT_HOME: home,
    PROTOAGENT_BOX_ROOT: box,
    PROTOAGENT_INSTANCE: "e2edecisions",
    PROTOAGENT_HEADLESS_SETUP: "1",
    PROTOAGENT_DISCOVERY_DISABLE: "1",
    PYTHONPATH: [path.join(REPO, "src"), REPO].join(path.delimiter),
  };
  // A dev shell's auth/fleet tokens must not leak in: they would gate /api/inbox.
  delete env.A2A_AUTH_TOKEN;
  delete env.PROTOAGENT_FLEET_TOKEN;
  const log: string[] = [];
  const proc = spawn(PYTHON, ["-m", "server", "--ui", "console", "--port", String(port)], {
    cwd: REPO,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  for (const stream of [proc.stdout, proc.stderr]) {
    stream?.on("data", (piece: Buffer) => {
      log.push(piece.toString("utf8"));
      if (log.length > 400) log.shift();
    });
  }
  return { proc, port, log };
}

async function waitForHealthz(port: number, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/healthz`);
      if (response.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((tick) => setTimeout(tick, 1000));
  }
  return false;
}

test.describe("background job decisions against the real backend", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(300_000);

  test("a job-session decision is answerable from Work; only its owning job resumes", async ({ page }, testInfo) => {
    test.skip(
      !PYTHON,
      "needs a Python runtime: PROTOAGENT_E2E_PYTHON=../../.venv/bin/python (see docs/dev/background-job-session-verification.md)",
    );
    test.skip(
      !existsSync(path.join(REPO, "apps", "web", "dist", "index.html")),
      "build the console first: npm run build --workspace @protoagent/web",
    );

    const gateway = await startGateway();
    const backend = startBackend(gateway.port);
    const base = `http://127.0.0.1:${backend.port}`;
    try {
      expect(await waitForHealthz(backend.port, 120_000), "the real server never became healthy").toBe(true);

      // Seed TWO independent background jobs through the production inbox intake:
      // each now-item fires on its own isolated job session and parks on a real
      // request_decision interrupt (the fake model's tool call).
      for (const name of ["alpha", "beta"]) {
        const seeded = await page.request.post(`${base}/api/inbox`, {
          data: { text: `[E2E-DECISION ${name}] decide the ${name} release`, priority: "now", source: "e2e" },
        });
        expect(seeded.ok()).toBe(true);
      }
      await expect
        .poll(
          async () => (await (await page.request.get(`${base}/api/decisions?state=pending`)).json()).pending,
          { timeout: 60_000 },
        )
        .toBe(2);
      const pendingListing = await (await page.request.get(`${base}/api/decisions?state=pending`)).json();
      const alpha = pendingListing.decisions.find((d: { question: string }) => d.question.includes("alpha"));
      const beta = pendingListing.decisions.find((d: { question: string }) => d.question.includes("beta"));
      // Both decisions live on isolated job execution sessions — no origin chat at all.
      expect(alpha.session_id).toMatch(/^job:/);
      expect(beta.session_id).toMatch(/^job:/);
      expect(alpha.session_id).not.toBe(beta.session_id);

      // The console against the real backend, in a FRESH chat the jobs never touched.
      await page.goto(`${base}/app/`);
      const pill = page.getByRole("button", { name: "Decisions, 2 waiting on you" });
      await expect(pill).toBeVisible({ timeout: 30_000 });

      // The approved fleet-wide Needs-you marker stays visible from ANOTHER chat.
      await page.locator(".pl-tabbar > .pl-tabbar__add").click();
      await expect(page.getByRole("button", { name: "Decisions, 2 waiting on you" })).toBeVisible();

      // Open Work: both job-session decisions are listed, with neutral meta copy and
      // no error banner — without ever opening either job session.
      await pill.click();
      const work = page.locator('.work-view[data-frame="modal"]');
      await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Needs you");
      const alphaRow = work.locator(".work-row", { hasText: "Ship the alpha release?" });
      await expect(alphaRow).toBeVisible();
      await expect(work.locator(".work-row", { hasText: "Ship the beta release?" })).toBeVisible();
      await expect(alphaRow).toContainText("Asked");
      await expect(work.locator(".pl-alert")).toHaveCount(0);

      // Answer alpha through Work's existing decision API (the real answer route,
      // real loopback resume, real checkpoint consumption).
      await alphaRow.click();
      const card = work.getByRole("article");
      await card.getByRole("button", { name: "Approve", exact: true }).click();
      await expect(work).toContainText('Sent: "Approve".', { timeout: 60_000 });
      await expect(work.locator(".pl-alert")).toHaveCount(0);

      // Only the owning job resumed: the gateway saw exactly one resumed completion,
      // and it was alpha's; beta's decision is still pending fleet-wide.
      await expect.poll(() => gateway.resumes.length, { timeout: 30_000 }).toBe(1);
      expect(gateway.resumes[0]).toContain("alpha");
      expect(gateway.resumes[0]).not.toContain("E2E-DECISION beta");
      expect(gateway.parks.length).toBe(2);
      await expect(page.getByRole("button", { name: "Decisions, 1 waiting on you" })).toBeVisible();

      const after = await (await page.request.get(`${base}/api/decisions?state=pending`)).json();
      expect(after.pending).toBe(1);
      expect(after.decisions[0].id).toBe(beta.id);
      const answered = await (await page.request.get(`${base}/api/decisions/${alpha.id}`)).json();
      expect(answered.state).toBe("answered");
      expect(answered.answer).toBe("Approve");
      expect(answered.answered_surface).toBe("console");
    } catch (error) {
      await testInfo.attach("real-backend-log", { body: backend.log.join(""), contentType: "text/plain" });
      throw error;
    } finally {
      backend.proc.kill("SIGTERM");
      await new Promise((done) => {
        const hardKill = setTimeout(() => {
          backend.proc.kill("SIGKILL");
          done(null);
        }, 5_000);
        backend.proc.once("exit", () => {
          clearTimeout(hardKill);
          done(null);
        });
      });
      await new Promise((done) => gateway.server.close(done));
    }
  });
});
