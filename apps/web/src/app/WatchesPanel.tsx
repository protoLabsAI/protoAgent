import "../watches/watches.css";

import { Button, Empty } from "@protolabsai/ui/primitives";
import {
  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { Suspense, useEffect } from "react";

import { api } from "../lib/api";
import { onServerEvent } from "../lib/events";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { watchesQuery, queryKeys } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { ScrollArea } from "@protolabsai/ui/data";
import { StatusPill } from "./StatusPill";

// The agent's watches (ADR 0067 passive-supervision layer), in the Work hub. Mirrors
// GoalsPanel on the TanStack Query + Suspense + ErrorBoundary data layer (ADR 0013): the read
// is a `useSuspenseQuery` (loading → <Suspense>, failure → <ErrorBoundary>), and clearing a
// watch is a `useMutation` that invalidates the watches query. Unlike goals (one per session,
// keyed by session_id), a watch is verifier-only and keyed by its own `id` — many at once.

function watchTone(status: string) {
  if (status === "met") return "success" as const;
  if (status === "active") return "warning" as const;
  if (status === "expired") return "error" as const;
  return "muted" as const;
}

const trunc = (t: string, n = 80) => (t.length > n ? `${t.slice(0, n)}…` : t);

function WatchesList() {
  const { data } = useSuspenseQuery(watchesQuery());
  const watches = data.watches;
  const queryClient = useQueryClient();
  const clear = useMutation({
    mutationFn: (id: string) => api.clearWatch(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.watches }),
  });

  // Live: refresh off the watch bus instead of polling, the same pattern as GoalsPanel.
  // `watch.changed` fires on create/check/clear; `watch.met`/`watch.expired`/`watch.stalled`
  // fire on the terminal/stall transitions — any of them re-reads the list.
  useEffect(() => {
    const refresh = () => void queryClient.invalidateQueries({ queryKey: queryKeys.watches });
    const offs = [
      onServerEvent("watch.changed", refresh),
      onServerEvent("watch.met", refresh),
      onServerEvent("watch.expired", refresh),
      onServerEvent("watch.stalled", refresh),
    ];
    return () => offs.forEach((off) => off());
  }, [queryClient]);

  if (!watches.length) {
    return (
      <Empty
        title="No watches"
        description={
          <>
            an agent or plugin creates them (or <code>POST /api/watches</code>)
          </>
        }
      />
    );
  }

  return (
    <>
      {watches.map((watch) => (
        <div className="watch-row" key={watch.id}>
          <div className="watch-row-head">
            <strong>{watch.condition || watch.id}</strong>
            <StatusPill label={watch.status} tone={watchTone(watch.status)} />
          </div>
          <span className="watch-row-meta">
            {watch.id} · {watch.verifier?.type || "llm"}
            {watch.last_reason ? ` · ${trunc(watch.last_reason)}` : ""}
          </span>
          <Button
            icon variant="ghost" className="watch-row-clear"
            type="button"
            onClick={() => clear.mutate(watch.id)}
            disabled={clear.isPending}
            title="Clear watch"
          >
            <Trash2 size={15} />
          </Button>
        </div>
      ))}
    </>
  );
}

export function WatchesPanel() {
  return (
    <section className="panel side-panel watches-panel">
      <PanelHeader
        compact
        title="Watches"
        kicker={<>passive verifier-only objectives · created by an agent or plugin</>}
      />
      <ScrollArea className="watches-list" role="region" aria-label="Watches" tabIndex={0}>
        <QueryErrorResetBoundary>
          {({ reset }: { reset: () => void }) => (
            <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="watches" />}>
              <Suspense fallback={<PanelSkeleton label="Loading watches…" />}>
                <WatchesList />
              </Suspense>
            </ErrorBoundary>
          )}
        </QueryErrorResetBoundary>
      </ScrollArea>
    </section>
  );
}
