import { Combobox, Input, Textarea } from "@protolabsai/ui/forms";
import { ConfirmDialog, Dialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, TextLink } from "@protolabsai/ui/primitives";
import { useMutation } from "@tanstack/react-query";
import { ArrowDownFromLine, ArrowUpToLine, ClipboardCheck, FileUp, Library, Pencil, Plus, Trash2 } from "lucide-react";

import { useEffect, useState } from "react";

import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { KNOWLEDGE_INGEST_ACCEPT } from "../lib/attachTypes";
import { ago, errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { CollectionsBrowser, highlight } from "./collections";
import { useCollections } from "./useCollections";
import { useUI } from "../state/uiStore";
import { QuickSetting } from "../settings/QuickSetting";
import type { KnowledgeChunk } from "../lib/types";

import { ReviewActions, ReviewChip } from "./ReviewVerdict";
import { takeKnowledgeSearchSeed, useKnowledgeSearchSeedVersion } from "./searchSeed";
type Draft = { heading: string; domain: string; content: string };
const EMPTY_DRAFT: Draft = { heading: "", domain: "general", content: "" };

function ChunkForm({
  draft,
  setDraft,
  onSave,
  onCancel,
  saving,
  saveLabel,
  collections,
}: {
  draft: Draft;
  setDraft: (d: Draft) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  saveLabel: string;
  collections: string[];
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
        <Combobox aria-label="Collection" placeholder="Collection" options={collections}
          value={draft.domain} onValueChange={domain => setDraft({ ...draft, domain })} />
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
// (typed facts): this is "bring a whole document in". The picker's accept list
// is KNOWLEDGE_INGEST_ACCEPT (lib/attachTypes — shared with the chat composer).

// The source a preview is holding, so Confirm can re-send the exact same thing.
type PendingSource = { kind: "file"; file: File } | { kind: "url"; url: string };
type IngestPreview = Awaited<ReturnType<typeof api.previewKnowledgeIngest>>;

function IngestForm({
  onDone,
  onError,
  onClose,
  collections,
  collection,
}: {
  onDone: () => void;
  onError: (message: string) => void;
  onClose: () => void;
  collections: string[];
  collection: string;
}) {
  const [url, setUrl] = useState("");
  const [domain, setDomain] = useState(collection || "general");
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
            <Combobox aria-label="Collection" placeholder="Collection" options={collections}
              value={domain} onValueChange={setDomain} disabled={busy} />
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
              accept={KNOWLEDGE_INGEST_ACCEPT}
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) previewFile(file);
                e.target.value = "";
              }}
            />
          </label>{" "}
          — txt, md, html, pdf, docx, audio &amp; video
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
  // Action failures (curate / share / ingest / review) self-report via toast; a read or
  // search failure renders as the contained <Alert> inside CollectionsBrowser. A blank
  // message is a clear-no-op.
  const model = useCollections();
  const names = model.collections.map(c => c.collection);
  const toast = useToast();
  const onError = (message: string) => {
    if (message) toast({ tone: "error", title: "Knowledge", message });
  };

  const seedVersion = useKnowledgeSearchSeedVersion();
  useEffect(() => {
    const seeded = takeKnowledgeSearchSeed();
    if (seeded !== null) { model.seedSearch(seeded); }
  }, [seedVersion]);
  const enabled = model.list.data?.enabled ?? true;
  const invalidate = () => void model.invalidate();

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
    // The chunk's OWN domain is the save value — the server writes this field back as the
    // row's domain. Use the row's own domain (never the browsed collection) so a content-only
    // edit of a legacy row (domain ≠ collection) can't silently relabel it to the open collection.
    setDraft({ heading: c.heading || "", domain: c.domain || c.collection || "general", content: c.content || "" });
  }

  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const renderCard = (c: KnowledgeChunk) => (
    <li key={`${c.tier}:${c.id}`} className="playbook-card">
      {editingId === c.id ? (
        <ChunkForm
          collections={names}
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
            {model.term && model.all && c.collection && <TextLink href={`?collection=${encodeURIComponent(c.collection)}`} onClick={e => { e.preventDefault(); model.open(c.collection); }}>{c.collection}</TextLink>}
            <div className="playbook-title">
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
              <ReviewChip chunk={c} />
              {c.heading ? <strong>{c.heading}</strong> : null}
            </div>
            <p className="playbook-desc" style={expanded.has(`${c.tier}:${c.id}`) ? { WebkitLineClamp: "unset" } : { WebkitLineClamp: 6 }}>{highlight(c.content || c.preview, model.term)}</p>
            <Button variant="ghost" size="sm" onClick={() => setExpanded(prev => {
              const next = new Set(prev); const key = `${c.tier}:${c.id}`;
              if (next.has(key)) next.delete(key); else next.add(key); return next;
            })}>{expanded.has(`${c.tier}:${c.id}`) ? "Show less" : "Show full text"}</Button>
            {c.source ? (
              <div className="playbook-tools">
                <code>{c.source}</code>
              </div>
            ) : null}
          </div>
          <div className="playbook-meta">
            <span title="added">{ago(c.created_at)}</span>
            <span className="knowledge-chunk-actions">
              {/* Review verdict (ADR 0108 D7) — nothing for commons rows (read-only here).
                  Agent-written rows arrive pending and still deliver, so confirm/reject
                  stays reachable on this surface until the Memory inspector owns it. */}
              <ReviewActions chunk={c} />
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
        kicker={`Collections this agent can read${model.pendingOnly ? " · pending review" : ""}`}
        actions={
          <>
            {/* Quick-set recall behaviour right where you inspect what the agent knows (ADR 0048). */}
            <QuickSetting keys={["knowledge.top_k", "knowledge.embeddings"]} title="Recall" label="Knowledge recall settings" />
            {enabled ? (
              <>
                {/* "Pending review" queue (ADR 0108 D7): narrow the browsed chunks + search to
                    rows awaiting an operator verdict. Agent-written rows keep delivering until
                    reviewed, so this stays on the Knowledge surface. */}
                <Button
                  icon
                  variant={model.pendingOnly ? "primary" : "ghost"}
                  type="button"
                  onClick={() => model.setPendingOnly((v) => !v)}
                  title={model.pendingOnly ? "Show every entry" : "Show only entries awaiting review"}
                  aria-label="pending review filter"
                  aria-pressed={model.pendingOnly}
                >
                  <ClipboardCheck size={16} />
                </Button>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  onClick={() => { setEditingId(null); setAdding(false); setIngesting((v) => !v); }}
                  title="Add a source — file (text/pdf/docx/audio/video), web URL, or YouTube link"
                >
                  <FileUp size={16} />
                </Button>
                <Button
                  icon
                  variant="ghost"
                  type="button"
                  onClick={() => { setEditingId(null); setDraft({ ...EMPTY_DRAFT, domain: model.collection || "general" }); setIngesting(false); setAdding((v) => !v); }}
                  title="Add a knowledge entry"
                >
                  <Plus size={16} />
                </Button>
              </>
            ) : null}
            <RefreshButton onClick={invalidate} busy={model.list.isFetching || model.details.isFetching} />
          </>
        }
      />

      <div className="stage-body">
        {/* Upload + Add open in a centered dialog (not inline) so the source/entry form has
            room and the knowledge list underneath stays in view (#1502). Per-row EDIT below
            stays inline — it belongs next to the chunk it edits. */}
        {ingesting ? (
          <Dialog
            open
            onClose={() => setIngesting(false)}
            title={<><FileUp size={16} /> Add a source</>}
            width="min(680px, 94vw)"
            padding="roomy"
          >
            <IngestForm
              collections={names}
              collection={model.collection}
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
            padding="roomy"
          >
            <ChunkForm
              collections={names}
              draft={draft}
              setDraft={setDraft}
              onSave={() => save.mutate()}
              onCancel={() => { setAdding(false); setDraft(EMPTY_DRAFT); }}
              saving={save.isPending}
              saveLabel="Add entry"
            />
          </Dialog>
        ) : null}

        <CollectionsBrowser model={model} renderCard={renderCard} onAdd={() => setIngesting(true)} onSettings={() => useUI.getState().openGlobalSettings("knowledge")} />
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
