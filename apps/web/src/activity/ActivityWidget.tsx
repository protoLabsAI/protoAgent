import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { UTIL_DIALOG_WIDTH } from "../shell/utilDialogSize";
import { Activity } from "lucide-react";
import { Count } from "@protolabsai/ui/primitives";

import { brandName } from "../lib/brand";
import { onServerEvent } from "../lib/events";
import { runtimeStatusQuery } from "../lib/queries";
import { UtilityWidget } from "../app/UtilityWidget";
import { ActivitySurface } from "./ActivitySurface";

/**
 * The unified feed widget (#3029) — one utility-bar pill that replaced the two former
 * Activity and Inbox pills. Its unread badge is a PLAIN count of BOTH bus events:
 * `inbox.item` (pending inbound stimuli, ADR 0003) and `activity.message` (completed
 * agent turns, ADR 0022) — incrementing only while the Feed dialog is closed; opening
 * it clears the badge. A priority-now item counts exactly like any other item: the pill
 * never alerts (docs/design/feed.md §2 — anything operator-class pages through Work ›
 * Needs you, not here). The dialog is the feed's own home: history left Work with the
 * four-group consolidation (ADR 0129 Q3, S4c removed the Activity section), so the pill
 * opens the full `ActivitySurface` (one merged newest-first list) right here instead of
 * routing into the Work modal.
 */
export function ActivityWidget() {
  const [unread, setUnread] = useState(0);
  const openRef = useRef(false);
  // The agent's display name for the resting hover info — the same cached probe the app
  // shell and the Feed dialog body read (one query key, deduped).
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const agentName = brandName(runtime?.identity?.name);
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
      badge={
        unread > 0 ? (
          <Count data-testid="activity-badge">{unread > 9 ? "9+" : unread}</Count>
        ) : null
      }
      label={unread ? `Feed — ${unread} new` : "Feed"}
      info={
        unread
          ? `${unread} new item${unread === 1 ? "" : "s"} since you last looked`
          : `Feed — what ${agentName} was asked to do and what it did, newest first`
      }
      dialogTitle="Feed"
      dialogWidth={UTIL_DIALOG_WIDTH}
      onOpen={() => {
        openRef.current = true;
        setUnread(0);
      }}
      onClose={() => {
        openRef.current = false;
      }}
    >
      <ActivitySurface />
    </UtilityWidget>
  );
}
