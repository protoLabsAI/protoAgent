import { useToast } from "@protolabsai/ui/overlays";

import { FS_ROOTS_QUERY_KEY } from "../chat/useEditorLinker";
import { api } from "../lib/api";
import { editorLabel, makeEditorLinker, joinProjectPath, type EditorId } from "../lib/editorLinks";
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
// A page may instead name the destination with `open_in` — the same two actions a chat file
// link offers (click = pane, ⌘/Ctrl-click = editor; see makeFileLinker): "pane" prefers the code
// pane, "editor" the external editor, each falling back to the other and then to a copy. Absent
// or unknown = the preference routing above, so older pages behave exactly as before.

export type PluginCodeTarget = {
  project: string;
  path: string;
  line?: number;
  endLine?: number;
  note?: string;
  /** The page's explicit destination (`open_in`); undefined = route by the operator's prefs. */
  openIn?: PluginCodeDestination;
};

export type PluginCodeDestination = "pane" | "editor";

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
  const openIn = o.open_in === "pane" || o.open_in === "editor" ? o.open_in : undefined;
  return { project, path, line, endLine, note, ...(openIn ? { openIn } : {}) };
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
  // The pane first: an explicit "pane", or no explicit choice and the operator didn't pick the editor.
  const paneFirst = t.openIn ? t.openIn === "pane" : deps.openIn !== "editor";
  if (deps.paneOn && paneFirst) {
    pane();
    return "pane";
  }
  const href = await editorHref(t, deps);
  if (href) {
    deps.navigate(href);
    return "editor";
  }
  // An editor-first route with no usable editor link still has the pane to fall back on.
  if (deps.paneOn) {
    pane();
    return "pane";
  }
  return (await deps.copy(targetLabel(t))) ? "copied" : "none";
}

const PANE_HINT = "Turn on the Code pane in Tools ▸ Filesystem ▸ Shell & filesystem tools to view it here.";

/** The toast for a routed target, or null when it went where it was asked (or, without an
 *  explicit `open_in`, anywhere but a copy — the pre-`open_in` behaviour). Pure; unit-tested. */
export function codeOpenNotice(
  t: PluginCodeTarget,
  outcome: RouteOutcome,
  ctx: Pick<RouteDeps, "paneOn" | "editor">,
): { title: string; message: string } | null {
  const label = targetLabel(t);
  if (outcome === "none") return { title: "Code location", message: label };
  if (!t.openIn) {
    return outcome === "copied" ? { title: "Path copied", message: `${label} — no code pane or editor is set up.` } : null;
  }
  if (outcome === t.openIn) return null;
  const editor = ctx.editor === "off" ? "an external editor" : editorLabel(ctx.editor);
  if (t.openIn === "editor") {
    const why = ctx.editor === "off"
      ? "No external editor is set (Settings ▸ Chat ▸ Open files in)"
      : `No ${editor} link for this file — it needs a filesystem shared with the agent`;
    return outcome === "pane"
      ? { title: "Opened in the code pane", message: `${why}.` }
      : { title: "Path copied", message: `${label} — ${why.charAt(0).toLowerCase()}${why.slice(1)}, and the code pane is off.` };
  }
  // Asked for the pane, which is off (the only way a "pane" request lands elsewhere).
  return outcome === "editor"
    ? { title: `Opened in ${editor}`, message: `The code pane is off. ${PANE_HINT}` }
    : { title: "Path copied", message: `${label} — the code pane is off and no editor link is available. ${PANE_HINT}` };
}

/** The console's deps. The pane opens on the dock the operator keeps it on (placeCodeSurface),
 *  even when that dock is the one showing the plugin view that asked. */
export function defaultRouteDeps(): RouteDeps {
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
    open: (ref) => openCode(ref),
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
