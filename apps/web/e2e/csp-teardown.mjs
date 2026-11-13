// Fails the run if the console's Content-Security-Policy blocked anything during it (bd-9ih.13).
// The mock server enforces the policy and appends every violation report it receives; a blocked
// font or image can break nothing a test looks at, so this is what makes such a block visible.
import { readFileSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";

const FILE = fileURLToPath(new URL("../test-results/csp-violations.jsonl", import.meta.url));

export function cspGlobalSetup() {
  rmSync(FILE, { force: true });
}

export default function cspTeardown() {
  let text = "";
  try {
    text = readFileSync(FILE, "utf8");
  } catch {
    return; // nothing reported
  }
  const lines = text.split("\n").filter(Boolean);
  if (!lines.length) return;
  const seen = new Set();
  for (const line of lines) {
    try {
      const r = JSON.parse(line)["csp-report"] || {};
      seen.add(`${r["effective-directive"] || r["violated-directive"]} blocked ${r["blocked-uri"]} (${r["source-file"] || r["document-uri"] || "?"})`);
    } catch {
      seen.add(line.slice(0, 200));
    }
  }
  throw new Error(`The console CSP blocked ${lines.length} load(s) during the e2e run:\n  ${[...seen].join("\n  ")}`);
}
