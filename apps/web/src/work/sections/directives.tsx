/**
 * The "Your instructions" Work section (ADR 0129 §3) — the standing instructions and approvals
 * in force, one `WorkRow` per in-force directive, registered into the Work view on import.
 *
 * Headline is WHAT IS APPROVED — the directive's `scope`, falling back to its `quote` when the
 * scope is empty (§3). Provenance rides below in muted "you" wording: `You said "…" in this chat ·
 * today HH:mm`. A directive the console can tie to an answered decision (the interim heuristic in
 * {@link linkDecision}, §3.1) is tagged **Approval** and reads `You chose "…" · HH:mm`; its detail
 * shows **You were asked** / **You chose** / **Your words** / **Open decision**. Everything else is
 * a plain **Instruction**. Withdrawal lives in the detail and goes through the directive ledger's
 * existing `revoke` path (`useDirectives`). History is NOT shown here — finished items leave Work
 * (§1); the ledger's own History accordion stays in the chat drawer.
 *
 * In-force membership, the all-chats scope and the tied-job hiding ALL come from the shared
 * `workGroups` projection, so Work and the chat rail (which read the same selector) can't drift:
 * when a tied job ends, its instruction leaves the list and the count in the SAME render, with no
 * server fulfil claim. The wire `DirectiveRow` carries no decision id and no job-link field, so a
 * tied job is rendered only when a projection supplies it explicitly (`tiedJob`) — never parsed
 * from prose. See the PR for the schema gap this interim stands in for.
 */
import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Badge, Button, Callout } from "@protolabsai/ui/primitives";
import { Alert, StatusDot } from "@protolabsai/ui/data";
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

/** The selected instruction's detail: the approval fields when linked, the provenance, any tied
 *  job's lifetime, and the withdraw confirm through the ledger's existing revoke path. */
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

  const tiedJob = item.tiedJob;
  const name = jobName(ctx.sources, tiedJob);
  const title = row.scope.trim() || row.quote;

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
      setNotice(result.conflict
        ? `This was already closed: ${closingSentence(result.conflict)}. Nothing changed.`
        : "This was already closed. Nothing changed.");
    } else if ("gone" in result) {
      setNotice("This instruction is no longer in the list. The list has been refreshed.");
    } else setError(true);
  }

  // A refresh can remove the row before the result lands; keep the notice in place for this view.
  if (notice) {
    return <div className="work-instruction-detail" ref={noticeRef} tabIndex={-1}>
      <h2 className="work-detail__title" data-surface-focus tabIndex={-1}>{title}</h2>
      <Callout tone="info">{notice}</Callout>
    </div>;
  }

  function openJob() {
    const jobItem = tiedJob && Object.values(ctx.sources).flatMap((source) => source.items)
      .find((candidate) => candidate.kind === tiedJob.kind && candidate.id === tiedJob.id);
    if (jobItem) ctx.onOpen(jobItem);
  }

  return <div className="work-instruction-detail">
    <header className="work-detail__header">
      <StatusDot status="info" />
      <h2 className="work-detail__title" data-surface-focus tabIndex={-1}>{title}</h2>
      <Badge status="info">{decision ? "Approval" : "Instruction"}</Badge>
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
      {tiedJob && <div className="work-detail__field">
        <dt>Lasts</dt>
        <dd>{`until "${name}" finishes. Then it closes itself.`}</dd>
      </div>}
    </dl>
    {confirm ? <div className="work-detail__confirm" role="group" aria-label="Withdraw this instruction" onKeyDown={(e) => {
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
      {decision && <Button size={size} onClick={() => ctx.decisionActions?.openSourceChat(decision.session_id)}>Open decision</Button>}
      {tiedJob && <Button size={size} onClick={openJob}>Open the job</Button>}
      <Button variant="danger" size={size} onClick={() => { setError(false); setConfirm(true); }}>Withdraw…</Button>
    </div>}
  </div>;
}

function DirectivesSection({ ctx }: { ctx: WorkSectionContext }): ReactNode {
  const decisions = useDecisions();
  // The link heuristic reads ANSWERED decisions from the shared store's by-id record cache (a
  // decision that produced a directive has just settled there, having left the pending list).
  const answered = useMemo(
    () => Object.values(decisions.records).filter((row) => row.state === "answered"),
    [decisions.records],
  );
  const thisChatOnly = ctx.state.filters?.thisChat === true;
  // In-force only, tied-job hiding and scope all come from the shared selector, so the rail agrees.
  const group = workGroups(ctx.sources, { thisChatOnly, sessionId: ctx.sessionId }).find((g) => g.id === "instructions")!;
  const items = group.items as InstructionItem[];

  if (ctx.state.itemId) {
    const selected = items.find((item) => `${item.kind}:${item.id}` === ctx.state.itemId || item.id === ctx.state.itemId);
    // A tied job that ended (or a withdrawal) drops the row; the detail follows it out.
    if (!selected) return <Empty title="This instruction is no longer here." />;
    return <InstructionDetail key={selected.id} ctx={ctx} item={selected} decision={linkDecision(selected.raw as DirectiveRow, answered)} />;
  }

  if (!items.length) {
    return <Empty
      title="No standing instructions"
      description="When you give this agent an instruction or approval that lasts beyond one reply, it records your words here. You can withdraw any of them at any time."
    />;
  }

  return <ul className="work-group">{items.map((item) => {
    const row = item.raw as DirectiveRow;
    const decision = linkDecision(row, answered);
    const name = jobName(ctx.sources, item.tiedJob);
    return <li key={`${item.kind}:${item.id}`}><WorkRow
      status={item.status}
      tone="info"
      badge={decision ? "Approval" : "Instruction"}
      title={row.scope.trim() || row.quote}
      meta={rowMeta(row, decision, item.tiedJob, name, ctx.sessionId)}
      focusKey={`${item.kind}:${item.id}`}
      onOpen={() => ctx.onOpen(item)}
    /></li>;
  })}</ul>;
}

function renderDirectives(ctx: WorkSectionContext): ReactNode {
  return <div className="work-section work-section--directives"><DirectivesSection ctx={ctx} /></div>;
}

registerWorkSection("directives", { label: "Your instructions", group: "instructions", render: renderDirectives });
