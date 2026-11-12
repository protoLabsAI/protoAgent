import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Badge, Button, Callout } from "@protolabsai/ui/primitives";
import { Accordion, AccordionItem } from "@protolabsai/ui/navigation";
import { Alert, StatusDot } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { useWorkActionSize } from "../shell/SurfaceFrame";
import { copyText } from "../lib/clipboard";
import { statusTone, statusWord, type WorkStatus } from "./tone";
import "./work-detail.css";

export type WorkField = { label: string; value: ReactNode };

/** Destructive actions confirm inline (§4.4), never with a stacked `ConfirmDialog`. */
export type WorkConfirm = { prompt: string; explanation: string; confirmLabel: string; keepLabel: string };

export type WorkAction = {
  id: string;
  label: string;
  destructive?: boolean;
  confirm?: WorkConfirm;
  /** Resolves to `{ undoToken }` when the change can be undone (a delete/clear); the
   *  token drives the §4.5 undo toast. A rejection surfaces the inline warning Alert. */
  run: () => Promise<{ undoToken?: string } | void>;
};

export type WorkDetailProps = {
  title: string;
  status: WorkStatus;
  fields: WorkField[];
  actions: WorkAction[];
  technical: Record<string, string>;
  /** Restores the just-deleted item when the §4.5 undo toast is used. */
  onUndo?: (token: string) => Promise<void>;
  /** "{Type} deleted." in the undo toast — the item's plain-word kind (Goal, Watch…). */
  type?: string;
  /** Overrides the header badge word (otherwise the status word) — the "kind as a tag"
   *  rule, e.g. a stopped goal reads "Goal stopped" with its neutral tone (§2, §6). */
  statusLabel?: string;
  /** Refreshes the item behind the `changedNotice` Callout. */
  onRefresh?: () => void;
  changedNotice?: string;
};

/** A field is hidden when its value is empty — never rendered as "—" (task §4.3). */
function isEmpty(value: ReactNode): boolean {
  return value == null || value === false || (typeof value === "string" && value.trim() === "");
}

/** The failure line reads "Couldn't {verb} it." — the action's word, sans trailing ellipsis. */
function verbOf(action: WorkAction): string {
  return action.label.replace(/[.…]+\s*$/u, "").trim().toLowerCase();
}

export function WorkDetail({
  title,
  status,
  fields,
  actions,
  technical,
  onUndo,
  type = "Item",
  statusLabel,
  onRefresh,
  changedNotice,
}: WorkDetailProps) {
  const size = useWorkActionSize();
  const toast = useToast();
  const [confirming, setConfirming] = useState<WorkAction | null>(null);
  const [busy, setBusy] = useState(false);
  // One action runs at a time, so a single error slot (the running action's verb + retry) covers both
  // the inline-confirm failure and a direct action's failure.
  const [error, setError] = useState<{ verb: string; retry: () => void } | null>(null);
  const promptRef = useRef<HTMLParagraphElement>(null);
  const promptId = useId();
  const actionsRef = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef<string | null>(null);

  useLayoutEffect(() => {
    if (confirming) promptRef.current?.focus();
  }, [confirming]);

  // Keep it / Escape returns focus to the trigger that opened the confirm (§6, WCAG 2.2.1).
  useLayoutEffect(() => {
    if (confirming || !restoreFocus.current) return;
    actionsRef.current
      ?.querySelector<HTMLButtonElement>(`[data-action-trigger="${restoreFocus.current}"]`)
      ?.focus();
    restoreFocus.current = null;
  }, [confirming]);

  function showUndoToast(token: string) {
    if (onUndo) {
      // duration 0 hands the lifetime to UndoToast, which runs a pausable 8 s timer (§4.5, §6).
      toast({ tone: "neutral", duration: 0, message: <UndoToast type={type} token={token} onUndo={onUndo} size={size} /> });
    } else {
      toast({ tone: "neutral", message: `${type} deleted.` });
    }
  }

  async function execute(action: WorkAction) {
    setBusy(true);
    setError(null);
    try {
      const result = await action.run();
      setBusy(false);
      setConfirming(null);
      const token = result && typeof result === "object" ? result.undoToken : undefined;
      if (token) showUndoToast(token);
    } catch {
      // The confirm group (if open) stays put so Try again can re-run; the warning Alert is
      // role="alert" (assertive) via DS, so the failure is announced (§4.5).
      setBusy(false);
      setError({ verb: verbOf(action), retry: () => void execute(action) });
    }
  }

  function onTrigger(action: WorkAction) {
    if (action.confirm) {
      setError(null);
      setConfirming(action);
    } else {
      void execute(action);
    }
  }

  function cancelConfirm() {
    if (busy) return;
    restoreFocus.current = confirming?.id ?? null;
    setError(null);
    setConfirming(null);
  }

  const visibleFields = fields.filter((f) => !isEmpty(f.value));
  const techEntries = Object.entries(technical);

  return (
    <div className="work-detail">
      <header className="work-detail__header">
        <StatusDot status={statusTone(status)} pulse={status === "running"} />
        <h2 className="work-detail__title" data-surface-focus tabIndex={-1}>
          {title}
        </h2>
        <Badge status={statusTone(status)}>{statusLabel ?? statusWord(status)}</Badge>
      </header>

      {changedNotice && (
        <Callout tone="info">
          {changedNotice}{" "}
          <Button variant="ghost" size={size} onClick={() => onRefresh?.()}>
            Refresh
          </Button>
        </Callout>
      )}

      {visibleFields.length > 0 && (
        <dl className="work-detail__fields">
          {visibleFields.map((field, i) => (
            <div className="work-detail__field" key={`${field.label}-${i}`}>
              <dt>{field.label}</dt>
              <dd>{field.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {confirming ? (
        <div
          className="work-detail__confirm"
          role="group"
          aria-labelledby={promptId}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              cancelConfirm();
            }
          }}
        >
          <p id={promptId} ref={promptRef} tabIndex={-1} className="work-detail__confirm-prompt">
            {confirming.confirm!.prompt}
          </p>
          <p>{confirming.confirm!.explanation}</p>
          {error && (
            <Alert status="warning">
              Couldn&apos;t {error.verb} it. Nothing changed.{" "}
              <Button variant="ghost" size={size} disabled={busy} onClick={error.retry}>
                Try again
              </Button>
            </Alert>
          )}
          <div className="work-detail__confirm-actions">
            {size === "md" && <Button size={size} disabled={busy} onClick={cancelConfirm}>
              {confirming.confirm!.keepLabel}
            </Button>}
            <Button variant="danger" size={size} loading={busy} onClick={() => void execute(confirming)}>
              {confirming.confirm!.confirmLabel}
            </Button>
            {size !== "md" && <Button size={size} disabled={busy} onClick={cancelConfirm}>
              {confirming.confirm!.keepLabel}
            </Button>}
          </div>
        </div>
      ) : (
        actions.length > 0 && (
          <>
            <div className="work-detail__actions" ref={actionsRef}>
              {(size === "md" ? [...actions].sort((a, b) => Number(!!a.destructive) - Number(!!b.destructive)) : actions).map((action) => (
                <Button
                  key={action.id}
                  data-action-trigger={action.id}
                  variant={action.destructive ? "danger" : "default"}
                  size={size}
                  disabled={busy}
                  onClick={() => onTrigger(action)}
                >
                  {action.label}
                </Button>
              ))}
            </div>
            {error && (
              <Alert status="warning">
                Couldn&apos;t {error.verb} it. Nothing changed.{" "}
                <Button variant="ghost" size={size} disabled={busy} onClick={error.retry}>
                  Try again
                </Button>
              </Alert>
            )}
          </>
        )
      )}

      {techEntries.length > 0 && (
        <Accordion>
          <AccordionItem title="Technical details">
            <dl className="work-detail__tech">
              {techEntries.map(([key, value]) => (
                <div className="work-detail__tech-row" key={key}>
                  <dt>{key}</dt>
                  <dd className="work-detail__tech-value">{value}</dd>
                  <CopyButton label={key} value={value} />
                </div>
              ))}
            </dl>
          </AccordionItem>
        </Accordion>
      )}
    </div>
  );
}

function CopyButton({ label, value }: { label: string; value: string }) {
  const size = useWorkActionSize();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    if (!(await copyText(value))) return;
    setCopied(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1500);
  }
  return (
    <Button variant="ghost" size={size} aria-label={`Copy ${label}`} onClick={() => void copy()}>
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

/** The §4.5 undo toast: "{Type} deleted." + Undo, up to 8 s, pausing on hover or focus. It owns
 *  its own timer (the toast is pushed with `duration: 0`) and clears itself through the DS toast's
 *  public Dismiss affordance (role="status" + aria-label="Dismiss"). */
function UndoToast({ type, token, onUndo, size }: { type: string; token: string; onUndo: (token: string) => Promise<void>; size: "sm" | "md" }) {
  const toast = useToast();
  const ref = useRef<HTMLSpanElement>(null);
  const [busy, setBusy] = useState(false);
  const remaining = useRef(8000);
  const startedAt = useRef<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  function closer(): HTMLButtonElement | null | undefined {
    return ref.current?.closest('[role="status"]')?.querySelector<HTMLButtonElement>('button[aria-label="Dismiss"]');
  }
  function start() {
    startedAt.current = Date.now();
    clearTimeout(timer.current);
    timer.current = setTimeout(() => closer()?.click(), remaining.current);
  }
  function pause() {
    if (startedAt.current == null) return;
    clearTimeout(timer.current);
    remaining.current = Math.max(0, remaining.current - (Date.now() - startedAt.current));
    startedAt.current = null;
  }
  useEffect(() => {
    start();
    return () => clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function undo() {
    if (busy) return;
    setBusy(true);
    clearTimeout(timer.current);
    const dismiss = closer();
    try {
      await onUndo(token);
      toast({ tone: "neutral", message: "Restored." });
    } catch {
      toast({ tone: "warning", message: "Couldn't restore it. The undo window has closed." });
    }
    dismiss?.click();
  }

  return (
    <span
      ref={ref}
      onMouseEnter={pause}
      onMouseLeave={start}
      onFocusCapture={pause}
      onBlurCapture={start}
    >
      {type} deleted.{" "}
      <Button variant="ghost" size={size} disabled={busy} onClick={() => void undo()}>
        Undo
      </Button>
    </span>
  );
}
