import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, currentSlug } from "../lib/api";
import { onConnectionChange, onTopic } from "../lib/events";
import { activityTitle, type WorkSource } from "../work/useWorkItems";
import { fromActivity } from "../work/tone";

/** History belongs to the Activity view, never the live Work inventory. */
export function useActivityHistory(): WorkSource {
  const qc = useQueryClient();
  const slug = currentSlug();
  const result = useQuery({
    queryKey: [slug, "activity-history"],
    queryFn: () => api.activity(),
  });
  useEffect(() => {
    const refresh = () => { void qc.invalidateQueries({ queryKey: [slug, "activity-history"] }); };
    let wasOffline = false;
    const offTopic = onTopic("activity.message", refresh);
    const offConnection = onConnectionChange((connected) => {
      if (connected && wasOffline) refresh();
      wasOffline = !connected;
    });
    return () => { offTopic(); offConnection(); };
  }, [qc, slug]);
  return {
    items: (result.data?.entries ?? []).map((row) => ({
      kind: "activity", id: String(row.id), status: fromActivity(row.state),
      title: activityTitle(row), meta: row.origin, raw: row,
    })),
    status: result.isError ? (result.data ? "stale" : "error") : result.isPending ? "loading" : "ready",
    loadedAt: result.dataUpdatedAt || null,
    error: result.error,
    retry: () => { void result.refetch(); },
  };
}
