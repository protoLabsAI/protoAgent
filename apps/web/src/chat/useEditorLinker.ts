import { useQuery } from "@tanstack/react-query";
import { useMemo, type MouseEvent as ReactMouseEvent } from "react";

import { useCodePaneEnabled } from "../codeviewer/enabled";
import { openCode } from "../codeviewer/open";
import type { CodeRef } from "../codeviewer/store";
import { api } from "../lib/api";
import { editorLabel, joinProjectPath, makeEditorLinker, type EditorId, type EditorLinker } from "../lib/editorLinks";
import { useEditorPref, useOpenFilesIn, type OpenFilesIn } from "../lib/editorPref";
import { queryClient } from "../lib/queryClient";

export const FS_ROOTS_QUERY_KEY = ["fs-roots"] as const;

/** The "open in editor" linker for fs tool output, or null when there's nothing to link
 *  with (pref Off, roots not loaded yet or failed). Lazy by construction: only an fs tool's
 *  output mounts this, so a chat with no file work never fetches the roots.
 *
 *  Passes the console's singleton QueryClient explicitly rather than relying on a provider —
 *  tool cards also mount in isolated trees (render tests, the publish preview) where no
 *  QueryClientProvider sits above them; with one present it is the same client anyway. */
export function useEditorLinker(): EditorLinker | null {
  const editor = useEditorPref();
  const roots = useQuery(
    {
      queryKey: FS_ROOTS_QUERY_KEY,
      queryFn: () => api.fsRoots(),
      enabled: editor !== "off",
      // The fence changes on a settings save or an onboard_project — rare. A minute keeps a
      // transcript full of tool cards from refetching per card mount.
      staleTime: 60_000,
      // A link is a nicety: never let this query's failure surface anywhere.
      retry: false,
    },
    queryClient,
  );
  const data = roots.data?.roots;
  return useMemo(() => makeEditorLinker(editor, data), [editor, data]);
}

// ── Click routing for file links (ADR 0112) ─────────────────────────────────────────────

/** What a file path in tool output renders as: the `<a>`'s href + title, and the click
 *  handler that routes it (the in-app code pane vs the external editor). */
export type FileLink = {
  href: string;
  title: string;
  onClick?: (e: ReactMouseEvent<HTMLAnchorElement>) => void;
};

/** (project, project-relative path, line?, endLine?) → a link, or null when there's none. */
export type FileLinker = (project: string, relPath: string, line?: number, endLine?: number) => FileLink | null;

// A path the fence would never accept (absolute, `~`, `..`) gets no link at all — the pane
// would only answer 400, and a link to somewhere the tool never read would be a lie.
const fenceable = (rel: string) => joinProjectPath("/r", rel) !== null;

const modifierClick = (e: ReactMouseEvent) => e.metaKey || e.ctrlKey;

/** Pure: build the file linker for a given preference state. Exported for unit tests.
 *  - "protoagent": a click opens the code pane — needs NO /api/fs/roots, so it works for a
 *    remote fleet member too; ⌘/Ctrl-click hands off to the external editor when there is one.
 *  - "editor": today's behavior — the link IS the editor URL (null without roots); ⌘/Ctrl-
 *    click opens the pane instead.
 *  `open` is null while the code pane toolset is OFF (ADR 0112 amendment): then there is no
 *  pane at all — every link is the plain editor link, whatever `openIn` says, exactly as
 *  before the pane existed (a stored "protoagent" falls back to the external editor). */
export function makeFileLinker(
  openIn: OpenFilesIn,
  editor: EditorId,
  external: EditorLinker | null,
  open: ((ref: CodeRef) => void) | null,
  navigate: (href: string) => void = (href) => {
    window.location.href = href;
  },
): FileLinker | null {
  if (!open) {
    if (!external) return null;
    return (project, path, line) => {
      const href = external(project, path, line);
      return href ? { href, title: `Open in ${editorLabel(editor)}` } : null;
    };
  }
  const pane = (project: string, path: string, line?: number, endLine?: number) =>
    open({ project, path, line, endLine, source: "link" });
  if (openIn === "editor") {
    if (!external) return null;
    return (project, path, line, endLine) => {
      const href = external(project, path, line);
      if (!href) return null;
      return {
        href,
        title: `Open in ${editorLabel(editor)} · ⌘-click to open here`,
        onClick: (e) => {
          if (!modifierClick(e)) return; // the plain click follows the href to the editor
          e.preventDefault();
          pane(project, path, line, endLine);
        },
      };
    };
  }
  return (project, path, line, endLine) => {
    if (!fenceable(path)) return null;
    const href = external?.(project, path, line) ?? null;
    return {
      // Without an editor URL the anchor still needs an href to stay focusable/keyboard-
      // reachable; the click handler always prevents it.
      href: href ?? "#",
      title: href ? `Open in protoAgent · ⌘-click for ${editorLabel(editor)}` : "Open in protoAgent",
      onClick: (e) => {
        e.preventDefault();
        if (modifierClick(e) && href) {
          // Same-window hand-off, like the editor-mode link: a custom scheme goes to the OS
          // without unloading the page, where a new tab would first flash open empty.
          navigate(href);
          return;
        }
        pane(project, path, line, endLine);
      },
    };
  };
}

/** The file linker for fs tool output under the operator's current preferences — and the
 *  connected agent's code pane toolset (off → editor links only). */
export function useFileLinker(): FileLinker | null {
  const openIn = useOpenFilesIn();
  const editor = useEditorPref();
  const external = useEditorLinker();
  const paneOn = useCodePaneEnabled();
  return useMemo(
    () => makeFileLinker(openIn, editor, external, paneOn ? (ref) => openCode(ref) : null),
    [openIn, editor, external, paneOn],
  );
}
