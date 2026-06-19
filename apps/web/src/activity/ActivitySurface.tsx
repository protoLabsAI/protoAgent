import "./activity.css";

import { Empty } from "@protolabsai/ui/primitives";
import { Clock, Inbox, MessageSquare, Users, Webhook, Zap } from "lucide-react";

import { useEffect, useRef, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
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

  async function load() {
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
  }
  useEffect(() => {
    void load();
  }, []);

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
    <section className="panel stage-panel" data-testid="activity-surface">
      <PanelHeader
        title="Activity"
        kicker="what the agent did on its own — and why"
        actions={<RefreshButton onClick={() => void load()} busy={loading} />}
      />

      <div className="stage-body activity-body">
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
              <Badge entry={e} />
              <div className="activity-content">
                <Markdown>{e.text}</Markdown>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
