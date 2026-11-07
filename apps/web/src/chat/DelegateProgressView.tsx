import "./delegate-progress.css";
import { ToolCard, ToolCardList } from "@protolabsai/ui/tool-card";
import { Check, Circle, CircleDot } from "lucide-react";

import type { DelegateProgress, DelegatePlanEntry, DelegateTool } from "../lib/types";
import { narrationTail, planProgress, toolLine } from "./delegateProgress";

// A coding delegate's live view on its delegation card (#3979): its own plan as a
// checklist, the tool it is running now, the last few it ran, and the tail of what it is
// saying. The same shape the Project Board drawer shows for a board coder — this is that
// view for a delegation the operator or the lead asked for in chat. Everything here is
// bounded server-side; the view only chooses how much of it to show.

/** How many recent tools render under the current one (the snapshot carries a few more). */
const RECENT_SHOWN = 4;
/** Narration tail shown while the delegate works — the last sentence or so. */
const TEXT_SHOWN = 220;
/** While live, the plan never changes size: at most this many entries render (a window
 *  around the in-progress one) with a "+N more" line standing in for the rest. */
const PLAN_SHOWN = 5;

function PlanGlyph({ status }: { status: string }) {
  if (status === "completed") return <Check size={12} aria-label="done" className="dp-glyph dp-glyph--done" />;
  if (status === "in_progress") return <CircleDot size={12} aria-label="in progress" className="dp-glyph dp-glyph--active" />;
  return <Circle size={12} aria-label="pending" className="dp-glyph" />;
}

/** The DS ToolCard status for a delegate tool — the same mapping the old ToolGlyph used:
 *  a failure is an error, a tool running RIGHT NOW (live) spins, everything else reads done. */
function toolStatus(status: string, live: boolean): "running" | "done" | "error" {
  if (status === "failed") return "error";
  if (status === "running" && live) return "running";
  return "done";
}

/** One tool call as a DS ToolCard: the status glyph + a mono name slot carrying the quiet
 *  `kind` prefix and the `toolLine`. Body-less (not expandable) and duration-less — a
 *  DelegateTool has neither. The row's hooks (`dp-kind`, `dp-tool-name`, `dp-tool--current`)
 *  are preserved so the existing styling and tests keep working. */
function Tool({ tool, live, current = false }: { tool: DelegateTool; live: boolean; current?: boolean }) {
  const loc = tool.locations?.[0];
  return (
    <ToolCard
      status={toolStatus(tool.status, live)}
      className={`dp-tool${current ? " dp-tool--current" : ""}`}
      name={
        <>
          {tool.kind ? <span className="dp-kind">{tool.kind}</span> : null}
          <span className="dp-tool-name" title={loc?.path}>
            {toolLine(tool)}
          </span>
        </>
      }
    />
  );
}

/** A reserved, aria-hidden row slot so the live tool list keeps a fixed height. It wraps a
 *  hidden collapsed ToolCard so the slot is EXACTLY the height a real row will be — a slot
 *  filling in never changes the list's height (the #4246 fixed-footprint contract). */
function EmptyTool() {
  return (
    <div className="dp-tool dp-tool--empty" aria-hidden="true">
      <ToolCard status="done" name={"\u00a0"} />
    </div>
  );
}

/** The capped window of plan entries shown while live: at most PLAN_SHOWN entries centred
 *  on the in-progress one (so the count never shifts as statuses advance), plus the count
 *  of entries left out. Under the cap every entry shows and `more` is 0. */
function planWindow(entries: DelegatePlanEntry[]): { shown: DelegatePlanEntry[]; more: number } {
  if (entries.length <= PLAN_SHOWN) return { shown: entries, more: 0 };
  const active = entries.findIndex((e) => e.status === "in_progress");
  const pending = active >= 0 ? active : entries.findIndex((e) => e.status !== "completed");
  const pivot = pending >= 0 ? pending : entries.length - 1;
  const start = Math.max(0, Math.min(pivot - Math.floor(PLAN_SHOWN / 2), entries.length - PLAN_SHOWN));
  return { shown: entries.slice(start, start + PLAN_SHOWN), more: entries.length - PLAN_SHOWN };
}

function Plan({ entries, windowed }: { entries: DelegatePlanEntry[]; windowed: boolean }) {
  const { shown, more } = windowed ? planWindow(entries) : { shown: entries, more: 0 };
  return (
    <ol className="dp-plan" aria-label="Plan">
      {shown.map((e, i) => (
        <li key={i} className={`dp-plan-entry dp-plan-entry--${e.status || "pending"}`}>
          <PlanGlyph status={e.status} />
          <span>{e.content}</span>
        </li>
      ))}
      {more > 0 ? <li className="dp-plan-more">+{more} more</li> : null}
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
  const tail = narrationTail(text, TEXT_SHOWN);
  // Settled and empty renders nothing (today's behaviour). While live the view keeps a
  // fixed footprint, so it stays mounted even before the first tool/plan/word lands.
  if (!running && !plan.length && !current && !recent.length && !tail) return null;
  // While live, pad the tool list to a constant 1 + RECENT_SHOWN slots (a current slot —
  // empty when nothing is running — plus RECENT_SHOWN recent slots) so it can't bounce.
  const pad = running ? RECENT_SHOWN - recent.length : 0;
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
      {plan.length ? <Plan entries={plan} windowed={running} /> : null}
      {running ? (
        <div aria-label="Tool calls">
          <ToolCardList className="dp-tools">
            {current ? <Tool tool={current} live current /> : <EmptyTool />}
            {recent.map((t, i) => (
              <Tool key={`${t.id ?? t.name}-${i}`} tool={t} live={running} />
            ))}
            {Array.from({ length: pad }, (_, i) => (
              <EmptyTool key={`empty-${i}`} />
            ))}
          </ToolCardList>
        </div>
      ) : recent.length ? (
        <div aria-label="Tool calls">
          <ToolCardList className="dp-tools">
            {recent.map((t, i) => (
              <Tool key={`${t.id ?? t.name}-${i}`} tool={t} live={running} />
            ))}
          </ToolCardList>
        </div>
      ) : null}
      {running ? <p className="dp-text">{tail}</p> : null}
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
