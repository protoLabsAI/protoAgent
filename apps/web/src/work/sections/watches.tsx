// The Watches section of the Work view (#4955 §2.6, §5). Registered on import — the side
// effect is the whole wiring; WorkView mounts it in a later card. ONE `render(ctx)` serves both
// panes: a falsy `ctx.state.itemId` lists the watches as `WorkRow`s, a set one finds the watch
// and shows its `WorkDetail`. Every field, its order and its wording come from the §2.6 table;
// the id, the raw status, the verifier, the run session and the raw evidence live only in
// Technical details.

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WorkEmpty as Empty } from "../WorkEmpty";
import { Alert } from "@protolabsai/ui/data";
import { Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";

import { registerWorkSection, type WorkSectionContext } from "../WorkView";
import type { WorkSection } from "../sectionRegistry";
import { WorkRow } from "../WorkRow";
import { WorkDetail, type WorkAction, type WorkField } from "../WorkDetail";
import { WorkEditDialog } from "../WorkCreateDialog";
import { draftFromWatch } from "../workPersistence";
import { relTime } from "../tone";
import { watchKind } from "../rowMeta";
import type { WorkItem } from "../useWorkItems";
import { api } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import type { WatchState } from "../../lib/types";

// §2.3 — a timestamp as local relative time, full stamp in a hover tooltip. Watches store
// epoch seconds, which `relTime` already expects.
function when(epoch: number | null | undefined, prefix: string): WorkField["value"] {
  if (epoch == null) return "";
  const { text, tooltip } = relTime(epoch);
  if (!text) return "";
  return (
    <span title={tooltip}>
      {prefix}
      {text}
    </span>
  );
}

// "5 min" / "2 h" / "7 d" for display.
function humanizeDuration(s: number): string {
  if (s % 86400 === 0) return `${s / 86400} d`;
  if (s % 3600 === 0) return `${s / 3600} h`;
  if (s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}

// §2.6 field 3 — the operator-facing collapse of trigger × repeat.
function firedText(w: WatchState): string {
  const fired = w.fire_count ?? 0;
  if (fired <= 0) return "";
  let text = `Fired ${fired} time${fired === 1 ? "" : "s"}`;
  const consecutive = w.consecutive_fires ?? 0;
  // The flapping signal: only worth the extra words once it's fired 3+ times back-to-back.
  if (consecutive >= 3) text += ` · ${consecutive} in a row`;
  return text;
}

function lastCheckedText(w: WatchState): WorkField["value"] {
  if (w.last_checked == null) return "";
  const { text, tooltip } = relTime(w.last_checked);
  if (!text) return "";
  const count = w.check_count ? ` · ${w.check_count} checks` : "";
  return (
    <span title={tooltip}>
      Checked {text}
      {count}
    </span>
  );
}

function deadlineText(w: WatchState): WorkField["value"] {
  if (w.deadline == null) return "";
  const { text, tooltip } = relTime(w.deadline);
  if (!text) return "";
  const past = w.deadline * 1000 <= Date.now();
  return <span title={tooltip}>{past ? `Expired ${text}` : `Until ${text}`}</span>;
}

function finishedText(w: WatchState): WorkField["value"] {
  if (w.finished_at == null) return "";
  const verb = w.status === "met" ? "Met " : w.status === "expired" ? "Expired " : "Finished ";
  return when(w.finished_at, verb);
}

// The §2.6 table, fields 3–10 (1 = Condition is the title, 2 = Status is the badge). An empty
// value hides its line.
function watchFields(w: WatchState): WorkField[] {
  return [
    { label: "Kind", value: watchKind(w) },
    { label: "Last checked", value: lastCheckedText(w) },
    { label: "Fired", value: firedText(w) },
    { label: "Then", value: w.run_prompt ? `When met, the agent will: ${w.run_prompt}` : "" },
    { label: "Deadline", value: deadlineText(w) },
    { label: "Latest check", value: w.last_reason ? `Last check: ${w.last_reason}` : "" },
    { label: "Cadence", value: w.interval_s ? `Checks every ${humanizeDuration(w.interval_s)}` : "" },
    {
      label: "Stall alert",
      value: w.stall_after ? `Tells you after ${w.stall_after} checks with no change` : "",
    },
    { label: "Set", value: when(w.created_at, "Set ") },
    { label: "Finished", value: finishedText(w) },
  ];
}

// §2.6 "Never shown" → Technical details: id, raw status, verifier type + args, run session,
// raw evidence.
function watchTechnical(w: WatchState): Record<string, string> {
  const tech: Record<string, string> = { Id: w.id, Status: w.status };
  if (w.verifier) tech.Verifier = JSON.stringify(w.verifier);
  if (w.run_session) tech["Run session"] = w.run_session;
  if (w.last_evidence) tech["Last evidence"] = w.last_evidence;
  return tech;
}

function WatchDetailView({ ctx, item }: { ctx: WorkSectionContext; item: WorkItem }) {
  void ctx;
  const qc = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const w = item.raw as WatchState;
  const refresh = () => void qc.invalidateQueries({ queryKey: queryKeys.watches });

  // Edit FIRST (§10.7): the same prefilled step-2 form the New flow confirms with — no
  // model call. Saving PATCHes THIS watch id in place, so its history and check count
  // survive; the watch is never cleared and recreated (the merge-watch-874 regression).
  const actions: WorkAction[] = [
    { id: "edit", label: "Edit", run: async () => void setEditing(true) },
    {
      id: "delete",
      label: "Delete…",
      destructive: true,
      confirm: {
        prompt: "Delete this watch?",
        explanation: "The agent stops checking for it. You can undo this for a short while.",
        confirmLabel: "Delete",
        keepLabel: "Keep it",
      },
      run: async () => {
        const res = await api.clearWatch(w.id);
        refresh();
        return { undoToken: res.undo_token };
      },
    },
  ];

  return (
    <>
      <WorkDetail
        title={w.condition || w.id}
        status={item.status}
        fields={watchFields(w)}
        actions={actions}
        technical={watchTechnical(w)}
        type="Watch"
        onUndo={async (token) => {
          await api.undo(token);
          refresh();
        }}
      />
      {editing && (
        <WorkEditDialog
          draft={draftFromWatch(w)}
          target={{ kind: "condition", id: w.id }}
          onClose={() => setEditing(false)}
          onSaved={() => {
            refresh();
            toast({ tone: "success", message: "Watch updated." });
          }}
        />
      )}
    </>
  );
}

function WatchesList({ ctx }: { ctx: WorkSectionContext }) {
  const source = ctx.sources.watches;
  if (source.status === "error") {
    return (
      <Alert status="warning">
        Couldn&apos;t load watches. The agent still has them. Only this view failed.{" "}
        <Button variant="ghost" size="sm" onClick={() => source.retry()}>
          Retry
        </Button>
      </Alert>
    );
  }
  if (source.items.length === 0) {
    return <Empty title="No watches" description="The agent sets one when it needs to wait for something." />;
  }
  return (
    <>
      <ul className="work-group">
        {source.items.map((item) => (
          <li key={item.id}>
            <WorkRow
              status={item.status}
              title={item.title}
              meta={watchKind(item.raw as WatchState)}
              focusKey={`${item.kind}:${item.id}`}
              onOpen={() => ctx.onOpen(item)}
            />
          </li>
        ))}
      </ul>
    </>
  );
}

export function WatchesSection({ ctx }: { ctx: WorkSectionContext }) {
  const itemId = ctx.state.itemId;
  if (itemId) {
    const item = ctx.sources.watches.items.find((i) => `${i.kind}:${i.id}` === itemId || i.id === itemId);
    if (!item) return <Empty title="This watch is no longer here." />;
    return <WatchDetailView ctx={ctx} item={item} />;
  }
  return <WatchesList ctx={ctx} />;
}

export const watchesSection = { label: "Watches", group: "coming-up", render: (ctx: WorkSectionContext) => <WatchesSection ctx={ctx} /> } satisfies WorkSection;

registerWorkSection("watches", watchesSection);
