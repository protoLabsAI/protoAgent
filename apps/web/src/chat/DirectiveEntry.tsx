import { useId, useLayoutEffect, useRef, useState } from "react";
import { Badge, Button, Callout } from "@protolabsai/ui/primitives";
import { Alert } from "@protolabsai/ui/data";
import { FormField, Textarea } from "@protolabsai/ui/forms";
import { Tooltip, useToast } from "@protolabsai/ui/overlays";
import type { DirectiveRow } from "../lib/api/chat";
import { formatGivenAt, localStampTitle } from "../lib/format";
import { useIsMobile } from "../lib/useIsMobile";
import type { RevokeResult } from "./useDirectives";

export function closingSentence(entry: DirectiveRow) {
  const when = formatGivenAt(entry.closed_at);
  if (entry.status === "fulfilled" && entry.closed_by === "system") {
    return `Done · it closed itself when its linked work finished ${when}`;
  }
  const who = entry.closed_by === "operator" ? "you" : "the agent";
  return entry.status === "fulfilled"
    ? `Done · ${who} marked it complete ${when}`
    : `Withdrawn by ${who} · ${when}`;
}

type Props = {
  actionSize?: "sm" | "md";
  entry: DirectiveRow;
  revoke: (id: string, reason?: string) => Promise<RevokeResult>;
};

export function DirectiveEntry({ entry, revoke, actionSize }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [clipped, setClipped] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [notice, setNotice] = useState("");
  const quote = useRef<HTMLQuoteElement>(null);
  const prompt = useRef<HTMLParagraphElement>(null);
  const result = useRef<HTMLDivElement>(null);
  const action = useRef<HTMLDivElement>(null);
  const label = useId();
  const quoteId = useId();
  const toast = useToast();
  const viewportMobile = useIsMobile();
  const mobile = actionSize ? actionSize === "md" : viewportMobile;
  const history = entry.status !== "in_force";
  useLayoutEffect(() => {
    const node = quote.current;
    if (!node || expanded) return;
    const measure = () => setClipped(node.scrollHeight > node.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [entry.quote, expanded]);
  useLayoutEffect(() => { if (confirm) prompt.current?.focus(); }, [confirm]);
  useLayoutEffect(() => { if (notice) result.current?.focus(); }, [notice]);
  function cancel() {
    setConfirm(false);
    action.current?.querySelector("button")?.focus();
  }
  async function withdraw() {
    setBusy(true);
    setError(false);
    const response = await revoke(entry.id, reason);
    setBusy(false);
    if ("ok" in response) {
      toast({ tone: "neutral", message: "Withdrawn. The agent won't treat it as approved from its next turn." });
    } else if ("conflict" in response) {
      setNotice(response.conflict
        ? `This was already closed: ${closingSentence(response.conflict)}. Nothing changed.`
        : "This was already closed. Nothing changed.");
    } else if ("gone" in response) {
      setNotice("This instruction is no longer in the list. The list has been refreshed.");
    } else setError(true);
  }
  if (notice) return <div ref={result} tabIndex={-1}><Callout tone="info">{notice}</Callout></div>;
  return <article aria-labelledby={quoteId} className={`chat-directive${history ? " chat-directive--history" : ""}`}>
    <blockquote id={quoteId} ref={quote} tabIndex={-1} className={expanded ? "chat-directive-quote" : `chat-directive-quote chat-directive-quote--clamped${clipped ? " chat-directive-quote--clipped" : ""}`}>{entry.quote}</blockquote>
    {(clipped || expanded) && <Button variant="ghost" size={actionSize ?? "sm"} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "Show less" : "Show full text"}</Button>}
    <div className="chat-directive-meta">
      {entry.conditions && <p>Only if: {entry.conditions}</p>}
      {entry.scope && <p>Applies to: {entry.scope}</p>}
      <Tooltip label={localStampTitle(entry.created_at)}><span>Given {formatGivenAt(entry.created_at)}</span></Tooltip>
      {history && <>
        <p><Badge>{entry.status === "fulfilled" ? "done" : "withdrawn"}</Badge> {closingSentence(entry)}</p>
        {entry.close_reason && !(entry.closed_by === "operator" && entry.close_reason === "revoked by the operator") && <p>Note: {entry.close_reason}</p>}
      </>}
    </div>
    {!history && <>
      <div ref={action}><Button variant="ghost" size={actionSize ?? "sm"} onClick={() => setConfirm(true)}>Withdraw…</Button></div>
      {confirm && <div role="group" aria-labelledby={label} onKeyDown={(e) => {
        if (e.key === "Escape") { e.preventDefault(); if (!busy) cancel(); }
      }}>
        <p id={label} ref={prompt} tabIndex={-1}>Withdraw this instruction?</p>
        <p>The agent will stop treating it as approved from its next turn. Anything it has already scheduled keeps running until you cancel it.</p>
        <FormField label="Reason (optional)"><Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} disabled={busy} placeholder="For example: holding the release until Monday" /></FormField>
        {error && <Alert status="warning">Couldn't withdraw it. It's still active. <Button variant="ghost" size={actionSize ?? "sm"} onClick={() => void withdraw()} disabled={busy}>Try again</Button></Alert>}
        <div className={`chat-directive-actions${mobile ? " chat-directive-actions--mobile" : ""}`}>
          {actionSize === "md" && <Button size="md" disabled={busy} onClick={cancel}>Keep it</Button>}
          <Button variant="danger" size={actionSize ?? "md"} loading={busy} onClick={() => void withdraw()}>Withdraw</Button>
          {actionSize !== "md" && <Button size={actionSize ?? "md"} disabled={busy} onClick={cancel}>Keep it</Button>}
        </div>
      </div>}
    </>}
  </article>;
}
