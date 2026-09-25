import { parseFsArgs, readRange, SEARCH_HIT } from "../chat/fsToolRenderers";
import type { CodeRef } from "./store";

// Follow mode (ADR 0112): which file (and lines) a COMPLETED fs tool call touched, so the
// pane can move there. Pure — the live stream handler (ChatSurface onToolCall) calls it and
// hands the result to `followCode`, which owns the opt-in, the pin and the throttle.

export const FOLLOW_TOOLS = new Set(["read_file", "search_files", "edit_file", "write_file"]);

export function followRefFromTool(
  name: string,
  input: string | undefined,
  output: string | undefined,
): Omit<CodeRef, "source"> | null {
  if (!FOLLOW_TOOLS.has(name)) return null;
  const args = parseFsArgs(input);
  if (!args.project) return null;
  if (name === "search_files") {
    // `path` is usually a DIRECTORY here — follow the first hit instead (`file:line: text`),
    // the line the operator would click first. A context line carries `-N-`, not `:N:`.
    for (const ln of (output || "").split("\n")) {
      const m = ln.match(SEARCH_HIT);
      if (m && !/-\d+- /.test(m[1])) return { project: args.project, path: m[1], line: Number(m[2]) };
    }
    return null;
  }
  if (!args.path) return null;
  if (name === "read_file") return { project: args.project, path: args.path, ...readRange(args) };
  return { project: args.project, path: args.path };
}
