import { useEffect } from "react";

import { chatStore } from "../chat/chat-store";
import { openWorkGroup } from "../chat/ChatStatusRail";
import { openChat as leaveConversationsForChat } from "../chat/conversations-store";
import { homeQueryKey, homeSessionId } from "../chat/homeTab";
import { mergeHomeEntries, type HomeEntriesRead } from "../chat/sessionHydration";
import { markUserScroll } from "../chat/streamFollow";
import { api } from "../lib/api";
import { agentHref, apiUrl, applyAuth, currentSlug } from "../lib/api/routing";
import { queryClient } from "../lib/queryClient";
import { keepIfTapped, logTap, noteRoutine } from "../lib/noticeTrace";
import { useUI } from "../state/uiStore";
import { openView } from "./palette/nav";

// A clicked push notification lands here (ADR 0135 W2, "Clicking fetches the current
// authoritative record"). The push worker (public/sw.js) either focuses an open console and
// posts `protoagent:notice-click`, or opens `/app/?notice=<id>`. Both carry only the opaque
// notice id; this turns it into the place the operator needs: the notice's entry in Home when
// Home holds one (#5545), otherwise a decision's answer form or the route the worker named.

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

// ── the notice's entry in Home (ADR 0135 D9, #5545) ──────────────────────────────────
// The server writes an owner notice into Home's thread with the notice id on it
// (`graph/home_entries.py`), and HomeAlertCard renders it with `data-home-notice`.
const HOME_READ_TIMEOUT_MS = 5_000; // the hub read a tap may wait for before falling back
const HOME_WAIT_MS = 4_000; // a console that just started: Home's tab is registered by ChatSurface
const SCROLL_LOOK_EVERY_MS = 100;
const SCROLL_LOOKS = 30; // the card mounts a few frames after Home is selected
const SCROLL_SETTLE_MS = 300;
const CONSOLE_PATH = /^\/app\/(?:agent\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\/)?$/; // sw.js CONSOLE_PATH
let latestTap = 0;

const later = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/** The Home session that already shows an entry for `id`, or null. */
function homeSessionHolding(id: string, homeId: string | undefined): string | null {
  if (!homeId) return null;
  const home = chatStore.getSnapshot().sessions.find((session) => session.id === homeId);
  return home?.messages.some((message) => message.home?.notice_id === id) ? homeId : null;
}

/** Find the Home session holding an entry for `id`, reading Home's entries from the hub when
 *  the transcript doesn't show it yet (a tap usually arrives before the next refresh). Null
 *  when Home is off, the read fails, or Home has no such entry. */
async function homeEntrySession(id: string): Promise<string | null> {
  const shown = homeSessionHolding(id, homeSessionId());
  if (shown) return shown;
  // `null` is the loaded answer "Home is off here" (ChatSurface maps the 404): don't ask again.
  if (queryClient.getQueryData(homeQueryKey()) === null) return null;
  let read: HomeEntriesRead | null = null;
  try {
    read = await Promise.race([api.homeEntries(), later(HOME_READ_TIMEOUT_MS).then(() => null)]);
  } catch {
    return null;
  }
  const homeId = typeof read?.session_id === "string" ? read.session_id : "";
  const entries = Array.isArray(read?.entries) ? read.entries : [];
  if (!homeId || !entries.some((entry) => entry.additional_kwargs?.protoagent_home_entry?.notice_id === id)) return null;
  // A console that is still starting has not registered Home's tab yet. Wait for it rather
  // than add it here: ChatSurface owns that, and loads Home's turns when it does.
  const until = Date.now() + HOME_WAIT_MS;
  while (!chatStore.getSnapshot().sessions.some((session) => session.id === homeId)) {
    if (Date.now() >= until) return null;
    await later(SCROLL_LOOK_EVERY_MS);
  }
  mergeHomeEntries(read);
  return homeSessionHolding(id, homeId);
}

/** Bring the Home card for `id` into view once it has rendered. Best-effort: Home is already
 *  selected, so a card that never shows still leaves the operator in the right chat. */
function scrollToHomeEntry(homeId: string, id: string, looks = SCROLL_LOOKS, settle = true): void {
  const root = document.getElementById(`pl-conv-${homeId}`);
  const cards = root ? [...root.querySelectorAll<HTMLElement>("[data-home-notice]")] : [];
  const card = cards.find((el) => el.dataset.homeNotice === id);
  if (!card) {
    if (looks > 0) window.setTimeout(() => scrollToHomeEntry(homeId, id, looks - 1, settle), SCROLL_LOOK_EVERY_MS);
    return;
  }
  // The transcript follows its newest message; this scroll is on the operator's behalf, so
  // the follower must let go instead of snapping back to the bottom.
  const scroller = card.closest<HTMLElement>(".pl-convo-scroll");
  if (scroller) markUserScroll(scroller);
  card.scrollIntoView?.({ block: "center" });
  // Once more after the transcript settles (a late merge, a slot restoring its last scroll).
  if (settle) window.setTimeout(() => scrollToHomeEntry(homeId, id, 0, false), SCROLL_SETTLE_MS);
}

/** Select Home and scroll to the entry for notice `id`. False when Home holds no such entry
 *  (or is off), in which case nothing changed. */
export async function openHomeEntry(id: unknown): Promise<boolean> {
  if (typeof id !== "string" || !NOTICE_ID.test(id)) return false;
  const homeId = await homeEntrySession(id);
  if (!homeId) return false;
  // Settings is an overlay above every surface: left open, it would hide Home.
  useUI.getState().closeGlobalSettings();
  leaveConversationsForChat();
  chatStore.switchSession(homeId);
  openView("chat");
  scrollToHomeEntry(homeId, id);
  return true;
}

/** The console route a worker message named, as a same-origin address, or null. Checked
 *  again here like sw.js `consoleRoute`: nothing but a known console route is followed. */
function routeHref(route: unknown, id: unknown): string | null {
  if (typeof route !== "string" || route.length > 512) return null;
  let url: URL;
  try {
    url = new URL(route, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin || url.hash || !CONSOLE_PATH.test(url.pathname)) return null;
  const keys = [...url.searchParams.keys()];
  if (keys.length > 1 || (keys.length === 1 && (keys[0] !== "notice" || url.searchParams.get("notice") !== id))) return null;
  return url.pathname + url.search;
}

/** A tapped notification, from any of the three ways a tap reaches the console. Home's entry
 *  for the notice wins; without one the tap goes where it went before Home existed: a
 *  decision's answer form, else the worker's `route` when that is another console page. */
export async function openClickedNotice(
  id: unknown, route?: unknown, go: (href: string) => void = (href) => window.location.assign(href),
): Promise<boolean> {
  // The latest tap wins: a slower Home read for an earlier tap must not replace it.
  const mine = ++latestTap;
  if (await openHomeEntry(id)) {
    logTap("home", `${String(id)} shown in Home`);
    return true;
  }
  if (mine !== latestTap) return false;
  if (await openNotice(id, go)) return true;
  if (mine !== latestTap) return false;
  const href = routeHref(route, id);
  // The worker only messages a console already on the route's page, so this is normally
  // "stay here"; loading the same page again would only reload the console.
  if (!href || new URL(href, window.location.origin).pathname === window.location.pathname) return false;
  logTap("route", `${String(id)} follows ${href}`);
  go(href);
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

export function NoticeClickWatch({ go }: { go?: (href: string) => void } = {}) {
  useEffect(() => {
    const fromUrl = takeNoticeFromUrl();
    if (fromUrl) logTap("start", `${window.location.pathname} with notice ${fromUrl}`);
    else noteRoutine("start", window.location.pathname);
    void openClickedNotice(fromUrl, undefined, go);
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
          void openClickedNotice(id, undefined, go);
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
      const data = event.data as { type?: unknown; id?: unknown; route?: unknown } | null;
      if (!data || data.type !== CLICK_MESSAGE) return;
      // This click arrived by message, so the note the worker left for it is spent: drop it,
      // or the next time the console comes to the front it would open the same decision again.
      void takeClickedNotice();
      logTap("message", `worker says ${String(data.id)} was tapped`);
      void openClickedNotice(data.id, data.route, go);
    };
    worker.addEventListener("message", onMessage);
    return () => { stop(); worker.removeEventListener("message", onMessage); };
  }, []);
  return null;
}
