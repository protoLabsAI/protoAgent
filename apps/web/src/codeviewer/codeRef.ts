import type { CodeRef } from "./store";

// The `code-ref` chat component's props (ADR 0112) — what the agent's `show_code` tool emits:
// `{project, path, line, end_line, note}`. Props are untrusted data off the wire (a fork, an
// older server, a hand-written component-v1 part), so every field is checked, never cast.

export const CODE_REF_COMPONENT = "code-ref";

export function codeRefFromProps(props: Record<string, unknown> | undefined): Omit<CodeRef, "source"> | null {
  if (!props) return null;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  const int = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && v >= 1 ? v : undefined);
  const project = str(props.project);
  const path = str(props.path);
  if (!project || !path) return null;
  const line = int(props.line);
  const endLine = line ? int(props.end_line) : undefined;
  const note = str(props.note) || undefined;
  return { project, path, line, endLine: endLine && endLine > line! ? endLine : undefined, note };
}

/** `path:12-18` / `path:12` / `path` — the chip's and the header's label. */
export function refLabel(ref: { path: string; line?: number; endLine?: number }): string {
  if (!ref.line) return ref.path;
  return ref.endLine && ref.endLine > ref.line ? `${ref.path}:${ref.line}-${ref.endLine}` : `${ref.path}:${ref.line}`;
}
