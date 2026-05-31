import { Check, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { onServerEvent } from "../lib/events";
import type { InboxItem } from "../lib/types";

// Right-sidebar view of the inbound inbox (ADR 0003). Lists pending stimuli
// (webhooks / external systems / sister agents), live-updates as items arrive
// over the `inbox.item` push event, and lets the operator dismiss one
// (mark it delivered). External intake is POST /api/inbox (token-gated); this
// surface is read + dismiss only.

const PRIORITY_TONE: Record<string, string> = { now: "now", next: "next", later: "later" };

export function InboxPanel({ onError }: { onError: (message: string) => void }) {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const r = await api.inbox("later", false); // all pending tiers
      setItems(r.items);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);

  // Live: a new item arrived. Reload to pick it up with its server-assigned id.
  useEffect(() => onServerEvent("inbox.item", () => void load()), []);

  async function dismiss(id: number) {
    try {
      await api.deliverInbox(id);
      setItems((prev) => prev.filter((i) => i.id !== id));
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel side-panel inbox-panel">
      <div className="panel-header compact">
        <div>
          <h2>Inbox</h2>
          <p className="panel-kicker">
            {loading ? "loading…" : `${items.length} pending`}
          </p>
        </div>
        <button className="icon-button" type="button" onClick={() => void load()} title="Refresh">
          {loading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
        </button>
      </div>

      <div className="inbox-list">
        {!loading && items.length === 0 ? (
          <div className="inbox-empty">
            Nothing pending. Inbound stimuli (webhooks, scripts, sister agents) posted to
            <code>/api/inbox</code> show up here.
          </div>
        ) : null}
        {items.map((item) => (
          <div className="inbox-item" key={item.id}>
            <div className="inbox-item-head">
              <span className={`inbox-pri inbox-pri-${PRIORITY_TONE[item.priority] || "next"}`}>
                {item.priority}
              </span>
              {item.source ? <span className="inbox-source">{item.source}</span> : null}
              <button
                className="icon-button inbox-dismiss"
                type="button"
                onClick={() => void dismiss(item.id)}
                title="Mark delivered (dismiss)"
              >
                <Check size={15} />
              </button>
            </div>
            <div className="inbox-text">{item.text}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
