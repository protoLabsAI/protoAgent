import { useToast } from "@protolabsai/ui/overlays";

import { FS_ROOTS_QUERY_KEY } from "../chat/useEditorLinker";
import { api } from "../lib/api";
import { makeEditorLinker, joinProjectPath, type EditorId } from "../lib/editorLinks";
import { getEditorPref, getOpenFilesIn, type OpenFilesIn } from "../lib/editorPref";
import { queryClient } from "../lib/queryClient";
import { isCodePaneEnabled } from "./enabled";
import { openCode } from "./open";
import type { CodeRef } from "./store";

// "Open this code" from a plugin view (`protoagent:code:open`) — first user: the Artifact
// panel's code-linked Mermaid diagrams (ADR 0038 amendment). PluginView only calls this for a
// message from its OWN iframe at the plugin page's origin; the artifact shell only posts a
// target it looked up in the rendered version's STORED links (validated server-side against the
// fs fence), never a path the sandboxed diagram named. Here the target is shape-checked once
// more and routed by the operator's preferences:
//   1. the code pane (ADR 0112) when that toolset is on — through its fenced /api/fs/file,
//      which also refuses secret-like paths, so nothing here widens what can be shown;
//   2. otherwise the external editor link (Settings ▸ Chat ▸ Open files in), when there is one;
//   3. otherwise the path is copied, so the operator can still get there.

export type PluginCodeTarget = {
  project: string;
  path: string;
  line?: number;
  endLine?: number;
  note?: string;
};

const MAX = { project: 200, path: 4096, note: 280 };

const posInt = (n: unknown): n is number => typeof n === "number" && Number.isInteger(n) && n > 0;

/** A `protoagent:code:open` payload → a target, or null when it isn't one. A path the fence would
 *  never accept (absolute, `~`, `..`) is refused here too — the pane would only answer 400. */
export function parsePluginCodeOpen(m: unknown): PluginCodeTarget | null {
  if (!m || typeof m !== "object") return null;
  const o = m as Record<string, unknown>;
  if (o.type !== "protoagent:code:open") return null;
  const { project, path } = o;
  if (typeof project !== "string" || !project.trim() || project.length > MAX.project) return null;
  if (typeof path !== "string" || !path.trim() || path.length > MAX.path) return null;
  if (joinProjectPath("/r", path) === null) return null;
  const line = posInt(o.line) ? o.line : undefined;
  const end = o.end_line ?? o.endLine;
  const endLine = line && posInt(end) && end >= line ? end : undefined;
  const note = typeof o.note === "string" && o.note.trim() ? o.note.trim().slice(0, MAX.note) : undefined;
  return { project, path, line, endLine, note };
}

export function targetLabel(t: PluginCodeTarget): string {
  const range = t.line ? (t.endLine && t.endLine !== t.line ? `:${t.line}-${t.endLine}` : `:${t.line}`) : "";
  return `${t.project}/${t.path}${range}`;
}

export type RouteDeps = {
  paneOn: boolean;
  openIn: OpenFilesIn;
  editor: EditorId;
  /** `{project: absolute root}` for editor links (GET /api/fs/roots), or null. */
  roots: () => Promise<Record<string, string> | null>;
  open: (ref: CodeRef) => void;
  navigate: (href: string) => void;
  copy: (text: string) => Promise<boolean>;
};

export type RouteOutcome = "pane" | "editor" | "copied" | "none";

async function editorHref(t: PluginCodeTarget, deps: RouteDeps): Promise<string | null> {
  if (deps.editor === "off") return null;
  let roots: Record<string, string> | null = null;
  try {
    roots = await deps.roots();
  } catch {
    roots = null;
  }
  return makeEditorLinker(deps.editor, roots)?.(t.project, t.path, t.line) ?? null;
}

/** Route one target. Pure over `deps` (unit-tested); `defaultRouteDeps()` wires the console. */
export async function routePluginCodeOpen(t: PluginCodeTarget, deps: RouteDeps): Promise<RouteOutcome> {
  const pane = () =>
    deps.open({ project: t.project, path: t.path, line: t.line, endLine: t.endLine, note: t.note, source: "link" });
  if (deps.paneOn && deps.openIn !== "editor") {
    pane();
    return "pane";
  }
  const href = await editorHref(t, deps);
  if (href) {
    deps.navigate(href);
    return "editor";
  }
  // "Open files in: editor" with no usable editor link still has the pane to fall back on.
  if (deps.paneOn) {
    pane();
    return "pane";
  }
  return (await deps.copy(targetLabel(t))) ? "copied" : "none";
}

/** The console's deps. `fromSurface` is the plugin view that asked — kept on screen beside the
 *  code pane, so a diagram and the code it links to sit side by side. */
export function defaultRouteDeps(fromSurface?: string): RouteDeps {
  return {
    paneOn: isCodePaneEnabled(),
    openIn: getOpenFilesIn(),
    editor: getEditorPref(),
    roots: async () =>
      (
        await queryClient.fetchQuery({
          queryKey: FS_ROOTS_QUERY_KEY,
          queryFn: () => api.fsRoots(),
          staleTime: 60_000,
        })
      )?.roots ?? null,
    open: (ref) => openCode(ref, { keep: fromSurface }),
    navigate: (href) => {
      // A custom scheme (zed://, vscode://) goes to the OS without unloading the page.
      window.location.href = href;
    },
    copy: async (text) => {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        return false;
      }
    },
  };
}

/** The DS toast when a ToastProvider is mounted above, else null — PluginView also mounts in
 *  isolated trees (its unit tests) with no provider, where `useToast` throws. The context read
 *  inside `useToast` happens on every render either way, so hook order is stable. */
export function useOptionalToast(): ReturnType<typeof useToast> | null {
  try {
    return useToast();
  } catch {
    return null;
  }
}
