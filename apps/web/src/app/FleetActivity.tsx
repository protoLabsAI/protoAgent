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
import type { FleetAgent } from "../lib/types";
import "./fleet-activity.css";

export type FleetEventKind = "online" | "offline" | "added" | "removed" | "broadcast";

export type FleetEvent = {
  id: string;
  ts: number;
  source: string;
  text: string;
  kind: FleetEventKind;
};

type FleetActivityState = {
  events: FleetEvent[];
  push: (e: Omit<FleetEvent, "id" | "ts">) => void;
};

let seq = 0;
const MAX = 60;

const useFleetActivity = create<FleetActivityState>((set) => ({
  events: [],
  push: (e) =>
    set((s) => ({
      events: [{ ...e, id: `flev-${(seq += 1)}`, ts: Date.now() }, ...s.events].slice(0, MAX),
    })),
}));

/** Append an event from anywhere (e.g. the Fleet Room broadcast). */
export const pushFleetEvent = (e: Omit<FleetEvent, "id" | "ts">) => useFleetActivity.getState().push(e);

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

/** Mounted once at the app root: keeps the feed capturing continuously (headless). */
export function FleetActivityCapture() {
  useRosterCapture();
  return null;
}

/** The activity column rendered inside the Fleet Room dialog (right of the roster). */
export function FleetActivityFeed() {
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
