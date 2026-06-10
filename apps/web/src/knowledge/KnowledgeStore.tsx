import { Input } from "@protolabsai/ui/forms";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Brain, Database, RefreshCw } from "lucide-react";

import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { PanelHeader } from "@protolabsai/ui/navigation";
import type { KnowledgeChunk } from "../lib/types";

// Knowledge → Store (ADR 0020) — a searchable window onto the agent's knowledge
// base (knowledge/store.py, FTS5): findings, daily-log entries, harvested
// sessions, operator notes. The same store KnowledgeMiddleware queries before
// every turn, so this is also where you debug "why did it recall that?". Empty
// query → most-recent chunks; typing runs server-side FTS5 search (debounced).

function ago(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function KnowledgeStore({ onError }: { onError: (message: string) => void }) {
  const [results, setResults] = useState<KnowledgeChunk[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");

  async function run(q: string) {
    setLoading(true);
    try {
      const r = await api.knowledgeSearch(q);
      setEnabled(r.enabled);
      setResults(r.results || []);
      setStats(r.stats || {});
      onError("");
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  // Fires on mount (query="" → recent) and debounced on every keystroke.
  useEffect(() => {
    const t = window.setTimeout(() => void run(query), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  const total = stats.chunks ?? stats.total ?? 0;

  return (
    <section className="panel stage-panel" data-testid="knowledge-store">
      <PanelHeader
        title="Knowledge"
        kicker={`searchable knowledge base${total ? ` · ${total} entr${total === 1 ? "y" : "ies"}` : ""}`}
        actions={
          <Button icon variant="ghost" type="button" onClick={() => void run(query)} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "spin" : ""} />
          </Button>
        }
      />

      <div className="stage-body">
        <Input
          className="playbook-search"
          type="search"
          placeholder="Search the knowledge base (findings, notes, daily log)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />

        {!enabled ? (
          <Empty>
            The knowledge store is off (enable <code>middleware.knowledge</code>).
          </Empty>
        ) : results.length === 0 ? (
          query.trim() ? (
            <Empty>No entries match your search.</Empty>
          ) : (
            <Empty
              title="The knowledge base is empty"
              description="findings, daily-log entries, and harvested sessions will appear here as the agent works."
            />
          )
        ) : (
          <ul className="playbook-list">
            {results.map((c) => (
              <li key={c.id} className="playbook-card">
                <div className="playbook-main">
                  <div className="playbook-title">
                    <span title={`domain: ${c.domain}`}>
                      <Badge status="success">
                        <Database size={12} /> {c.domain}
                      </Badge>
                    </span>
                    {c.finding_type ? (
                      <span title="finding type">
                        <Badge status="neutral">{c.finding_type}</Badge>
                      </span>
                    ) : null}
                    {c.heading ? <strong>{c.heading}</strong> : null}
                  </div>
                  <p className="playbook-desc">{c.content || c.preview}</p>
                  {c.source ? (
                    <div className="playbook-tools">
                      <code>{c.source}</code>
                    </div>
                  ) : null}
                </div>
                <div className="playbook-meta">
                  <span title="added">{ago(c.created_at)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className="playbook-foot">
        <Brain size={13} /> This is the memory the agent retrieves into context before each turn — search it to see what it knows.
      </p>
    </section>
  );
}
