import { useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";
import { Count } from "@protolabsai/ui/primitives";

import { onServerEvent } from "../lib/events";
import { needsOperatorAttention } from "../lib/queries";
import { UtilityWidget } from "../app/UtilityWidget";
import { ActivitySurface } from "./ActivitySurface";

/**
 * The unified feed widget (#3029) — one utility-bar pill that replaced the two former
 * Activity and Inbox pills. Its unread badge tracks BOTH bus events: `inbox.item`
 * (pending inbound stimuli, ADR 0003) and `activity.message` (completed agent turns,
 * ADR 0022) — incrementing only while the Feed dialog is closed; opening it clears
 * the badge. The dialog is the feed's own home: history left Work with the four-group
 * consolidation (ADR 0129 Q3, S4c removed the Activity section), so the pill opens the
 * full `ActivitySurface` (pending triage + completed provenance timeline) right here
 * instead of routing into the Work modal.
 */
export function ActivityWidget() {
  const [unread, setUnread] = useState(0);
  // Only an OPERATOR-CLASS pending now page (needs_human) raises the pill alert: an
  // ordinary undelivered page is queued and retries on its own once the agent is free,
  // so it must read as plain unread, never as an alert. It rides the SAME `inbox.item`
  // bump — no extra unread increment — so a fired now item (which pushes only
  // `activity.message`, never `inbox.item`) can't double-count.
  const [pendingNow, setPendingNow] = useState(false);
  const openRef = useRef(false);
  useEffect(
    () => onServerEvent("activity.message", () => { if (!openRef.current) setUnread((n) => n + 1); }),
    [],
  );
  useEffect(
    () =>
      onServerEvent("inbox.item", (data) => {
        if (openRef.current) return;
        setUnread((n) => n + 1);
        if (needsOperatorAttention(data)) setPendingNow(true);
      }),
    [],
  );
  return (
    <UtilityWidget
      testId="activity-widget"
      icon={<Activity size={14} />}
      badge={
        unread || pendingNow ? (
          <Count
            data-testid="activity-badge"
            className={pendingNow ? "activity-badge--alert" : undefined}
            data-alert={pendingNow ? "now" : undefined}
          >
            {unread > 9 ? "9+" : unread || "!"}
          </Count>
        ) : null
      }
      label={
        pendingNow
          ? `Feed — priority-now page needs delivery${unread ? ` (${unread} new)` : ""}`
          : unread
            ? `Feed — ${unread} new`
            : "Feed"
      }
      info={
        pendingNow
          ? "A priority-now page could not be delivered automatically — open the feed to deliver or dismiss it"
          : unread
            ? `${unread} new item${unread === 1 ? "" : "s"} since you last looked`
            : "Feed — pending inbox items + what the agent did on its own"
      }
      dialogTitle="Feed"
      dialogWidth="min(720px, 94vw)"
      onOpen={() => {
        openRef.current = true;
        setUnread(0);
        setPendingNow(false);
      }}
      onClose={() => {
        openRef.current = false;
      }}
    >
      <ActivitySurface />
    </UtilityWidget>
  );
}
