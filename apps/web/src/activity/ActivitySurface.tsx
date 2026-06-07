import { Clock, Inbox, Loader2, MessageSquare, RefreshCw, Send, Users, Webhook, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { PanelHeader } from "../app/PanelHeader";
import { onServerEvent } from "../lib/events";
import type { ActivityEntry } from "../lib/types";

// The Activity provenance feed (ADR 0022): a timeline of agent-initiated turns,
// each tagged with what triggered it (scheduled job / webhook / inbox / sister
// agent / your reply). Loads from GET /api/activity, appends live via the
// `activity.message` push event, and lets the operator reply into the
// `system:activity` thread — the reply's answer arrives as a new feed entry.

const ACTIVITY = "system:activity";

// origin → badge (icon + label). "" / unknown falls back to a generic agent turn.
const ORIGIN: Record<string, { icon: typeof Clock; label: string }> = {
  scheduler: { icon: Clock, label: "scheduled" },
  inbox: { icon: Inbox, label: "inbox" },
  webhook: { icon: Webhook, label: "webhook" },
  a2a: { icon: Users, label: "sister-agent" },
  operator: { icon: MessageSquare, label: "you" },
};

function ago(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

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

export function ActivitySurface({ onError }: { onError: (message: string) => void }) {
  // Held newest-first (as the API returns), rendered oldest-first.
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await api.activity();
      setEntries(r.entries || []);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
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

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setDraft("");
    try {
      // Reply into the Activity thread; the answer returns as a feed entry.
      await api.streamChat(text, ACTIVITY, {});
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  const chronological = [...entries].reverse();

  return (
    <section className="panel stage-panel" data-testid="activity-surface">
      <PanelHeader
        title="Activity"
        kicker="what the agent did on its own — and why"
        actions={
          <button className="icon-button" type="button" onClick={() => void load()} title="Refresh">
            {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
          </button>
        }
      />

      <div className="stage-body activity-body">
        <div className="activity-feed" ref={scrollRef}>
          {chronological.length === 0 && !loading ? (
            <div className="activity-empty">
              Nothing yet. Scheduled fires, inbox items, and sister-agent pushes land here — each tagged with what triggered it.
            </div>
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

        <form
          className="activity-composer"
          onSubmit={(ev) => {
            ev.preventDefault();
            void send();
          }}
        >
          <textarea
            value={draft}
            onChange={(ev) => setDraft(ev.target.value)}
            placeholder="Reply in the activity thread…"
            rows={2}
            onKeyDown={(ev) => {
              if (ev.key === "Enter" && !ev.shiftKey && !ev.ctrlKey) {
                ev.preventDefault();
                void send();
              }
            }}
          />
          <button className="primary-button" type="submit" disabled={sending || !draft.trim()}>
            {sending ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
            Send
          </button>
        </form>
      </div>
    </section>
  );
}
