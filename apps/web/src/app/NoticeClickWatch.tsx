import { useEffect } from "react";

import { openWorkGroup } from "../chat/ChatStatusRail";
import { agentHref, apiUrl, applyAuth, currentSlug } from "../lib/api/routing";
import { useUI } from "../state/uiStore";

// A clicked push notification lands here (ADR 0135 W2, "Clicking fetches the current
// authoritative record"). The push worker (public/sw.js) either focuses an open console and
// posts `protoagent:notice-click`, or opens `/app/?notice=<id>`. Both carry only the opaque
// notice id; this turns it into the place the operator needs: for a decision, its answer form.

const CLICK_MESSAGE = "protoagent:notice-click"; // sw.js CLICK_MESSAGE
const NOTICE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/; // sw.js NOTICE_ID
const DECISION_PREFIX = "decision-"; // graph/decisions.py: notice_id = f"decision-{rec.id}"

/** The fleet member that raised a notice, or null for this hub's own. The push payload names
 *  no agent, so the hub is asked after authentication (`GET /api/notices/<id>`, #5614). Any
 *  failure reads as "here": the decision then opens in this console, as before. */
async function agentOf(id: string): Promise<string | null> {
  try {
    const res = await fetch(apiUrl(`/api/notices/${encodeURIComponent(id)}`, { host: true }), {
      headers: applyAuth(new Headers()), cache: "no-store", signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { agent?: unknown };
    return typeof body.agent === "string" && body.agent ? body.agent : null;
  } catch {
    return null;
  }
}

/** Open what a notice is about. False when the id names nothing this console can open, in
 *  which case the operator stays where they are. A decision another agent asked for opens
 *  in THAT agent's console: this one hands the notice over with the same `?notice=` link. */
export async function openNotice(id: unknown, go: (href: string) => void = (href) => window.location.assign(href)): Promise<boolean> {
  if (typeof id !== "string" || !NOTICE_ID.test(id) || !id.startsWith(DECISION_PREFIX)) return false;
  const decisionId = id.slice(DECISION_PREFIX.length);
  if (!decisionId) return false;
  const agent = await agentOf(id);
  if (agent && agent !== currentSlug()) {
    go(`${agentHref(agent)}?notice=${encodeURIComponent(id)}`);
    return true;
  }
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
    void openNotice(takeNoticeFromUrl());
    const worker = typeof navigator !== "undefined" ? navigator.serviceWorker : undefined;
    if (!worker) return;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; id?: unknown } | null;
      if (data && data.type === CLICK_MESSAGE) void openNotice(data.id);
    };
    worker.addEventListener("message", onMessage);
    return () => worker.removeEventListener("message", onMessage);
  }, []);
  return null;
}
