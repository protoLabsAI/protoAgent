import { Input, Textarea } from "@protolabsai/ui/forms";
import { Alert } from "@protolabsai/ui/data";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownFromLine, ArrowUpToLine, Database, FileUp, Library, Pencil, Plus, Trash2 } from "lucide-react";

import { useEffect, useState } from "react";

import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { knowledgeQuery, queryKeys } from "../lib/queries";
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
//
// Search is read with `useQuery` (not `useSuspenseQuery`) + keepPreviousData
// (ADR 0013): it's a search-as-you-type surface, so suspending on each new term
// would blank the list every keystroke. A read failure is a contained <Alert>;
// each write is a `useMutation` that toasts on failure and invalidates the list.

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

// Document ingestion (ADR 0021) — extract a file / web URL / YouTube link into
// the KB, chunked + enriched + embedded server-side. Distinct from ChunkForm
// (typed facts): this is "bring a whole document in".
const INGEST_ACCEPT =
  ".txt,.text,.log,.csv,.md,.markdown,.html,.htm,.pdf," +
  ".mp3,.wav,.m4a,.flac,.ogg,.opus,.aac," +
  ".mp4,.mov,.mkv,.webm,.avi,.m4v";

function IngestForm({
  onDone,
  onError,
  onClose,
}: {
  onDone: () => void;
  onError: (message: string) => void;
  onClose: () => void;
}) {
  const [url, setUrl] = useState("");
  const [domain, setDomain] = useState("general");
  const [drag, setDrag] = useState(false);
  const [note, setNote] = useState("");

  const ingest = useMutation({
    mutationFn: (form: FormData) => {
      form.set("domain", domain.trim() || "general");
      return api.ingestKnowledge(form);
    },
    onSuccess: (r) => {
      setNote(`Added ${r.chunks} chunk${r.chunks === 1 ? "" : "s"}${r.title ? ` from “${r.title}”` : ""}.`);
      setUrl("");
      onDone();
    },
    onError: (e) => onError(errMsg(e)),
  });
  const busy = ingest.isPending;

  function ingestFile(file: File) {
    const f = new FormData();
    f.append("file", file);
    setNote("");
    ingest.mutate(f);
  }

  function ingestUrl() {
    if (!url.trim()) return;
    const f = new FormData();
    f.append("url", url.trim());
    setNote("");
    ingest.mutate(f);
  }

  return (
    <div className="knowledge-chunk-form">
      <div
        className={`knowledge-ingest-drop${drag ? " is-drag" : ""}${busy ? " is-busy" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          const file = e.dataTransfer.files?.[0];
          if (file) ingestFile(file);
        }}
      >
        <FileUp size={18} />
        <span>
          Drop a file here, or{" "}
          <label className="knowledge-ingest-pick">
            browse
            <input
              type="file"
              hidden
              accept={INGEST_ACCEPT}
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) ingestFile(file);
                e.target.value = "";
              }}
            />
          </label>{" "}
          — txt, md, html, pdf, audio &amp; video
        </span>
      </div>
      <div className="knowledge-chunk-form-row">
        <Input
          type="url"
          placeholder="…or paste a web / YouTube URL"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ingestUrl(); }}
          aria-label="source url"
          disabled={busy}
        />
        <Input
          type="text"
          placeholder="domain"
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          aria-label="ingest domain"
          style={{ maxWidth: 160 }}
          disabled={busy}
        />
      </div>
      <div className="knowledge-chunk-form-row">
        <Button type="button" variant="primary" size="sm" disabled={busy || !url.trim()} onClick={ingestUrl}>
          {busy ? "Importing…" : "Import URL"}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onClose} disabled={busy}>
          Close
        </Button>
        {note ? <span className="knowledge-ingest-note">{note}</span> : null}
      </div>
    </div>
  );
}

export function KnowledgeStore() {
  // Action failures (curate / share / ingest) self-report via toast; a read/search
  // failure is the contained <Alert> below. A blank message is a clear-no-op.
  const qc = useQueryClient();
  const toast = useToast();
  const onError = (message: string) => {
    if (message) toast({ tone: "error", title: "Knowledge", message });
  };

  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  // Debounce the search term — TanStack owns the fetch; this only delays the key
  // change so a fast typist doesn't fire an FTS request per keystroke.
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(query), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  const { data, isFetching, error, refetch } = useQuery({
    ...knowledgeQuery(debouncedQuery),
    placeholderData: keepPreviousData,
  });
  const enabled = data?.enabled ?? true;
  const results = data?.results ?? [];
  const stats = data?.stats ?? {};
  const invalidate = () => void qc.invalidateQueries({ queryKey: queryKeys.knowledge });

  const [adding, setAdding] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [pendingDelete, setPendingDelete] = useState<KnowledgeChunk | null>(null);
  const [forgetPending, setForgetPending] = useState<KnowledgeChunk | null>(null);

  const save = useMutation({
    mutationFn: () => (editingId !== null ? api.updateKnowledgeChunk(editingId, draft) : api.addKnowledgeChunk(draft)),
    onSuccess: () => {
      setAdding(false);
      setEditingId(null);
      setDraft(EMPTY_DRAFT);
      invalidate();
    },
    onError: (e) => onError(errMsg(e)),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteKnowledgeChunk(id),
    onSuccess: () => invalidate(),
    onError: (e) => onError(errMsg(e)),
  });

  // Share a private chunk into the shared commons (ADR 0041 / bd-2wu).
  const promote = useMutation({
    mutationFn: (c: KnowledgeChunk) => api.promoteKnowledgeChunk(c.id),
    onSuccess: (r) => {
      if (!r.promoted) {
        onError(r.error || "promote failed");
        return;
      }
      invalidate(); // the chunk now also reads from the commons tier
    },
    onError: (e) => onError(errMsg(e)),
  });
  const promotingId = promote.isPending ? promote.variables?.id : undefined;

  // Unshare = forget from the commons (the inverse of promote). Confirmed — it affects
  // every agent on the box. The private copy (if any) is untouched.
  const unshare = useMutation({
    mutationFn: (c: KnowledgeChunk) => api.forgetKnowledgeChunk(c.id),
    onSuccess: (r) => {
      if (!r.forgotten) {
        onError(r.error || "unshare failed");
        return;
      }
      invalidate();
    },
    onError: (e) => onError(errMsg(e)),
  });

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
        kicker={`searchable knowledge base${total ? ` · ${total} entr${total === 1 ? "y" : "ies"}` : ""}${stats.commons ? ` · ${stats.commons} shared` : ""}`}
        actions={
          <>
            {/* Quick-set recall behaviour right where you inspect what the agent knows (ADR 0048). */}
            <QuickSetting keys={["knowledge.top_k", "knowledge.embeddings"]} title="Recall" label="Knowledge recall settings" />
            {enabled ? (
              <>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  onClick={() => { setEditingId(null); setAdding(false); setIngesting((v) => !v); }}
                  title="Add a source — file (text/pdf/audio/video), web URL, or YouTube link"
                >
                  <FileUp size={16} />
                </Button>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  onClick={() => { setEditingId(null); setDraft(EMPTY_DRAFT); setIngesting(false); setAdding((v) => !v); }}
                  title="Add a knowledge entry"
                >
                  <Plus size={16} />
                </Button>
              </>
            ) : null}
            <RefreshButton onClick={() => void refetch()} busy={isFetching} />
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

        {error ? (
          <Alert status="error">Couldn't search the knowledge base — {errMsg(error)}</Alert>
        ) : null}

        {ingesting ? (
          <IngestForm
            onDone={invalidate}
            onError={onError}
            onClose={() => setIngesting(false)}
          />
        ) : null}

        {adding ? (
          <ChunkForm
            draft={draft}
            setDraft={setDraft}
            onSave={() => save.mutate()}
            onCancel={() => { setAdding(false); setDraft(EMPTY_DRAFT); }}
            saving={save.isPending}
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
                    onSave={() => save.mutate()}
                    onCancel={() => { setEditingId(null); setDraft(EMPTY_DRAFT); }}
                    saving={save.isPending}
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
                        {c.tier === "commons" ? (
                          <span title="Shared commons — readable by every agent on this box">
                            <Badge status="neutral">
                              <Library size={12} /> commons
                            </Badge>
                          </span>
                        ) : c.tier === "private" ? (
                          <span title="Private to this agent — share to make it readable by the fleet">
                            <Badge status="neutral">private</Badge>
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
                        {c.tier === "private" ? (
                          <Button
                            icon
                            variant="ghost"
                            type="button"
                            title="Share to the commons (every agent on this box can then recall it)"
                            onClick={() => promote.mutate(c)}
                            disabled={promotingId === c.id}
                            aria-label={`share entry ${c.id}`}
                          >
                            <ArrowUpToLine size={14} className={promotingId === c.id ? "spin" : ""} />
                          </Button>
                        ) : null}
                        {c.tier === "commons" ? (
                          // Commons chunks are read-only here (edit/delete target the PRIVATE
                          // tier) — manage them with Unshare, the inverse of Share.
                          <Button
                            icon
                            variant="ghost"
                            type="button"
                            title="Unshare — remove from the commons (no other agent will recall it)"
                            onClick={() => setForgetPending(c)}
                            aria-label={`unshare entry ${c.id}`}
                          >
                            <ArrowDownFromLine size={14} />
                          </Button>
                        ) : (
                          <>
                            <Button icon variant="ghost" type="button" title="Edit entry" onClick={() => startEdit(c)} aria-label={`edit entry ${c.id}`}>
                              <Pencil size={14} />
                            </Button>
                            <Button icon variant="ghost" type="button" title="Delete entry" onClick={() => setPendingDelete(c)} aria-label={`delete entry ${c.id}`}>
                              <Trash2 size={14} />
                            </Button>
                          </>
                        )}
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
          if (pendingDelete) del.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        onClose={() => setPendingDelete(null)}
      >
        {pendingDelete
          ? `"${pendingDelete.heading || pendingDelete.preview.slice(0, 80)}" will be removed from the knowledge base — the agent will no longer recall it. This can't be undone.`
          : undefined}
      </ConfirmDialog>

      <ConfirmDialog
        open={forgetPending !== null}
        title="Unshare from the commons?"
        confirmLabel="Unshare"
        destructive
        onConfirm={() => {
          if (forgetPending) unshare.mutate(forgetPending);
          setForgetPending(null);
        }}
        onClose={() => setForgetPending(null)}
      >
        {forgetPending
          ? `"${forgetPending.heading || forgetPending.preview.slice(0, 80)}" will be removed from the shared commons — no other agent on this box will recall it. A private copy (if any) is untouched.`
          : undefined}
      </ConfirmDialog>
    </section>
  );
}
