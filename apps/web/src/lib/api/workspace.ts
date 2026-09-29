/**
 * Workspace filesystem: server-side path pickers, the fs fence roots/projects (ADR 0095),
 * the code pane (ADR 0112), artifact refs and the Zed handoff.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type { BrowseListing, FsProject, ManagedProjects } from "../types";
import { request } from "./http";

/** GET /api/fs/file (ADR 0112). `text` is lines start..end (1-based, inclusive); null when
 *  the file is binary. `truncated` = a server cap cut the read — page with start/end. */
export type FsFile = {
  project: string;
  path: string;
  size: number;
  /** null for a binary file (no lines to count). */
  line_count: number | null;
  start: number | null;
  end: number | null;
  /** A server cap cut the read: lines past `end` (page with start/end) and/or single lines
   *  past the per-line cap (each cut line ends with " … [line truncated]"). */
  truncated: boolean;
  language: string;
  binary: boolean;
  text: string | null;
};

export type FsDiffFile = {
  path: string;
  status: "M" | "A" | "D" | "R" | "?";
  additions: number;
  deletions: number;
  binary: boolean;
  denied: boolean;
  /** A rename's previous path (status "R"). */
  old_path?: string;
  /** An untracked file over the server's 256 KB cap — listed, content not in `patch`. */
  too_large?: boolean;
};

/** GET /api/fs/diff (ADR 0112) — the working tree vs HEAD, untracked files included. */
export type FsDiff = {
  project: string;
  is_git: boolean;
  head?: string;
  branch?: string;
  files: FsDiffFile[];
  patch: string;
  truncated?: boolean;
};

export const workspaceApi = {
  // Server-side directory listing behind the path pickers. Deliberately the SERVER's
  // filesystem: the console may be configuring a different machine, and the browser's
  // own pickers can't produce an absolute path on it.
  browseDir(opts: { path?: string; files?: boolean; hidden?: boolean } = {}) {
    const qs = new URLSearchParams();
    if (opts.path) qs.set("path", opts.path);
    if (opts.files) qs.set("files", "true");
    if (opts.hidden) qs.set("hidden", "true");
    const q = qs.toString();
    return request<BrowseListing>(`/api/fs/browse${q ? `?${q}` : ""}`);
  },
  // `{project name: absolute root}` for the LIVE fs fence — the same registry the fs tools
  // resolve through (not /api/projects, which the fence can shadow). Backs the tool cards'
  // "open in editor" links, which join a tool's project-relative path onto its root.
  fsRoots() {
    return request<{ roots: Record<string, string> }>("/api/fs/roots");
  },
  // The code pane (ADR 0112): one file's text through the SAME fence read_file uses, and the
  // project's read-only working-tree diff vs HEAD. Both are GETs with no side effects.
  fsFile(project: string, path: string, range: { start?: number; end?: number } = {}) {
    const qs = new URLSearchParams({ project, path });
    if (range.start) qs.set("start", String(range.start));
    if (range.end) qs.set("end", String(range.end));
    return request<FsFile>(`/api/fs/file?${qs.toString()}`);
  },
  fsDiff(project: string) {
    return request<FsDiff>(`/api/fs/diff?${new URLSearchParams({ project }).toString()}`);
  },
  // The artifact plugin's chip metadata (#3617): for each id still in the store, its lifetime
  // version count and the oldest version it still keeps. An evicted/deleted id is absent.
  artifactRefs(ids: string[]) {
    return request<{
      artifacts: Record<string, { title: string; kind: string; version_count: number; oldest: number }>;
    }>(`/api/plugins/artifact/refs?${new URLSearchParams({ ids: ids.join(",") }).toString()}`);
  },
  // "Continue in Zed": offer this chat to the next agent thread the operator starts in Zed
  // under `project`'s root (no project = any folder). The protoagent-acp shim claims it on
  // session/new and continues the same A2A context. 120 s TTL, one-shot, latest wins.
  editorHandoff(body: { session_id: string; project?: string; path?: string; line?: number; title?: string }) {
    return request<{ id: string; expires_at: string; root: string | null }>("/api/editor/handoff", {
      method: "POST",
      body,
    });
  },
  fsProjects() {
    return request<{ enabled: boolean; projects: FsProject[] }>("/api/settings/filesystem-projects");
  },
  // The ADR 0095 managed-projects registry. Read-only by design: the fs-projects POST
  // above REPLACES `filesystem.projects`, so writing back a registry-derived list here
  // would silently materialize the projection and sever the registry link.
  managedProjects() {
    return request<ManagedProjects>("/api/projects");
  },
  // `replace: true` because this editor genuinely IS a replace-list editor — the form
  // holds every root and removing a row is how you delete one. The server refuses an
  // unacknowledged removal (409) precisely so that callers which DIDN'T mean to replace
  // — a script posting one folder to "add" it — can't strip the fence silently (#2556).
  setFsProjects(projects: FsProject[]) {
    return request<{ ok: boolean; projects: FsProject[]; removed?: FsProject[] }>(
      "/api/settings/filesystem-projects",
      {
        method: "POST",
        body: { projects, replace: true },
      },
    );
  },
};
