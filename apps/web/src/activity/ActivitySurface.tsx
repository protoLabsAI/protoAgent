import "./activity.css";

import { Empty } from "@protolabsai/ui/primitives";
import { Clock, Inbox, Maximize2, MessageSquare, Users, Webhook, Zap } from "lucide-react";

import { useCallback, useEffect, useRef, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { openDocument } from "../docviewer";
import { useUtilityHeaderReload } from "../app/UtilityWidget";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { onServerEvent } from "../lib/events";
import type { ActivityEntry } from "../lib/types";

// The Activity provenance feed (ADR 0022): a READ-ONLY timeline of agent-initiated
// turns, each tagged with what triggered it (scheduled job / webhook / inbox /
// sister agent). Loads from GET /api/activity and appends live via the
// `activity.message` push event. Read-only since the 2026-06 IA pass — Activity is a
// utility-bar widget now (ActivityWidget), not a rail surface you reply into.

// origin → badge (icon + label). "" / unknown falls back to a generic agent turn.
const ORIGIN: Record<string, { icon: typeof Clock; label: string }> = {
  scheduler: { icon: Clock, label: "scheduled" },
  inbox: { icon: Inbox, label: "inbox" },
  webhook: { icon: Webhook, label: "webhook" },
  a2a: { icon: Users, label: "sister-agent" },
  operator: { icon: MessageSquare, label: "you" },
};

function Badge({ entry }: { entry: ActivityEntry }) {
  const o = ORIGIN[entry.origin] ?? { icon: Zap, label: entry.origin || "agent" };
  const Icon = o.icon;
  return (
    <div className="activity-prov">
      <span className={`activity-origin activity-origin-${entry.origin || "agent"}`}>
        <Icon size={12} /> {o.label}
      </span>
      {entry.trigger ? <span className="activity-trigger">{entry.trigger}</span> : null}
      {entry.priority ? <span className={`inbox-pri inbox-pri-${entry.priority}`}>{entry.priority}</span> : null}
      {entry.created_at ? <span className="activity-time">{ago(entry.created_at)}</span> : null}
    </div>
  );
}

export function ActivitySurface() {
  // Held newest-first (as the API returns), rendered oldest-first.
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.activity();
      setEntries(r.entries || []);
      setError(null);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  // The reload lives in the dialog header (UtilityWidget) — no second panel header here.
  useUtilityHeaderReload(load, loading);

  // Live append: every completed Activity turn pushes `activity.message` with
  // the assistant text + provenance. Prepend (newest-first store order).
  useEffect(
    () =>
      onServerEvent("activity.message", (data) => {
        const text = typeof data.text === "string" ? data.text : "";
        if (!text) return;
        const entry: ActivityEntry = {
          id: Date.now(),
          created_at: new Date().toISOString(),
          origin: typeof data.origin === "string" ? data.origin : "",
          trigger: typeof data.trigger === "string" ? data.trigger : "",
          priority: typeof data.priority === "string" ? data.priority : "",
          state: "completed",
          text,
          task_id: "",
        };
        setEntries((prev) => [entry, ...prev]);
      }),
    [],
  );

  // Keep the newest (bottom, since we render chronological) in view.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [entries]);

  const chronological = [...entries].reverse();

  return (
    <div className="activity-body util-dialog-fill" data-testid="activity-surface">
      {error ? (
        <div className="activity-error" role="alert">
          {error}
        </div>
      ) : null}
      <div className="activity-feed" ref={scrollRef}>
          {chronological.length === 0 && !loading ? (
            <Empty
              className="activity-empty"
              title="Nothing yet"
              description="Scheduled fires, inbox items, and sister-agent pushes land here — each tagged with what triggered it."
            />
          ) : null}
          {chronological.map((e) => (
            <div className="activity-entry" key={e.id} data-origin={e.origin}>
              <div className="activity-entry-head">
                <Badge entry={e} />
                {/* Open the full entry in the shared full-screen reader (ADR 0062) —
                    the same view the chat report card opens. */}
                <button
                  type="button"
                  className="pl-iconbtn activity-entry-open"
                  aria-label="Open in reader"
                  title="Open in reader"
                  onClick={() =>
                    openDocument({
                      title: ORIGIN[e.origin]?.label ?? e.origin ?? "Activity",
                      subtitle: [e.trigger, e.created_at ? ago(e.created_at) : ""].filter(Boolean).join(" · ") || undefined,
                      content: e.text,
                    })
                  }
                >
                  <Maximize2 size={13} />
                </button>
              </div>
              <div className="activity-content">
                <Markdown>{e.text}</Markdown>
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}
