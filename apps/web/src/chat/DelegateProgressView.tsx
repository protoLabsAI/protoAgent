import "./delegate-progress.css";
import { Spinner } from "@protolabsai/ui/data";
import { Check, Circle, CircleDot, X } from "lucide-react";

import type { DelegateProgress, DelegatePlanEntry, DelegateTool } from "../lib/types";
import { planProgress, toolLine } from "./delegateProgress";

// A coding delegate's live view on its delegation card (#3979): its own plan as a
// checklist, the tool it is running now, the last few it ran, and the tail of what it is
// saying. The same shape the Project Board drawer shows for a board coder — this is that
// view for a delegation the operator or the lead asked for in chat. Everything here is
// bounded server-side; the view only chooses how much of it to show.

/** How many recent tools render under the current one (the snapshot carries a few more). */
const RECENT_SHOWN = 4;
/** Narration tail shown while the delegate works — the last sentence or so. */
const TEXT_SHOWN = 220;

function PlanGlyph({ status }: { status: string }) {
  if (status === "completed") return <Check size={12} aria-label="done" className="dp-glyph dp-glyph--done" />;
  if (status === "in_progress") return <CircleDot size={12} aria-label="in progress" className="dp-glyph dp-glyph--active" />;
  return <Circle size={12} aria-label="pending" className="dp-glyph" />;
}

function ToolGlyph({ status, live }: { status: string; live: boolean }) {
  if (status === "failed") return <X size={12} aria-label="failed" className="dp-glyph dp-glyph--failed" />;
  if (status === "running" && live) return <Spinner size={10} />;
  return <Check size={12} aria-label="done" className="dp-glyph dp-glyph--done" />;
}

function Tool({ tool, live, current = false }: { tool: DelegateTool; live: boolean; current?: boolean }) {
  const loc = tool.locations?.[0];
  return (
    <li className={`dp-tool${current ? " dp-tool--current" : ""}`} title={loc?.path}>
      <ToolGlyph status={tool.status} live={live} />
      {tool.kind ? <span className="dp-kind">{tool.kind}</span> : null}
      <span className="dp-tool-name">{toolLine(tool)}</span>
    </li>
  );
}

function Plan({ entries }: { entries: DelegatePlanEntry[] }) {
  return (
    <ol className="dp-plan" aria-label="Plan">
      {entries.map((e, i) => (
        <li key={i} className={`dp-plan-entry dp-plan-entry--${e.status || "pending"}`}>
          <PlanGlyph status={e.status} />
          <span>{e.content}</span>
        </li>
      ))}
    </ol>
  );
}

/** One delegate's progress. `live` = its card is still running: the current tool spins
 *  and the narration tail shows; settled, it reads as the run's final state. */
export function DelegateProgressView({
  progress,
  live,
  showTarget = false,
}: {
  progress: DelegateProgress;
  live: boolean;
  /** Name the delegate — when one card covers several (`@a @b`). */
  showTarget?: boolean;
}) {
  const running = live && !progress.done;
  const current = running && progress.currentTool?.status === "running" ? progress.currentTool : undefined;
  // The current tool is the newest recent row too — don't list it twice.
  const recent = progress.recentTools
    .filter((t) => !(current && t.id !== undefined && t.id === current.id))
    .slice(-RECENT_SHOWN)
    .reverse();
  const plan = progress.plan ?? [];
  const planLabel = planProgress(progress);
  const text = running ? (progress.text ?? "").trim() : "";
  const tail = text.length > TEXT_SHOWN ? `…${text.slice(-TEXT_SHOWN)}` : text;
  if (!plan.length && !current && !recent.length && !tail) return null;
  return (
    <div className={`delegate-progress${running ? " delegate-progress--live" : ""}`} aria-live="polite">
      <div className="dp-head">
        {showTarget ? <span className="dp-target">@{progress.target}</span> : null}
        {planLabel ? <span className="dp-count">plan {planLabel}</span> : null}
        {progress.toolCount ? (
          <span className="dp-count">
            {progress.toolCount} {progress.toolCount === 1 ? "tool call" : "tool calls"}
          </span>
        ) : null}
        {!running && !progress.ok ? <span className="dp-failed">did not finish</span> : null}
      </div>
      {plan.length ? <Plan entries={plan} /> : null}
      {current || recent.length ? (
        <ul className="dp-tools" aria-label="Tool calls">
          {current ? <Tool tool={current} live current /> : null}
          {recent.map((t, i) => (
            <Tool key={`${t.id ?? t.name}-${i}`} tool={t} live={running} />
          ))}
        </ul>
      ) : null}
      {tail ? <p className="dp-text">{tail}</p> : null}
    </div>
  );
}

/** Every delegate's progress on one card (a mention card can address several). */
export function DelegateProgressList({
  progress,
  live,
}: {
  progress: Record<string, DelegateProgress> | undefined;
  live: boolean;
}) {
  const all = Object.values(progress ?? {});
  if (!all.length) return null;
  return (
    <>
      {all.map((p) => (
        <DelegateProgressView key={p.target} progress={p} live={live} showTarget={all.length > 1} />
      ))}
    </>
  );
}
