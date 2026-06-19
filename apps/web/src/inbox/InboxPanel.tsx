import "./inbox.css";

import { Button, Empty } from "@protolabsai/ui/primitives";
import {
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Check } from "lucide-react";
import { useEffect, useState } from "react";

import { StagePanel } from "../app/ErrorBoundary";
import { useUtilityHeaderReload } from "../app/UtilityWidget";
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

  // The reload lives in the dialog header (UtilityWidget) — no second panel header here.
  useUtilityHeaderReload(refetch, isFetching);

  return (
      <div className="inbox-list">
        {items.length === 0 ? (
          <Empty
            title="Nothing pending"
            description={
              <>
                Inbound stimuli (webhooks, scripts, sister agents) posted to <code>/api/inbox</code> show up here.
              </>
            }
          />
        ) : null}
        {items.map((item) => (
          <div className="inbox-item" key={item.id}>
            <div className="inbox-item-head">
              <span className={`inbox-pri inbox-pri-${PRIORITY_TONE[item.priority] || "next"}`}>
                {item.priority}
              </span>
              {item.source ? <span className="inbox-source">{item.source}</span> : null}
              <Button
                icon variant="ghost" className="inbox-dismiss"
                type="button"
                onClick={() => dismiss.mutate(item.id)}
                title="Mark delivered (dismiss)"
              >
                <Check size={15} />
              </Button>
            </div>
            <div className="inbox-text">{item.text}</div>
          </div>
        ))}
      </div>
  );
}

export function InboxPanel() {
  // Held above the Suspense boundary so a delivered item stays dismissed across
  // the live-event refetch cycle (the inner body remounts on re-suspend).
  const [dismissed, setDismissed] = useState<Set<number>>(() => new Set());
  return (
    <StagePanel label="inbox" variant="side" className="inbox-panel util-dialog-fill">
      <InboxBody
        dismissed={dismissed}
        onDismiss={(id) => setDismissed((s) => new Set(s).add(id))}
      />
    </StagePanel>
  );
}
