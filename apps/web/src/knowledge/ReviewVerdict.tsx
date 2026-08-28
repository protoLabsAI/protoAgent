import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, RotateCcw, X } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys } from "../lib/queries";
import type { KnowledgeChunk, ReviewState } from "../lib/types";

// Operator review verdicts (ADR 0108 D7) — the console half of the memory write
// lifecycle. Agent-derived memories land as `pending`; the operator confirms or
// rejects them here, on the Knowledge → Store rows and the Memory → Hot memory rows.
// A rejected row stays in the store for audit but leaves delivery (D6); re-open puts
// it back to pending. Backend: POST /api/memory/chunks/{id}/review.
//
// Two rules the route relies on the CLIENT for (memory_routes.py):
//   - a commons row is read-only here (its id would collide with a private row's),
//     so commons rows get the chip and NO actions, and every request carries the
//     row's `tier` so the server can refuse a commons id it wasn't told about;
//   - an ADR 0031 backend without verdicts answers 501 once — after that the actions
//     hide everywhere (module-level, so sibling rows learn it on their next render).

const REVIEW_STATES: readonly ReviewState[] = ["confirmed", "pending", "rejected"];

/** The row's verdict as the typed enum, or null when the backend sent nothing / an
 *  unknown value (pre-D7 rows, custom backends) — the chip is drawn only for a value. */
export function reviewStateOf(chunk: Pick<KnowledgeChunk, "review_state">): ReviewState | null {
  const raw = chunk.review_state;
  if (typeof raw !== "string") return null;
  const v = raw.trim().toLowerCase();
  return (REVIEW_STATES as readonly string[]).includes(v) ? (v as ReviewState) : null;
}

let reviewUnsupported = false;

/** Test seam: forget a 501 learned by an earlier render. */
export function resetReviewSupport(): void {
  reviewUnsupported = false;
}

const CHIP: Record<ReviewState, { status: "info" | "success" | "warning"; label: string; title: string }> = {
  pending: {
    status: "info",
    label: "pending review",
    title: "Written by the agent and not yet reviewed — it still delivers; confirm to trust it, reject to stop delivering it.",
  },
  confirmed: {
    status: "success",
    label: "confirmed",
    title: "Confirmed by an operator — trusted context.",
  },
  rejected: {
    status: "warning",
    label: "rejected",
    title: "Rejected — kept for audit, no longer delivered to the agent. Re-open to review it again.",
  },
};

export function ReviewChip({ chunk }: { chunk: Pick<KnowledgeChunk, "review_state"> }) {
  const state = reviewStateOf(chunk);
  if (!state) return null;
  const chip = CHIP[state];
  return (
    <span title={chip.title} data-review-state={state}>
      <Badge status={chip.status}>{chip.label}</Badge>
    </span>
  );
}

type ReviewVars = { chunk: KnowledgeChunk; state: ReviewState };
type KnowledgeListData = { results?: KnowledgeChunk[] };
type HotListData = { chunks?: KnowledgeChunk[] };

function restamp<T extends KnowledgeChunk>(rows: T[] | undefined, id: number, state: ReviewState): T[] | undefined {
  return rows?.map((row) => (row.id === id ? { ...row, review_state: state } : row));
}

/** The verdict mutation: optimistic chip flip across every cached knowledge / hot-memory
 *  list, rollback + verbatim server `detail` on failure, one toast per outcome. */
export function useReviewVerdict() {
  const qc = useQueryClient();
  const toast = useToast();
  const [unsupported, setUnsupported] = useState(() => reviewUnsupported);

  const mutation = useMutation({
    mutationFn: ({ chunk, state }: ReviewVars) =>
      api.reviewMemoryChunk(chunk.id, chunk.tier ? { state, tier: chunk.tier } : { state }),
    onMutate: async ({ chunk, state }) => {
      await qc.cancelQueries({ queryKey: queryKeys.knowledge });
      await qc.cancelQueries({ queryKey: queryKeys.memoryHot });
      const prevKnowledge = qc.getQueriesData<KnowledgeListData>({ queryKey: queryKeys.knowledge });
      const prevHot = qc.getQueriesData<HotListData>({ queryKey: queryKeys.memoryHot });
      qc.setQueriesData<KnowledgeListData>({ queryKey: queryKeys.knowledge }, (old) =>
        old ? { ...old, results: restamp(old.results, chunk.id, state) } : old,
      );
      qc.setQueriesData<HotListData>({ queryKey: queryKeys.memoryHot }, (old) =>
        old ? { ...old, chunks: restamp(old.chunks, chunk.id, state) } : old,
      );
      return { prevKnowledge, prevHot };
    },
    onSuccess: (r, { state }, ctx) => {
      if (r.enabled === false) {
        rollback(ctx);
        toast({
          tone: "error",
          title: "Memory",
          message: "The knowledge store is off — the verdict was not saved (enable middleware.knowledge).",
        });
        return;
      }
      toast({ tone: "success", title: "Memory", message: `Marked ${r.review_state ?? state}.` });
      void qc.invalidateQueries({ queryKey: queryKeys.knowledge });
      void qc.invalidateQueries({ queryKey: queryKeys.memoryHot });
    },
    onError: (e, _vars, ctx) => {
      rollback(ctx);
      const status = (e as { status?: number }).status;
      if (status === 501) {
        // An ADR 0031 backend without verdicts — learn it once, hide the actions.
        reviewUnsupported = true;
        setUnsupported(true);
        toast({
          tone: "info",
          title: "Memory",
          message: "This knowledge backend doesn't support review verdicts.",
        });
        return;
      }
      // 400 (bad state / commons row) and 404 carry the server's own `detail` — show it verbatim.
      toast({ tone: "error", title: "Memory", message: errMsg(e) });
    },
  });

  function rollback(ctx: { prevKnowledge: [unknown, unknown][]; prevHot: [unknown, unknown][] } | undefined) {
    for (const [key, data] of ctx?.prevKnowledge ?? []) qc.setQueryData(key as readonly unknown[], data);
    for (const [key, data] of ctx?.prevHot ?? []) qc.setQueryData(key as readonly unknown[], data);
  }

  return {
    setVerdict: (chunk: KnowledgeChunk, state: ReviewState) => mutation.mutate({ chunk, state }),
    pendingId: mutation.isPending ? mutation.variables?.chunk.id : undefined,
    unsupported,
  };
}

/** Confirm / Reject / Re-open for one row. Nothing for commons rows (read-only here) or
 *  once the backend has said it has no verdicts. A row with no verdict from the backend
 *  is treated as pending (ADR 0108 D4: NULL reads as pending). */
export function ReviewActions({ chunk }: { chunk: KnowledgeChunk }) {
  const { setVerdict, pendingId, unsupported } = useReviewVerdict();
  if (unsupported || chunk.tier === "commons") return null;
  const state = reviewStateOf(chunk) ?? "pending";
  const busy = pendingId === chunk.id;
  const confirm = (
    <Button
      icon
      variant="ghost"
      type="button"
      title="Confirm — keep this memory as trusted context"
      aria-label={`confirm entry ${chunk.id}`}
      onClick={() => setVerdict(chunk, "confirmed")}
      loading={busy}
    >
      <Check size={14} />
    </Button>
  );
  const reject = (
    <Button
      icon
      variant="ghost"
      type="button"
      title="Reject — keep the row for audit but stop delivering it to the agent"
      aria-label={`reject entry ${chunk.id}`}
      onClick={() => setVerdict(chunk, "rejected")}
      loading={busy}
    >
      <X size={14} />
    </Button>
  );
  const reopen = (
    <Button
      icon
      variant="ghost"
      type="button"
      title="Re-open — back to pending review (delivers again)"
      aria-label={`reopen entry ${chunk.id}`}
      onClick={() => setVerdict(chunk, "pending")}
      loading={busy}
    >
      <RotateCcw size={14} />
    </Button>
  );
  if (state === "rejected") return reopen;
  if (state === "confirmed") return reject;
  return (
    <>
      {confirm}
      {reject}
    </>
  );
}
