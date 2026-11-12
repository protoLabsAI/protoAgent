/**
 * The "Your instructions" Work section (ADR 0129 §3) — the standing instructions and approvals
 * in force, one `WorkRow` per in-force directive, registered into the Work view on import.
 *
 * Headline is WHAT IS APPROVED — the directive's `scope`, falling back to its `quote` when the
 * scope is empty (§3). Provenance rides below in muted "you" wording: `You said "…" in this chat ·
 * today HH:mm`. A directive the console can tie to an answered decision (the interim heuristic in
 * {@link linkDecision}, §3.1) is tagged **Approval** and reads `You chose "…" · HH:mm`; its detail
 * shows **You were asked** / **You chose** / **Your words** / **Open decision** (which opens the
 * decision in place, through the Work detail). Everything else is a plain **Instruction**.
 * Withdrawal lives in the detail and goes through the directive ledger's existing `revoke` path
 * (`useDirectives`). The LIST stays in-force only — finished items leave Work (§1) and the
 * ledger's own History accordion stays in the chat drawer — but the Overview still lists closed
 * and other-chat directives, so the DETAIL resolves those too: an in-force row from another chat
 * keeps its full detail (withdraw included); a fulfilled/revoked row renders read-only with its
 * closing sentence. The decisions the linker reads come from the shared decisions store, topped
 * up with the server's answered ledger on mount so an answer that predates this tab (a reload,
 * another surface) still links.
 *
 * In-force membership, the all-chats scope and the tied-job hiding ALL come from the shared
 * `workGroups` projection, so Work and the chat rail (which read the same selector) can't drift:
 * when a tied job ends, its instruction leaves the list and the count in the SAME render, with no
 * server fulfil claim — and the detail fallback never resurrects it (closed rows only). The wire
 * `DirectiveRow` carries no decision id and no job-link field, so a tied job is rendered only when
 * a projection supplies it explicitly (`tiedJob`) — never parsed from prose. See the PR for the
 * schema gap this interim stands in for.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Badge, Button, Callout, type Status } from "@protolabsai/ui/primitives";
import { Alert, Skeleton, StatusDot } from "@protolabsai/ui/data";
import { FormField, Textarea } from "@protolabsai/ui/forms";
import { useToast } from "@protolabsai/ui/overlays";
import { useWorkActionSize } from "../../shell/SurfaceFrame";
import { WorkEmpty as Empty } from "../WorkEmpty";

import type { DirectiveRow } from "../../lib/api/chat";
import type { DecisionRow } from "../../lib/api/decisions";
import { chatStore } from "../../chat/chat-store";
import { useDecisions } from "../../decisions/useDecisions";
import { useDirectives } from "../../chat/useDirectives";
import { closingSentence } from "../../chat/DirectiveEntry";
import { localStampTitle, parseInstant } from "../../lib/format";
import { linkDecision } from "../linkDecision";
import { workGroups } from "../workGroups";
import { WorkRow } from "../WorkRow";
import { fromDecision, statusTone } from "../tone";
import type { WorkItem } from "../useWorkItems";
import type { WorkJobRef } from "../tone";
import { registerWorkSection, type WorkSectionContext } from "../sectionRegistry";
import "../work-detail.css";

/** An item in the "instructions" group is always a directive row — the projection's directive
 *  arm, which may carry an explicit `tiedJob` (never one parsed from the directive's prose). */
type InstructionItem = WorkItem & { tiedJob?: WorkJobRef };

/** "16:25" — a clock time, 24h, in the viewer's timezone. Empty when unparseable. */
function clock(iso: string | null | undefined): string {
  const ms = parseInstant(iso);
  if (ms === null) return "";
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** The muted provenance stamp: "today 09:12" / "yesterday 09:12" / "Nov 7 09:12". */
function provenanceWhen(iso: string | null | undefined): string {
  const ms = parseInstant(iso);
  if (ms === null) return "";
  const date = new Date(ms);
  const now = new Date();
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayDelta = Math.round((startOf(now) - startOf(date)) / 86_400_000);
  const time = clock(iso);
  if (dayDelta === 0) return `today ${time}`;
  if (dayDelta === 1) return `yesterday ${time}`;
  const day = date.toLocaleDateString(undefined, {
    month: "short", day: "numeric", ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
  return `${day} ${time}`;
}

/** Which chat recorded the instruction — "in this chat" for the focused one, else its title. */
function chatWhere(sessionId: string, focused: string | undefined): string {
  if (sessionId && sessionId === focused) return "in this chat";
  const title = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.title;
  return title ? `in "${title}"` : "in another chat";
}

/** The option the operator chose, by its server-computed label (or the raw answer). */
function chosenLabel(decision: DecisionRow): string {
  return decision.answer_label
    ?? decision.options.find((o) => o.id === decision.answer)?.label
    ?? decision.answer
    ?? "an option";
}

/** `You said "<quote>" in this chat · today HH:mm` — the provenance line (§3). */
function provenance(row: DirectiveRow, focused: string | undefined): string {
  const where = chatWhere(row.session_id, focused);
  const when = provenanceWhen(row.created_at);
  return `You said "${row.quote}"${where ? ` ${where}` : ""}${when ? ` · ${when}` : ""}`;
}

/** The tied job's human name from the shared sources (title), falling back to its id. */
function jobName(sources: WorkSectionContext["sources"], tiedJob: WorkJobRef | undefined): string {
  if (!tiedJob) return "";
  const match = Object.values(sources)
    .flatMap((source) => source.items)
    .find((item) => item.kind === tiedJob.kind && item.id === tiedJob.id);
  return match?.title || tiedJob.id;
}

/** The row meta: "You chose …" for an approval, else the "You said …" provenance, with an
 *  explicit tied job appended as "· ends with <job>" (§3, Q2). */
function rowMeta(
  row: DirectiveRow, decision: DecisionRow | null, tiedJob: WorkJobRef | undefined, name: string, focused: string | undefined,
): string {
  const base = decision ? `You chose "${chosenLabel(decision)}" · ${clock(decision.answered_at)}` : provenance(row, focused);
  return tiedJob ? `${base} · ends with ${name}` : base;
}

/** One §3 row, wherever it renders: this section's own list and the Overview's instructions
 *  group share this single presentation, so a directive can never show one kind on the
 *  Overview ("Instruction") and another ("Approval") in its picked group. Headline = what is
 *  approved — the scope, falling back to the quote (§3); meta = "You chose …" when the §3.1
 *  link holds, else the "You said …" provenance, with a tied job's "ends with <job>" suffix. */
export function instructionRowPresentation(
  item: Pick<InstructionItem, "raw" | "tiedJob">, answered: readonly DecisionRow[],
  sources: WorkSectionContext["sources"], focused: string | undefined,
): { tag: string; tone: Status; icon: ReactNode; title: string; meta: string } {
  const row = item.raw as DirectiveRow;
  const decision = linkDecision(row, answered);
  return {
    tag: decision ? "Approval" : "Instruction",
    tone: "neutral",
    icon: <StatusDot status="info" />,
    title: row.scope?.trim() || row.quote,
    meta: rowMeta(row, decision, item.tiedJob, jobName(sources, item.tiedJob), focused),
  };
}

/** The selected instruction's detail: the approval fields when linked, the provenance, any tied
 *  job's lifetime, and — while in force — the withdraw confirm through the ledger's existing
 *  revoke path. A closed row (opened from the Overview's Done lane, or one that closed under
 *  this very view) renders read-only with its closing sentence instead of the withdraw action. */
function InstructionDetail({ ctx, item, decision }: {
  ctx: WorkSectionContext; item: InstructionItem; decision: DecisionRow | null;
}): ReactNode {
  const size = useWorkActionSize();
  const row = item.raw as DirectiveRow;
  const { revoke } = useDirectives(row.session_id);
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [notice, setNotice] = useState("");
  const promptRef = useRef<HTMLParagraphElement>(null);
  const noticeRef = useRef<HTMLDivElement>(null);
  const actionRef = useRef<HTMLDivElement>(null);

  const inForce = row.status === "in_force";
  const tiedJob = item.tiedJob;
  const name = jobName(ctx.sources, tiedJob);
  // The tied job's own Work item, when the sources carry it — "Open the job" renders only then,
  // never as a dead button.
  const jobItem = tiedJob && Object.values(ctx.sources).flatMap((source) => source.items)
    .find((candidate) => candidate.kind === tiedJob.kind && candidate.id === tiedJob.id);
  const title = row.scope.trim() || row.quote;
  const tone = inForce ? "info" : statusTone(item.status);

  useLayoutEffect(() => { if (confirm) promptRef.current?.focus(); }, [confirm]);
  useLayoutEffect(() => { if (notice) noticeRef.current?.focus(); }, [notice]);

  function cancel() {
    setConfirm(false);
    actionRef.current?.querySelector("button")?.focus();
  }
  async function withdraw() {
    setBusy(true);
    setError(false);
    const result = await revoke(row.id, reason);
    setBusy(false);
    if ("ok" in result) {
      setConfirm(false);
      toast({ tone: "neutral", message: "Withdrawn. The agent won't treat it as approved from its next turn." });
    } else if ("conflict" in result) {
      // The refreshed ledger re-lists the row as closed, so this same detail (keyed by id)
      // stays mounted, now read-only, and the notice shows in place.
      setNotice(result.conflict
        ? `This was already closed: ${closingSentence(result.conflict)}. Nothing changed.`
        : "This was already closed. Nothing changed.");
    } else if ("gone" in result) {
      // A 404 removes the row from the ledger entirely, so this detail is about to unmount —
      // the feedback must outlive it.
      toast({ tone: "neutral", message: "This instruction is no longer in the list. The list has been refreshed." });
    } else setError(true);
  }

  if (notice) {
    return <div className="work-instruction-detail" ref={noticeRef} tabIndex={-1}>
      <h2 className="work-detail__title" data-surface-focus tabIndex={-1}>{title}</h2>
      <Callout tone="info">{notice}</Callout>
    </div>;
  }

  /** The linked decision, opened IN PLACE through the Work detail — WorkView's retained-records
   *  path keeps the settled record visible even though the pending source no longer lists it. */
  function openDecision() {
    if (!decision) return;
    ctx.onOpen({ kind: "decision", id: decision.id, status: fromDecision(decision.state),
      title: decision.question, meta: "", sessionId: decision.session_id, raw: decision });
  }

  return <div className="work-instruction-detail">
    <header className="work-detail__header">
      <StatusDot status={tone} />
      <h2 className="work-detail__title" data-surface-focus tabIndex={-1}>{title}</h2>
      <Badge status={tone}>{decision ? "Approval" : "Instruction"}</Badge>
    </header>
    <dl className="work-detail__fields">
      {decision && <>
        <div className="work-detail__field"><dt>You were asked</dt><dd>{decision.question}</dd></div>
        <div className="work-detail__field"><dt>You chose</dt><dd>{`"${chosenLabel(decision)}" · ${clock(decision.answered_at)}`}</dd></div>
      </>}
      <div className="work-detail__field">
        <dt>Your words</dt>
        <dd><span title={localStampTitle(row.created_at)}>{provenance(row, ctx.sessionId)}</span></dd>
      </div>
      {inForce && tiedJob && <div className="work-detail__field">
        <dt>Lasts</dt>
        <dd>{`until "${name}" finishes. Then it closes itself.`}</dd>
      </div>}
      {!inForce && <div className="work-detail__field">
        <dt>Closed</dt>
        <dd><span title={localStampTitle(row.closed_at)}>{closingSentence(row)}</span></dd>
      </div>}
      {!inForce && row.close_reason && !(row.closed_by === "operator" && row.close_reason === "revoked by the operator")
        && <div className="work-detail__field"><dt>Note</dt><dd>{row.close_reason}</dd></div>}
    </dl>
    {confirm && inForce ? <div className="work-detail__confirm" role="group" aria-label="Withdraw this instruction" onKeyDown={(e) => {
      if (e.key === "Escape") { e.preventDefault(); if (!busy) cancel(); }
    }}>
      <p ref={promptRef} tabIndex={-1} className="work-detail__confirm-prompt">Withdraw this instruction?</p>
      <p>The agent will stop treating it as approved from its next turn. Anything it has already scheduled keeps running until you cancel it.</p>
      <FormField label="Reason (optional)">
        <Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} disabled={busy} placeholder="For example: holding the release until Monday" />
      </FormField>
      {error && <Alert status="warning">Couldn&apos;t withdraw it. It&apos;s still active. <Button variant="ghost" size={size} onClick={() => void withdraw()} disabled={busy}>Try again</Button></Alert>}
      <div className="work-detail__confirm-actions">
        {size === "md" && <Button size="md" disabled={busy} onClick={cancel}>Keep it</Button>}
        <Button variant="danger" size={size} loading={busy} onClick={() => void withdraw()}>Withdraw</Button>
        {size !== "md" && <Button size={size} disabled={busy} onClick={cancel}>Keep it</Button>}
      </div>
    </div> : <div className="work-detail__actions" ref={actionRef}>
      {decision && <Button size={size} onClick={openDecision}>Open decision</Button>}
      {jobItem && <Button size={size} onClick={() => ctx.onOpen(jobItem)}>Open the job</Button>}
      {inForce && <Button variant="danger" size={size} onClick={() => { setError(false); setConfirm(true); }}>Withdraw…</Button>}
    </div>}
  </div>;
}

function DirectivesSection({ ctx }: { ctx: WorkSectionContext }): ReactNode {
  const size = useWorkActionSize();
  const decisions = useDecisions();
  // The link heuristic reads ANSWERED decisions from the shared store's by-id record cache. The
  // pending-only refresh never carries an answer that predates this tab (a reload) or one that
  // settled on another surface without being seen pending here, so top the cache up with the
  // server's answered ledger once per mount (coalesced + retried in the store).
  useEffect(() => { void decisions.loadAnswered(); }, [decisions.loadAnswered]);
  const answered = useMemo(
    () => Object.values(decisions.records).filter((row) => row.state === "answered"),
    [decisions.records],
  );
  const source = ctx.sources.directives;
  const thisChatOnly = ctx.state.filters?.thisChat === true;
  // In-force only, tied-job hiding and scope all come from the shared selector, so the rail agrees.
  const group = workGroups(ctx.sources, { thisChatOnly, sessionId: ctx.sessionId }).find((g) => g.id === "instructions")!;
  const items = group.items as InstructionItem[];

  const retry = <Button size={size} onClick={source.retry}>Retry</Button>;
  const loadingView = <div aria-busy="true" aria-label="Loading your instructions"><Skeleton lines={3} /><Skeleton lines={3} /></div>;
  const failedView = <Alert status="warning">
    Couldn&apos;t load standing instructions. The agent still has them. Only this view failed to load. {retry}
  </Alert>;
  const staleNote = <Callout tone="warning">Showing the last load. Couldn&apos;t refresh. {retry}</Callout>;

  if (ctx.state.itemId) {
    const matches = (item: InstructionItem) => `${item.kind}:${item.id}` === ctx.state.itemId || item.id === ctx.state.itemId;
    // The Overview lists directives this in-force, this-chat-scoped group doesn't: an in-force
    // one from another chat (under the thisChat filter), and fulfilled/revoked ones in its Done
    // lane. Resolve those here too — first through the UNSCOPED projection (which keeps the
    // tied-job hiding, so an instruction whose job just ended is never resurrected), then from
    // the raw source for CLOSED rows only.
    const selected = items.find(matches)
      ?? (workGroups(ctx.sources, { thisChatOnly: false }).find((g) => g.id === "instructions")!.items as InstructionItem[]).find(matches)
      ?? source.items.filter((item) => (item.raw as DirectiveRow).status !== "in_force").find(matches);
    if (!selected) {
      // While the source hasn't loaded, the row's absence proves nothing — don't claim it's gone.
      if (source.status === "loading") return loadingView;
      if (source.status === "error") return failedView;
      return <>
        {source.status === "stale" && staleNote}
        <Empty title="This instruction is no longer here." />
      </>;
    }
    return <InstructionDetail key={selected.id} ctx={ctx} item={selected} decision={linkDecision(selected.raw as DirectiveRow, answered)} />;
  }

  if (source.status === "loading") return loadingView;
  if (source.status === "error") return failedView;

  if (!items.length) {
    return <>
      {source.status === "stale" && staleNote}
      <Empty
        title="No standing instructions"
        description="When you give this agent an instruction or approval that lasts beyond one reply, it records your words here. You can withdraw any of them at any time."
      />
    </>;
  }

  return <>
    {source.status === "stale" && staleNote}
    <ul className="work-group">{items.map((item) => {
      const row = instructionRowPresentation(item, answered, ctx.sources, ctx.sessionId);
      return <li key={`${item.kind}:${item.id}`}><WorkRow
        status={item.status}
        tone="info"
        badge={row.tag}
        title={row.title}
        meta={row.meta}
        focusKey={`${item.kind}:${item.id}`}
        onOpen={() => ctx.onOpen(item)}
      /></li>;
    })}</ul>
  </>;
}

function renderDirectives(ctx: WorkSectionContext): ReactNode {
  return <div className="work-section work-section--directives"><DirectivesSection ctx={ctx} /></div>;
}

registerWorkSection("directives", { label: "Your instructions", group: "instructions", render: renderDirectives });
