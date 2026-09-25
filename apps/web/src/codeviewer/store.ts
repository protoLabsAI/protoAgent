import { create } from "zustand";

// The code pane's state (ADR 0112) — what file/range the pane shows, the "Recent" trail,
// which tab is up, and the follow-mode switches. A module-level zustand store (the ADR 0062
// docviewer pattern) so ANY caller — a tool-card link, the code-ref chip, the live tool
// stream — can seed it without a reference into the pane, and the pane (mounted or not)
// picks it up on its next render. Not in the persisted UI store: the open file and the trail
// live in sessionStorage (this TAB only), so a reload with the Code surface up lands back on
// the file you were reading — while a new tab, or tomorrow, starts clean.

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

export const SESSION_KEY = "protoagent.codePane";

/** The last `{current, recent}` this tab saved, or empty. Every access wrapped — storage can
 *  throw (private mode, blocked site data) and the pane must render anyway. */
export function loadSession(): { current: CodeRef | null; recent: CodeRef[] } {
  try {
    const raw = globalThis.sessionStorage?.getItem(SESSION_KEY);
    if (!raw) return { current: null, recent: [] };
    const v = JSON.parse(raw) as { current?: unknown; recent?: unknown };
    const one = (x: unknown): CodeRef | null =>
      x && typeof x === "object" ? normalizeRef({ ...(x as CodeRef), source: "recent" }) : null;
    const current = one(v.current);
    const recent = Array.isArray(v.recent) ? v.recent.map(one).filter((r): r is CodeRef => r !== null) : [];
    return { current, recent: recent.slice(0, RECENT_CAP) };
  } catch {
    return { current: null, recent: [] };
  }
}

function saveSession(current: CodeRef | null, recent: CodeRef[]): void {
  try {
    globalThis.sessionStorage?.setItem(SESSION_KEY, JSON.stringify({ current, recent }));
  } catch {
    /* storage blocked — the pane just won't survive a reload */
  }
}

export const useCodeViewer = create<CodeViewerState>(() => ({
  ...loadSession(),
  seq: 0,
  tab: "file",
  diffProject: null,
  follow: false,
  pinned: false,
}));

useCodeViewer.subscribe((s, prev) => {
  if (s.current !== prev.current || s.recent !== prev.recent) saveSession(s.current, s.recent);
});

/** `./src//x.ts` → `src/x.ts` — the cheap client-side half of "one file, one Recent entry"
 *  (the server's canonical path, applied by `canonicalizeRef`, is the other half). */
export function tidyPath(path: string): string {
  const p = path.trim().replace(/\\/g, "/");
  // A leading "/" stays: an absolute path is the server's to refuse (bad_path), not ours to fix.
  const lead = p.startsWith("/") ? "/" : "";
  return lead + p.split("/").filter((seg) => seg !== "" && seg !== ".").join("/");
}

const sameTarget = (a: CodeRef, b: CodeRef) =>
  a.project === b.project && a.path === b.path && (a.line ?? 0) === (b.line ?? 0) && (a.endLine ?? 0) === (b.endLine ?? 0);

/** Normalize a ref: positive integer lines, endLine ≥ line, trimmed note. Returns null for
 *  a ref with no project or path — there is nothing to show. */
export function normalizeRef(ref: CodeRef): CodeRef | null {
  const project = (ref.project || "").trim();
  const path = tidyPath(ref.path || "");
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

/** The server answered with its CANONICAL relative path for `requested` (resolved through
 *  the fence: `./a`, `a//b`, a symlinked dir…). Rewrite the current ref and the trail to it,
 *  folding duplicates, so one file never shows as two Recent entries. No seq bump: this is
 *  the same open, just named correctly. */
export function canonicalizeRef(project: string, requested: string, canonical: string): void {
  if (!canonical || canonical === requested) return;
  useCodeViewer.setState((s) => {
    const fix = (r: CodeRef): CodeRef => (r.project === project && r.path === requested ? { ...r, path: canonical } : r);
    const current = s.current ? fix(s.current) : null;
    const recent: CodeRef[] = [];
    for (const r of s.recent.map(fix)) if (!recent.some((x) => sameTarget(x, r))) recent.push(r);
    return { current, recent };
  });
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
