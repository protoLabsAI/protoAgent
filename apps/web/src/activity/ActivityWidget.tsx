import { useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";

import { onServerEvent } from "../lib/events";
import { isPendingNowInboxPage } from "../lib/queries";
import { UtilityWidget } from "../app/UtilityWidget";
import { ActivitySurface } from "./ActivitySurface";

/**
 * The unified feed widget (#3029) — one utility-bar pill that replaced the two former
 * Activity and Inbox pills. Its unread badge tracks BOTH bus events: `inbox.item`
 * (pending inbound stimuli, ADR 0003) and `activity.message` (completed agent turns,
 * ADR 0022) — incrementing only while the dialog is closed. Click opens the merged
 * feed (pending items on top, completed provenance timeline below) and clears the badge.
 */
export function ActivityWidget() {
  const [unread, setUnread] = useState(0);
  // A pending priority-`now` page is only queued because its automatic delivery failed (#3351).
  // Flag it so the pill signals a blocked page needing triage even while the dialog is closed and
  // no chat turn is open. It rides the SAME `inbox.item` bump — no extra unread increment — so a
  // fired now item (which pushes only `activity.message`, never `inbox.item`) can't double-count.
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
        if (isPendingNowInboxPage(data)) setPendingNow(true);
      }),
    [],
  );
  return (
    <UtilityWidget
      testId="activity-widget"
      icon={<Activity size={14} />}
      badge={
        unread || pendingNow ? (
          <span
            data-testid="activity-badge"
            className={pendingNow ? "activity-badge--alert" : undefined}
            data-alert={pendingNow ? "now" : undefined}
          >
            {unread > 9 ? "9+" : unread || "!"}
          </span>
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
      onOpen={() => { openRef.current = true; setUnread(0); setPendingNow(false); }}
      onClose={() => { openRef.current = false; }}
    >
      <ActivitySurface />
    </UtilityWidget>
  );
}
