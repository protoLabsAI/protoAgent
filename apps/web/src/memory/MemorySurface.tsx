import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert } from "@protolabsai/ui/data";
import { Input, Textarea } from "@protolabsai/ui/forms";
import { PanelHeader, Tabs } from "@protolabsai/ui/navigation";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Flame, History, Pencil, Syringe, Trash2 } from "lucide-react";

import { RefreshButton } from "../app/ui-kit";
import { openDocument } from "../docviewer";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { queryKeys } from "../lib/queries";
import type { KnowledgeChunk, MemorySessionDigest } from "../lib/types";

import "./memory.css";

// Memory inspector (ADR 0069 D7) — the audit surface over the memory DELIVERY
// layer: which session summaries feed the <prior_sessions> digest, which hot
// chunks ride every turn, and (D6) exactly which memory items entered which
// turn. A security control first — SpAIware-class memory poisoning gets
// *detected* here — UX second, so the panels favor raw ids/timestamps over
// polish. Distinct from Knowledge → Store (the whole KB): this is only what
// auto-injects.

type MemoryTab = "sessions" | "hot" | "injections";

const TABS: { id: MemoryTab; label: string; icon: React.ReactNode }[] = [
  { id: "sessions", label: "Sessions", icon: <History size={15} /> },
  { id: "hot", label: "Hot memory", icon: <Flame size={15} /> },
  { id: "injections", label: "Injections", icon: <Syringe size={15} /> },
];

export function MemorySurface() {
  const [tab, setTab] = useState<MemoryTab>("sessions");
  // Set by a session row's "injections" jump — pre-filters the Injections panel.
  const [injectionFilter, setInjectionFilter] = useState("");

  return (
    <section className="panel stage-panel memory-panel" data-testid="memory-surface">
      <PanelHeader
        title="Memory"
        kicker="what auto-injects into the agent's turns — inspect, audit, prune"
      />
      {/* Not `responsive`: this surface lives on the main stage (wide), and the
          deterministic role="tab" strip keeps the e2e + a11y contract simple. */}
      <Tabs
        active={tab}
        onSelect={(t) => setTab(t as MemoryTab)}
        items={TABS.map((t) => ({ id: t.id, label: t.label, icon: t.icon }))}
      />
      <div className="stage-body">
        {tab === "sessions" ? (
          <SessionsPanel
            onShowInjections={(sid) => {
              setInjectionFilter(sid);
              setTab("injections");
            }}
          />
        ) : tab === "hot" ? (
          <HotMemoryPanel />
        ) : (
          <InjectionsPanel filter={injectionFilter} setFilter={setInjectionFilter} />
        )}
      </div>
    </section>
  );
}

// ── Sessions — the summaries behind the <prior_sessions> digest ───────────────

function SessionsPanel({ onShowInjections }: { onShowInjections: (sid: string) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, isFetching, error, refetch } = useQuery({
    queryKey: queryKeys.memorySessions,
    queryFn: () => api.memorySessions(),
  });
  const sessions = data?.sessions ?? [];
  const [pendingDelete, setPendingDelete] = useState<MemorySessionDigest | null>(null);

  const del = useMutation({
    mutationFn: (sid: string) => api.deleteMemorySession(sid),
    onSuccess: (_r, sid) => {
      toast({ tone: "success", title: "Memory", message: `Session summary ${sid} deleted.` });
      void qc.invalidateQueries({ queryKey: queryKeys.memorySessions });
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  // Open the FULL summary (the render recall_session returns) in the document
  // viewer. It's the raw <session> record — show it verbatim in a <pre>, never
  // as markdown (an ingested summary could otherwise smuggle live HTML).
  async function openSession(row: MemorySessionDigest) {
    try {
      const r = await api.memorySession(row.session_id);
      openDocument({
        title: row.session_id,
        subtitle: `${row.surface} · ${row.timestamp} · ${row.message_count} msgs`,
        render: () => <pre className="memory-session-pre">{r.session.rendered || "(empty summary)"}</pre>,
      });
    } catch (e) {
      toast({ tone: "error", title: "Memory", message: errMsg(e) });
    }
  }

  return (
    <>
      <div className="memory-panel-head">
        <p className="memory-panel-hint">
          Persisted session summaries — each is one line of the <code>&lt;prior_sessions&gt;</code>{" "}
          digest the agent sees every turn. Click a row for the full summary
          (what <code>recall_session</code> returns); delete to forget a session.
        </p>
        <RefreshButton onClick={() => void refetch()} busy={isFetching} />
      </div>
      {error ? <Alert status="error">Couldn't list session summaries — {errMsg(error)}</Alert> : null}
      {!error && sessions.length === 0 ? (
        <Empty
          title="No session summaries"
          description="finished sessions persist a summary here; the digest stays empty until then."
        />
      ) : (
        <ul className="playbook-list memory-list">
          {sessions.map((s) => (
            <li key={s.session_id} className="playbook-card memory-row">
              <button
                type="button"
                className="memory-row-main"
                title="Open the full summary in the reader"
                onClick={() => void openSession(s)}
              >
                <span className="memory-row-title">
                  <Badge status="neutral">{s.surface}</Badge>
                  <code>{s.session_id}</code>
                </span>
                <span className="memory-row-topic">{s.topic || "(no user message)"}</span>
                <span className="memory-row-meta">
                  {s.timestamp !== "unknown" ? ago(s.timestamp) : "unknown"} · {s.message_count} msgs
                </span>
              </button>
              <span className="knowledge-chunk-actions">
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  title="Show what this session's turns injected"
                  aria-label={`injections for ${s.session_id}`}
                  onClick={() => onShowInjections(s.session_id)}
                >
                  <Syringe size={14} />
                </Button>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  title="Delete this session summary (drops it from the digest)"
                  aria-label={`delete session ${s.session_id}`}
                  onClick={() => setPendingDelete(s)}
                >
                  <Trash2 size={14} />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this session summary?"
        confirmLabel="Delete summary"
        destructive
        onConfirm={() => {
          if (pendingDelete) del.mutate(pendingDelete.session_id);
          setPendingDelete(null);
        }}
        onClose={() => setPendingDelete(null)}
      >
        {pendingDelete
          ? `"${pendingDelete.session_id}" leaves the <prior_sessions> digest and recall_session — the agent will no longer know of this session. This can't be undone.`
          : undefined}
      </ConfirmDialog>
    </>
  );
}

// ── Hot memory — the domain="hot" chunks injected every turn ─────────────────

function HotMemoryPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, isFetching, error, refetch } = useQuery({
    queryKey: queryKeys.memoryHot,
    queryFn: () => api.memoryHot(),
  });
  const enabled = data?.enabled ?? true;
  const chunks = data?.chunks ?? [];
  const [pendingDelete, setPendingDelete] = useState<KnowledgeChunk | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const invalidate = () => void qc.invalidateQueries({ queryKey: queryKeys.memoryHot });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteMemoryHot(id),
    onSuccess: () => {
      toast({ tone: "success", title: "Memory", message: "Hot-memory entry deleted." });
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  const save = useMutation({
    mutationFn: ({ id, content }: { id: number; content: string }) =>
      api.updateMemoryHot(id, { content }),
    onSuccess: () => {
      toast({ tone: "success", title: "Memory", message: "Hot-memory entry updated." });
      setEditingId(null);
      setDraft("");
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  return (
    <>
      <div className="memory-panel-head">
        <p className="memory-panel-hint">
          Always-on memory — every entry here is injected into <em>every</em> turn. Keep it
          small; anything wrong or planted here steers the agent until it's removed.
        </p>
        <RefreshButton onClick={() => void refetch()} busy={isFetching} />
      </div>
      {error ? <Alert status="error">Couldn't list hot memory — {errMsg(error)}</Alert> : null}
      {!enabled ? (
        <Empty>
          The knowledge store is off (enable <code>middleware.knowledge</code>).
        </Empty>
      ) : !error && chunks.length === 0 ? (
        <Empty
          title="No hot memory"
          description="the agent (or you, via the Knowledge surface with domain 'hot') hasn't pinned any always-on memory."
        />
      ) : (
        <ul className="playbook-list memory-list">
          {chunks.map((c) => (
            <li key={c.id} className="playbook-card memory-row">
              {editingId === c.id ? (
                <div className="memory-edit-form">
                  <Textarea
                    rows={3}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    aria-label={`hot entry ${c.id} content`}
                  />
                  <div className="memory-edit-actions">
                    <Button
                      type="button"
                      variant="primary"
                      size="sm"
                      disabled={save.isPending || !draft.trim()}
                      onClick={() => save.mutate({ id: c.id, content: draft })}
                    >
                      Save
                    </Button>
                    <Button type="button" variant="ghost" size="sm" onClick={() => setEditingId(null)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="memory-row-main memory-row-static">
                    <span className="memory-row-title">
                      {c.heading ? <strong>{c.heading}</strong> : null}
                      {c.source ? (
                        <span title="provenance — the session/source that wrote this row">
                          <Badge status="neutral">{c.source}</Badge>
                        </span>
                      ) : null}
                    </span>
                    <span className="memory-row-topic">{c.content || c.preview}</span>
                    <span className="memory-row-meta">{ago(c.created_at)}</span>
                  </div>
                  <span className="knowledge-chunk-actions">
                    <Button
                      icon
                      variant="ghost"
                      type="button"
                      title="Edit this hot-memory entry"
                      aria-label={`edit hot entry ${c.id}`}
                      onClick={() => {
                        setEditingId(c.id);
                        setDraft(c.content || "");
                      }}
                    >
                      <Pencil size={14} />
                    </Button>
                    <Button
                      icon
                      variant="ghost"
                      type="button"
                      title="Delete this hot-memory entry (stops injecting immediately)"
                      aria-label={`delete hot entry ${c.id}`}
                      onClick={() => setPendingDelete(c)}
                    >
                      <Trash2 size={14} />
                    </Button>
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete this hot-memory entry?"
        confirmLabel="Delete entry"
        destructive
        onConfirm={() => {
          if (pendingDelete) del.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        onClose={() => setPendingDelete(null)}
      >
        {pendingDelete
          ? `"${(pendingDelete.heading || pendingDelete.content || pendingDelete.preview || "").slice(0, 80)}" stops injecting into every turn. This can't be undone.`
          : undefined}
      </ConfirmDialog>
    </>
  );
}

// ── Injections — which memory entered which turn (ADR 0069 D6) ───────────────

function InjectionsPanel({ filter, setFilter }: { filter: string; setFilter: (v: string) => void }) {
  const { data, isFetching, error, refetch } = useQuery({
    queryKey: [...queryKeys.memoryInjections, filter] as const,
    queryFn: () => api.memoryInjections(filter, 100),
  });
  const rows = data?.injections ?? [];

  const idList = (ids: Array<string | number>) =>
    ids.length ? <code>{ids.join(", ")}</code> : <span className="memory-none">—</span>;

  return (
    <>
      <div className="memory-panel-head">
        <p className="memory-panel-hint">
          The per-turn injection record — which digest sessions, hot chunks, and RAG chunks
          entered each model call. This is the "why did it say that?" / poisoning-forensics
          trail.
        </p>
        <RefreshButton onClick={() => void refetch()} busy={isFetching} />
      </div>
      <Input
        type="search"
        placeholder="Filter by session id (blank = all sessions)…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        aria-label="filter injections by session id"
      />
      {error ? <Alert status="error">Couldn't read the injection log — {errMsg(error)}</Alert> : null}
      {!error && rows.length === 0 ? (
        <Empty
          title="No injection records"
          description={filter ? "no recorded turns for that session id." : "records appear as turns run."}
        />
      ) : (
        <table className="memory-injections">
          <thead>
            <tr>
              <th>when</th>
              <th>session</th>
              <th>digest sessions</th>
              <th>hot chunks</th>
              <th>RAG chunks</th>
              <th>~tokens</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.ts}-${i}`}>
                <td title={r.ts}>{ago(r.ts)}</td>
                <td>
                  <code>{r.session_id}</code>
                </td>
                <td>{idList(r.digest_session_ids)}</td>
                <td>{idList(r.hot_chunk_ids)}</td>
                <td>{idList(r.rag_chunk_ids)}</td>
                <td>{r.approx_tokens}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
