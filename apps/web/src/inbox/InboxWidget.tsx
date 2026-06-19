import { useEffect, useRef, useState } from "react";
import { Inbox } from "lucide-react";

import { onServerEvent } from "../lib/events";
import { UtilityWidget } from "../app/UtilityWidget";
import { InboxPanel } from "./InboxPanel";

/**
 * The inbox as a utility-bar widget (2026-06 IA pass) — moved out of the Activity surface
 * into the bottom-left widgets cluster. A pill with an unread badge; hover shows the count,
 * click opens the inbox in a dialog (which marks it read). Tracks its own unread off the
 * `inbox.item` bus event — incrementing only while the dialog is closed.
 */
export function InboxWidget() {
  const [unread, setUnread] = useState(0);
  const openRef = useRef(false);
  useEffect(
    () => onServerEvent("inbox.item", () => { if (!openRef.current) setUnread((n) => n + 1); }),
    [],
  );
  return (
    <UtilityWidget
      testId="inbox-widget"
      icon={<Inbox size={14} />}
      badge={unread ? <span data-testid="inbox-badge">{unread > 9 ? "9+" : unread}</span> : null}
      label={unread ? `Inbox — ${unread} unread` : "Inbox"}
      info={unread ? `${unread} unread inbound item${unread === 1 ? "" : "s"}` : "Inbox — nothing new"}
      dialogTitle="Inbox"
      onOpen={() => { openRef.current = true; setUnread(0); }}
      onClose={() => { openRef.current = false; }}
    >
      <InboxPanel />
    </UtilityWidget>
  );
}
