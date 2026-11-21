// Supervisor for the e2e mock backend (#5712). Playwright's `webServer` starts THIS, and it
// runs mock-server.mjs as a child. If the child exits while the run is still going, a dead
// port only shows up as "Connection refused" in every later test; this says so instead: the
// exit code or signal and the server's last stderr output go to stderr (Playwright prints it
// as `[WebServer] …`) and to a marker file that the global teardown turns into a run failure.
//
// It never restarts the server: a restart would lose the mock's in-memory state and hide the
// failure. Playwright's normal shutdown kills the whole process group, so no report is made.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

export const MOCK_EXIT_FILE =
  process.env.E2E_MOCK_EXIT_FILE || fileURLToPath(new URL("../test-results/mock-server-exit.json", import.meta.url));
const TAIL_CHARS = 4000;

function main() {
  const script = process.env.E2E_MOCK_SERVER_SCRIPT || fileURLToPath(new URL("./mock-server.mjs", import.meta.url));
  const child = spawn(process.execPath, [script, ...process.argv.slice(2)], { stdio: ["ignore", "inherit", "pipe"] });
  let tail = "";
  child.stderr.on("data", (chunk) => {
    process.stderr.write(chunk);
    tail = (tail + chunk).slice(-TAIL_CHARS);
  });

  // Asked to stop (Ctrl-C, a graceful webServer shutdown): pass it on, and it is not a death.
  let stopping = false;
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
    process.on(signal, () => {
      stopping = true;
      child.kill(signal);
    });
  }

  child.on("error", (err) => {
    tail = (tail + `\nspawn failed: ${err?.stack || err}`).slice(-TAIL_CHARS);
  });
  // `close`, not `exit`: stderr has been fully read by then, so the tail is complete.
  child.on("close", (code, signal) => {
    if (stopping) process.exit(0);
    const how = signal ? `signal ${signal}` : `exit code ${code}`;
    const lastError = tail.trim() || "(the server wrote nothing to stderr)";
    console.error(
      `[e2e mock] THE MOCK SERVER PROCESS EXITED (${how}) while the run was still going. ` +
        `Tests after this point fail with "Connection refused" because of it, not on their own.\n` +
        `[e2e mock] last stderr output of the server:\n${lastError}`,
    );
    try {
      mkdirSync(dirname(MOCK_EXIT_FILE), { recursive: true });
      writeFileSync(MOCK_EXIT_FILE, JSON.stringify({ code, signal, lastError, time: new Date().toISOString() }));
    } catch (err) {
      console.error(`[e2e mock] could not write ${MOCK_EXIT_FILE}: ${err?.stack || err}`);
    }
    process.exit(code || 1);
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
