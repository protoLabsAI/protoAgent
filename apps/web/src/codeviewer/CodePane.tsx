import "./code-pane.css";

import { File as PierreFile, PatchDiff, useVirtualizer, Virtualizer } from "@pierre/diffs/react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useToast } from "@protolabsai/ui/overlays";
import { Tabs } from "@protolabsai/ui/navigation";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { DropdownSelect } from "@protolabsai/ui/forms";
import {
  Copy,
  Eye,
  EyeOff,
  FileCode2,
  FileDiff as FileDiffIcon,
  GitBranch,
  History,
  Lock,
  Pin,
  PinOff,
  SquareArrowOutUpRight,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { FS_ROOTS_QUERY_KEY, useEditorLinker } from "../chat/useEditorLinker";
import { api, ApiError, type FsDiffFile, type FsFile } from "../lib/api";
import { editorLabel } from "../lib/editorLinks";
import { useEditorPref } from "../lib/editorPref";
import { useIsMobile } from "../lib/useIsMobile";
import { refLabel } from "./codeRef";
import { splitPatch, type PatchFile } from "./diffParse";
import { openCode } from "./open";
import {
  setCodeTab,
  setDiffProject,
  setFollow,
  setPinned,
  useCodeViewer,
  type CodeRef,
  type CodeTab,
} from "./store";
import { useThemeMode } from "./themeMode";

// The code pane (ADR 0112) — a READ-ONLY view of the agent's project files beside chat, for
// the operator as navigator: the agent points (show_code, a tool-card link, follow mode) and
// the operator reads the evidence at their own pace. Lazy-loaded (CodeSurface.tsx): the
// highlighter (@pierre/diffs over Shiki) is only fetched once the pane is first shown.

const THEMES = { dark: "github-dark", light: "github-light" } as const;

/** Past this many lines a window of the file is fetched around the target line; the rows
 *  before it are padded so the gutter still shows TRUE line numbers. */
const WINDOW_BEFORE = 5_000;
const WINDOW_SIZE = 20_000;
/** pierre's row height (DEFAULT_VIRTUAL_FILE_METRICS.lineHeight) — the scroll estimate for a
 *  row the virtual window hasn't rendered yet. */
const ROW_PX = 20;
/** Past this many lines the file renders as plain text: Shiki on 20k lines blocks the main
 *  thread for ~7 s (measured), and a pane that freezes the console is worse than no colour. */
const HIGHLIGHT_MAX_LINES = 5_000;

export default function CodePane() {
  const tab = useCodeViewer((s) => s.tab);
  const follow = useCodeViewer((s) => s.follow);
  const pinned = useCodeViewer((s) => s.pinned);
  const isMobile = useIsMobile();

  return (
    <section className="panel stage-panel code-pane" data-testid="code-pane">
      <div className="code-pane__bar">
        <Tabs
          active={tab}
          onSelect={(t) => setCodeTab(t as CodeTab)}
          items={[
            { id: "file", label: "File", icon: <FileCode2 size={14} /> },
            { id: "diff", label: "Diff", icon: <FileDiffIcon size={14} /> },
          ]}
          ariaLabel="Code pane view"
        />
        {/* Follow mode is desktop-only: on a phone the pane is a pushed screen, and moving
            it under the operator's thumb would be the pane driving, not them. */}
        {!isMobile ? (
          <div className="code-pane__follow">
            <Button
              size="sm"
              variant={follow ? "primary" : "ghost"}
              aria-pressed={follow}
              data-testid="code-follow"
              title={
                follow
                  ? "Following: the pane moves to each file the agent reads or edits. Click to stop."
                  : "Follow the agent: move the pane to each file it reads or edits (off by default)"
              }
              onClick={() => setFollow(!follow)}
            >
              {follow ? <Eye size={14} aria-hidden /> : <EyeOff size={14} aria-hidden />}
              Follow
            </Button>
            {follow ? (
              <Button
                size="sm"
                icon
                variant={pinned ? "primary" : "ghost"}
                aria-pressed={pinned}
                aria-label={pinned ? "Unpin: let follow move the pane again" : "Pin: keep this file while following"}
                title={pinned ? "Pinned — follow won't move the pane. Click to unpin." : "Pin this file while following"}
                data-testid="code-pin"
                onClick={() => setPinned(!pinned)}
              >
                {pinned ? <PinOff size={14} aria-hidden /> : <Pin size={14} aria-hidden />}
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      {tab === "file" ? <FileTab /> : <DiffTab />}
    </section>
  );
}

// ── File tab ─────────────────────────────────────────────────────────────────────────────

function FileTab() {
  const current = useCodeViewer((s) => s.current);
  const seq = useCodeViewer((s) => s.seq);
  const recent = useCodeViewer((s) => s.recent);

  if (!current) {
    return (
      <div className="code-pane__body code-pane__body--empty">
        <Empty
          icon={<FileCode2 size={22} />}
          title="No file open"
          description="Click a file path in a tool result, or ask the agent to show you the code it's talking about."
        />
        <RecentTrail recent={recent} current={null} />
      </div>
    );
  }
  return <FileView key={`${current.project}\u0000${current.path}`} current={current} seq={seq} recent={recent} />;
}

function errorKind(error: unknown): "denied" | "gone" | "bad" | "other" {
  if (error instanceof ApiError) {
    if (error.status === 403) return "denied";
    if (error.status === 404) return "gone";
    if (error.status === 400) return "bad";
  }
  return "other";
}

async function loadFile(ref: CodeRef): Promise<FsFile> {
  const first = await api.fsFile(ref.project, ref.path);
  // A capped read that stops before the target line: fetch a window around it instead.
  if (first.truncated && ref.line && ref.line > first.end) {
    const start = Math.max(1, ref.line - WINDOW_BEFORE);
    return api.fsFile(ref.project, ref.path, { start, end: start + WINDOW_SIZE - 1 });
  }
  return first;
}

function FileView({ current, seq, recent }: { current: CodeRef; seq: number; recent: CodeRef[] }) {
  const mode = useThemeMode();
  const toast = useToast();
  const editor = useEditorPref();
  const external = useEditorLinker();
  const bodyRef = useRef<HTMLDivElement>(null);
  const virtRef = useRef<VirtualizerHandle | null>(null);

  // Keyed on `seq` too: every open re-reads the file, so a follow jump after an edit_file (or
  // the operator revisiting from Recent) shows the file as it is NOW, not a cached copy.
  const q = useQuery({
    queryKey: ["code-pane-file", current.project, current.path, current.line ?? 0, seq],
    queryFn: () => loadFile(current),
    retry: false,
    staleTime: Infinity,
    gcTime: 30_000,
    // FileView is keyed per FILE, so the previous data is always this same file: keep it on
    // screen while a re-open refetches instead of flashing "Loading…" between two lines.
    placeholderData: keepPreviousData,
  });
  const data = q.data;

  const file = useMemo(() => {
    if (!data || data.binary || data.text == null) return null;
    // Pad a windowed read so line N renders at row N (pierre numbers rows from 1).
    const pad = data.start > 1 ? "\n".repeat(data.start - 1) : "";
    return {
      name: data.path.split("/").pop() || data.path,
      contents: pad + data.text,
      // No `lang`: pierre infers it from the file name, from the same extension table the
      // server's `language` guess comes from — and an id Shiki doesn't bundle would render
      // an error block where the file should be.
    };
  }, [data]);

  const options = useMemo(
    () => ({
      theme: THEMES,
      themeType: mode,
      disableFileHeader: true,
      overflow: "scroll" as const,
      tokenizeMaxLength: HIGHLIGHT_MAX_LINES,
    }),
    [mode],
  );
  const selected = useMemo(
    () => (current.line ? { start: current.line, end: current.endLine ?? current.line } : null),
    [current.line, current.endLine],
  );

  useScrollToLine(bodyRef, virtRef, current.line, seq, Boolean(file));

  const href = external?.(current.project, current.path, current.line) ?? null;
  const copyPath = async () => {
    try {
      await navigator.clipboard.writeText(current.path);
      toast({ tone: "success", title: "Copied", message: current.path });
    } catch {
      toast({ tone: "error", title: "Couldn't copy", message: current.path });
    }
  };

  return (
    <div className="code-pane__file">
      <div className="code-pane__head">
        <div className="code-pane__where" title={`${current.project}/${current.path}`}>
          <span className="code-pane__project">{current.project}</span>
          <span className="code-pane__sep">·</span>
          <span className="code-pane__path" data-testid="code-pane-path">
            {current.path}
          </span>
          {current.line ? (
            <span className="code-pane__range" data-testid="code-pane-range">
              {current.endLine ? `L${current.line}–${current.endLine}` : `L${current.line}`}
            </span>
          ) : null}
        </div>
        <div className="code-pane__actions">
          <Button size="sm" icon variant="ghost" aria-label="Copy path" title="Copy path" onClick={copyPath}>
            <Copy size={14} aria-hidden />
          </Button>
          {href ? (
            <a
              className="pl-btn pl-btn--ghost pl-btn--sm pl-btn--icon"
              href={href}
              aria-label={`Open in ${editorLabel(editor)}`}
              title={`Open in ${editorLabel(editor)}`}
              data-testid="code-pane-external"
            >
              <SquareArrowOutUpRight size={14} aria-hidden />
            </a>
          ) : null}
        </div>
      </div>
      {current.note ? (
        <div className="code-pane__note" data-testid="code-pane-note">
          {current.note}
        </div>
      ) : null}
      {data?.truncated ? (
        <div className="code-pane__notice">
          Showing lines {data.start}–{data.end} of {data.line_count} — the rest is past the viewer's cap.
        </div>
      ) : null}
      <div className="code-pane__body" ref={bodyRef} data-testid="code-pane-body">
        {q.isPending ? (
          <div className="code-pane__status">Loading {current.path}…</div>
        ) : q.isError ? (
          <FileError error={q.error} path={current.path} />
        ) : data?.binary ? (
          <div className="code-pane__status" data-testid="code-pane-binary">
            Binary file · {formatBytes(data.size)} — not shown.
          </div>
        ) : file ? (
          <Virtualizer className="code-pane__virt">
            <VirtualizerBridge into={virtRef} />
            <PierreFile file={file} options={options} selectedLines={selected} className="code-pane__view" />
          </Virtualizer>
        ) : null}
      </div>
      <RecentTrail recent={recent} current={current} />
    </div>
  );
}

function FileError({ error, path }: { error: unknown; path: string }) {
  const kind = errorKind(error);
  if (kind === "denied") {
    return (
      <div className="code-pane__status code-pane__status--denied" data-testid="code-pane-denied">
        <Lock size={14} aria-hidden /> Hidden: secret-like file
      </div>
    );
  }
  if (kind === "gone") {
    return (
      <div className="code-pane__status" data-testid="code-pane-gone">
        {path} no longer exists.
      </div>
    );
  }
  return (
    <div className="code-pane__status" data-testid="code-pane-error">
      {kind === "bad" ? `Can't open ${path} — it's outside the agent's work folders, or not a file.` : `Couldn't load ${path}.`}
    </div>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Scroll the target line into view. Keyed on `seq`, so re-opening the SAME ref re-scrolls.
 *  The file is virtualized (pierre's Virtualizer is the scroll container), so the target row
 *  may not exist yet: jump to its ESTIMATED offset first (rows are a fixed height), then poll
 *  a few frames for the real row — rendered into pierre's shadow root once the highlight pass
 *  and the virtual window land — and settle on its measured position. */
type VirtualizerHandle = { scrollTo: (o: { top: number; behavior?: ScrollBehavior }) => void };

/** Hands the enclosing Virtualizer instance out to the scroll effect. Scrolling THROUGH it
 *  matters: a raw `scrollTop =` write is undone by its anchor-preserving "scroll fix" (it
 *  snapped a jump to line 15000 of 20000 straight to the bottom). */
function VirtualizerBridge({ into }: { into: React.MutableRefObject<VirtualizerHandle | null> }) {
  const v = useVirtualizer();
  useEffect(() => {
    into.current = v ?? null;
    return () => {
      into.current = null;
    };
  }, [v, into]);
  return null;
}

function useScrollToLine(
  bodyRef: React.RefObject<HTMLDivElement | null>,
  virtRef: React.RefObject<VirtualizerHandle | null>,
  line: number | undefined,
  seq: number,
  ready: boolean,
) {
  useEffect(() => {
    if (!ready) return;
    const body = bodyRef.current;
    if (!body) return;
    const scroller = () => body.querySelector<HTMLElement>(".code-pane__virt") ?? body;
    const scrollTo = (top: number) => {
      if (virtRef.current) virtRef.current.scrollTo({ top, behavior: "instant" });
      else scroller().scrollTop = top;
    };
    if (!line) {
      scrollTo(0);
      return;
    }
    let raf = 0;
    let frames = 0;
    let estimated = false;
    let settled = 0;
    const tick = () => {
      const sc = scroller();
      const row = body
        .querySelector("diffs-container")
        ?.shadowRoot?.querySelector<HTMLElement>(`[data-line="${line}"]`);
      if (row) {
        // The range's first line a third of the way down — context above, room for the range.
        // Re-measure until it holds still: the virtual window re-lays rows out after a jump,
        // so the first measurement can be taken mid-layout.
        const top = row.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
        const want = Math.max(0, top - sc.clientHeight / 3);
        if (Math.abs(sc.scrollTop - want) <= 2 && ++settled >= 3) return;
        if (Math.abs(sc.scrollTop - want) > 2) {
          settled = 0;
          scrollTo(want);
        }
      }
      // Not rendered yet: jump to its estimated offset (re-issued every ~30 frames, in case a
      // layout pass moved us off it before the window caught up).
      if (!row && body.querySelector("diffs-container") && (!estimated || frames % 30 === 0)) {
        estimated = true;
        scrollTo(Math.max(0, ROW_PX * (line - 1) - sc.clientHeight / 3));
      }
      if (++frames < 240) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [bodyRef, virtRef, line, seq, ready]);
}

function RecentTrail({ recent, current }: { recent: CodeRef[]; current: CodeRef | null }) {
  const [open, setOpen] = useState(false);
  if (recent.length === 0) return null;
  return (
    <div className="code-pane__recent" data-testid="code-pane-recent">
      <button type="button" className="code-pane__recent-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
        <History size={13} aria-hidden />
        Recent <span className="code-pane__count">{recent.length}</span>
      </button>
      {open ? (
        <ol className="code-pane__recent-list">
          {recent.map((r, i) => {
            const active =
              current &&
              current.project === r.project &&
              current.path === r.path &&
              current.line === r.line &&
              current.endLine === r.endLine;
            return (
              <li key={`${i}:${r.project}:${refLabel(r)}`}>
                <button
                  type="button"
                  className={`code-pane__recent-item${active ? " is-active" : ""}`}
                  title={r.note || `${r.project}/${refLabel(r)}`}
                  onClick={() => openCode({ ...r, source: "recent" })}
                >
                  <span className="code-pane__recent-path">{refLabel(r)}</span>
                  <span className="code-pane__recent-project">{r.project}</span>
                </button>
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}

// ── Diff tab ─────────────────────────────────────────────────────────────────────────────

function DiffTab() {
  const current = useCodeViewer((s) => s.current);
  const diffProject = useCodeViewer((s) => s.diffProject);
  const roots = useQuery({ queryKey: FS_ROOTS_QUERY_KEY, queryFn: () => api.fsRoots(), staleTime: 60_000, retry: false });
  const projects = useMemo(() => Object.keys(roots.data?.roots ?? {}).sort(), [roots.data]);
  const project = diffProject ?? current?.project ?? projects[0] ?? null;

  if (!project) {
    return (
      <div className="code-pane__body code-pane__body--empty">
        <Empty
          icon={<GitBranch size={22} />}
          title="No project"
          description={roots.isPending ? "Loading work folders…" : "The agent has no work folders to diff."}
        />
      </div>
    );
  }
  return <DiffView key={project} project={project} projects={projects} />;
}

function DiffView({ project, projects }: { project: string; projects: string[] }) {
  const mode = useThemeMode();
  const q = useQuery({
    queryKey: ["code-pane-diff", project],
    queryFn: () => api.fsDiff(project),
    retry: false,
    staleTime: 5_000,
  });
  const patches = useMemo(() => splitPatch(q.data?.patch ?? ""), [q.data]);
  const files = q.data?.files ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  const selectable = files.filter((f) => !f.denied && !f.binary);
  const active = picked ?? selectable[0]?.path ?? null;
  const patch: PatchFile | undefined = patches.find((p) => p.path === active);
  const options = useMemo(
    () => ({
      theme: THEMES,
      themeType: mode,
      diffStyle: "unified" as const,
      overflow: "scroll" as const,
      disableFileHeader: true,
      // A click on a diff row opens that line in the File tab — the new-file side's
      // number for an added/context row, the old side's for a removed one.
      onLineClick: (props: { lineNumber: number }) => {
        if (active) openCode({ project, path: active, line: props.lineNumber, source: "diff" });
      },
    }),
    [mode, active, project],
  );
  const choices = projects.includes(project) ? projects : [project, ...projects];

  return (
    <div className="code-pane__diff" data-testid="code-pane-diff">
      <div className="code-pane__head">
        <div className="code-pane__where">
          {choices.length > 1 ? (
            <DropdownSelect
              aria-label="Project"
              value={project}
              onValueChange={(v) => setDiffProject(v)}
              options={choices.map((p) => ({ value: p, label: p }))}
            />
          ) : (
            <span className="code-pane__project">{project}</span>
          )}
          {q.data?.is_git && q.data.branch ? (
            <span className="code-pane__branch" title={q.data.head}>
              <GitBranch size={12} aria-hidden /> <span className="code-pane__branch-name">{q.data.branch}</span>
            </span>
          ) : null}
        </div>
        <div className="code-pane__actions">
          <Button size="sm" variant="ghost" onClick={() => void q.refetch()} loading={q.isFetching}>
            Refresh
          </Button>
        </div>
      </div>
      {q.isPending ? (
        <div className="code-pane__status">Loading changes…</div>
      ) : q.isError ? (
        <div className="code-pane__status" data-testid="code-pane-error">
          {q.error instanceof ApiError && q.error.status === 504
            ? "git took too long to answer — try Refresh."
            : "Couldn't load the diff."}
        </div>
      ) : !q.data?.is_git ? (
        <div className="code-pane__status">{project} isn't a git repository — there's no diff to show.</div>
      ) : files.length === 0 ? (
        <div className="code-pane__status" data-testid="code-pane-clean">
          No changes vs HEAD.
        </div>
      ) : (
        <>
          <ul className="code-pane__files" data-testid="code-pane-files">
            {files.map((f) => (
              <DiffFileRow key={f.path} f={f} active={f.path === active} onPick={() => setPicked(f.path)} />
            ))}
          </ul>
          {q.data.truncated ? <div className="code-pane__notice">The diff is past the viewer's cap — some changes aren't shown.</div> : null}
          <div className="code-pane__body code-pane__body--diff">
            {patch ? (
              <PatchDiff patch={patch.patch} options={options} className="code-pane__view" />
            ) : (
              <div className="code-pane__status">Pick a file to see its changes.</div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function DiffFileRow({ f, active, onPick }: { f: FsDiffFile; active: boolean; onPick: () => void }) {
  const disabled = f.denied || f.binary;
  return (
    <li>
      <button
        type="button"
        className={`code-pane__file-row${active ? " is-active" : ""}`}
        disabled={disabled}
        onClick={onPick}
        title={f.denied ? "Hidden: secret-like file" : f.binary ? "Binary file" : f.path}
      >
        <span className={`code-pane__status-letter is-${f.status === "?" ? "u" : f.status.toLowerCase()}`}>
          {f.status === "?" ? "U" : f.status}
        </span>
        <span className="code-pane__file-path">{f.path}</span>
        {f.denied ? (
          <span className="code-pane__file-meta">
            <Lock size={11} aria-hidden /> hidden
          </span>
        ) : f.binary ? (
          <span className="code-pane__file-meta">binary</span>
        ) : (
          <span className="code-pane__file-meta">
            <span className="code-pane__add">+{f.additions}</span> <span className="code-pane__del">−{f.deletions}</span>
          </span>
        )}
      </button>
    </li>
  );
}
