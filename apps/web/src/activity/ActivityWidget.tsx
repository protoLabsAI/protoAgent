import { useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";

import { onServerEvent } from "../lib/events";
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
  const openRef = useRef(false);
  useEffect(
    () => onServerEvent("activity.message", () => { if (!openRef.current) setUnread((n) => n + 1); }),
    [],
  );
  useEffect(
    () => onServerEvent("inbox.item", () => { if (!openRef.current) setUnread((n) => n + 1); }),
    [],
  );
  return (
    <UtilityWidget
      testId="activity-widget"
      icon={<Activity size={14} />}
      badge={unread ? <span data-testid="activity-badge">{unread > 9 ? "9+" : unread}</span> : null}
      label={unread ? `Feed — ${unread} new` : "Feed"}
      info={
        unread
          ? `${unread} new item${unread === 1 ? "" : "s"} since you last looked`
          : "Feed — pending inbox items + what the agent did on its own"
      }
      dialogTitle="Feed"
      dialogWidth="min(720px, 94vw)"
      onOpen={() => { openRef.current = true; setUnread(0); }}
      onClose={() => { openRef.current = false; }}
    >
      <ActivitySurface />
    </UtilityWidget>
  );
}
