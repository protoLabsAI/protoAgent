import { create } from "zustand";

// The code pane's state (ADR 0112) — what file/range the pane shows, the "Recent" trail,
// which tab is up, and the follow-mode switches. A module-level zustand store (the ADR 0062
// docviewer pattern) so ANY caller — a tool-card link, the code-ref chip, the live tool
// stream — can seed it without a reference into the pane, and the pane (mounted or not)
// picks it up on its next render. Ephemeral by design: never persisted, so a reload lands on
// an empty pane instead of re-opening a file the operator has moved on from.

/** Where an open came from. Only telemetry-shaped today — the pane renders the same for all —
 *  but `follow` is what the throttle and the pin key off, so it has to be carried. */
export type CodeRefSource = "link" | "component" | "follow" | "recent" | "diff" | "palette";

export type CodeRef = {
  project: string;
  /** Project-relative path — exactly what the fs tools speak. */
  path: string;
  /** 1-based first line of the range to highlight + scroll to. */
  line?: number;
  /** 1-based last line (inclusive). Absent = just `line`. */
  endLine?: number;
  /** The agent's one-sentence "why this matters" (show_code), shown as a banner. */
  note?: string;
  source: CodeRefSource;
};

export type CodeTab = "file" | "diff";

export const RECENT_CAP = 20;

type CodeViewerState = {
  current: CodeRef | null;
  /** Bumped on every open — a re-open of the SAME ref must still re-scroll, and a
   *  value-keyed effect would not fire for it. */
  seq: number;
  recent: CodeRef[];
  tab: CodeTab;
  /** Diff tab: which project it shows (defaults to the current file's). */
  diffProject: string | null;
  /** Follow mode — the live tool stream moves the pane. Opt-in, default OFF. */
  follow: boolean;
  /** Pinned — follow is on but must not move the pane away from what's being read. */
  pinned: boolean;
};

export const useCodeViewer = create<CodeViewerState>(() => ({
  current: null,
  seq: 0,
  recent: [],
  tab: "file",
  diffProject: null,
  follow: false,
  pinned: false,
}));

const sameTarget = (a: CodeRef, b: CodeRef) =>
  a.project === b.project && a.path === b.path && (a.line ?? 0) === (b.line ?? 0) && (a.endLine ?? 0) === (b.endLine ?? 0);

/** Normalize a ref: positive integer lines, endLine ≥ line, trimmed note. Returns null for
 *  a ref with no project or path — there is nothing to show. */
export function normalizeRef(ref: CodeRef): CodeRef | null {
  const project = (ref.project || "").trim();
  const path = (ref.path || "").trim();
  if (!project || !path) return null;
  const pos = (n: unknown) => (typeof n === "number" && Number.isFinite(n) && n >= 1 ? Math.floor(n) : undefined);
  const line = pos(ref.line);
  let endLine = line ? pos(ref.endLine) : undefined;
  if (line && endLine && endLine < line) endLine = line;
  if (endLine === line) endLine = undefined;
  const note = typeof ref.note === "string" && ref.note.trim() ? ref.note.trim() : undefined;
  return { project, path, line, endLine, note, source: ref.source };
}

/** Put `ref` in front of the pane (File tab) and at the head of the Recent trail. Pure
 *  store write — routing the surface onto a dock is `openCode` (open.ts). */
export function showCodeRef(ref: CodeRef): CodeRef | null {
  const r = normalizeRef(ref);
  if (!r) return null;
  useCodeViewer.setState((s) => ({
    current: r,
    seq: s.seq + 1,
    tab: "file",
    // Revisiting from the trail keeps the trail's order — it's a history, not an MRU list
    // that reshuffles under the operator's cursor while they walk it.
    recent: r.source === "recent" ? s.recent : [r, ...s.recent.filter((x) => !sameTarget(x, r))].slice(0, RECENT_CAP),
  }));
  return r;
}

export function setCodeTab(tab: CodeTab): void {
  useCodeViewer.setState({ tab });
}

export function setDiffProject(project: string | null): void {
  useCodeViewer.setState({ diffProject: project });
}

export function setFollow(on: boolean): void {
  // Turning follow off also drops the pin — a pin only means something while following.
  useCodeViewer.setState(on ? { follow: true } : { follow: false, pinned: false });
}

export function setPinned(on: boolean): void {
  useCodeViewer.setState({ pinned: on });
}

/** Test-only: back to the pristine state. */
export function resetCodeViewer(): void {
  useCodeViewer.setState({
    current: null,
    seq: 0,
    recent: [],
    tab: "file",
    diffProject: null,
    follow: false,
    pinned: false,
  });
}
