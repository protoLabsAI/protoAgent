import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "../lib/api";
import { makeEditorLinker, type EditorLinker } from "../lib/editorLinks";
import { useEditorPref } from "../lib/editorPref";
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
