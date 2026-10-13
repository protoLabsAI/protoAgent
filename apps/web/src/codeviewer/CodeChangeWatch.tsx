import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { onTopic } from "../lib/events";
import { useCodePaneEnabled } from "./enabled";
import { createChangeCoalescer, FS_CHANGED_TOPIC, invalidateCodePane, parseFsChanged } from "./liveRefresh";
import { followDiff } from "./open";

// ADR 0112 — the code pane's live half on the event bus. The server publishes `fs.changed`
// when a write it observes lands in a registered project (a coding delegate's edit, the
// agent's own write_file/edit_file); this watcher re-fetches the pane's diff and open file
// for it — debounced and coalesced (liveRefresh.ts) — and, with Follow on, moves the pane to
// a DELEGATE's edit on the Diff tab. The agent's own edits already follow through the live
// tool stream (live.ts), so following them here too would jump twice. Mounted once, app-wide,
// beside the other bus watchers; subscribed only while the code pane toolset is on.
export function CodeChangeWatch() {
  const enabled = useCodePaneEnabled();
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    const coalescer = createChangeCoalescer((batch) => invalidateCodePane(qc, batch));
    const off = onTopic(FS_CHANGED_TOPIC, (data) => {
      const change = parseFsChanged(data);
      if (!change) return;
      coalescer.note(change.project, change.paths);
      if (change.source !== "agent" && change.paths.length > 0) followDiff(change.project, change.paths[0]);
    });
    return () => {
      off();
      coalescer.dispose();
    };
  }, [enabled, qc]);
  return null;
}
