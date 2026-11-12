// The Goals detail of the Work view (#4955 §2.5, work-consolidation v2). Registered on import —
// the side effect is the whole wiring. The four-group consolidation moved the LIST to the shared
// group rows (workGroups + WorkOverview), so `render(ctx)` serves only the detail pane: it finds
// the goal `ctx.state.itemId` names and shows its `WorkDetail`. Every field, its order and its
// wording come from the §2.5 table; ids, the raw status, the verifier and the per-turn evidence
// live only in Technical details.

import { useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WorkEmpty as Empty } from "../WorkEmpty";
import { useToast } from "@protolabsai/ui/overlays";

import { registerWorkSection, type WorkSection, type WorkSectionContext } from "../sectionRegistry";
import { WorkDetail, type WorkAction, type WorkField } from "../WorkDetail";
import { goalProgress } from "../rowMeta";
import { groupFor, relTime, type GroupableWorkItem } from "../tone";
import type { WorkItem } from "../useWorkItems";
import { useUI } from "../../state/uiStore";
import { api } from "../../lib/api";
import { queryKeys } from "../../lib/queries";
import { errMsg } from "../../lib/format";
import { GoalCreateDialog } from "../../app/GoalsPanel";
import { HitlForm } from "../../chat/HitlForm";
import {
  DEFAULT_MAX_ITERATIONS,
  goalFormPayload,
  goalSetDetail,
  parseMaxIterations,
  splitLines,
  type GoalSetBody,
} from "../../chat/goalForm";
import type { GoalEvent, GoalState, HitlPayload } from "../../lib/types";

// How many iterations the Extend action grants an active goal at once — matches the base
// `GoalDetailDrawer` (ADR 0079): the running loop picks up the higher cap.
const EXTEND_BY = 4;

// §2.3 — every timestamp is local relative time ("10 min ago", "today at 17:14"), with the
// full stamp in a hover tooltip. Goals store epoch seconds, which `relTime` already expects.
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

function bullets(items: string[] | undefined): WorkField["value"] {
  if (!items || items.length === 0) return "";
  return (
    <ul>
      {items.map((t, i) => (
        <li key={i}>{t}</li>
      ))}
    </ul>
  );
}

// §2.5 field 10 — the raw per-iteration `status` ("continue" …) is never shown; it reads as
// prose, and the terminal turn says "achieved", "ran out of turns" or "couldn't be done".
function historyWord(status: string): string {
  switch (status) {
    case "achieved":
      return "achieved";
    case "exhausted":
      return "ran out of turns";
    case "unachievable":
      return "couldn't be done";
    default:
      return "kept going";
  }
}

function history(events: GoalEvent[] | undefined): WorkField["value"] {
  if (!events || events.length === 0) return "";
  return (
    <ul>
      {events.map((e, i) => (
        <li key={i}>
          Turn {e.iteration} · {historyWord(e.status)}
          {e.reason ? ` · ${e.reason}` : ""}
        </li>
      ))}
    </ul>
  );
}

// A goal that stopped short of its finish line (exhausted / unachievable) is exactly the goal
// `groupFor` sends to Needs you — deriving the "Goal stopped" tag, its NEUTRAL tone (§2, §6)
// and the Keep going / Drop the goal pair from the shared selector keeps them in step with
// where the row actually sits.
function goalStopped(item: WorkItem): boolean {
  return groupFor(item as GroupableWorkItem) === "needs-you";
}

// The §2.5 table, fields 3–11 (1 = Goal is the title, 2 = Status is the badge), led by the
// finish line (§2). An empty value hides its line (WorkDetail drops it — never a dash).
function goalFields(g: GoalState): WorkField[] {
  const streak = g.no_progress_streak ?? 0;
  const stalled =
    streak > 0
      ? `No progress for ${streak}${g.no_progress_limit ? ` of ${g.no_progress_limit}` : ""} checks`
      : "";
  return [
    { label: "Finish line", value: g.outcome?.trim() || g.condition || "" },
    { label: "Progress", value: goalProgress(g) },
    { label: "Stalled", value: stalled },
    { label: "Latest check", value: g.last_reason ? `Last check: ${g.last_reason}` : "" },
    { label: "Started", value: when(g.started_at, "Started ") },
    { label: "Finished", value: when(g.finished_at, "Finished ") },
    { label: "Stop rule", value: g.stop_when ? `Stops and asks you if: ${g.stop_when}` : "" },
    { label: "Must not change", value: bullets(g.constraints) },
    { label: "In scope", value: bullets(g.boundaries) },
    { label: "Why it stopped", value: g.abandon_reason ? `The agent stopped: ${g.abandon_reason}` : "" },
    { label: "History", value: history(g.history) },
    { label: "Fresh context", value: g.fresh_context ? "Each turn starts a clean thread" : "" },
  ];
}

// §2.5 "Never shown" → Technical details: session id, the raw status, verifier type + args,
// and the raw evidence. Copyable, collapsed at the bottom of the detail.
function goalTechnical(g: GoalState): Record<string, string> {
  const tech: Record<string, string> = { "Session id": g.session_id, Status: g.status };
  if (g.verifier) tech.Verifier = JSON.stringify(g.verifier);
  if (g.last_evidence) tech["Last evidence"] = g.last_evidence;
  return tech;
}

// The Edit form reuses the goal creator's completion-contract fields (goalForm step 2) —
// outcome / constraints / boundaries / stop rule / budget — pre-filled with the goal's current
// values so it opens ready to tweak. These are exactly the fields `PATCH /api/goals` accepts.
function editGoalPayload(g: GoalState): HitlPayload {
  const contract = goalFormPayload().steps![1];
  const props = (contract.schema as { properties: Record<string, Record<string, unknown>> }).properties;
  const withDefault = (key: string, value: unknown) => ({ ...props[key], default: value });
  return {
    kind: "form",
    title: "Edit goal",
    description: contract.description,
    steps: [
      {
        title: "Edit goal",
        schema: {
          type: "object",
          properties: {
            outcome: withDefault("outcome", g.outcome ?? ""),
            constraints: withDefault("constraints", (g.constraints ?? []).join("\n")),
            boundaries: withDefault("boundaries", (g.boundaries ?? []).join("\n")),
            stop_when: withDefault("stop_when", g.stop_when ?? ""),
            max_iterations: withDefault("max_iterations", g.max_iterations ?? DEFAULT_MAX_ITERATIONS),
          },
        },
      },
    ],
  };
}

function GoalDetailView({ ctx, item }: { ctx: WorkSectionContext; item: WorkItem }) {
  void ctx;
  const qc = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const g = item.raw as GoalState;
  const refresh = () => void qc.invalidateQueries({ queryKey: queryKeys.goals });

  if (editing) {
    return (
      <div className="work-detail" data-testid="goal-edit">
        <HitlForm
          payload={editGoalPayload(g)}
          busy={saving}
          onCancel={() => setEditing(false)}
          onSubmit={(answers) => {
            const a = typeof answers === "object" && answers ? (answers as Record<string, unknown>) : {};
            setSaving(true);
            api
              .editGoal(g.session_id, {
                outcome: String(a.outcome ?? "").trim(),
                constraints: splitLines(a.constraints),
                boundaries: splitLines(a.boundaries),
                stop_when: String(a.stop_when ?? "").trim(),
                max_iterations: parseMaxIterations(a.max_iterations),
              })
              .then(() => {
                refresh();
                toast({ tone: "success", message: "Goal updated." });
                setEditing(false);
              })
              .catch((e) => toast({ tone: "error", title: "Couldn't update the goal", message: errMsg(e) }))
              .finally(() => setSaving(false));
          }}
        />
      </div>
    );
  }

  const title = g.outcome?.trim() || g.condition || g.session_id;
  const active = item.status === "running" || item.status === "waiting";
  const stopped = goalStopped(item);
  const actions: WorkAction[] = [{ id: "edit", label: "Edit", run: async () => void setEditing(true) }];

  // Lifecycle (ADR 0079), restoring the base `GoalDetailDrawer`'s re-arm controls so a stopped
  // goal can be reached from the Work surface. An ACTIVE goal gets more room (+N iterations — the
  // running loop picks up the higher cap); a STOPPED one (exhausted / unachievable) is told to
  // Keep going (the backend resets the loop and kicks a fresh drive turn, so the shared refresh
  // re-groups it into Doing now). An achieved goal shows neither — it has left Work. Re-arm has
  // no undo, so feedback is a toast + the field table refresh — not the §4.5 undo flow.
  const rearm = (body: { add_iterations?: number }) => async () => {
    try {
      const res = await api.rearmGoal(g.session_id, body);
      refresh();
      toast({ tone: "success", title: "Goal re-armed", message: res.message || "The drive loop will resume." });
    } catch (e) {
      toast({ tone: "error", title: "Couldn't re-arm the goal", message: errMsg(e) });
    }
  };
  if (g.status === "active") {
    actions.push({ id: "extend", label: `Add ${EXTEND_BY} iterations`, run: rearm({ add_iterations: EXTEND_BY }) });
  } else if (stopped) {
    actions.push({ id: "restart", label: "Keep going", run: rearm({}) });
  }

  // An active goal can be Stopped (stops the drive loop and closes its tasks, §4.4 confirm);
  // any goal can be Deleted, which hands back an undo token for the §4.5 Undo toast.
  if (active) {
    actions.push({
      id: "stop",
      label: "Stop…",
      destructive: true,
      confirm: {
        prompt: "Stop this goal?",
        explanation: "The agent stops driving it and closes the tasks it opened.",
        confirmLabel: "Stop goal",
        keepLabel: "Keep it",
      },
      run: async () => {
        const res = await api.clearGoal(g.session_id, true);
        refresh();
        return { undoToken: res.undo_token };
      },
    });
  }
  // Any goal can be dropped, handing back an undo token for the §4.5 Undo toast. A stopped goal
  // frames it as Drop the goal (its Needs-you partner to Keep going) — the undo toast says
  // "dropped" to match — otherwise Delete….
  actions.push({
    id: "delete",
    label: stopped ? "Drop the goal" : "Delete…",
    destructive: true,
    undoMessage: stopped ? "Goal dropped." : undefined,
    confirm: {
      prompt: stopped ? "Drop this goal?" : "Delete this goal?",
      explanation: "The agent stops working toward it. Work it has already done stays.",
      confirmLabel: stopped ? "Drop the goal" : "Delete",
      keepLabel: "Keep it",
    },
    run: async () => {
      const res = await api.clearGoal(g.session_id, false);
      refresh();
      return { undoToken: res.undo_token };
    },
  });

  return (
    <WorkDetail
      title={title}
      status={stopped ? "paused" : item.status}
      statusLabel={stopped ? "Goal stopped" : undefined}
      fields={goalFields(g)}
      actions={actions}
      technical={goalTechnical(g)}
      type="Goal"
      onUndo={async (token) => {
        await api.undo(token);
        refresh();
      }}
    />
  );
}

/** The shared goal creator: the palette's New goal intent opens it through `filters.create`.
 *  Its one live host is WorkView's `GoalCreatorHost` — the group rows offer no create control
 *  (WorkMount's v2 §4 contract); goal creation stays on the palette intent and the `/goal new`
 *  chat command. Closing clears the filter. */
export function useGoalCreator(state: WorkSectionContext["state"]): { creator: ReactNode } {
  const qc = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  // Open after the containing Work modal mounts, so this dialog's portal sits above it.
  useEffect(() => {
    if (state.filters?.create === true) setCreating(true);
  }, [state.filters?.create]);

  const closeCreator = () => {
    setCreating(false);
    const ui = useUI.getState();
    const current = ui.surfaceStates.work ?? state;
    if (current.filters?.create === true) {
      const filters = { ...current.filters };
      delete filters.create;
      ui.setSurfaceState("work", { ...current, filters });
    }
  };

  const createGoal = (body: GoalSetBody) => {
    setBusy(true);
    api
      .setGoal(body)
      .then((res) => {
        void qc.invalidateQueries({ queryKey: queryKeys.goals });
        toast({ tone: "success", title: "Goal set", message: goalSetDetail(res.message) || "The agent has a new goal." });
        closeCreator();
      })
      .catch((e) => toast({ tone: "error", title: "Couldn't set goal", message: errMsg(e) }))
      .finally(() => setBusy(false));
  };

  return {
    creator: <GoalCreateDialog open={creating} onClose={closeCreator} onCreate={createGoal} busy={busy} />,
  };
}

/** Detail-only since the v2 consolidation: the group rows list goals (WorkOverview reads
 *  `workGroups` and shows the shared `goalProgress` meta), so an unresolved id — gone, or a
 *  source still loading behind a restored deep link — keeps this kind's own copy. */
export function GoalsSection({ ctx }: { ctx: WorkSectionContext }) {
  const itemId = ctx.state.itemId;
  const item = itemId
    ? ctx.sources.goals.items.find((i) => `${i.kind}:${i.id}` === itemId || i.id === itemId)
    : undefined;
  if (!item) return <Empty title="This goal is no longer here." />;
  return <GoalDetailView ctx={ctx} item={item} />;
}

export const goalsSection = { label: "Goals", group: "doing-now", render: (ctx: WorkSectionContext) => <GoalsSection ctx={ctx} /> } satisfies WorkSection;

registerWorkSection("goals", goalsSection);
