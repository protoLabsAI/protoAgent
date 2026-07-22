// Fleet Activity — the live, fleet-wide event feed that renders as the RIGHT COLUMN of
// the Fleet Room dialog (next to the roster), matching the concept mockup. The capture
// runs headless at the app root so the log accumulates even while the room is closed;
// FleetActivityFeed is the column the Fleet Room renders.
//
// v1 sources REAL events only: member presence transitions (online/offline/added/removed,
// diffed from the roster poll) + broadcasts you send. Richer cross-member events (PRs,
// approvals, a member's running turn) come next — aggregate each member's event bus
// (ADR 0039) through the hub proxy — and are deliberately NOT faked here.
import { useEffect, useRef } from "react";
import { create } from "zustand";
import { Radio } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { fleetQuery } from "../lib/queries";
import { api, memberPath } from "../lib/api";
import { buildEventsUrl } from "../lib/events";
import type { FleetAgent } from "../lib/types";
import "./fleet-activity.css";

export type FleetEventKind = "online" | "offline" | "added" | "removed" | "broadcast" | "turn" | "activity";

export type FleetEvent = {
  id: string;
  ts: number;
  source: string;
  text: string;
  kind: FleetEventKind;
};

type FleetActivityState = {
  events: FleetEvent[];
  /** slug → count of turns currently in flight (drives the roster "running" pill). */
  running: Record<string, number>;
  push: (e: Omit<FleetEvent, "id" | "ts">) => void;
  markRunning: (slug: string) => void;
  markDone: (slug: string) => void;
  clear: (slug: string) => void;
};

let seq = 0;
const MAX = 60;

const useFleetActivity = create<FleetActivityState>((set) => ({
  events: [],
  running: {},
  push: (e) =>
    set((s) => ({
      events: [{ ...e, id: `flev-${(seq += 1)}`, ts: Date.now() }, ...s.events].slice(0, MAX),
    })),
  markRunning: (slug) => set((s) => ({ running: { ...s.running, [slug]: (s.running[slug] ?? 0) + 1 } })),
  markDone: (slug) =>
    set((s) => {
      const n = Math.max(0, (s.running[slug] ?? 0) - 1);
      const running = { ...s.running };
      if (n === 0) delete running[slug];
      else running[slug] = n;
      return { running };
    }),
  clear: (slug) =>
    set((s) => {
      if (!(slug in s.running)) return s;
      const running = { ...s.running };
      delete running[slug];
      return { running };
    }),
}));

/** Append an event from anywhere (e.g. the Fleet Room broadcast). */
export const pushFleetEvent = (e: Omit<FleetEvent, "id" | "ts">) => useFleetActivity.getState().push(e);
/** Optimistically mark a member busy when you send to it (cleared by its terminal turn.usage). */
export const markMemberRunning = (slug: string) => useFleetActivity.getState().markRunning(slug);
/** Undo an optimistic mark when the send itself fails (no turn will run to clear it). */
export const markMemberDone = (slug: string) => useFleetActivity.getState().markDone(slug);
/** Reset a member's in-flight count entirely (its stream closed / it went offline). */
export const clearMemberRunning = (slug: string) => useFleetActivity.getState().clear(slug);
/** Members with a turn in flight — for the roster "running a turn" pill. */
export const useMemberRunning = (): Record<string, number> => useFleetActivity((s) => s.running);

const slugOf = (a: FleetAgent): string => (a.host ? "host" : a.id);
const hhmm = (ts: number): string =>
  new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/** Diff consecutive roster polls into presence events. */
function useRosterCapture() {
  const { data } = useQuery(fleetQuery());
  const prev = useRef<Map<string, FleetAgent> | null>(null);
  useEffect(() => {
    if (!data) return; // wait for the first real roster (undefined→data isn't a diff)
    const cur = new Map(data.agents.map((a) => [slugOf(a), a]));
    const before = prev.current;
    prev.current = cur;
    if (!before) return; // first real roster seeds the baseline — don't emit a burst
    const push = useFleetActivity.getState().push;
    for (const [slug, a] of cur) {
      const was = before.get(slug);
      if (!was) push({ source: a.name, text: "joined the fleet", kind: "added" });
      else if (was.running !== a.running)
        a.running
          ? push({ source: a.name, text: "came online", kind: "online" })
          : push({ source: a.name, text: "went offline", kind: "offline" });
    }
    for (const [slug, a] of before) if (!cur.has(slug)) push({ source: a.name, text: "left the fleet", kind: "removed" });
  }, [data]);
}

const shorten = (s: string, n = 60): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** Curated map of a member's event-bus topics → feed items. Noisy topics
 *  (goal.iteration, task.changed, background.progress, watch.*) are skipped. */
function mapTopic(topic: string, data: Record<string, unknown>): { text: string; kind: FleetEventKind } | null {
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  switch (topic) {
    // A live member turn only bus-pushes `turn.usage` at completion (started/tool frames
    // stay on the turn's own SSE, not the event bus) — so this is the "member responded"
    // signal for a DM or broadcast.
    case "turn.usage": {
      const state = str(data.state);
      if (state === "failed") return { text: "hit an error on a turn", kind: "offline" };
      if (state === "canceled" || state === "cancelled") return null;
      return { text: "finished a turn", kind: "turn" };
    }
    case "turn.started":
      return { text: "is running a turn", kind: "turn" };
    // turn.finished is intentionally unmapped — turn.usage (which fires for both autonomous
    // and direct turns) is the single "finished a turn" row, so autonomous turns don't
    // produce two.
    case "activity.message": {
      const t = str(data.text) || str(data.message);
      return { text: t ? `“${shorten(t)}”` : "posted to Activity", kind: "activity" };
    }
    case "inbox.item":
      return { text: "received an inbox item", kind: "activity" };
    case "scheduler.fired": {
      const n = str(data.name) || str(data.title);
      return { text: n ? `ran a scheduled task: ${n}` : "ran a scheduled task", kind: "activity" };
    }
    case "goal.achieved":
      return { text: "achieved a goal", kind: "activity" };
    case "goal.failed":
      return { text: "a goal failed", kind: "offline" };
    case "background.completed":
      return { text: "finished background work", kind: "activity" };
    case "chat.resumed": {
      const t = str(data.text);
      return { text: t ? `resumed: “${shorten(t)}”` : "resumed a turn", kind: "activity" };
    }
    case "goal.changed": {
      const c = str(data.condition) || str(data.title);
      return { text: c ? `working a goal: ${shorten(c, 40)}` : "updated a goal", kind: "activity" };
    }
    default:
      return null;
  }
}

/** Open an SSE stream per ONLINE member (/agents/<slug>/api/events, via the hub proxy)
 *  and map its event-bus topics into the feed — this is the fleet-wide "one event log"
 *  (ADR 0039). Streams open/close as members come online/go offline; a stream that errors
 *  is dropped and reopened (with a fresh token) on the next roster poll. */
function useFleetStreams() {
  const { data } = useQuery(fleetQuery());
  const streams = useRef<Map<string, EventSource>>(new Map());
  const opening = useRef<Set<string>>(new Set());
  const seen = useRef<Set<string>>(new Set());
  const nameBySlug = useRef<Map<string, string>>(new Map());
  const wantRef = useRef<Set<string>>(new Set());
  const disposed = useRef(false);

  const agents = data?.agents ?? [];
  const sig = agents
    .filter((a) => a.running)
    .map(slugOf)
    .sort()
    .join(",");

  useEffect(() => {
    nameBySlug.current = new Map(agents.map((a) => [slugOf(a), a.name]));
    const want = new Set(agents.filter((a) => a.running).map(slugOf));
    wantRef.current = want;

    for (const [slug, es] of streams.current) {
      if (!want.has(slug)) {
        es.close();
        streams.current.delete(slug);
        useFleetActivity.getState().clear(slug); // member offline → reset its in-flight count
      }
    }

    const handle = (slug: string, raw: string) => {
      let frame: { topic?: string; data?: Record<string, unknown>; seq?: number };
      try {
        frame = JSON.parse(raw || "{}");
      } catch {
        return;
      }
      if (!frame.topic) return;
      if (typeof frame.seq === "number") {
        const key = `${slug}:${frame.seq}`;
        if (seen.current.has(key)) return;
        seen.current.add(key);
        if (seen.current.size > 1500) seen.current.clear();
      }
      // Live "running" state for the roster pill. Count UP on turn.started (autonomous
      // turns) or an optimistic broadcast mark; count DOWN only on the terminal turn.usage,
      // which fires for BOTH autonomous and direct turns — so each turn is +1/−1 exactly
      // once. (turn.finished also fires for autonomous turns but is NOT counted, to avoid a
      // double-decrement that would corrupt a concurrently-running member's count.)
      const store = useFleetActivity.getState();
      if (frame.topic === "turn.started") store.markRunning(slug);
      else if (frame.topic === "turn.usage") store.markDone(slug);
      const m = mapTopic(frame.topic, frame.data ?? {});
      if (!m) return;
      pushFleetEvent({ source: nameBySlug.current.get(slug) ?? slug, text: m.text, kind: m.kind });
    };

    const openStream = async (slug: string) => {
      if (streams.current.has(slug) || opening.current.has(slug)) return;
      opening.current.add(slug);
      let token = "";
      try {
        token = (await api.sseTokenFor(slug)).token || "";
      } catch {
        /* open mode → tokenless */
      }
      opening.current.delete(slug);
      // Bail if we were torn down (or the member went offline) while awaiting the token.
      if (disposed.current || !wantRef.current.has(slug) || streams.current.has(slug) || typeof EventSource === "undefined")
        return;
      const es = new EventSource(buildEventsUrl(memberPath(slug, "/api/events"), token, null));
      es.onmessage = (e) => handle(slug, (e as MessageEvent).data);
      es.onerror = () => {
        es.close();
        streams.current.delete(slug);
        // Reopen with a fresh token after a backoff — the roster sig may not change, so we
        // can't rely on the effect re-running. Only while still mounted + still a member.
        if (!disposed.current)
          setTimeout(() => {
            if (!disposed.current && wantRef.current.has(slug)) void openStream(slug);
          }, 3000);
      };
      streams.current.set(slug, es);
    };

    for (const slug of want) void openStream(slug);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  useEffect(() => {
    const map = streams.current;
    const flag = disposed;
    return () => {
      flag.current = true;
      for (const es of map.values()) es.close();
      map.clear();
      // Streams are closing, so terminal turn.usage frames won't arrive to clear in-flight
      // counts — reset them (a reopen re-detects fresh turns via turn.started).
      const st = useFleetActivity.getState();
      for (const slug of Object.keys(st.running)) st.clear(slug);
    };
  }, []);
}

/** The activity column rendered inside the Fleet Room dialog (right of the roster).
 *  Captures WHILE MOUNTED (i.e. while the room is open) — presence transitions + each
 *  online member's event-bus stream. The module-level store keeps the log across opens,
 *  and closing the room tears the streams down so we don't hold SSE connections idle. */
export function FleetActivityFeed() {
  useRosterCapture();
  useFleetStreams();
  const events = useFleetActivity((s) => s.events);
  return (
    <div className="flr-feed">
      <div className="flr-feed__head">
        <h2>Fleet activity</h2>
        <span className="flr-feed__live">
          <span className="flr-feed__livedot" />
          live
        </span>
      </div>
      <div className="flr-feed__list">
        {events.length === 0 && (
          <div className="flr-feed__empty">
            No activity yet. Start/stop a member or broadcast, and it shows up here.
          </div>
        )}
        {events.map((e) => (
          <div key={e.id} className="flr-feed__event">
            <time className="flr-feed__time">{hhmm(e.ts)}</time>
            <div className="flr-feed__body">
              <span className={`flr-feed__src flr-feed__src--${e.kind}`}>
                {e.kind === "broadcast" && <Radio size={12} />}
                {e.source}
              </span>
              <p className="flr-feed__text">{e.text}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
