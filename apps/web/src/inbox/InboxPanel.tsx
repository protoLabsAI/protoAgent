import {
  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Check, RefreshCw } from "lucide-react";
import { Suspense, useEffect, useState } from "react";

import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { api } from "../lib/api";
import { onServerEvent } from "../lib/events";
import { inboxQuery, queryKeys } from "../lib/queries";

// Right-sidebar view of the inbound inbox (ADR 0003). Lists pending stimuli
// (webhooks / external systems / sister agents), live-updates as items arrive
// over the `inbox.item` push event, and lets the operator dismiss one (mark it
// delivered). External intake is POST /api/inbox (token-gated); this surface is
// read + dismiss only. On the TanStack Query data layer (ADR 0013): the list is
// a useSuspenseQuery the `inbox.item` event invalidates; dismiss is a mutation.

const PRIORITY_TONE: Record<string, string> = { now: "now", next: "next", later: "later" };

function InboxBody({
  dismissed,
  onDismiss,
}: {
  dismissed: Set<number>;
  onDismiss: (id: number) => void;
}) {
  const queryClient = useQueryClient();
  const { data, isFetching, refetch } = useSuspenseQuery(inboxQuery());

  // Delivered items stay hidden even if a refetch re-includes them (the server
  // drops them once delivered). `dismissed` is held by the parent so it
  // survives the live-event refetch cycle.
  const items = data.items.filter((i) => !dismissed.has(i.id));

  // Live: a new item arrived — refresh to pick it up with its server id.
  useEffect(
    () => onServerEvent("inbox.item", () => void queryClient.invalidateQueries({ queryKey: queryKeys.inbox })),
    [queryClient],
  );

  const dismiss = useMutation({
    mutationFn: (id: number) => api.deliverInbox(id),
    // Hide immediately on click (optimistic); it won't return once delivered.
    onMutate: (id) => onDismiss(id),
  });

  return (
    <>
      <div className="panel-header compact">
        <div>
          <h2>Inbox</h2>
          <p className="panel-kicker">{items.length} pending</p>
        </div>
        <button className="icon-button" type="button" onClick={() => void refetch()} disabled={isFetching} title="Refresh">
          <RefreshCw size={16} className={isFetching ? "spin" : ""} />
        </button>
      </div>

      <div className="inbox-list">
        {items.length === 0 ? (
          <div className="inbox-empty">
            Nothing pending. Inbound stimuli (webhooks, scripts, sister agents) posted to
            <code>/api/inbox</code> show up here.
          </div>
        ) : null}
        {items.map((item) => (
          <div className="inbox-item" key={item.id}>
            <div className="inbox-item-head">
              <span className={`inbox-pri inbox-pri-${PRIORITY_TONE[item.priority] || "next"}`}>
                {item.priority}
              </span>
              {item.source ? <span className="inbox-source">{item.source}</span> : null}
              <button
                className="icon-button inbox-dismiss"
                type="button"
                onClick={() => dismiss.mutate(item.id)}
                title="Mark delivered (dismiss)"
              >
                <Check size={15} />
              </button>
            </div>
            <div className="inbox-text">{item.text}</div>
          </div>
        ))}
      </div>
    </>
  );
}

export function InboxPanel() {
  // Held above the Suspense boundary so a delivered item stays dismissed across
  // the live-event refetch cycle (the inner body remounts on re-suspend).
  const [dismissed, setDismissed] = useState<Set<number>>(() => new Set());
  return (
    <section className="panel side-panel inbox-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="inbox" />}>
            <Suspense fallback={<PanelSkeleton label="Loading inbox…" />}>
              <InboxBody
                dismissed={dismissed}
                onDismiss={(id) => setDismissed((s) => new Set(s).add(id))}
              />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
