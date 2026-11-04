import { isRunId } from "../lib/viewDeepLink";

// `protoagent:run:open` from a plugin view (#4726): "show the operator this run". First user:
// the org chart's drawer, whose history rows and live cards name an edge or run id.
//
//   { type: "protoagent:run:open", run_id: "<any id the run route resolves>" }
//
// PluginView only hands this a message from its OWN iframe at the plugin page's origin (the
// same trust as `protoagent:code:open`). The id is shape-checked like a `?run=` link: it only
// ever becomes an encoded `/api/runs/{id}` read in a drawer the operator can close, never a
// navigation, a write, or anything a page could chain into more.

/** The run id a `protoagent:run:open` message names, or null when it isn't one. */
export function parsePluginRunOpen(m: unknown): string | null {
  if (!m || typeof m !== "object") return null;
  const o = m as Record<string, unknown>;
  if (o.type !== "protoagent:run:open") return null;
  const id = typeof o.run_id === "string" ? o.run_id.trim() : "";
  return isRunId(id) ? id : null;
}
