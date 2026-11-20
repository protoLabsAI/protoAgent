/** A short record of what happened to a tapped notification, for the one place it can't be
 *  observed from outside: an installed phone console. The push worker writes its half to a
 *  Cache Storage entry (public/sw.js `trace`); the console writes its half here. Opaque notice
 *  ids, page paths and step names only — never notice text, tokens or endpoints. */
import { readKey, writeKey } from "./storage";

export type TapStep = { at: number; who: "worker" | "console"; step: string; detail: string };

const KEY = "protoagent.noticeTapLog";
const MAX = 40;
const TRACE_CACHE = "protoagent-notice-trace"; // sw.js TRACE_CACHE
const TRACE_KEY = "/app/__notice-trace"; // sw.js TRACE_KEY

function valid(row: unknown): row is TapStep {
  const r = row as TapStep | null;
  return !!r && typeof r.at === "number" && typeof r.step === "string" && typeof r.detail === "string" &&
    (r.who === "worker" || r.who === "console");
}

function consoleSteps(): TapStep[] {
  try {
    const rows = JSON.parse(readKey("local", KEY) || "[]") as unknown;
    return Array.isArray(rows) ? rows.filter(valid) : [];
  } catch {
    return [];
  }
}

/** Record one console-side step. Best effort: a full or blocked store loses the line. */
export function logTap(step: string, detail = ""): void {
  try {
    const rows = [...consoleSteps(), { at: Date.now(), who: "console" as const, step, detail: detail.slice(0, 160) }];
    writeKey("local", KEY, JSON.stringify(rows.slice(-MAX)));
  } catch { /* diagnostics must never break the console */ }
}

async function workerSteps(): Promise<TapStep[]> {
  try {
    if (typeof caches === "undefined" || !(await caches.has(TRACE_CACHE))) return [];
    const hit = await (await caches.open(TRACE_CACHE)).match(TRACE_KEY);
    const rows = hit ? ((await hit.json()) as unknown) : [];
    return Array.isArray(rows) ? rows.filter(valid) : [];
  } catch {
    return [];
  }
}

/** Both halves, oldest first. */
export async function readTapLog(): Promise<TapStep[]> {
  return [...(await workerSteps()), ...consoleSteps()].sort((a, b) => a.at - b.at);
}

export async function clearTapLog(): Promise<void> {
  try { writeKey("local", KEY, "[]"); } catch { /* ignore */ }
  try { if (typeof caches !== "undefined") await caches.delete(TRACE_CACHE); } catch { /* ignore */ }
}

/** The log as plain lines, ready to paste into a message. */
export function formatTapLog(rows: TapStep[]): string {
  return rows.map((r) => `${new Date(r.at).toISOString().slice(11, 23)} ${r.who} ${r.step}${r.detail ? ` — ${r.detail}` : ""}`).join("\n");
}
