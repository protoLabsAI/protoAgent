import { useEffect } from "react";

import { openWorkGroup } from "../chat/ChatStatusRail";
import { agentHref, apiUrl, applyAuth, currentSlug } from "../lib/api/routing";
import { keepIfTapped, logTap, noteRoutine } from "../lib/noticeTrace";
import { useUI } from "../state/uiStore";

// A clicked push notification lands here (ADR 0135 W2, "Clicking fetches the current
// authoritative record"). The push worker (public/sw.js) either focuses an open console and
// posts `protoagent:notice-click`, or opens `/app/?notice=<id>`. Both carry only the opaque
// notice id; this turns it into the place the operator needs: for a decision, its answer form.

const CLICK_MESSAGE = "protoagent:notice-click"; // sw.js CLICK_MESSAGE
const NOTICE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/; // sw.js NOTICE_ID
const DECISION_PREFIX = "decision-"; // graph/decisions.py: notice_id = f"decision-{rec.id}"
let latestClick = 0;
// The push worker also leaves the clicked id here (sw.js `rememberClick`): on iOS an installed
// console can come to the front with neither a `?notice=` URL nor a worker message.
const CLICK_CACHE = "protoagent-notice-click"; // sw.js CLICK_CACHE
const CLICK_KEY = "/app/__notice-click"; // sw.js CLICK_KEY
const CLICK_MAX_AGE_MS = 5 * 60_000; // older than this is not "the tap that brought me here"
// A phone can bring the console to the front BEFORE the worker has handled the tap, so one
// look is not enough: keep looking for a short while after every foreground.
const POLL_EVERY_MS = 400;
const POLL_FOR_MS = 10_000;

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
  // The latest click wins: a slower lookup for an earlier click must not replace it.
  const mine = ++latestClick;
  const agent = await agentOf(id);
  if (mine !== latestClick) return false;
  logTap("open", `${id} raised by ${agent ?? "this agent"}; here is ${currentSlug()}`);
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

/** Read and delete the click the push worker left, if it is recent. Null when there is none,
 *  it is stale, or Cache Storage is unavailable (private mode). */
export async function takeClickedNotice(now: number = Date.now()): Promise<string | null> {
  try {
    // `has` first: opening a cache creates it, and the console must leave nothing behind
    // just by looking. The whole cache goes once its one entry is read.
    if (typeof caches === "undefined" || !(await caches.has(CLICK_CACHE))) return null;
    const hit = await (await caches.open(CLICK_CACHE)).match(CLICK_KEY);
    await caches.delete(CLICK_CACHE);
    if (!hit) return null;
    const body = (await hit.json()) as { id?: unknown; at?: unknown };
    if (typeof body.id !== "string" || typeof body.at !== "number") return null;
    return now - body.at >= 0 && now - body.at <= CLICK_MAX_AGE_MS ? body.id : null;
  } catch {
    return null;
  }
}

export function NoticeClickWatch() {
  useEffect(() => {
    const fromUrl = takeNoticeFromUrl();
    if (fromUrl) logTap("start", `${window.location.pathname} with notice ${fromUrl}`);
    else noteRoutine("start", window.location.pathname);
    void openNotice(fromUrl);
    let stopped = false;
    let polling = false;
    const fromWorker = () => {
      if (document.visibilityState !== "visible" || polling) return;
      polling = true;
      const until = Date.now() + POLL_FOR_MS;
      noteRoutine("front", "looking for a tapped notice");
      const look = async () => {
        const id = stopped ? null : await takeClickedNotice();
        if (id) {
          polling = false;
          logTap("note", `found ${id}`);
          void openNotice(id);
        } else if (stopped || Date.now() >= until) {
          polling = false;
          if (!stopped) {
            noteRoutine("note", "none found");
            void keepIfTapped();
          }
        } else {
          window.setTimeout(() => void look(), POLL_EVERY_MS);
        }
      };
      void look();
    };
    fromWorker();
    document.addEventListener("visibilitychange", fromWorker);
    window.addEventListener("focus", fromWorker);
    window.addEventListener("pageshow", fromWorker);
    const stop = () => {
      stopped = true;
      document.removeEventListener("visibilitychange", fromWorker);
      window.removeEventListener("focus", fromWorker);
      window.removeEventListener("pageshow", fromWorker);
    };
    const worker = typeof navigator !== "undefined" ? navigator.serviceWorker : undefined;
    if (!worker) return stop;
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; id?: unknown } | null;
      if (!data || data.type !== CLICK_MESSAGE) return;
      // This click arrived by message, so the note the worker left for it is spent: drop it,
      // or the next time the console comes to the front it would open the same decision again.
      void takeClickedNotice();
      logTap("message", `worker says ${String(data.id)} was tapped`);
      void openNotice(data.id);
    };
    worker.addEventListener("message", onMessage);
    return () => { stop(); worker.removeEventListener("message", onMessage); };
  }, []);
  return null;
}
