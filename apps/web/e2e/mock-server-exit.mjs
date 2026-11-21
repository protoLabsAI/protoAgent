// Global setup + teardown around the e2e mock server (#5712).
// Setup clears the previous run's records; teardown runs the CSP check (csp-teardown.mjs) and
// fails the run, naming the exit code or signal and the last error, if the supervisor
// (mock-server-run.mjs) recorded that the mock server process exited mid-run.
import { readFileSync, rmSync } from "node:fs";
import cspTeardown, { cspGlobalSetup } from "./csp-teardown.mjs";
import { MOCK_EXIT_FILE } from "./mock-server-run.mjs";

export function mockServerExitError(file = MOCK_EXIT_FILE) {
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    return null; // no record: the server lived through the run
  }
  let exit = {};
  try {
    exit = JSON.parse(text);
  } catch {
    exit = { lastError: text.slice(0, 4000) };
  }
  const how = exit.signal ? `signal ${exit.signal}` : `exit code ${exit.code ?? "unknown"}`;
  return new Error(
    `The e2e mock server process exited during the run (${how}${exit.time ? `, at ${exit.time}` : ""}). ` +
      `"Connection refused" / ECONNREFUSED failures after that point are caused by it.\n` +
      `Last stderr output of the server:\n${exit.lastError || "(none)"}`,
  );
}

export function globalSetup() {
  cspGlobalSetup();
  rmSync(MOCK_EXIT_FILE, { force: true });
}

export default function globalTeardown() {
  const errors = [];
  const exited = mockServerExitError();
  if (exited) errors.push(exited);
  try {
    cspTeardown();
  } catch (err) {
    errors.push(err);
  }
  if (errors.length === 1) throw errors[0];
  if (errors.length) throw new Error(errors.map((e) => e.message).join("\n\n"));
}
