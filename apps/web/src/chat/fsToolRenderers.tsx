import { FileCode2 } from "lucide-react";
import type { ReactNode } from "react";

import { editorLabel, type EditorLinker } from "../lib/editorLinks";
import { useEditorPref } from "../lib/editorPref";
import { useEditorLinker } from "./useEditorLinker";

// "Open in editor" links for the fenced filesystem tools' output (tools/fs_tools.py).
//
// Every renderer here is an ENHANCEMENT over the plain render, never a replacement of
// what it shows: the tool's text is kept verbatim and only a path (or `file:line`) inside
// it becomes an <a>. Whenever a link can't be made — pref Off, /api/fs/roots not loaded,
// the call's project unknown, the args preview cut before `project`/`path` — the caller's
// `fallback` (exactly today's render) is returned instead. Links are same-window `<a href>`
// on purpose: a custom scheme hands off to the OS without unloading the page, where
// `target="_blank"` would first open an empty tab.

export const FS_LINK_TOOLS = new Set(["read_file", "search_files", "find_files", "write_file", "edit_file"]);

type FsArgs = { project?: string; path?: string; offset?: number };

/** Pull `project` / `path` / `offset` from the call's args preview. The preview is cut at
 *  ~800 chars (a write_file's `content` blows straight through that), so a JSON parse
 *  failure falls back to reading each field out of the truncated text — `project` and
 *  `path` come first in every fs tool's signature, so they survive the cut. */
export function parseFsArgs(input: string | undefined): FsArgs {
  if (!input) return {};
  try {
    const v = JSON.parse(input) as unknown;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const o = v as Record<string, unknown>;
      return {
        project: typeof o.project === "string" ? o.project : undefined,
        path: typeof o.path === "string" ? o.path : undefined,
        offset: typeof o.offset === "number" ? o.offset : undefined,
      };
    }
    return {};
  } catch {
    /* truncated preview — scrape the fields below */
  }
  const str = (key: string): string | undefined => {
    const m = input.match(new RegExp(`"${key}"\\s*:\\s*("(?:[^"\\\\]|\\\\.)*")`));
    if (!m) return undefined;
    try {
      return JSON.parse(m[1]) as string;
    } catch {
      return undefined;
    }
  };
  const off = input.match(/"offset"\s*:\s*(\d+)\s*[,}]/);
  return { project: str("project"), path: str("path"), offset: off ? Number(off[1]) : undefined };
}

function EditorLink({ href, children, title }: { href: string; children: ReactNode; title: string }) {
  return (
    <a className="tool-editor-link" href={href} title={title}>
      {children}
    </a>
  );
}

/** Output of an fs tool, with its paths linked when possible — else `fallback`. */
export function FsToolOutput({
  tool,
  raw,
  input,
  fallback,
}: {
  tool: string;
  raw: string;
  input?: string;
  fallback: ReactNode;
}) {
  const linker = useEditorLinker();
  const editor = useEditorPref();
  const args = parseFsArgs(input);
  if (!linker || !args.project) return <>{fallback}</>;
  const title = `Open in ${editorLabel(editor)}`;
  const view = renderLinked(tool, raw, args, linker, title, fallback);
  return <>{view ?? fallback}</>;
}

function renderLinked(
  tool: string,
  raw: string,
  args: FsArgs,
  linker: EditorLinker,
  title: string,
  fallback: ReactNode,
): ReactNode | null {
  const project = args.project as string;
  switch (tool) {
    case "read_file":
      return renderReadFile(project, args, linker, title, fallback);
    case "search_files":
      return renderSearch(project, raw, linker, title);
    case "find_files":
      return renderFind(project, raw, linker, title);
    case "write_file":
    case "edit_file":
      return renderWrite(project, raw, args, linker, title);
    default:
      return null;
  }
}

/** read_file: a header link to the file at the read's `offset`, then the content exactly as
 *  it renders today (the fallback), so a JSON file still gets its structured view. */
function renderReadFile(project: string, args: FsArgs, linker: EditorLinker, title: string, fallback: ReactNode) {
  if (!args.path) return null;
  const line = args.offset && args.offset > 1 ? args.offset : undefined;
  const href = linker(project, args.path, line);
  if (!href) return null;
  return (
    <div className="tool-fs">
      <div className="tool-fs-head">
        <FileCode2 size={12} aria-hidden />
        <EditorLink href={href} title={title}>
          {args.path}
          {line ? `:${line}` : ""}
        </EditorLink>
      </div>
      {fallback}
    </div>
  );
}

// A search hit: `rel/path.py:42: text` (context lines use `-42-` and stay plain). Lazy on
// the path so the FIRST `:<digits>: ` ends it — the same split grep output reads by.
const SEARCH_HIT = /^(.+?):(\d+): /;

/** search_files: each `file:line` hit is a link; context lines, `--` separators and the
 *  cap note stay plain. Null (→ fallback) when no line is a hit, e.g. "(no matches)". */
function renderSearch(project: string, raw: string, linker: EditorLinker, title: string) {
  const lines = raw.split("\n");
  let linked = 0;
  const out = lines.map((ln, i) => {
    const m = ln.match(SEARCH_HIT);
    const nl = i < lines.length - 1 ? "\n" : "";
    // A CONTEXT line (`file-42- …`) whose text happens to contain `:<n>: ` would otherwise
    // lazily match as a hit on a bogus path — its prefix carries the `-<n>- ` marker.
    if (!m || /-\d+- /.test(m[1])) return <span key={i}>{ln + nl}</span>;
    const href = linker(project, m[1], Number(m[2]));
    if (!href) return <span key={i}>{ln + nl}</span>;
    linked++;
    return (
      <span key={i}>
        <EditorLink href={href} title={title}>
          {`${m[1]}:${m[2]}`}
        </EditorLink>
        {ln.slice(m[1].length + 1 + m[2].length) + nl}
      </span>
    );
  });
  return linked ? <div className="tool-text">{out}</div> : null;
}

/** find_files: one project-relative path per line — each becomes a link. The `… (+N more)`
 *  tail and "(no matches)" stay plain. */
function renderFind(project: string, raw: string, linker: EditorLinker, title: string) {
  const lines = raw.split("\n");
  let linked = 0;
  const out = lines.map((ln, i) => {
    const nl = i < lines.length - 1 ? "\n" : "";
    const plain = !ln.trim() || ln.startsWith("…") || ln.startsWith("(");
    const href = plain ? null : linker(project, ln, undefined);
    if (!href) return <span key={i}>{ln + nl}</span>;
    linked++;
    return (
      <span key={i}>
        <EditorLink href={href} title={title}>
          {ln}
        </EditorLink>
        {nl}
      </span>
    );
  });
  return linked ? <div className="tool-text">{out}</div> : null;
}

/** write_file / edit_file ("Created x (N chars)." / "Edited x."): the path named in the
 *  output becomes a link; if the output doesn't quote it, a header link carries it. */
function renderWrite(project: string, raw: string, args: FsArgs, linker: EditorLinker, title: string) {
  if (!args.path) return null;
  const href = linker(project, args.path, undefined);
  if (!href) return null;
  const at = raw.indexOf(args.path);
  if (at < 0) {
    return (
      <div className="tool-fs">
        <div className="tool-fs-head">
          <FileCode2 size={12} aria-hidden />
          <EditorLink href={href} title={title}>
            {args.path}
          </EditorLink>
        </div>
        <div className="tool-text">{raw}</div>
      </div>
    );
  }
  return (
    <div className="tool-text">
      {raw.slice(0, at)}
      <EditorLink href={href} title={title}>
        {args.path}
      </EditorLink>
      {raw.slice(at + args.path.length)}
    </div>
  );
}
