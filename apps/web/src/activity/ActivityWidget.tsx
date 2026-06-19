import { useEffect, useRef, useState } from "react";
import { Activity } from "lucide-react";

import { onServerEvent } from "../lib/events";
import { UtilityWidget } from "../app/UtilityWidget";
import { ActivitySurface } from "./ActivitySurface";

/**
 * Activity as a utility-bar widget (2026-06 IA pass) — moved off the left rail into the
 * bottom-left widgets cluster, alongside the inbox + background jobs. A pill with an unread
 * badge; hover shows the count, click opens the read-only provenance feed in a dialog (which
 * clears the badge). Tracks its own unread off the `activity.message` bus event —
 * incrementing only while the dialog is closed.
 */
export function ActivityWidget() {
  const [unread, setUnread] = useState(0);
  const openRef = useRef(false);
  useEffect(
    () => onServerEvent("activity.message", () => { if (!openRef.current) setUnread((n) => n + 1); }),
    [],
  );
  return (
    <UtilityWidget
      testId="activity-widget"
      icon={<Activity size={14} />}
      badge={unread ? <span data-testid="activity-badge">{unread > 9 ? "9+" : unread}</span> : null}
      label={unread ? `Activity — ${unread} new` : "Activity"}
      info={
        unread
          ? `${unread} new agent turn${unread === 1 ? "" : "s"} since you last looked`
          : "Activity — what the agent did on its own"
      }
      dialogTitle="Activity"
      dialogWidth="min(720px, 94vw)"
      onOpen={() => { openRef.current = true; setUnread(0); }}
      onClose={() => { openRef.current = false; }}
    >
      <ActivitySurface />
    </UtilityWidget>
  );
}
