import { Loader2, RefreshCw, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { onServerEvent } from "../lib/events";
import type { ActivityMessage } from "../lib/types";

// The durable Activity thread (ADR 0003): where agent-initiated turns
// (scheduled fires, inbox items) land. Loads history from GET /api/activity,
// appends live via the `activity.message` push event, and lets the operator
// reply — a normal turn into the `system:activity` context. We don't render the
// reply's stream; the assistant's response arrives uniformly via the bus event,
// the same path a scheduled fire takes, so there's no double-render.

export function ActivitySurface({ onError }: { onError: (message: string) => void }) {
  const [messages, setMessages] = useState<ActivityMessage[]>([]);
  const [contextId, setContextId] = useState("system:activity");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await api.activity();
      setContextId(r.context_id);
      setMessages(r.messages);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);

  // Live append: every completed Activity turn (scheduled fire, inbox item, or
  // our own reply) pushes an `activity.message` event with the assistant text.
  useEffect(
    () =>
      onServerEvent("activity.message", (data) => {
        const text = typeof data.text === "string" ? data.text : "";
        if (text) setMessages((prev) => [...prev, { role: "assistant", content: text }]);
      }),
    [],
  );

  // Keep the latest message in view.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setDraft("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    try {
      // Fire the turn into the Activity context; the reply arrives via the bus.
      await api.streamChat(text, contextId, {});
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="panel stage-panel">
      <div className="panel-header">
        <div>
          <h1>Activity</h1>
          <p className="panel-kicker">scheduled fires &amp; agent-initiated messages</p>
        </div>
        <button className="icon-button" type="button" onClick={() => void load()} title="Refresh">
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
        </button>
      </div>

      <div className="stage-body activity-body">
        <div className="activity-log" ref={scrollRef}>
          {messages.length === 0 && !loading ? (
            <div className="activity-empty">
              Nothing yet. Scheduled prompts and other agent-initiated messages land here.
            </div>
          ) : null}
          {messages.map((m, i) => (
            <div className={`activity-msg activity-${m.role}`} key={i}>
              <span className="activity-role">{m.role === "user" ? "you" : "agent"}</span>
              <div className="activity-content">
                <Markdown>{m.content}</Markdown>
              </div>
            </div>
          ))}
        </div>

        <form
          className="activity-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Reply in the activity thread…"
            rows={2}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) {
                e.preventDefault();
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
