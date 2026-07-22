// Fleet Activity — a right-side popout drawer showing a live, fleet-wide event feed
// (the "one event log" idea from Buzz, on our own infra). Opened from the Fleet Room's
// header or the ⌘K "Fleet Activity" command; slides over the console, independent of the
// palette so it stays ambient.
//
// v1 sources REAL events only: member presence transitions (online/offline/added/removed,
// diffed from the roster poll) + broadcasts you send. Richer cross-member events (PRs,
// approvals, tool runs) are the next step — aggregate each member's event bus (ADR 0039)
// through the hub proxy — and are deliberately NOT faked here.
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { create } from "zustand";
import { Radio, X } from "lucide-react";
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
  open: boolean;
  events: FleetEvent[];
  setOpen: (open: boolean) => void;
  push: (e: Omit<FleetEvent, "id" | "ts">) => void;
};

let seq = 0;
const MAX = 60;

const useFleetActivity = create<FleetActivityState>((set) => ({
  open: false,
  events: [],
  setOpen: (open) => set({ open }),
  push: (e) =>
    set((s) => ({
      events: [{ ...e, id: `flev-${(seq += 1)}`, ts: Date.now() }, ...s.events].slice(0, MAX),
    })),
}));

/** Open/close the drawer + append events from anywhere (Fleet Room, palette command). */
export const openFleetActivity = () => useFleetActivity.getState().setOpen(true);
export const pushFleetEvent = (e: Omit<FleetEvent, "id" | "ts">) => useFleetActivity.getState().push(e);

const slugOf = (a: FleetAgent): string => (a.host ? "host" : a.id);
const hhmm = (ts: number): string =>
  new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/** Diff consecutive roster polls into presence events. Runs whenever the drawer is
 *  mounted (app root), so the log accumulates even while the drawer is closed. */
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

/** Mounted once at the app root: captures the live feed continuously and renders the
 *  drawer when open. */
export function FleetActivityDrawer() {
  const open = useFleetActivity((s) => s.open);
  const events = useFleetActivity((s) => s.events);
  const setOpen = useFleetActivity((s) => s.setOpen);
  useRosterCapture();

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <div className={`flact-root${open ? " is-open" : ""}`} aria-hidden={!open}>
      <button type="button" className="flact-scrim" aria-label="Close activity" tabIndex={open ? 0 : -1} onClick={() => setOpen(false)} />
      <aside className="flact" role="complementary" aria-label="Fleet activity">
        <header className="flact__head">
          <span className="flact__title">Fleet activity</span>
          <span className="flact__live">
            <span className="flact__livedot" />
            live
          </span>
          <span className="flact__spacer" />
          <button type="button" className="flact__close" onClick={() => setOpen(false)} aria-label="Close">
            <X size={15} />
          </button>
        </header>
        <div className="flact__feed">
          {events.length === 0 && (
            <div className="flact__empty">
              No activity yet. Start or stop a member, or broadcast from the Fleet Room, and it shows up here.
            </div>
          )}
          {events.map((e) => (
            <div key={e.id} className="flact__event">
              <time className="flact__time">{hhmm(e.ts)}</time>
              <div className="flact__body">
                <span className={`flact__src flact__src--${e.kind}`}>
                  {e.kind === "broadcast" && <Radio size={12} />}
                  {e.source}
                </span>
                <p className="flact__text">{e.text}</p>
              </div>
            </div>
          ))}
        </div>
      </aside>
    </div>,
    document.body,
  );
}
