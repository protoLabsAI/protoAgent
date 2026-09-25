// "Continue in Zed" (chat tab menu) — hand the current chat to Zed's agent panel.
//
// Zed can't deep-link into an agent thread (`zed://agent` only takes `?prompt=`), so the
// console OFFERS the session (POST /api/editor/handoff) and the protoagent-acp shim claims it
// when the operator starts a new thread in Zed under that project within 2 minutes. The
// thread then continues this chat: same A2A context, history replayed.
//
// Pure orchestration with every side effect injected, so the rules are unit-tested without a
// server or a browser (continueInZed.test.ts):
//   - project = the code pane's current file's project, if one is open; else omitted, which the
//     server stores as "any folder".
//   - the editor opens on the pane's current FILE (zed://file/<abs>:<line>) — never on a bare
//     project directory: the desktop shell refuses directory links on purpose (a directory
//     opens as a workspace, whose `.zed/settings.json` an agent with write access could have
//     planted), and the console only ever links files. With no file open, nothing is launched
//     and the toast asks the operator to switch to Zed themselves.

import { ApiError } from "../lib/api";
import { editorUrl, joinProjectPath } from "../lib/editorLinks";
import type { CodeRef } from "../codeviewer/store";

export type HandoffBody = { session_id: string; project?: string; path?: string; line?: number; title?: string };

export type ToastFn = (t: { tone: "success" | "error" | "info"; title: string; message: string }) => unknown;

export type ContinueInZedDeps = {
  post: (body: HandoffBody) => Promise<unknown>;
  /** `{project: absolute root}` — GET /api/fs/roots. Only asked for when a file is open. */
  roots: () => Promise<Record<string, string>>;
  navigate: (href: string) => void;
  toast: ToastFn;
};

export type ContinueInZedArgs = {
  sessionId: string;
  title?: string;
  /** The code pane's current file, if any. */
  current: CodeRef | null;
  /** The agent's display name — what the operator picks in Zed's agent panel. */
  agentName: string;
};

/** Returns the editor URL it opened (or null) — handy for tests and the e2e spec. */
export async function continueInZed(args: ContinueInZedArgs, deps: ContinueInZedDeps): Promise<string | null> {
  const { sessionId, title, current, agentName } = args;
  const body: HandoffBody = { session_id: sessionId };
  const t = (title || "").trim();
  if (t) body.title = t;
  if (current) {
    body.project = current.project;
    body.path = current.path;
    if (current.line) body.line = current.line;
  }
  try {
    await deps.post(body);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      deps.toast({ tone: "error", title: "Nothing to continue yet", message: "Send a message in this chat first." });
    } else {
      const msg = err instanceof Error ? err.message : String(err);
      deps.toast({ tone: "error", title: "Couldn't hand off to Zed", message: msg });
    }
    return null;
  }

  let href: string | null = null;
  if (current) {
    try {
      const roots = await deps.roots();
      const root = Object.prototype.hasOwnProperty.call(roots, current.project) ? roots[current.project] : undefined;
      const abs = root ? joinProjectPath(root, current.path) : null;
      href = abs ? editorUrl("zed", abs, current.line) : null;
    } catch {
      href = null; // the hand-off stands; only the jump is lost
    }
  }
  if (href) deps.navigate(href);
  deps.toast({
    tone: "success",
    title: "Ready to continue in Zed",
    message: href
      ? `Start a ${agentName} thread in Zed within 2 minutes to continue this chat.`
      : `Switch to Zed and start a ${agentName} thread within 2 minutes to continue this chat.`,
  });
  return href;
}
