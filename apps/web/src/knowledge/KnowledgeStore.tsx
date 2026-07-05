import { Input, Textarea } from "@protolabsai/ui/forms";
import { Alert } from "@protolabsai/ui/data";
import { ConfirmDialog, Dialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownFromLine, ArrowUpToLine, ChevronRight, ChevronsDownUp, ChevronsUpDown, Database, FileUp, Library, Pencil, Plus, Trash2 } from "lucide-react";

import { useEffect, useState } from "react";

import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { knowledgeQuery, queryKeys } from "../lib/queries";
import { QuickSetting } from "../settings/QuickSetting";
import type { KnowledgeChunk } from "../lib/types";

// The shape every knowledge list/search query caches — reused for optimistic
// bulk-delete cache surgery (#1770) without re-declaring the response fields.
type KnowledgeSearchData = Awaited<ReturnType<typeof api.knowledgeSearch>>;

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

// The source a preview is holding, so Confirm can re-send the exact same thing.
type PendingSource = { kind: "file"; file: File } | { kind: "url"; url: string };
type IngestPreview = Awaited<ReturnType<typeof api.previewKnowledgeIngest>>;

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
  // Preview gate (#1801): picking a source runs a no-persist dry-run; the result
  // is held here and shown in-place until the operator Confirms (ingest) or
  // Cancels. `pending` is the source Confirm re-sends; `title` is editable here.
  const [pending, setPending] = useState<PendingSource | null>(null);
  const [preview, setPreview] = useState<IngestPreview | null>(null);
  const [title, setTitle] = useState("");

  function formFor(src: PendingSource): FormData {
    const f = new FormData();
    if (src.kind === "file") f.append("file", src.file);
    else f.append("url", src.url);
    return f;
  }

  function resetPreview() {
    setPending(null);
    setPreview(null);
    setTitle("");
  }

  const previewMut = useMutation({
    mutationFn: (src: PendingSource) => api.previewKnowledgeIngest(formFor(src)),
    onSuccess: (r, src) => {
      setPending(src);
      setPreview(r);
      setTitle(r.title ?? "");
    },
    onError: (e) => onError(errMsg(e)),
  });

  const ingest = useMutation({
    // Confirm re-sends the source to /ingest (extraction reruns there, preserving
    // each source's real provenance — a PDF stays a PDF, not "pasted text").
    mutationFn: (src: PendingSource) => {
      const f = formFor(src);
      f.set("domain", domain.trim() || "general");
      if (title.trim()) f.set("title", title.trim());
      return api.ingestKnowledge(f);
    },
    onSuccess: (r) => {
      setNote(`Added ${r.chunks} chunk${r.chunks === 1 ? "" : "s"}${r.title ? ` from “${r.title}”` : ""}.`);
      setUrl("");
      resetPreview();
      onDone();
    },
    onError: (e) => onError(errMsg(e)),
  });

  const busy = previewMut.isPending || ingest.isPending;

  function previewFile(file: File) {
    setNote("");
    previewMut.mutate({ kind: "file", file });
  }
  function previewUrl() {
    if (!url.trim()) return;
    setNote("");
    previewMut.mutate({ kind: "url", url: url.trim() });
  }

  // Confirm step — the dry-run result held in-place; nothing is ingested until
  // the operator clicks Confirm (or Cancel goes back to source selection).
  if (pending && preview) {
    const srcLabel = pending.kind === "file" ? pending.file.name : pending.url;
    const sizeKb = pending.kind === "file" ? `${(pending.file.size / 1024).toFixed(1)} KB` : null;
    const chunkLabel = `${preview.chunks} chunk${preview.chunks === 1 ? "" : "s"}`;
    return (
      <div className="knowledge-chunk-form">
        <div className="knowledge-ingest-preview">
          <div className="knowledge-ingest-preview-meta">
            <Badge status="neutral">{preview.source_type}</Badge>
            <Badge status="success">{chunkLabel}</Badge>
            <Badge status="neutral">~{preview.approx_tokens.toLocaleString()} tokens</Badge>
            {sizeKb ? <Badge status="neutral">{sizeKb}</Badge> : null}
          </div>
          <div className="knowledge-ingest-preview-source" title={srcLabel}>
            {srcLabel}
          </div>
          <pre className="knowledge-ingest-preview-snippet" aria-label="content preview">
            {preview.snippet}
            {preview.truncated ? "\n…" : ""}
          </pre>
          <div className="knowledge-chunk-form-row">
            <Input
              type="text"
              placeholder="title (optional)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="ingest title"
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
            <Button
              type="button"
              variant="primary"
              size="sm"
              disabled={busy || preview.chunks === 0}
              onClick={() => ingest.mutate(pending)}
            >
              {ingest.isPending
                ? "Ingesting…"
                : preview.chunks === 0
                  ? "Nothing to ingest"
                  : `Confirm — ingest ${chunkLabel}`}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={resetPreview} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      </div>
    );
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
          if (file) previewFile(file);
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
                if (file) previewFile(file);
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
          onKeyDown={(e) => { if (e.key === "Enter") previewUrl(); }}
          aria-label="source url"
          disabled={busy}
        />
      </div>
      <div className="knowledge-chunk-form-row">
        <Button type="button" variant="primary" size="sm" disabled={busy || !url.trim()} onClick={previewUrl}>
          {previewMut.isPending ? "Reading…" : "Preview URL"}
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
  // Bulk delete-by-source (#1770): the group awaiting a confirm (source + count).
  const [pendingSourceDelete, setPendingSourceDelete] = useState<{ source: string; count: number } | null>(null);

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

  // Bulk delete-by-source (#1770) — remove a whole ingest at once. It's a reversible
  // SOFT delete, so restore just re-validates the same rows. The restore mutation is
  // declared first because the delete's success toast embeds an Undo that calls it.
  const restoreBySource = useMutation({
    mutationFn: (source: string) => api.restoreKnowledgeBySource(source),
    onSuccess: (r) => {
      if (r.error) { onError(r.error); return; }
      invalidate();
    },
    onError: (e) => onError(errMsg(e)),
  });

  const delBySource = useMutation({
    mutationFn: ({ source }: { source: string; count: number }) => api.deleteKnowledgeBySource(source),
    // Optimistically drop the group from every knowledge list cache so it vanishes
    // instantly; snapshot for rollback if the server rejects it.
    onMutate: async ({ source }) => {
      await qc.cancelQueries({ queryKey: queryKeys.knowledge });
      const prev = qc.getQueriesData<KnowledgeSearchData>({ queryKey: queryKeys.knowledge });
      qc.setQueriesData<KnowledgeSearchData>({ queryKey: queryKeys.knowledge }, (old) =>
        old ? { ...old, results: old.results.filter((c) => c.source !== source) } : old,
      );
      return { prev };
    },
    onSuccess: (r, { source, count }, ctx) => {
      if (r.error) {
        for (const [key, data] of ctx?.prev ?? []) qc.setQueryData(key, data); // rollback
        onError(r.error);
        return;
      }
      invalidate();
      const n = r.deleted || count;
      toast({
        tone: "success",
        title: "Knowledge",
        duration: 10000, // long enough to catch the Undo
        message: (
          <span>
            Deleted {n} chunk{n === 1 ? "" : "s"} from “{source}”.{" "}
            <Button variant="ghost" size="sm" onClick={() => restoreBySource.mutate(source)}>
              Undo
            </Button>
          </span>
        ),
      });
    },
    onError: (e, _vars, ctx) => {
      for (const [key, data] of ctx?.prev ?? []) qc.setQueryData(key, data); // rollback
      onError(errMsg(e));
    },
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

  // Shift+click a delete button → hard-delete with NO confirm dialog — the same
  // quick-delete the chat tabs use (#1582). While Shift is held the delete buttons
  // arm (turn red) to signal the fast path. Mirrors ChatSurface's shiftDel.
  const [shiftDel, setShiftDel] = useState(false);
  useEffect(() => {
    const sync = (e: KeyboardEvent) => setShiftDel(e.shiftKey);
    const clear = () => setShiftDel(false);
    window.addEventListener("keydown", sync);
    window.addEventListener("keyup", sync);
    window.addEventListener("blur", clear);
    return () => {
      window.removeEventListener("keydown", sync);
      window.removeEventListener("keyup", sync);
      window.removeEventListener("blur", clear);
    };
  }, []);

  function startEdit(c: KnowledgeChunk) {
    setAdding(false);
    setEditingId(c.id);
    setDraft({ heading: c.heading || "", domain: c.domain || "general", content: c.content || "" });
  }

  const total = stats.chunks ?? stats.total ?? 0;

  // Collapsible source grouping (#1575): chunks from the same ingested source
  // collapse under one section header. Only a source with ≥2 loaded chunks becomes
  // a section — sourceless + single-chunk sources stay flat (no regression). Open
  // state persists per source; an active search force-expands so matches stay visible.
  const GROUPS_LS_KEY = "protoagent.kb.openGroups";
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(() => {
    try { return JSON.parse(localStorage.getItem(GROUPS_LS_KEY) || "{}") as Record<string, boolean>; }
    catch { return {}; }
  });
  const persistGroups = (next: Record<string, boolean>) => {
    setOpenGroups(next);
    try { localStorage.setItem(GROUPS_LS_KEY, JSON.stringify(next)); } catch { /* private mode — ignore */ }
  };
  const searching = debouncedQuery.trim().length > 0;
  const isGroupOpen = (src: string) => searching || (openGroups[src] ?? false);
  const toggleGroup = (src: string) => persistGroups({ ...openGroups, [src]: !(openGroups[src] ?? false) });

  const sourceCounts = new Map<string, number>();
  for (const c of results) if (c.source) sourceCounts.set(c.source, (sourceCounts.get(c.source) ?? 0) + 1);
  const groupSources = [...sourceCounts.entries()].filter(([, n]) => n >= 2).map(([s]) => s);
  const isGrouped = (c: KnowledgeChunk) => !!c.source && (sourceCounts.get(c.source) ?? 0) >= 2;
  const allGroupsOpen = groupSources.length > 0 && groupSources.every((s) => openGroups[s]);
  const toggleAllGroups = () => {
    const next = { ...openGroups };
    for (const s of groupSources) next[s] = !allGroupsOpen;
    persistGroups(next);
  };

  const renderCard = (c: KnowledgeChunk) => (
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
                  loading={promotingId === c.id}
                  aria-label={`share entry ${c.id}`}
                >
                  <ArrowUpToLine size={14} />
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
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    className={shiftDel ? "knowledge-del-armed" : undefined}
                    title={shiftDel ? "Delete now — Shift skips the confirmation" : "Delete entry (Shift+click to skip confirm)"}
                    onClick={(e) => { if (e.shiftKey) del.mutate(c.id); else setPendingDelete(c); }}
                    aria-label={`delete entry ${c.id}`}
                  >
                    <Trash2 size={14} />
                  </Button>
                </>
              )}
            </span>
          </div>
        </>
      )}
    </li>
  );

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
                {groupSources.length > 0 ? (
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    onClick={toggleAllGroups}
                    title={allGroupsOpen ? "Collapse all sources" : "Expand all sources"}
                    aria-label={allGroupsOpen ? "collapse all sources" : "expand all sources"}
                  >
                    {allGroupsOpen ? <ChevronsDownUp size={16} /> : <ChevronsUpDown size={16} />}
                  </Button>
                ) : null}
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

        {/* Upload + Add open in a centered dialog (not inline) so the source/entry form has
            room and the knowledge list underneath stays in view (#1502). Per-row EDIT below
            stays inline — it belongs next to the chunk it edits. */}
        {ingesting ? (
          <Dialog
            open
            onClose={() => setIngesting(false)}
            title={<><FileUp size={16} /> Add a source</>}
            width="min(680px, 94vw)"
          >
            <IngestForm
              onDone={invalidate}
              onError={onError}
              onClose={() => setIngesting(false)}
            />
          </Dialog>
        ) : null}

        {adding ? (
          <Dialog
            open
            onClose={() => { setAdding(false); setDraft(EMPTY_DRAFT); }}
            title={<><Plus size={16} /> Add a knowledge entry</>}
            width="min(680px, 94vw)"
          >
            <ChunkForm
              draft={draft}
              setDraft={setDraft}
              onSave={() => save.mutate()}
              onCancel={() => { setAdding(false); setDraft(EMPTY_DRAFT); }}
              saving={save.isPending}
              saveLabel="Add entry"
            />
          </Dialog>
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
            {(() => {
              // Emit a source's section once (at its first chunk) so grouped sources
              // appear in first-seen order, interleaved with loose (flat) chunks.
              const emitted = new Set<string>();
              return results.map((c) => {
                if (!isGrouped(c)) return renderCard(c);
                const src = c.source as string;
                if (emitted.has(src)) return null;
                emitted.add(src);
                const members = results.filter((x) => x.source === src);
                const open = isGroupOpen(src);
                const st = members.find((m) => m.source_type)?.source_type;
                return (
                  <li key={`grp:${src}`} className="kb-group">
                    {/* Toggle + a bulk-delete button are SIBLINGS (a button can't nest
                        inside the header button). Bulk delete a whole ingest at once
                        (#1770): Shift+click skips the confirm, like the per-chunk path. */}
                    <div className="kb-group-header-row">
                      <button
                        type="button"
                        className="kb-group-header"
                        aria-expanded={open}
                        onClick={() => toggleGroup(src)}
                      >
                        <ChevronRight
                          size={14}
                          className="kb-group-caret"
                          style={{ transform: open ? "rotate(90deg)" : undefined }}
                        />
                        <span className="kb-group-title" title={src}>{src}</span>
                        {st ? <Badge status="neutral">{st}</Badge> : null}
                        <Badge status="neutral">{members.length} chunks</Badge>
                      </button>
                      <Button
                        icon
                        variant="ghost"
                        type="button"
                        className={`kb-group-delete${shiftDel ? " knowledge-del-armed" : ""}`}
                        title={
                          shiftDel
                            ? `Delete all ${members.length} chunks now — Shift skips the confirmation`
                            : `Delete all ${members.length} chunks from this source`
                        }
                        aria-label={`delete all chunks from ${src}`}
                        onClick={(e) => {
                          if (e.shiftKey) delBySource.mutate({ source: src, count: members.length });
                          else setPendingSourceDelete({ source: src, count: members.length });
                        }}
                      >
                        <Trash2 size={14} />
                      </Button>
                    </div>
                    {open ? (
                      <ul className="playbook-list kb-group-body">{members.map(renderCard)}</ul>
                    ) : null}
                  </li>
                );
              });
            })()}
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
        open={pendingSourceDelete !== null}
        title="Delete all chunks from this source?"
        confirmLabel="Delete chunks"
        destructive
        onConfirm={() => {
          if (pendingSourceDelete) delBySource.mutate(pendingSourceDelete);
          setPendingSourceDelete(null);
        }}
        onClose={() => setPendingSourceDelete(null)}
      >
        {pendingSourceDelete
          ? `Delete all ${pendingSourceDelete.count} chunk${pendingSourceDelete.count === 1 ? "" : "s"} from “${pendingSourceDelete.source}”? The agent will stop recalling them. You can undo this from the toast.`
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
