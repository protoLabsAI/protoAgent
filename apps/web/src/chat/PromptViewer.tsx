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
import type { PromptCall } from "../lib/types";
import { budgetRows, callTabs, fmtTok, promptText, splitLine, usageLine } from "./promptView";

export function openPromptViewer(taskId: string): void {
  openDocument({
    title: "System prompt",
    subtitle: "what the model actually received on this turn",
    render: () => <PromptViewerBody taskId={taskId} />,
  });
}

function PromptViewerBody({ taskId }: { taskId: string }) {
  const [calls, setCalls] = useState<PromptCall[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "disabled" | "empty" | "error">("loading");
  const [error, setError] = useState("");
  const [active, setActive] = useState("0");
  const [copied, setCopied] = useState(false);

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

  const call = calls.find((c) => String(c.call_index) === active) ?? calls[0];
  const usage = usageLine(call);
  const budget = budgetRows(call);
  const copy = () => {
    void navigator.clipboard.writeText(promptText(call)).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="prompt-viewer">
      {calls.length > 1 ? (
        <Tabs
          variant="segmented"
          responsive
          ariaLabel="model calls this turn"
          items={callTabs(calls)}
          active={active}
          onSelect={setActive}
        />
      ) : null}
      <div className="prompt-viewer__meta">
        {call.model ? <code>{call.model}</code> : null}
        <span>{splitLine(call)}</span>
        {usage ? <span>{usage}</span> : null}
        <span className="prompt-viewer__spacer" />
        <Button size="sm" variant="ghost" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {budget.length ? (
        <div className="prompt-viewer__budget" aria-label="context budget by section">
          {budget.map((row, i) => (
            <div className="prompt-viewer__budget-row" key={i} title={`${row.chars.toLocaleString()} chars`}>
              <span className="prompt-viewer__budget-label">{row.label}</span>
              <span className="prompt-viewer__budget-bar">
                <span
                  className={`prompt-viewer__budget-fill${row.scope === "context" ? " prompt-viewer__budget-fill--context" : ""}`}
                  style={{ width: `${row.pct}%` }}
                />
              </span>
              <span className="prompt-viewer__budget-tokens">≈{fmtTok(row.approx_tokens)}</span>
            </div>
          ))}
        </div>
      ) : null}
      <pre className="prompt-viewer__text">{promptText(call)}</pre>
    </div>
  );
}
