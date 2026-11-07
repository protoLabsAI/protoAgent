import { useEffect, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert } from "@protolabsai/ui/data";
import { Input, Select, Textarea } from "@protolabsai/ui/forms";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Check, Pencil, RotateCcw, Trash2, X } from "lucide-react";

import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { queryKeys } from "../lib/queries";
import type { MemoryKind, MemoryRecord } from "../lib/types";
import { ReviewChip } from "../knowledge/ReviewVerdict";

// Review pass (ADR 0125 P3 D6/D7) — the operator's one-time curation of the core
// MemoryStore after the P2 migration carried the old hot/preference chunks into it.
// Nothing promotes automatically: each migrated/agent-written row is kept, edited,
// forgotten, rejected, or (if an automatic subject-consolidation retired it) reverted
// here. A separate section proposes the shared-profile candidates for explicit promotion.
// Backed by the P3-D1 routes under /api/memory/memories (+ /profile-candidates).

const PAGE_SIZE = 50;

// The editable kinds (mirrors the memories.kind CHECK and the PATCH route's allowlist).
const KINDS: MemoryKind[] = ["profile", "standing", "fact", "decision"];

// Filter chips — an empty value means "no filter" (all rows). review_state defaults to
// `pending` because the review pass opens on the queue that still needs a verdict.
const KIND_CHIPS = [{ value: "", label: "All kinds" }, ...KINDS.map((k) => ({ value: k, label: k }))];
const STATE_CHIPS = [
  { value: "", label: "All states" },
  { value: "pending", label: "pending" },
  { value: "confirmed", label: "confirmed" },
  { value: "rejected", label: "rejected" },
];
const PROVENANCE_CHIPS = [
  { value: "", label: "All sources" },
  { value: "agent", label: "agent" },
  { value: "extracted", label: "extracted" },
  { value: "operator", label: "operator" },
];

type Filters = {
  kind: string;
  reviewState: string;
  provenance: string;
  includeSuperseded: boolean;
};

function ChipGroup({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="memory-filter-group" role="group" aria-label={label}>
      <span className="memory-filter-label">{label}</span>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value || "all"}
            type="button"
            className={`memory-chip${on ? " memory-chip--on" : ""}`}
            aria-pressed={on}
            aria-label={`${label}: ${o.label}`}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function ReviewPassPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const [filters, setFilters] = useState<Filters>({
    kind: "",
    reviewState: "pending",
    provenance: "",
    includeSuperseded: false,
  });
  const [page, setPage] = useState(0);
  const [pendingForget, setPendingForget] = useState<MemoryRecord | null>(null);
  const [editing, setEditing] = useState<EditDraft | null>(null);

  // One change-a-filter helper: any filter edit resets to the first page (the old
  // offset is meaningless against the new, smaller result set).
  const setFilter = <K extends keyof Filters>(key: K, value: Filters[K]) => {
    setFilters((f) => ({ ...f, [key]: value }));
    setPage(0);
  };

  const listKey = [...queryKeys.memoryReview, { ...filters, page }] as const;
  const { data, isFetching, isPlaceholderData, error, refetch } = useQuery({
    queryKey: listKey,
    queryFn: () =>
      api.memoryMemories({
        kind: filters.kind,
        reviewState: filters.reviewState,
        provenance: filters.provenance,
        includeSuperseded: filters.includeSuperseded,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  });

  const enabled = data?.enabled ?? true;
  const rows = data?.memories ?? [];
  const total = data?.total ?? 0;

  const lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);
  const pageOutOfRange = page > lastPage;
  // Clamp from this page's response, never the previous page's placeholder.
  // This also recovers when another operator removes rows before a refresh.
  useEffect(() => {
    if (data && !isPlaceholderData && !isFetching && !error && pageOutOfRange) {
      setPage(lastPage);
    }
  }, [data, isPlaceholderData, isFetching, error, pageOutOfRange, lastPage]);

  // Every write refreshes the whole memory subtree (the review list and candidates both
  // live under it) so a kept/edited/reverted row and a freshly promotable subject re-read.
  const invalidate = () => void qc.invalidateQueries({ queryKey: queryKeys.memory });

  const verdict = useMutation({
    mutationFn: ({ id, state }: { id: number; state: "confirmed" | "rejected" }) => api.memoryReview(id, state),
    onSuccess: (_r, { state }) => {
      toast({ tone: "success", title: "Memory", message: state === "confirmed" ? "Kept." : "Rejected." });
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  const save = useMutation({
    mutationFn: (draft: EditDraft) =>
      api.editMemory(draft.id, {
        content: draft.content,
        heading: draft.heading,
        subject: draft.subject,
        kind: draft.kind,
      }),
    onSuccess: () => {
      toast({ tone: "success", title: "Memory", message: "Memory updated." });
      setEditing(null);
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  const forget = useMutation({
    mutationFn: (memory: MemoryRecord) =>
      memory.delivery === "always"
        ? api.deleteMemoryHot(`memory:${memory.id}`)
        : api.deleteMemory(memory.id),
    onSuccess: () => {
      toast({ tone: "success", title: "Memory", message: "Memory forgotten." });
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  const revert = useMutation({
    mutationFn: (id: number) => api.revertMemory(id),
    onSuccess: () => {
      toast({ tone: "success", title: "Memory", message: "Supersession reverted." });
      invalidate();
    },
    onError: (e) => toast({ tone: "error", title: "Memory", message: errMsg(e) }),
  });

  const start = page * PAGE_SIZE;
  const hasPrev = page > 0;
  const hasNext = start + rows.length < total;

  return (
    <>
      <div className="memory-panel-head">
        <p className="memory-panel-hint">
          A one-time review of the agent's migrated memories. Nothing is promoted automatically —
          keep, edit, forget or reject each row, revert a wrong consolidation, and confirm
          shared-profile candidates below.
        </p>
        <RefreshButton onClick={() => void refetch()} busy={isFetching} />
      </div>

      <div className="memory-filters">
        <ChipGroup label="Kind" options={KIND_CHIPS} value={filters.kind} onChange={(v) => setFilter("kind", v)} />
        <ChipGroup
          label="State"
          options={STATE_CHIPS}
          value={filters.reviewState}
          onChange={(v) => setFilter("reviewState", v)}
        />
        <ChipGroup
          label="Source"
          options={PROVENANCE_CHIPS}
          value={filters.provenance}
          onChange={(v) => setFilter("provenance", v)}
        />
        <button
          type="button"
          className={`memory-chip${filters.includeSuperseded ? " memory-chip--on" : ""}`}
          aria-pressed={filters.includeSuperseded}
          aria-label="include superseded"
          onClick={() => setFilter("includeSuperseded", !filters.includeSuperseded)}
        >
          Superseded
        </button>
      </div>

      {error ? <Alert status="error">Couldn't list memories — {errMsg(error)}</Alert> : null}
      {!enabled ? (
        <Empty>
          The memory store is off (enable <code>middleware.knowledge</code>).
        </Empty>
      ) : !error && !isFetching && !pageOutOfRange && rows.length === 0 ? (
        <Empty
          title="Nothing to review"
          description="no memories match these filters — migrated rows that still need a verdict show here by default."
        />
      ) : (
        <ul className="playbook-list memory-list">
          {rows.map((m) =>
            editing && editing.id === m.id ? (
              <li key={m.id} className="playbook-card memory-row">
                <EditForm
                  draft={editing}
                  busy={save.isPending}
                  onChange={setEditing}
                  onSave={() => save.mutate(editing)}
                  onCancel={() => setEditing(null)}
                />
              </li>
            ) : (
              <li key={m.id} className="playbook-card memory-row">
                <div className="memory-row-main memory-row-static">
                  <span className="memory-row-title">
                    <Badge status="neutral">{m.kind}</Badge>
                    {m.subject ? <code>{m.subject}</code> : null}
                    {m.legacy_domain ? (
                      <span title="the knowledge domain this row carried before the P2 migration">
                        <Badge status="neutral">from {m.legacy_domain}</Badge>
                      </span>
                    ) : null}
                    <Badge status="neutral">{m.provenance}</Badge>
                    {m.superseded_by != null ? (
                      <span title="retired by an automatic subject consolidation — Revert to make it live again">
                        <Badge status="warning">superseded</Badge>
                      </span>
                    ) : null}
                    <ReviewChip chunk={m} />
                  </span>
                  <span className="memory-row-topic">{m.heading ? <strong>{m.heading} — </strong> : null}{m.content}</span>
                  <span className="memory-row-meta">
                    {ago(m.created_at)}
                    {m.source_session ? <> · {m.source_session}</> : null}
                  </span>
                </div>
                <span className="knowledge-chunk-actions">
                  {m.superseded_by != null ? (
                    <Button
                      icon
                      variant="ghost"
                      type="button"
                      title="Revert this supersession — make the row live again"
                      aria-label={`revert memory ${m.id}`}
                      loading={revert.isPending && revert.variables === m.id}
                      onClick={() => revert.mutate(m.id)}
                    >
                      <RotateCcw size={14} />
                    </Button>
                  ) : null}
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    title="Keep — confirm this memory as trusted context"
                    aria-label={`keep memory ${m.id}`}
                    onClick={() => verdict.mutate({ id: m.id, state: "confirmed" })}
                  >
                    <Check size={14} />
                  </Button>
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    title="Reject — keep for audit but stop delivering it"
                    aria-label={`reject memory ${m.id}`}
                    onClick={() => verdict.mutate({ id: m.id, state: "rejected" })}
                  >
                    <X size={14} />
                  </Button>
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    title="Edit this memory's content, subject or kind"
                    aria-label={`edit memory ${m.id}`}
                    onClick={() =>
                      setEditing({
                        id: m.id,
                        content: m.content,
                        heading: m.heading ?? "",
                        subject: m.subject ?? "",
                        kind: (KINDS as string[]).includes(m.kind) ? (m.kind as MemoryKind) : "fact",
                      })
                    }
                  >
                    <Pencil size={14} />
                  </Button>
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    title="Forget this memory outright"
                    aria-label={`forget memory ${m.id}`}
                    onClick={() => setPendingForget(m)}
                  >
                    <Trash2 size={14} />
                  </Button>
                </span>
              </li>
            ),
          )}
        </ul>
      )}

      {enabled && (total > PAGE_SIZE || hasPrev) ? (
        <div className="memory-paging">
          <span className="memory-row-meta">
            {isFetching || pageOutOfRange ? "Loading…" : `${start + 1}–${start + rows.length} of ${total}`}
          </span>
          <Button type="button" variant="ghost" size="sm" disabled={!hasPrev} onClick={() => setPage((p) => p - 1)}>
            Previous
          </Button>
          <Button type="button" variant="ghost" size="sm" disabled={isFetching || pageOutOfRange || !hasNext} onClick={() => setPage((p) => p + 1)}>
            Next
          </Button>
        </div>
      ) : null}

      <ProfileCandidates />

      <ConfirmDialog
        open={pendingForget !== null}
        title="Forget this memory?"
        confirmLabel="Forget"
        destructive
        onConfirm={() => {
          if (pendingForget) forget.mutate(pendingForget);
          setPendingForget(null);
        }}
        onClose={() => setPendingForget(null)}
      >
        {pendingForget
          ? `"${(pendingForget.heading || pendingForget.content || "").slice(0, 80)}" is removed from memory. This can't be undone.`
          : undefined}
      </ConfirmDialog>
    </>
  );
}

type EditDraft = { id: number; content: string; heading: string; subject: string; kind: MemoryKind };

function EditForm({
  draft,
  busy,
  onChange,
  onSave,
  onCancel,
}: {
  draft: EditDraft;
  busy: boolean;
  onChange: (d: EditDraft) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="memory-edit-form">
      <Textarea
        rows={3}
        value={draft.content}
        onChange={(e) => onChange({ ...draft, content: e.target.value })}
        aria-label={`memory ${draft.id} content`}
      />
      <Input
        value={draft.subject}
        placeholder="subject (the identity key, e.g. time zone)"
        onChange={(e) => onChange({ ...draft, subject: e.target.value })}
        aria-label={`memory ${draft.id} subject`}
      />
      <Select
        value={draft.kind}
        onChange={(e) => onChange({ ...draft, kind: e.target.value as MemoryKind })}
        aria-label={`memory ${draft.id} kind`}
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </Select>
      <div className="memory-edit-actions">
        <Button
          type="button"
          variant="primary"
          size="sm"
          disabled={busy || !draft.content.trim()}
          onClick={onSave}
        >
          Save
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ── Shared-profile candidates ────────────────────────────────────────────────
// The newest profile memory per subject (ADR 0125 P2-G). Hidden entirely when the
// shared profile is off (enabled:false). Promote copies one in as a confirmed operator
// row; a subject already shared shows a disabled "Shared", and a 409 (a race / already
// shared) refreshes the list so the button flips without a manual reload.

function ProfileCandidates() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, error } = useQuery({
    queryKey: queryKeys.memoryProfileCandidates,
    queryFn: () => api.memoryProfileCandidates(),
  });

  const promote = useMutation({
    mutationFn: (id: number) => api.promoteMemory(id),
    onSuccess: () => {
      toast({ tone: "success", title: "Memory", message: "Promoted to the shared profile." });
      void qc.invalidateQueries({ queryKey: queryKeys.memoryProfileCandidates });
    },
    onError: (e) => {
      // A 409 means the subject is already shared (a concurrent promotion, or the row
      // flipped since this list loaded) — refresh so the button becomes a disabled "Shared".
      if ((e as { status?: number }).status === 409) {
        void qc.invalidateQueries({ queryKey: queryKeys.memoryProfileCandidates });
      }
      toast({ tone: "error", title: "Memory", message: errMsg(e) });
    },
  });

  // enabled:false ⇒ the shared profile is off — the whole section disappears.
  if (data && data.enabled === false) return null;
  const candidates = data?.candidates ?? [];

  return (
    <section className="memory-review-sub" data-testid="profile-candidates">
      <h4 className="memory-review-sub-title">Shared profile candidates</h4>
      <p className="memory-panel-hint">
        Confirmed profile facts you promote become part of the shared operator profile that
        opted-in agents receive. One per subject.
      </p>
      {error ? <Alert status="error">Couldn't list candidates — {errMsg(error)}</Alert> : null}
      {!error && candidates.length === 0 ? (
        <Empty title="No candidates" description="profile memories with a subject show here once the agent has learned them." />
      ) : (
        <ul className="playbook-list memory-list">
          {candidates.map((c) => (
            <li key={c.id} className="playbook-card memory-row">
              <div className="memory-row-main memory-row-static">
                <span className="memory-row-title">
                  <Badge status="neutral">{c.kind}</Badge>
                  {c.subject ? <code>{c.subject}</code> : null}
                </span>
                <span className="memory-row-topic">{c.content}</span>
              </div>
              <span className="knowledge-chunk-actions">
                {c.already_shared ? (
                  <Button type="button" variant="ghost" size="sm" disabled aria-label={`shared ${c.id}`}>
                    Shared
                  </Button>
                ) : (
                  <Button
                    type="button"
                    variant="primary"
                    size="sm"
                    loading={promote.isPending && promote.variables === c.id}
                    aria-label={`promote ${c.id}`}
                    onClick={() => promote.mutate(c.id)}
                  >
                    Promote
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
