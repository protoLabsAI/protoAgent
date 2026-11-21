import { type ChildProcess, spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { mockServerExitError } from "./mock-server-exit.mjs";

// The e2e mock backend must not die silently (#5712). An aborted `POST /api/chat/presence`
// (WebKit closing a page with the body still unsent) rejected the request handler; the
// rejection was unhandled, node exited, and every later test failed with "Connection refused".
// These tests start their OWN server processes on free ports, so nothing here can disturb the
// mock the other specs share.

const E2E_DIR = fileURLToPath(new URL(".", import.meta.url));
const MOCK = join(E2E_DIR, "mock-server.mjs");
const SUPERVISOR = join(E2E_DIR, "mock-server-run.mjs");
const DYING = join(E2E_DIR, "fixtures", "dying-server.mjs");

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address() as { port: number };
      probe.close(() => resolve(port));
    });
  });
}

type Proc = { child: ChildProcess; stderr: () => string; exited: Promise<{ code: number | null; signal: string | null }> };

function run(script: string, args: string[], env: Record<string, string> = {}): Proc {
  const child = spawn(process.execPath, [script, ...args], {
    env: { ...process.env, ...env },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let err = "";
  child.stderr!.on("data", (chunk) => (err += chunk));
  const exited = new Promise<{ code: number | null; signal: string | null }>((resolve) =>
    child.once("close", (code, signal) => resolve({ code, signal })),
  );
  return { child, stderr: () => err, exited };
}

async function healthz(port: number): Promise<number> {
  return (await fetch(`http://127.0.0.1:${port}/healthz`)).status;
}

async function waitUp(port: number) {
  await expect
    .poll(() => healthz(port).catch(() => 0), { message: "the mock server answers /healthz" })
    .toBe(200);
}

test.describe("a request that fails costs that request, not the server", () => {
  let port = 0;
  let server: Proc;

  test.beforeAll(async () => {
    port = await freePort();
    server = run(MOCK, [String(port)]);
    await waitUp(port);
  });
  test.afterAll(() => {
    server?.child.kill("SIGKILL");
  });

  test("a handler that throws answers 500 (or is logged, once it started answering) and the server keeps serving", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/__e2e/throw`);
    expect(res.status).toBe(500);
    expect(await res.json()).toMatchObject({ error: "e2e deliberate handler failure" });
    expect(server.stderr()).toContain("[e2e mock] GET /__e2e/throw handler failed: Error: e2e deliberate handler failure");
    expect(await healthz(port)).toBe(200);
    expect(server.child.exitCode).toBeNull();

    // The same failure after the handler started answering: no 500 can be sent, so it is logged.
    await fetch(`http://127.0.0.1:${port}/__e2e/throw?late=1`)
      .then((res) => res.text())
      .catch(() => "");
    await expect.poll(() => server.stderr()).toContain("[e2e mock] GET /__e2e/throw?late=1 handler failed");
    expect(await healthz(port)).toBe(200);
    expect(server.child.exitCode).toBeNull();
  });

  test("a POST whose connection closes before its body arrives does not stop the server", async () => {
    // What killed the server in #5712: headers sent, body promised, connection gone.
    await new Promise<void>((resolve, reject) => {
      const socket = connect(port, "127.0.0.1", () => {
        socket.write(
          "POST /api/chat/presence HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n" +
            'Content-Length: 200\r\n\r\n{"console_id":"a"',
          () => setTimeout(() => (socket.destroy(), resolve()), 100),
        );
      });
      socket.once("error", reject);
    });
    await expect
      .poll(() => server.stderr())
      .toContain("[e2e mock] POST /api/chat/presence: client closed the connection mid-request");
    expect(await healthz(port)).toBe(200);
    expect(server.child.exitCode).toBeNull();
  });

  test("the presence report is answered like the real route: 204, or 400 when malformed", async () => {
    const post = (body: unknown) =>
      fetch(`http://127.0.0.1:${port}/api/chat/presence`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    expect((await post({ console_id: "c1", session: "s1", present: true, input_age_s: 2.5 })).status).toBe(204);
    expect((await post({ console_id: "c1", session: "s1", present: "yes", input_age_s: 2.5 })).status).toBe(400);
  });
});

test.describe("a mock server that exits mid-run is reported", () => {
  let dir = "";
  test.beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "e2e-mock-exit-"));
  });
  test.afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  test("an exit code or signal and the last error reach stderr and the teardown's failure", async () => {
    const file = join(dir, "code.json");
    const sup = run(SUPERVISOR, [], { E2E_MOCK_SERVER_SCRIPT: DYING, E2E_MOCK_EXIT_FILE: file });
    expect(await sup.exited).toEqual({ code: 3, signal: null });
    expect(sup.stderr()).toContain("THE MOCK SERVER PROCESS EXITED (exit code 3)");
    expect(sup.stderr()).toContain("last stderr output of the server:\n[fixture] Error: the last thing the server said");
    expect(JSON.parse(readFileSync(file, "utf8"))).toMatchObject({
      code: 3,
      signal: null,
      lastError: "[fixture] Error: the last thing the server said",
    });
    const error = mockServerExitError(file);
    expect(error?.message).toContain("exited during the run (exit code 3");
    expect(error?.message).toContain("[fixture] Error: the last thing the server said");

    // A server ended by a signal is named by that signal.
    const signalFile = join(dir, "signal.json");
    const killed = run(SUPERVISOR, ["signal"], { E2E_MOCK_SERVER_SCRIPT: DYING, E2E_MOCK_EXIT_FILE: signalFile });
    expect((await killed.exited).code).toBe(1);
    expect(killed.stderr()).toContain("THE MOCK SERVER PROCESS EXITED (signal SIGKILL)");
    expect(mockServerExitError(signalFile)?.message).toContain("exited during the run (signal SIGKILL");
  });

  test("a server that is still running, or was asked to stop, leaves no record", async () => {
    const file = join(dir, "clean.json");
    expect(mockServerExitError(file)).toBeNull();
    const port = await freePort();
    const sup = run(SUPERVISOR, [String(port)], { E2E_MOCK_EXIT_FILE: file });
    try {
      await waitUp(port);
      sup.child.kill("SIGTERM");
      expect((await sup.exited).code).toBe(0);
    } finally {
      sup.child.kill("SIGKILL");
    }
    expect(sup.stderr()).not.toContain("THE MOCK SERVER PROCESS EXITED");
    expect(mockServerExitError(file)).toBeNull();
  });
});
