import { Input, Textarea } from "@protolabsai/ui/forms";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Brain, Database, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";

import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { QuickSetting } from "../settings/QuickSetting";
import type { KnowledgeChunk } from "../lib/types";

// Knowledge → Store (ADR 0020) — a searchable window onto the agent's knowledge
// base (knowledge/store.py, FTS5): findings, daily-log entries, harvested
// sessions, operator notes. The same store KnowledgeMiddleware queries before
// every turn, so this is also where you debug "why did it recall that?". Empty
// query → most-recent chunks; typing runs server-side FTS5 search (debounced).
// The operator can also CURATE the store here: add a fact, fix a stale chunk,
// delete a wrong one. Edit replaces the chunk server-side (new id — the new
// revision is added before the old row is dropped, and a hybrid store re-embeds).

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

type Draft = { heading: string; domain: string; content: string };
const EMPTY_DRAFT: Draft = { heading: "", domain: "general", content: "" };

function ChunkForm({
  draft,
  setDraft,
  onSave,
  onCancel,
  saving,
  saveLabel,
}: {
  draft: Draft;
  setDraft: (d: Draft) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  saveLabel: string;
}) {
  return (
    <div className="knowledge-chunk-form">
      <div className="knowledge-chunk-form-row">
        <Input
          type="text"
          placeholder="heading (optional)"
          value={draft.heading}
          onChange={(e) => setDraft({ ...draft, heading: e.target.value })}
          aria-label="heading"
        />
        <Input
          type="text"
          placeholder="domain"
          value={draft.domain}
          onChange={(e) => setDraft({ ...draft, domain: e.target.value })}
          aria-label="domain"
          style={{ maxWidth: 160 }}
        />
      </div>
      <Textarea
        rows={4}
        placeholder="What should the agent know? This becomes retrievable context."
        value={draft.content}
        onChange={(e) => setDraft({ ...draft, content: e.target.value })}
        aria-label="content"
      />
      <div className="knowledge-chunk-form-row">
        <Button type="button" variant="primary" size="sm" disabled={saving || !draft.content.trim()} onClick={onSave}>
          {saveLabel}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

export function KnowledgeStore({ onError }: { onError: (message: string) => void }) {
  const [results, setResults] = useState<KnowledgeChunk[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");

  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<KnowledgeChunk | null>(null);

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

  async function save() {
    setSaving(true);
    try {
      if (editingId !== null) {
        await api.updateKnowledgeChunk(editingId, draft);
      } else {
        await api.addKnowledgeChunk(draft);
      }
      setAdding(false);
      setEditingId(null);
      setDraft(EMPTY_DRAFT);
      await run(query);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: number) {
    try {
      await api.deleteKnowledgeChunk(id);
      await run(query);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }

  function startEdit(c: KnowledgeChunk) {
    setAdding(false);
    setEditingId(c.id);
    setDraft({ heading: c.heading || "", domain: c.domain || "general", content: c.content || "" });
  }

  const total = stats.chunks ?? stats.total ?? 0;

  return (
    <section className="panel stage-panel" data-testid="knowledge-store">
      <PanelHeader
        title="Knowledge"
        kicker={`searchable knowledge base${total ? ` · ${total} entr${total === 1 ? "y" : "ies"}` : ""}`}
        actions={
          <>
            {/* Quick-set recall behaviour right where you inspect what the agent knows (ADR 0048). */}
            <QuickSetting keys={["knowledge.top_k", "knowledge.embeddings"]} title="Recall" label="Knowledge recall settings" />
            {enabled ? (
              <Button
                icon
                variant="ghost"
                type="button"
                onClick={() => { setEditingId(null); setDraft(EMPTY_DRAFT); setAdding((v) => !v); }}
                title="Add a knowledge entry"
              >
                <Plus size={16} />
              </Button>
            ) : null}
            <Button icon variant="ghost" type="button" onClick={() => void run(query)} disabled={loading} title="Refresh">
              <RefreshCw size={16} className={loading ? "spin" : ""} />
            </Button>
          </>
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

        {adding ? (
          <ChunkForm
            draft={draft}
            setDraft={setDraft}
            onSave={() => void save()}
            onCancel={() => { setAdding(false); setDraft(EMPTY_DRAFT); }}
            saving={saving}
            saveLabel="Add entry"
          />
        ) : null}

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
              description="findings, daily-log entries, and harvested sessions will appear here as the agent works — or add one with +."
            />
          )
        ) : (
          <ul className="playbook-list">
            {results.map((c) => (
              <li key={c.id} className="playbook-card">
                {editingId === c.id ? (
                  <ChunkForm
                    draft={draft}
                    setDraft={setDraft}
                    onSave={() => void save()}
                    onCancel={() => { setEditingId(null); setDraft(EMPTY_DRAFT); }}
                    saving={saving}
                    saveLabel="Save changes"
                  />
                ) : (
                  <>
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
                      <span className="knowledge-chunk-actions">
                        <Button icon variant="ghost" type="button" title="Edit entry" onClick={() => startEdit(c)} aria-label={`edit entry ${c.id}`}>
                          <Pencil size={14} />
                        </Button>
                        <Button icon variant="ghost" type="button" title="Delete entry" onClick={() => setPendingDelete(c)} aria-label={`delete entry ${c.id}`}>
                          <Trash2 size={14} />
                        </Button>
                      </span>
                    </div>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this knowledge entry?"
        confirmLabel="Delete entry"
        destructive
        onConfirm={() => {
          if (pendingDelete) void remove(pendingDelete.id);
          setPendingDelete(null);
        }}
        onClose={() => setPendingDelete(null)}
      >
        {pendingDelete
          ? `"${pendingDelete.heading || pendingDelete.preview.slice(0, 80)}" will be removed from the knowledge base — the agent will no longer recall it. This can't be undone.`
          : undefined}
      </ConfirmDialog>

      <p className="playbook-foot">
        <Brain size={13} /> This is the memory the agent retrieves into context before each turn — search it to see what it knows.
      </p>
    </section>
  );
}
