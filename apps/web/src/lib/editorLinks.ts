// "Open in editor" deep links for file paths in tool output — pure URL building, no React,
// no storage, so every encoding rule is unit-tested in isolation (editorLinks.test.ts).
//
// The fs tools speak PROJECT-RELATIVE paths (`read_file(project, path)`); the console joins
// them onto the project's absolute root from GET /api/fs/roots (the same fence the tools
// resolve through) and hands the result to the operator's editor via its URL scheme:
//
//   Zed      zed://file/<abs>:<line>:<col>      (open_listener.rs strips `zed://file`,
//                                                 URL-decodes, parses PathWithPosition)
//   VS Code  vscode://file/<abs>:<line>:<col>
//   Cursor   cursor://file/<abs>:<line>:<col>   (a VS Code fork — same handler shape)
//
// The editor is a property of the operator's MACHINE, not of the agent, which is why the
// preference is per-viewer (lib/editorPref.ts) and why these links point at the server's
// paths: they only resolve when the console and the agent share a filesystem (the desktop
// app, a local `protoagent serve`). A remote fleet member's paths simply won't exist locally.

export type EditorId = "zed" | "vscode" | "cursor" | "off";

export const DEFAULT_EDITOR: EditorId = "zed";

export const EDITOR_OPTIONS: ReadonlyArray<{ value: EditorId; label: string }> = [
  { value: "zed", label: "Zed" },
  { value: "vscode", label: "VS Code" },
  { value: "cursor", label: "Cursor" },
  { value: "off", label: "Off (plain text)" },
];

const SCHEMES: Record<Exclude<EditorId, "off">, string> = {
  zed: "zed",
  vscode: "vscode",
  cursor: "cursor",
};

export function isEditorId(v: unknown): v is EditorId {
  return v === "zed" || v === "vscode" || v === "cursor" || v === "off";
}

export function editorLabel(editor: EditorId): string {
  return EDITOR_OPTIONS.find((o) => o.value === editor)?.label ?? editor;
}

const WIN_DRIVE = /^[A-Za-z]:$/;

/** Join a project root and a tool's project-relative path into an absolute path, or null
 *  when the relative path isn't one the fence would accept (absolute, `~`, `..` escapes) —
 *  a link to somewhere the tool never read would be a lie. Separators are normalized to
 *  `/` (Windows roots come back from the server as `C:\…`; every editor scheme wants `/`). */
export function joinProjectPath(root: string, rel: string): string | null {
  const base = (root || "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (!base) return null;
  const r = (rel || "").trim().replace(/\\/g, "/");
  if (r.startsWith("/") || r.startsWith("~") || /^[A-Za-z]:/.test(r)) return null;
  const parts = r.split("/").filter((p) => p !== "" && p !== ".");
  if (parts.includes("..")) return null;
  // A bare Windows drive root ("C:") would otherwise join to "C:" + "" — keep its slash.
  if (!parts.length) return WIN_DRIVE.test(base) ? `${base}/` : base;
  return `${base}/${parts.join("/")}`;
}

/** Percent-encode an absolute path for an editor URL: each segment is encoded (spaces,
 *  `#`, `?`, `%`, unicode → UTF-8 escapes) while the `/` separators and a leading Windows
 *  drive (`C:`) stay literal. A raw `#` would otherwise truncate the path at a fragment. */
function encodePath(abs: string): string {
  const segs = abs.replace(/\\/g, "/").split("/");
  const out = segs.map((seg, i) => {
    const first = i === 0 || (i === 1 && segs[0] === "");
    if (first && WIN_DRIVE.test(seg)) return seg;
    return encodeURIComponent(seg);
  });
  const joined = out.join("/");
  // `zed://file` + path: POSIX paths already start with "/"; a Windows drive path doesn't.
  return joined.startsWith("/") ? joined : `/${joined}`;
}

const posInt = (n: unknown): n is number => typeof n === "number" && Number.isInteger(n) && n > 0;

/** The editor URL for an absolute path (+ optional 1-based line/column), or null when the
 *  preference is Off or the path is empty. A column without a line is dropped — every
 *  scheme reads `:N` as the LINE. */
export function editorUrl(editor: EditorId, absPath: string, line?: number, col?: number): string | null {
  if (editor === "off" || !absPath) return null;
  const scheme = SCHEMES[editor];
  if (!scheme) return null;
  let pos = "";
  if (posInt(line)) {
    pos = `:${line}`;
    if (posInt(col)) pos += `:${col}`;
  }
  return `${scheme}://file${encodePath(absPath)}${pos}`;
}

/** Resolve (project, project-relative path, line) → editor URL, or null when there's no
 *  link to make: pref Off, roots not loaded, unknown project, or a path outside the fence. */
export type EditorLinker = (project: string, relPath: string, line?: number) => string | null;

export function makeEditorLinker(editor: EditorId, roots: Record<string, string> | null | undefined): EditorLinker | null {
  if (editor === "off" || !roots) return null;
  return (project, relPath, line) => {
    const root = Object.prototype.hasOwnProperty.call(roots, project) ? roots[project] : undefined;
    if (!root) return null;
    const abs = joinProjectPath(root, relPath);
    return abs ? editorUrl(editor, abs, line) : null;
  };
}
