// "View prompt" (#2243) — the exact system prompt any turn's model calls
// received, in the shared DocumentViewer host (one tab per call, raw monospace
// text, copy, real usage). Opened imperatively from the message action row,
// the BackgroundReportCard precedent — no per-message dialog state.

import "./promptviewer.css";
import { Spinner } from "@protolabsai/ui/data";
import { Tabs } from "@protolabsai/ui/navigation";
import { Button } from "@protolabsai/ui/primitives";
import { useEffect, useState } from "react";

import { openDocument } from "../docviewer";
import { api, ApiError } from "../lib/api";
import type { PromptBreakdown, PromptCall, PromptSection } from "../lib/types";
import { Markdown } from "./LazyMarkdown";
import {
  budgetRows,
  callTabs,
  deliveryBudget,
  diffLine,
  fmtTok,
  historyRows,
  historyTopToolsLine,
  overflowLine,
  promptText,
  sectionDiff,
  splitLine,
  usageLine,
} from "./promptView";

/** Tab id for the speculative next-call preview — namespaced so it can't
 *  collide with a call index or a "sub:<i>" id. */
const PREVIEW_TAB = "preview:next";

/** Bar tint per section scope. "projected" (ADR 0108 D2) is its own colour —
 *  falling through to the stable tint painted the per-turn projection as if it
 *  were the cacheable prefix, the one distinction this breakdown exists to show. */
function scopeFill(scope: PromptSection["scope"]): string {
  if (scope === "context") return " prompt-viewer__budget-fill--context";
  if (scope === "projected") return " prompt-viewer__budget-fill--projected";
  return "";
}

export function openPromptViewer(taskId: string, sessionId?: string): void {
  openDocument({
    title: "System prompt",
    subtitle: "what the model actually received on this turn",
    render: () => <PromptViewerBody taskId={taskId} sessionId={sessionId} />,
  });
}

function PromptViewerBody({ taskId, sessionId }: { taskId: string; sessionId?: string }) {
  const [calls, setCalls] = useState<PromptCall[] | null>(null);
  const [subs, setSubs] = useState<PromptCall[]>([]);
  const [prev, setPrev] = useState<PromptCall | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "disabled" | "empty" | "error">("loading");
  const [error, setError] = useState("");
  const [active, setActive] = useState("0");
  const [copied, setCopied] = useState(false);
  // Rendered (markdown) by default — the raw monospace dump was rough to read;
  // Raw stays one click away (and Copy always copies the exact bytes) because
  // this dialog's whole identity is "the exact prompt".
  const [raw, setRaw] = useState(false);
  // History breakdown (#2843) — the session's CURRENT window composition, so it's
  // labeled "as of now" rather than pinned to this turn. Best-effort: absent
  // (older server, dead thread) just hides the block.
  const [history, setHistory] = useState<PromptBreakdown | null>(null);
  // Speculative next-call preview (#2388 P3) — loaded ON DEMAND, never with the
  // dialog: it re-runs the whole dynamic layer (retrieval included), so it is
  // the one tab that costs something to open.
  const [preview, setPreview] = useState<PromptCall | null>(null);
  const [previewState, setPreviewState] = useState<"idle" | "loading" | "error" | "unavailable">("idle");
  const [previewNote, setPreviewNote] = useState("");

  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    api
      .promptBreakdown(sessionId)
      .then((res) => {
        if (alive && res.found && res.breakdown) setHistory(res.breakdown);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [sessionId]);

  useEffect(() => {
    let alive = true;
    api
      .promptsForTask(taskId)
      .then((res) => {
        if (!alive) return;
        if (!res.enabled) {
          setStatus("disabled");
        } else {
          setCalls(res.calls);
          setSubs(res.subagents ?? []);
          setPrev(res.prev ?? null);
          setActive(String(res.calls[0]?.call_index ?? 0));
          setStatus("ready");
        }
      })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) {
          setStatus("empty");
        } else {
          setError(e instanceof Error ? e.message : String(e));
          setStatus("error");
        }
      });
    return () => {
      alive = false;
    };
  }, [taskId]);

  // Fetch the preview the first time its tab is selected, then keep it.
  useEffect(() => {
    if (active !== PREVIEW_TAB || preview || previewState !== "idle") return;
    let alive = true;
    setPreviewState("loading");
    api
      .promptPreview(sessionId ?? "")
      .then((res) => {
        if (!alive) return;
        if (res.call) {
          setPreview(res.call);
          setPreviewState("idle");
        } else {
          setPreviewNote(res.reason || (res.enabled ? "" : "prompt capture is off"));
          setPreviewState("unavailable");
        }
      })
      .catch((e) => {
        if (!alive) return;
        setPreviewNote(e instanceof Error ? e.message : String(e));
        setPreviewState("error");
      });
    return () => {
      alive = false;
    };
  }, [active, preview, previewState, sessionId]);

  if (status === "loading") {
    return (
      <div className="prompt-viewer__status">
        <Spinner size={16} /> Loading captured prompt…
      </div>
    );
  }
  if (status === "disabled") {
    return (
      <div className="prompt-viewer__status">
        Prompt capture is off — enable <code>prompts.capture</code> in Settings ▸ Telemetry to
        record what each model call receives.
      </div>
    );
  }
  if (status === "empty") {
    return (
      <div className="prompt-viewer__status">
        No prompt snapshots for this turn — it ran before capture was on, or the snapshots were
        trimmed by retention.
      </div>
    );
  }
  if (status === "error" || !calls) {
    return <div className="prompt-viewer__status">Couldn't load the prompt — {error}</div>;
  }

  // Tabs: main-loop calls, then any subagent calls nested under this turn (#2388 P3,
  // keyed "sub:<i>" so they can't collide with main call indexes), then the
  // speculative next-call preview (keyed PREVIEW_TAB, loaded on selection).
  const isPreview = active === PREVIEW_TAB;
  // The preview tab with nothing loaded must NOT fall through to calls[0] — that
  // would render this turn's first call under a "speculative next call" banner.
  const previewPending = isPreview && !preview;
  const call =
    (isPreview ? (preview ?? undefined) : undefined) ??
    (active.startsWith("sub:") ? subs[Number(active.slice(4))] : undefined) ??
    calls.find((c) => String(c.call_index) === active) ??
    calls[0];
  const usage = usageLine(call);
  const budget = budgetRows(call);
  const delivery = deliveryBudget(call);
  const overflow = overflowLine(call);
  // Diff anchor (#2388 P3): a later call diffs against the previous call of the SAME
  // turn (in-payload); the first call diffs against the previous TURN's last call
  // (`prev` — null on the first turn, an incognito gap, or a retention trim). Only
  // meaningful when both sides carry P2 section rows; degrade honestly otherwise.
  // The preview has no anchor: it is the NEXT call, not one of this turn's.
  const noAnchor = active.startsWith("sub:") || isPreview;
  const anchorCall = noAnchor ? null : (calls.find((c) => c.call_index === call.call_index - 1) ?? prev);
  const anchorName = noAnchor
    ? ""
    : calls.some((c) => c.call_index === call.call_index - 1)
      ? `call ${call.call_index}`
      : "previous turn";
  const deltas =
    anchorCall && anchorCall.sections?.length && call.sections?.length
      ? sectionDiff(anchorCall.sections, call.sections)
      : null;
  const copy = () => {
    void navigator.clipboard.writeText(promptText(call)).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="prompt-viewer">
      {calls.length > 1 || subs.length || sessionId ? (
        <Tabs
          variant="segmented"
          responsive
          ariaLabel="model calls this turn"
          items={[
            ...callTabs(calls),
            ...subs.map((s, i) => ({ id: `sub:${i}`, label: s.subagent_type || "subagent" })),
            // Only offered when we know the session to preview FOR — the route
            // composes against that session's own history and exclusion rules.
            ...(sessionId ? [{ id: PREVIEW_TAB, label: "Next call (preview)" }] : []),
          ]}
          active={active}
          onSelect={setActive}
        />
      ) : null}
      {isPreview ? (
        <div className="prompt-viewer__speculative" role="note">
          Speculative — this runs the dynamic layer (retrieval included) for the NEXT call. No model
          call was made and nothing was written to the injection log, so what actually ships can
          still differ.
        </div>
      ) : null}
      {previewPending ? (
        <div className="prompt-viewer__status">
          {previewState === "loading" ? (
            <>
              <Spinner size={16} /> Composing the next call…
            </>
          ) : (
            `Couldn't preview the next call${previewNote ? ` — ${previewNote}` : ""}.`
          )}
        </div>
      ) : (
        <>
      <div className="prompt-viewer__meta">
        {call.model ? <code>{call.model}</code> : null}
        <span>{splitLine(call)}</span>
        {usage ? <span>{usage}</span> : null}
        <span className="prompt-viewer__spacer" />
        <Button size="sm" variant="ghost" onClick={() => setRaw((r) => !r)}>
          {raw ? "Rendered" : "Raw"}
        </Button>
        <Button size="sm" variant="ghost" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {!noAnchor ? (
        <div className="prompt-viewer__diff" aria-label="changes vs the comparison call">
          {diffLine(deltas, anchorName)}
        </div>
      ) : null}
      {delivery ? (
        <div className="prompt-viewer__delivery-budget" aria-label="delivery budget">
          <div className="prompt-viewer__history-head">
            Delivery budget — {fmtTok(delivery.used)} of {fmtTok(delivery.chars)} chars used,{" "}
            {fmtTok(delivery.headroom)} spare{" "}
            <span className="prompt-viewer__history-note">(ADR 0108 D6 — the ceiling on projected context)</span>
          </div>
          <div className="prompt-viewer__budget-row">
            <span className="prompt-viewer__budget-label">delivered</span>
            <span className="prompt-viewer__budget-bar">
              <span
                className={`prompt-viewer__budget-fill prompt-viewer__budget-fill--delivery${delivery.overflow.length ? " prompt-viewer__budget-fill--shed" : ""}`}
                style={{ width: `${delivery.pct}%` }}
              />
            </span>
            <span className="prompt-viewer__budget-tokens">{delivery.pct}%</span>
          </div>
          {overflow ? (
            <div className="prompt-viewer__overflow" role="note">
              Shed to fit: {overflow}
            </div>
          ) : null}
        </div>
      ) : null}
      {budget.length ? (
        <div className="prompt-viewer__budget" aria-label="section sizes">
          {budget.map((row, i) => (
            <div
              className="prompt-viewer__budget-row"
              key={i}
              title={`${row.chars.toLocaleString()} chars${row.truncated ? " — shed by the delivery budget" : ""}`}
            >
              <span className="prompt-viewer__budget-label">
                {row.label}
                {row.truncated ? <span className="prompt-viewer__shed" title="shed by the delivery budget"> ✂</span> : null}
              </span>
              <span className="prompt-viewer__budget-bar">
                <span
                  className={`prompt-viewer__budget-fill${scopeFill(row.scope)}${row.truncated ? " prompt-viewer__budget-fill--shed" : ""}`}
                  style={{ width: `${row.pct}%` }}
                />
              </span>
              <span className="prompt-viewer__budget-tokens">≈{fmtTok(row.approx_tokens)}</span>
            </div>
          ))}
        </div>
      ) : null}
      {history ? (
        <div className="prompt-viewer__budget" aria-label="conversation history composition (as of now)">
          <div className="prompt-viewer__history-head">
            Conversation history — ≈{fmtTok(history.total_est_tokens)} tok across {history.message_count} messages{" "}
            <span className="prompt-viewer__history-note">(as of now, not this call)</span>
          </div>
          {historyRows(history).map((row, i) => (
            <div className="prompt-viewer__budget-row" key={i} title={`≈${row.approx_tokens.toLocaleString()} tokens`}>
              <span className="prompt-viewer__budget-label">{row.label}</span>
              <span className="prompt-viewer__budget-bar">
                <span className="prompt-viewer__budget-fill prompt-viewer__budget-fill--context" style={{ width: `${row.pct}%` }} />
              </span>
              <span className="prompt-viewer__budget-tokens">≈{fmtTok(row.approx_tokens)}</span>
            </div>
          ))}
          {historyTopToolsLine(history) ? (
            <div className="prompt-viewer__history-note">{historyTopToolsLine(history)}</div>
          ) : null}
        </div>
      ) : null}
      {call.system.wire_differs ? (
        <div className="prompt-viewer__wire" role="alert">
          {call.system.wire ? (
            <>
              <div className="prompt-viewer__wire-head">
                The wire carried a transformed version of this prompt (provider shape) — the
                composed text below is what the agent assembled; what was delivered:
              </div>
              <details className="prompt-viewer__wire-details">
                <summary>Show delivered text</summary>
                <pre className="prompt-viewer__text">{call.system.wire}</pre>
              </details>
            </>
          ) : (
            <div className="prompt-viewer__wire-head prompt-viewer__wire-head--alarm">
              ⚠ No system text reached the wire on this call — the composed prompt below was
              NOT delivered to the model.
            </div>
          )}
        </div>
      ) : null}
      {call.delivery ? <div className="prompt-viewer__delivery">{call.delivery}</div> : null}
      {raw ? (
        <pre className="prompt-viewer__text">{promptText(call)}</pre>
      ) : (
        <div className="prompt-viewer__md">
          <Markdown>{promptText(call)}</Markdown>
        </div>
      )}
        </>
      )}
    </div>
  );
}
