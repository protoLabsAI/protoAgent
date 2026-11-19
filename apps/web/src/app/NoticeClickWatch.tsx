import { useEffect } from "react";

import { openWorkGroup } from "../chat/ChatStatusRail";
import { useUI } from "../state/uiStore";

// A clicked push notification lands here (ADR 0135 W2, "Clicking fetches the current
// authoritative record"). The push worker (public/sw.js) either focuses an open console and
// posts `protoagent:notice-click`, or opens `/app/?notice=<id>`. Both carry only the opaque
// notice id; this turns it into the place the operator needs: for a decision, its answer form.

const CLICK_MESSAGE = "protoagent:notice-click"; // sw.js CLICK_MESSAGE
const NOTICE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/; // sw.js NOTICE_ID
const DECISION_PREFIX = "decision-"; // graph/decisions.py: notice_id = f"decision-{rec.id}"

/** Open what a notice is about. False when the id names nothing this console can open, in
 *  which case the operator stays where they are. */
export function openNotice(id: unknown): boolean {
  if (typeof id !== "string" || !NOTICE_ID.test(id) || !id.startsWith(DECISION_PREFIX)) return false;
  const decisionId = id.slice(DECISION_PREFIX.length);
  if (!decisionId) return false;
  // Settings is an overlay above every surface: left open, it would hide the decision.
  useUI.getState().closeGlobalSettings();
  openWorkGroup("needs-you", `decision:${decisionId}`);
  return true;
}

/** Read and remove `?notice=<id>` so a reload or a shared link doesn't reopen it. */
export function takeNoticeFromUrl(): string | null {
  const url = new URL(window.location.href);
  const id = url.searchParams.get("notice");
  if (id === null) return null;
  url.searchParams.delete("notice");
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  return id;
}

export function NoticeClickWatch() {
  useEffect(() => {
    openNotice(takeNoticeFromUrl());
    const worker = typeof navigator !== "undefined" ? navigator.serviceWorker : undefined;
    if (!worker) return;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; id?: unknown } | null;
      if (data && data.type === CLICK_MESSAGE) openNotice(data.id);
    };
    worker.addEventListener("message", onMessage);
    return () => worker.removeEventListener("message", onMessage);
  }, []);
  return null;
}
