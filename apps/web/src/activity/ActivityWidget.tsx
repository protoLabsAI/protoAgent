import { useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";
import { Count } from "@protolabsai/ui/primitives";

import { useUI } from "../state/uiStore";
import { onServerEvent } from "../lib/events";
import { isPendingNowInboxPage } from "../lib/queries";
import { UtilityWidget } from "../app/UtilityWidget";
import { ActivitySurface } from "./ActivitySurface";

/**
 * The unified feed widget (#3029) — one utility-bar pill that replaced the two former
 * Activity and Inbox pills. Its unread badge tracks BOTH bus events: `inbox.item`
 * (pending inbound stimuli, ADR 0003) and `activity.message` (completed agent turns,
 * ADR 0022) — incrementing only while the feed is closed. The pill opens the widget's
 * own Feed dialog: history left Work with the four-group consolidation (ADR 0129 Q3),
 * so this dialog is where pending pages are triaged and completed turns are read. A
 * legacy saved Work `activity` section still clears the badge if something opens it.
 */
export function ActivityWidget() {
  const [unread, setUnread] = useState(0);
  // A pending priority-`now` page is only queued because its automatic delivery failed (#3351).
  // Flag it so the pill signals a blocked page needing triage even while the dialog is closed and
  // no chat turn is open. It rides the SAME `inbox.item` bump — no extra unread increment — so a
  // fired now item (which pushes only `activity.message`, never `inbox.item`) can't double-count.
  const [pendingNow, setPendingNow] = useState(false);
  const activityOpen = useUI((s) =>
    s.surfaceModal?.viewId === "work" && s.surfaceModal.state.section === "activity",
  );
  const openRef = useRef(activityOpen);
  useEffect(() => {
    openRef.current = activityOpen;
    if (activityOpen) {
      setUnread(0);
      setPendingNow(false);
    }
  }, [activityOpen]);
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
      onOpenChange={(open) => {
        openRef.current = open;
        if (open) {
          setUnread(0);
          setPendingNow(false);
        }
      }}
    >
      <ActivitySurface />
    </UtilityWidget>
  );
}
