/**
 * Close a settled decision's delivered push notification on THIS device (#5664, slice 4).
 *
 * The push worker (`public/sw.js` `showNotice`) shows one notification per notice, tagged with
 * the notice id; a decision's notice id is `decision-<decision id>` (`src/graph/decisions.py`).
 * Once the decision is answered, cancelled, expired or withdrawn, the open console closes that
 * notification so the lock screen never shows a question that is already settled. No new push
 * is sent: each device's console does this when it hears `decision.changed` or comes to the
 * front.
 *
 * Every helper here is best-effort: it never throws, and it does nothing when the page has no
 * service worker API, no registration, or the registration can't list notifications.
 */

export const DECISION_NOTICE_PREFIX = "decision-";

/** How long to wait for the service worker registration before giving up. */
const READY_TIMEOUT_MS = 5000;

type NoticeLike = { tag?: string; timestamp?: number; close: () => void };
type RegistrationLike = { getNotifications?: (filter?: { tag?: string }) => Promise<NoticeLike[]> };

/** The page's service worker registration, or null when there is none within the bound. */
async function readyRegistration(): Promise<RegistrationLike | null> {
  const sw = typeof navigator !== "undefined"
    ? (navigator as { serviceWorker?: { ready?: Promise<unknown> } }).serviceWorker
    : undefined;
  if (!sw?.ready) return null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), READY_TIMEOUT_MS);
    });
    const reg = await Promise.race([sw.ready, timeout]);
    return reg && typeof (reg as RegistrationLike).getNotifications === "function"
      ? reg as RegistrationLike : null;
  } catch {
    return null;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** Close the shown notification(s) of one decision on this device. Never throws. */
export async function closeDecisionNotice(decisionId: string): Promise<void> {
  if (!decisionId) return;
  try {
    const reg = await readyRegistration();
    if (!reg?.getNotifications) return;
    const shown = await reg.getNotifications({ tag: DECISION_NOTICE_PREFIX + decisionId });
    for (const n of shown) {
      try { n.close(); } catch { /* one stuck notification must not block the rest */ }
    }
  } catch {
    /* best effort: no API, no registration or a failed read leaves the notification alone */
  }
}

/**
 * Close every shown decision notification whose decision is not in `pending` — the console's
 * sweep on load and on coming back to the front. One `getNotifications()` call. Notifications of
 * pending decisions, non-decision notices, and any notification delivered after `listedAt` (the
 * moment the pending list was read, so a brand-new decision the list predates) are left alone.
 * Never throws.
 */
export async function closeSettledDecisionNotices(
  pending: ReadonlyArray<{ id: string; notice_id?: string }>,
  listedAt?: number,
): Promise<void> {
  try {
    const reg = await readyRegistration();
    if (!reg?.getNotifications) return;
    const keep = new Set<string>();
    for (const d of pending) {
      keep.add(DECISION_NOTICE_PREFIX + d.id);
      if (d.notice_id) keep.add(d.notice_id);
    }
    const shown = await reg.getNotifications();
    for (const n of shown) {
      const tag = typeof n.tag === "string" ? n.tag : "";
      if (!tag.startsWith(DECISION_NOTICE_PREFIX) || tag.length === DECISION_NOTICE_PREFIX.length) continue;
      if (keep.has(tag)) continue;
      if (listedAt != null && typeof n.timestamp === "number" && n.timestamp > listedAt) continue;
      try { n.close(); } catch { /* keep going */ }
    }
  } catch {
    /* best effort */
  }
}
