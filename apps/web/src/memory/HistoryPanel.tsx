import { useEffect, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert } from "@protolabsai/ui/data";
import { Input } from "@protolabsai/ui/forms";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Trash2 } from "lucide-react";

import { RefreshButton } from "../app/ui-kit";
import { openDocument } from "../docviewer";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { queryKeys } from "../lib/queries";
import type { HistorySearchHit, HistorySessionRow } from "../lib/types";

// History view (ADR 0125 D6) — the conversation `history` table (compaction
// archives, retired-thread summaries, background-job reports), its own
// HistoryStore. Visible ONLY here; the Knowledge browser lists chunks only. A
// per-session rollup, a search that runs the same query recall uses, an
// open-an-entry reader, and deletes that reuse the chat-delete forget semantics
// (#3957): a single entry is removed outright (any kind); a whole session keeps
// background-job reports, exactly like the chat delete's forget.

function spanLabel(s: { span_start: string | null; span_end: string | null }): string {
  if (!s.span_start) return "";
  if (s.span_end && s.span_end !== s.span_start) return `${s.span_start} → ${s.span_end}`;
  return s.span_start;
}

export function HistoryPanel() {
  const qc = useQueryClient();
  const toast = useToast();

  // The search box drives the view: empty → the per-session rollup; a query →
  // history search (the same normalization session_search runs, server-side).
  // Local echo paints instantly; the debounced value is what refires the query.
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(input.trim()), 250);
    return () => window.clearTimeout(t);
  }, [input]);
  const searching = query.length > 0;

  const statsQ = useQuery({ queryKey: queryKeys.memoryStats, queryFn: () => api.memoryStats() });
  const sessionsQ = useQuery({
    queryKey: [...queryKeys.memoryHistory, "sessions"],
    queryFn: () => api.memoryHistorySessions(),
  });
  const searchQ = useQuery({
    queryKey: [...queryKeys.memoryHistory, "search", query],
    queryFn: () => api.memoryHistorySearch(query),
    enabled: searching,
    placeholderData: keepPreviousData,
  });

  const enabled = sessionsQ.data?.enabled ?? true;
  const sessions = sessionsQ.data?.sessions ?? [];
  const hits = searchQ.data?.results ?? [];
  const history = statsQ.data?.history;
  const memory = statsQ.data?.memory;

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: queryKeys.memoryHistory });
    void qc.invalidateQueries({ queryKey: queryKeys.memoryStats });
  };

  const [pendingSession, setPendingSession] = useState<HistorySessionRow | null>(null);
  const [pendingEntry, setPendingEntry] = useState<HistorySearchHit | null>(null);

  const delSession = useMutation({
    mutationFn: (sid: string) => api.deleteMemoryHistorySession(sid),
    onSuccess: (r, sid) => {
      const n = r.deleted;
      toast({
        tone: "success",
        title: "History",
        message: `Deleted ${n} ${n === 1 ? "entry" : "entries"} from ${sid} (background reports kept).`,
      });
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "History", message: errMsg(e) }),
  });

  const delEntry = useMutation({
    mutationFn: (id: number) => api.deleteMemoryHistoryEntry(id),
    onSuccess: () => {
      toast({ tone: "success", title: "History", message: "History entry deleted." });
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "History", message: errMsg(e) }),
  });

  // Open the FULL entry in the document reader — the raw archived/summarized text,
  // shown verbatim in a <pre> (never markdown: an archived transcript could
  // otherwise smuggle live HTML), like the session-summary reader above it.
  async function openEntry(id: number) {
    try {
      const r = await api.memoryHistoryEntry(id);
      if (!r.entry) {
        toast({ tone: "error", title: "History", message: "That entry is no longer stored." });
        invalidate();
        return;
      }
      const e = r.entry;
      openDocument({
        title: e.heading || `${e.kind} · ${e.session_id}`,
        subtitle: `${e.kind} · ${e.surface || "unknown"} · ${e.session_id}`,
        render: () => <pre className="memory-session-pre">{e.content || "(empty entry)"}</pre>,
      });
    } catch (err) {
      toast({ tone: "error", title: "History", message: errMsg(err) });
    }
  }

  const refetchAll = () => {
    void sessionsQ.refetch();
    void statsQ.refetch();
    if (searching) void searchQ.refetch();
  };
  const isFetching = sessionsQ.isFetching || searchQ.isFetching || statsQ.isFetching;
  const listError = searching ? searchQ.error : sessionsQ.error;

  return (
    <>
      <div className="memory-panel-head">
        <p className="memory-panel-hint">
          Conversation history — archives, retired-thread summaries and background-job
          reports. Separate from the Knowledge browser (that's chunks only).{" "}
          {history ? (
            <span data-testid="history-stats">
              {history.total} {history.total === 1 ? "entry" : "entries"} across{" "}
              {history.sessions} {history.sessions === 1 ? "session" : "sessions"} —{" "}
              {history.archive ?? 0} archives · {history.summary ?? 0} summaries ·{" "}
              {history.report ?? 0} reports
              {memory ? <> (memory store: {memory.total ?? 0} chunks)</> : null}.
            </span>
          ) : null}{" "}
          Search to open or delete an individual entry.
        </p>
        <RefreshButton onClick={refetchAll} busy={isFetching} />
      </div>
      <Input
        type="search"
        placeholder="Search history (same query the agent's recall uses)…"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        aria-label="search history"
      />
      {listError ? (
        <Alert status="error">Couldn't read history — {errMsg(listError)}</Alert>
      ) : null}
      {!enabled ? (
        <Empty>
          History is off (enable <code>middleware.knowledge</code>).
        </Empty>
      ) : searching ? (
        hits.length === 0 ? (
          <Empty title="No matching history" description="no archives, summaries or reports match that search." />
        ) : (
          <ul className="playbook-list memory-list">
            {hits.map((h) => (
              <li key={h.id} className="playbook-card memory-row">
                <button
                  type="button"
                  className="memory-row-main"
                  title="Open the full entry in the reader"
                  onClick={() => void openEntry(h.id)}
                >
                  <span className="memory-row-title">
                    <Badge status="neutral">{h.kind}</Badge>
                    <code>{h.session_id}</code>
                  </span>
                  <span className="memory-row-topic">{h.heading || h.excerpt}</span>
                  <span className="memory-row-meta">
                    {h.surface} · {ago(h.created_at)}
                  </span>
                </button>
                <span className="knowledge-chunk-actions">
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    title="Delete this history entry outright"
                    aria-label={`delete history entry ${h.id}`}
                    onClick={() => setPendingEntry(h)}
                  >
                    <Trash2 size={14} />
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        )
      ) : sessions.length === 0 ? (
        <Empty
          title="No history yet"
          description="compaction archives, retired-session summaries and job reports show up here."
        />
      ) : (
        <ul className="playbook-list memory-list">
          {sessions.map((s) => (
            <li key={s.session_id} className="playbook-card memory-row">
              <div className="memory-row-main memory-row-static">
                <span className="memory-row-title">
                  <Badge status="neutral">{s.surface || "unknown"}</Badge>
                  <code>{s.session_id}</code>
                </span>
                <span className="memory-row-topic">
                  {s.kinds.archive} archive · {s.kinds.summary} summary · {s.kinds.report} report
                </span>
                <span className="memory-row-meta">
                  {s.total} {s.total === 1 ? "entry" : "entries"}
                  {spanLabel(s) ? ` · ${spanLabel(s)}` : ""} · {ago(s.last_at)}
                </span>
              </div>
              <span className="knowledge-chunk-actions">
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  title="Delete this session's history (archives + summaries; reports kept)"
                  aria-label={`delete history session ${s.session_id}`}
                  onClick={() => setPendingSession(s)}
                >
                  <Trash2 size={14} />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <ConfirmDialog
        open={pendingEntry !== null}
        title="Delete this history entry?"
        confirmLabel="Delete entry"
        destructive
        onConfirm={() => {
          if (pendingEntry) delEntry.mutate(pendingEntry.id);
          setPendingEntry(null);
        }}
        onClose={() => setPendingEntry(null)}
      >
        {pendingEntry
          ? `This ${pendingEntry.kind} from "${pendingEntry.session_id}" is removed outright. This can't be undone.`
          : undefined}
      </ConfirmDialog>
      <ConfirmDialog
        open={pendingSession !== null}
        title="Delete this session's history?"
        confirmLabel="Delete history"
        destructive
        onConfirm={() => {
          if (pendingSession) delSession.mutate(pendingSession.session_id);
          setPendingSession(null);
        }}
        onClose={() => setPendingSession(null)}
      >
        {pendingSession
          ? `Removes "${pendingSession.session_id}"'s archives and summaries, the same way the chat delete's "forget" does — background-job reports are kept. This can't be undone.`
          : undefined}
      </ConfirmDialog>
    </>
  );
}
